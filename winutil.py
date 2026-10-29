"""winutil.py - the Win32 surface everything else stands on.

One place for the things every part of this project kept re-declaring:
finding a window, reading its controls, rendering it, and sending input.

Two facts learned on this machine that are not optional:

  * `grab()` of a window uses PrintWindow, not a screen grab.  RYL's client is
    an exclusive-fullscreen DirectDraw surface, so BitBlt / ImageGrab / mss all
    return a nearly-black frame (mean ~11 against a real ~37).  A frame that
    black is not `max() < 8`, so it cannot be detected after the fact - it has
    to be tried the other way round.
  * `click()` uses SendInput-shaped input through `mouse_event` and
    `SetCursorPos`, because these launchers inspect the real mouse state rather
    than trusting the message queue.

Nothing here injects, hooks or debugs: read-only windows and synthesised input,
which is what keeps RYL Shield and AntiDebugDLL quiet.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

u32 = ctypes.WinDLL("user32", use_last_error=True)
gdi = ctypes.WinDLL("gdi32", use_last_error=True)

__all__ = [
    "elevated", "wtext", "wclass", "wrect", "children", "windows_of", "pid_of",
    "find_window", "wait_window", "listbox_items", "click_at", "grab",
    "printwindow_bgr", "is_black", "scan_value",
]


# ------------------------------------------------------------------- windows
def elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:                                     # pragma: no cover
        return False


def wtext(h) -> str:
    n = u32.GetWindowTextLengthW(h)
    b = ctypes.create_unicode_buffer(n + 2)
    u32.GetWindowTextW(h, b, n + 2)
    return b.value


def wclass(h) -> str:
    b = ctypes.create_unicode_buffer(256)
    u32.GetClassNameW(h, b, 256)
    return b.value


def wrect(h):
    """(x, y, w, h) of a window, empty when there is no window."""
    r = wt.RECT()
    u32.GetWindowRect(h, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def pid(h) -> int:
    p = wt.DWORD()
    u32.GetWindowThreadProcessId(h, ctypes.byref(p))
    return p.value


def children(parent) -> list[int]:
    """Children of a window.  A missing parent returns [] - not every window.

    `EnumChildWindows(NULL)` behaves like `EnumWindows`, so a caller looking for
    controls on a window that has gone away would be handed every top-level
    window on the desktop.  That is how a bug becomes a hang.
    """
    if not parent:
        return []
    out: list[int] = []
    EP = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(h, l):
        out.append(h)
        return True

    u32.EnumChildWindows(parent, EP(cb), 0)
    return out


def descendants(parent, depth: int = 2) -> list[int]:
    """Children, and their children when `depth` allows it.

    MFC screens nest their controls several levels deep (Shell Embedding ->
    DocObject View -> Internet Explorer_Server, for one), so a flat walk is not
    enough when looking for a control by name.
    """
    out, frontier = [], [parent]
    for _ in range(max(0, depth)):
        nxt = []
        for w in frontier:
            kids = children(w)
            out.extend(kids)
            nxt.extend(kids)
        frontier = nxt
    return out


def windows_of(pid_: int) -> list[int]:
    """Every top-level window of a process, titled or not."""
    out: list[int] = []
    EP = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(h, l):
        if pid(h) == pid_:
            out.append(h)
        return True

    u32.EnumWindows(EP(cb), 0)
    return out


def pid_of(image: str) -> int | None:
    """Pid of a process image, or None.  tasklist, not WMI: no dependency."""
    import subprocess
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}",
                          "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, errors="ignore").stdout
    for line in out.splitlines():
        parts = [p.strip(' "') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == image.lower():
            try:
                return int(parts[1])
            except ValueError:
                return None
    return None


# ------------------------------------------------------------------- looking
def find_window(cls: str | None = None, title: str | None = None,
                pid_: int | None = None):
    """A visible window by class, title, or which process owns it."""
    h = u32.FindWindowW(cls, title)
    if h and (pid_ is None or pid(h) == pid_):
        return h
    if pid_ is not None:
        for w in windows_of(pid_):
            if not u32.IsWindowVisible(w):
                continue
            if cls and wclass(w) != cls:
                continue
            if title and wtext(w) != title:
                continue
            return w
    return 0


def wait_window(cls=None, title=None, pid_=None, timeout: float = 30.0,
                say=print):
    """Wait for a window, in 5 Hz steps.  0 on timeout."""
    t0 = __import__("time").time()
    while __import__("time").time() - t0 < timeout:
        h = find_window(cls, title, pid_)
        if h:
            return h
        __import__("time").sleep(0.2)
    say(f"  ! timed out waiting for {cls or ''} {title or ''} pid={pid_}")
    return 0


def listbox_items(lb) -> list[str]:
    """The items of a Win32 listbox, read straight out of the control."""
    out: list[str] = []
    n = u32.SendMessageW(lb, 0x018B, 0, 0)
    for i in range(n):
        ln = u32.SendMessageW(lb, 0x018A, i, 0)
        buf = ctypes.create_unicode_buffer(max(2, ln + 2))
        u32.SendMessageW(lb, 0x0189, i, ctypes.byref(buf))
        out.append(buf.value)
    return out


def scan_value(h, value: int, chunk: int = 0x100000):
    """Every address in a process whose int32 equals `value`.

    Used to find a field from a number visible on screen - the Cheat Engine
    trick, without Cheat Engine.
    """
    addrs = []
    for base, size in regions(h):
        off = 0
        while off < size:
            n = min(chunk, size - off)
            buf = read(h, base + off, n)
            if buf:
                import struct
                item = struct.pack("<i", int(value))
                start = 0
                while True:
                    k = buf.find(item, start)
                    if k < 0:
                        break
                    addrs.append(base + off + k)
                    start = k + 1
            if n <= 96:
                break
            off += n - 96
    return addrs


# ------------------------------------------------------------------- reading
class _BIH(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG),
                ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def is_black(frame) -> bool:
    """A pure-black frame is a failed capture, not a dark room."""
    try:
        return frame is None or int(frame.max()) < 8
    except Exception:
        return True


def printwindow_bgr(hwnd, w: int, h: int):
    """Render a window to a BGR array with PrintWindow + PW_RENDERFULLCONTENT.

    The only reliable way to see RYL's fullscreen client: its surface is not
    composited, so the screen-grab APIs see a black rectangle.
    """
    if not hwnd or w <= 0 or h <= 0:
        return None
    try:
        hdc = u32.GetWindowDC(hwnd)
        mdc = gdi.CreateCompatibleDC(hdc)
        bmp = gdi.CreateCompatibleBitmap(hdc, w, h)
        gdi.SelectObject(mdc, bmp)
        ok = u32.PrintWindow(hwnd, mdc, 2)
        bi = _BIH()
        bi.biSize = ctypes.sizeof(bi)
        bi.biWidth, bi.biHeight = w, -h
        bi.biPlanes, bi.biBitCount = 1, 32
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
        gdi.DeleteObject(bmp)
        gdi.DeleteDC(mdc)
        u32.ReleaseDC(hwnd, hdc)
        if not ok:
            return None
        import numpy as np
        return np.frombuffer(buf.raw, dtype=np.uint8).reshape(h, w, 4)[:, :, :3].copy()
    except Exception:
        return None


def grab(rect=None, hwnd=None):
    """A BGR frame of the game, or None.

    The frame comes from the window, never from the screen: a screen grab of
    this client is about 3x too dark to read a HUD through.  `rect` is accepted
    only so callers that think in screen rectangles can keep doing so.
    """
    if hwnd is None:
        hwnd = u32.FindWindowW(None, "Risk Your Life")
    if not hwnd and rect:
        x0, y0 = int(rect[0]), int(rect[1])
        hwnd = u32.WindowFromPoint(wt.POINT(x0, y0))
    if not hwnd:
        return None
    x, y, w, h = wrect(hwnd)
    return printwindow_bgr(hwnd, w, h)


# -------------------------------------------------------------------- input
def click_at(x: int, y: int, double: bool = False, delay: float = 0.05):
    """A real click, with the cursor actually moved first.

    These launchers read the mouse state, not the queue, so a posted
    WM_LBUTTONDOWN is ignored.
    """
    import time
    u32.SetCursorPos(int(x), int(y))
    time.sleep(delay)
    for i in range(2 if double else 1):
        u32.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.03)
        u32.mouse_event(0x0004, 0, 0, 0, 0)
        if i == 0 and double:
            time.sleep(0.07)


# ------------------------------------------------------------------- process
def regions(h):
    """Committed, readable pages of a process, 32-bit or 64-bit.

    A byte cap stops the walk early on a 64-bit process, whose heap starts far
    above the 2 GB mark that is right for a 32-bit target.
    """
    limit = 0x7FFFFFFFFFFF
    mbi = wt.MEMORY_BASIC_INFORMATION()
    addr = 0
    while addr < limit:
        if u32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                              ctypes.sizeof(mbi)) == 0:
            break
        base = mbi.BaseAddress or addr
        size = mbi.RegionSize or 0x1000
        prot = mbi.Protect or 0
        if (mbi.State == MEM_COMMIT and not (prot & PAGE_GUARD)
                and not (prot & PAGE_NOACCESS) and (prot & READABLE)):
            yield base, size
        addr = base + size


MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE = 0x04 | 0x20 | 0x02 | 0x40


def read(h, addr, size):
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t()
    if k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size,
                             ctypes.byref(got)):
        return buf.raw[:got.value]
    return b""


k32 = ctypes.WinDLL("kernel32", use_last_error=True)
