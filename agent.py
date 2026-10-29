#!/usr/bin/env python3
"""OCR BOT - a screen-only RYL autopilot.  Three classes, one loop.

The loop is the whole bot:

    find a pink nameplate -> move under it -> does the model go blue?
    -> yes: click once and measure -> no damage: one re-click -> next.

Modes (the three classes)
-------------------------
melee    left-click the mob, cycle skill keys on a cadence
ranger   same click, separate cadence so the two can diverge
gunner   hold the RIGHT button on the locked target, fire skills into it

Measurement
-----------
hover   the client's own marker: a saturated-blue model/ring near the name.
        Measured TWICE - a baseline before the cursor arrives and a poll while
        it sits there - so blue scenery in the window cannot fake a lock.
fight   damage numbers, or the enemy's green/blue HP bar near the target.
        The bar does not exist until something hits, so it is the best tell.
kill    the chat prints "Obtained N experience" on every kill: the newest
        line is OCR'd off-thread and remembered, so a bar hidden by effects
        cannot hide the death.  Counted in the footer as k<N>.
HP      the digits on the red plate, by OCR, stabilised with a learned
        maximum: the max is fixed for a session, so a stray digit read
        ("1534/71534") is rejected instead of driving a potion.

Safety
------
* the right button is released on stop, error, exit and loss of focus
* nothing is sent unless the game window is focused
* one click per lock; a second only when the first produced no damage

    python agent.py                    live
    python agent.py --replay f.png     one saved frame, no input
    python agent.py --inputtest        prove clicks land in the client
"""

import ctypes
import ctypes.wintypes as wt
import json
import math
import queue
import sys
import threading
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np

from winutil import printwindow_bgr

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "agent.json"

# ---- measured colour bands ------------------------------------------------
# Pink nameplates: H 160-177, but on dark rock the pink gets dimmer (S 44)
# and brighter (V 239), so the band is wide on purpose.
PINK_LO, PINK_HI = np.array([140, 45, 105]), np.array([176, 175, 255])

DEFAULTS = {
    "mode": "melee",               # melee | ranger | gunner
    # --- the loop -------------------------------------------------------
    "scan_gap_s": 0.015,
    "target_zone": [0.02, 0.88],
    "require_blue": True,          # hover first, click only on blue
    "hover_confirm_s": 0.50,       # blue must last this long before clicking
    "hover_poll_s": 0.03,          # re-check this often once hovered
    "hover_blue_min": 60,          # blue px in the window = a real hover
    "hover_delta_min": 25,         # ... and this much over the pre-hover base
    "hover_red_min": 24,           # red feet-ring px = hover (the stronger cue)
    "hover_red_delta_min": 12,     # ... over the pre-hover base
    # --- damage / measurement -------------------------------------------
    "attack_interval_s": 0.30,     # gunner re-fire cadence
    "skill_keys": ["1", "2", "3", "4"],
    "skill_every_s": 0.9,
    "track_radius": 90,            # px a locked mob may drift between ticks
    "fight_white_min": 6,          # damage-number pixels meaning "fight on"
    "no_hit_s": 6.0,               # locked but nothing connects -> re-click
    "give_up_s": 15.0,             # no damage at all this long -> drop it
    "max_clicks": 2,               # one click + one correction
    # --- hp --------------------------------------------------------------
    "potion_key": "0",
    "hp_potion_below": 0.55,
    "potion_confirm_s": 1.0,       # low HP must show twice before drinking
    "potion_cooldown_s": 4.0,
    "hp_box": [24, 988, 150, 1010],    # digits inside the red HP plate
    # --- loot -------------------------------------------------------------
    "loot_key": "z",
    "loot_after_hit": False,
    "loot_every_s": 1.5,
    # --- chat -------------------------------------------------------------
    "chat_watch": True,            # read exp lines as a kill receipt
    # fractions of the frame: the chat text block, scrollbar excluded
    "chat_box": [0.770, 0.672, 0.984, 0.914],
}

SC = {
    "esc": 0x01, "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06,
    "6": 0x07, "7": 0x08, "8": 0x09, "9": 0x0A, "0": 0x0B,
    "tab": 0x0F, "q": 0x10, "w": 0x11, "e": 0x12, "r": 0x13, "t": 0x14,
    "a": 0x1E, "s": 0x1F, "d": 0x20, "f": 0x21, "g": 0x22,
    "z": 0x2C, "x": 0x2D, "c": 0x2E, "v": 0x2F, "b": 0x30,
    "l": 0x26, "space": 0x39, "f8": 0x42, "f9": 0x43, "f10": 0x44,
}

u32 = ctypes.WinDLL("user32", use_last_error=True)
u32.FindWindowW.restype = wt.HANDLE
u32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]

# panel palette
BG, FG, DIM, ACCENT = "#14161a", "#e6e6e6", "#888888", "#39d353"
DANGER, WARN = "#f85149", "#d29922"


_TESS_CMD = None


def _tesseract():
    """Find tesseract.exe once: Program Files, Program Files (x86), PATH."""
    global _TESS_CMD
    if _TESS_CMD is None:
        import shutil
        cand = [r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"]
        _TESS_CMD = next((p for p in cand if Path(p).exists()),
                         shutil.which("tesseract") or cand[0])
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = _TESS_CMD
    return _TESS_CMD


def _ocr(th):
    """Tesseract once, configured once.  ~0.2 s for the HP digits."""
    import pytesseract
    _tesseract()
    return pytesseract.image_to_string(
        th, config="--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789/")


def _ocr_text(th):
    """One line of UI text, no whitelist.  ~0.5 s for a chat line."""
    import pytesseract
    _tesseract()
    return pytesseract.image_to_string(th, config="--oem 3 --psm 7")


# ---------------------------------------------------------------- window
def game_window():
    return u32.FindWindowW(None, "Risk Your Life")


def _is_black(bgr) -> bool:
    """A pure-black frame is a failed capture, not a dark scene."""
    try:
        return bgr is None or int(bgr.max()) < 8
    except Exception:
        return True


def client_rect():
    h = game_window()
    if not h:
        return None
    r = wt.RECT()
    u32.GetClientRect(h, ctypes.byref(r))
    pt = wt.POINT(0, 0)
    u32.ClientToScreen(h, ctypes.byref(pt))
    return (pt.x, pt.y, pt.x + r.right, pt.y + r.bottom)


def focused():
    gw = game_window()
    if not gw:
        return False
    a, b = wt.DWORD(), wt.DWORD()
    u32.GetWindowThreadProcessId(u32.GetForegroundWindow(), ctypes.byref(a))
    u32.GetWindowThreadProcessId(gw, ctypes.byref(b))
    return a.value == b.value


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def focus_game() -> bool:
    """Bring the client to the foreground after START stole it."""
    h = game_window()
    if not h:
        return False
    try:
        if u32.IsIconic(h):
            u32.ShowWindow(h, 9)          # SW_RESTORE
        # a background process is otherwise refused by the foreground lock
        u32.AllowSetForegroundWindow(0xFFFFFFFF)      # ASFW_ANY
        u32.BringWindowToTop(h)
        return bool(u32.SetForegroundWindow(h))
    except Exception:
        return False


def cursor_pos():
    pt = wt.POINT()
    u32.GetCursorPos(ctypes.byref(pt))
    return (pt.x, pt.y)


# ---------------------------------------------------------------- input
#
# The INPUT structure is 40 bytes on x64 (the union starts at offset 8).  A
# hand-rolled 28-byte buffer puts dwFlags at the wrong offset and SendInput
# silently drops the event - the cursor still moves (SetCursorPos is separate)
# and the click does nothing, with no error.  Declare the real structs.

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


u32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
u32.SendInput.restype = wt.UINT


class Input:
    """SendInput only.  Tracks the right button so it can never stick down."""

    def __init__(self):
        self.right_held = False

    def move(self, x, y):
        """Glide the pointer there with relative moves the client can see.

        The client follows injected relative motion for hover and aiming;
        teleporting with SetCursorPos only lands the OS cursor (and its ring
        fades in late).  Windows scales relative moves by the pointer speed
        (20 here: a 24px step arrives as ~195px - the jumping pointer).  Small
        steps stay smooth: 5px nominal lands as ~12px, and the loop corrects
        every step so it cannot oscillate.  The last pixel or two are set
        directly so the click lands exactly where perception said.
        """
        x, y = int(x), int(y)
        deadline = time.time() + 0.7
        for _ in range(300):
            cx, cy = cursor_pos()
            dx, dy = x - cx, y - cy
            if abs(dx) <= 2 and abs(dy) <= 2:
                break
            self._mouse(0x0001,
                        max(-5, min(5, dx)), max(-5, min(5, dy)))
            time.sleep(0.0025)
            if time.time() > deadline:
                break
        cx, cy = cursor_pos()
        if abs(cx - x) > 1 or abs(cy - y) > 1:
            u32.SetCursorPos(x, y)

    def _send(self, inp):
        return u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def _mouse(self, flag, dx=0, dy=0):
        inp = INPUT(type=0)
        inp.mi = MOUSEINPUT(dx=dx, dy=dy, mouseData=0, dwFlags=flag,
                            time=0, dwExtraInfo=0)
        return self._send(inp)

    def click(self, x, y, settle=0.045, hold=0.060):
        # the client polls the cursor; firing the button in the same tick as
        # the move registers on the old spot or not at all
        self.move(x, y)
        time.sleep(settle)
        self._mouse(0x0002)               # LEFTDOWN
        time.sleep(hold)
        self._mouse(0x0004)               # LEFTUP
        time.sleep(0.020)

    def right_down(self, x=None, y=None):
        if x is not None:
            self.move(x, y)
        if not self.right_held:
            self._mouse(0x0008)           # RIGHTDOWN
            self.right_held = True

    def right_up(self):
        if self.right_held:
            self._mouse(0x0010)           # RIGHTUP
            self.right_held = False

    def _key(self, sc, up=False):
        inp = INPUT(type=1)
        inp.ki = KEYBDINPUT(wVk=0, wScan=sc,
                            dwFlags=0x0008 | (0x0002 if up else 0),
                            time=0, dwExtraInfo=0)
        return self._send(inp)

    def key(self, name, hold_ms=25):
        sc = SC.get(str(name).lower())
        if sc is None:
            return False
        self._key(sc)
        time.sleep(hold_ms / 1000)
        self._key(sc, up=True)
        return True

    def move_rel(self, dx, dy=0):
        return self._mouse(0x0001, int(dx), int(dy))

    def pressed(self, name):
        sc = SC.get(str(name).lower())
        if sc is None:
            return False
        return bool(u32.GetAsyncKeyState(u32.MapVirtualKeyW(sc, 1)) & 0x8000)


# ---------------------------------------------------------------- perception
class Perception:
    def __init__(self, cfg):
        self.cfg = cfg
        # (x0,y0,x1,y1) client-relative: the agent's own panel, which sits
        # over the game and must never be clicked or seen.
        self.exclude = None
        # HP: tesseract takes 0.1-1 s on a weak laptop, so it runs on its own
        # thread.  The tick only ever reads the last confirmed value.
        self._hp_next = 0.0
        self._plate_cache = None
        self._plate_miss_until = 0.0
        self._hp_last = None
        self._hp_cur = 0
        self.hp_now = self.hp_max = None
        self.hp_valid = False
        self.read_id = 0                  # bumps once per real OCR result
        self.mx_hist = []
        self.last_hover = None
        self.last_confirm_s = 0.0
        self.hp_urgent = False           # set by the brain while engaged
        self._ocr_q = queue.Queue(maxsize=1)
        threading.Thread(target=self._ocr_loop, daemon=True).start()
        # chat: the newest line is OCR'd on a worker only when the text
        # block changes.  kill_seen is the capture time of the last exp line,
        # so the brain can confirm a kill without waiting on Tesseract.
        self.kill_seen = 0.0
        self.kill_count = 0
        self.chat_text = ""
        self._chat_sig = None
        self._chat_next = 0.0
        self._chat_q = queue.Queue(maxsize=1)
        threading.Thread(target=self._chat_loop, daemon=True).start()

    def grab(self, rect):
        """Render the client window.  None on failure, never raises.

        PrintWindow + PW_RENDERFULLCONTENT, never a screen grab: a fullscreen
        RYL client is an exclusive surface and every screen-grab API returns a
        frame about 3x too dark to read a HUD from.
        """
        try:
            w = int(rect[2] - rect[0])
            h = int(rect[3] - rect[1])
            if w < 2 or h < 2:
                return None
            hw = game_window()
            frame = printwindow_bgr(hw, w, h) if hw else None
            if frame is not None and not _is_black(frame):
                return frame
        except Exception:
            pass
        return None

    def _cut(self, mask, shape, k=1):
        """Zero the agent's own window out of a mask (small-frame aware)."""
        if not self.exclude:
            return mask
        x0, y0, x1, y1 = (int(v) // k for v in self.exclude)
        h, w = shape[:2]
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(w, x1), min(h, y1)
        if x1 > x0 and y1 > y0:
            mask[y0:y1, x0:x1] = 0
        return mask

    def _cut_at(self, mask, x0, y0):
        """Cut the panel out of a mask that was cropped at (x0, y0)."""
        if not self.exclude:
            return mask
        ex0, ey0, ex1, ey1 = (int(v) for v in self.exclude)
        xa, ya = max(0, ex0 - x0), max(0, ey0 - y0)
        xb = min(mask.shape[1], ex1 - x0)
        yb = min(mask.shape[0], ey1 - y0)
        if xb > xa and yb > ya:
            mask[ya:yb, xa:xb] = 0
        return mask

    def in_exclude(self, cx, cy, pad=6):
        if not self.exclude:
            return False
        x0, y0, x1, y1 = self.exclude
        return (x0 - pad) <= cx <= (x1 + pad) and (y0 - pad) <= cy <= (y1 + pad)

    def mobs(self, bgr):
        """Pink nameplates, nearest to screen centre first.

        Scanned at half resolution on a big frame: nameplates are 20+ px wide,
        so nothing is lost and the mask work drops to a quarter of the pixels.
        """
        h, w = bgr.shape[:2]
        k = 2 if w >= 1280 else 1
        if k > 1:
            small = cv2.resize(bgr, (w // k, h // k),
                               interpolation=cv2.INTER_NEAREST)
            hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        else:
            hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        sh, sw = hsv.shape[:2]
        sx, sy = sw / 1920.0, sh / 1080.0
        m = cv2.inRange(hsv, PINK_LO, PINK_HI)
        # HUD masks, in fractions of the frame - never fixed pixels.
        m[:max(1, int(140 * sy)), :max(1, int(150 * sx))] = 0
        m[int(sh * .86):, :] = 0
        m[int(sh * .66):, :int(sw * .16)] = 0
        m[int(sh * .70):, int(sw * .70):] = 0
        m = self._cut(m, hsv.shape, k)       # never see our own panel
        raw_pink = m
        m = cv2.dilate(m, np.ones((3, 3), np.uint8), 2)
        n, lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
        min_area = max(6, int(12 * sx * sy))
        # "Small Rock Merman" is a ~100px name at this resolution; after the
        # dilate it is ONE blob, so the pre-merge cap has to clear a whole
        # name or the plate is thrown away before it can be merged.
        max_part_w = max(40, int(260 * sx))
        max_part_h = max(12, int(20 * sy))
        parts = [[int(s[0]), int(s[1]), int(s[2]), int(s[3])]
                 for s in stats[1:]
                 if s[4] >= min_area and s[3] <= max_part_h
                 and s[2] <= max_part_w]
        if not parts:
            return []
        parts.sort(key=lambda p: (p[1], p[0]))
        merge_y = max(3, int(6 * sy))
        gap_lo, gap_hi = int(-14 * sx), max(8, int(30 * sx))
        lines = []
        for p in parts:
            for ln in lines:
                gap_after = p[0] - (ln[0] + ln[2])
                gap_before = ln[0] - (p[0] + p[2])
                if abs(p[1] - ln[1]) <= merge_y \
                        and (gap_lo <= gap_after <= gap_hi
                             or gap_lo <= gap_before <= gap_hi):
                    x2 = max(ln[0] + ln[2], p[0] + p[2])
                    y2 = max(ln[1] + ln[3], p[1] + p[3])
                    ln[0] = min(ln[0], p[0]); ln[1] = min(ln[1], p[1])
                    ln[2] = x2 - ln[0];       ln[3] = y2 - ln[1]
                    ln[4] += 1
                    break
            else:
                lines.append([p[0], p[1], p[2], p[3], 1])
        lo, hi = self.cfg["target_zone"]
        # the plate floats well above the model: aiming right under the text
        # lands on empty air, the click selects nothing and the character just
        # runs there (the live "misses a lot").  The user calibrated the body
        # at ~30px below the plate on the 1366x768 client.
        click_off = max(6, int(round(30 * (h / 768.0))))
        # "Batz" is only ~18 px wide at 1366 and 11 after the half-res dilate:
        # a 12 px floor silently threw short names away
        min_name_w = max(8, int(14 * sx))
        out = []
        for x, y, bw, bh, cnt in lines:
            if bw < min_name_w or bw > max(60, int(300 * sx)):
                continue
            if bh > max_part_h + 2:
                continue
            # real plates are solid pink (density ~0.3); jpeg fringes around
            # yellow system text are ~0.05 specks that dilation inflates
            if int(raw_pink[y:y + bh, x:x + bw].sum()) // 255 \
                    < 0.15 * bw * bh:
                continue
            cx = x + bw // 2
            if not (sw * lo <= cx <= sw * hi) or not (sh * 0.03 <= y <= sh * 0.86):
                continue
            out.append({"x": x * k, "y": y * k, "w": bw * k, "h": bh * k,
                        "cx": cx * k, "cy": (y + bh // 2) * k,
                        "click_y": min(h - 1, (y + bh) * k + click_off)})
        out.sort(key=lambda b: math.hypot(b["cx"] - w / 2.0, b["cy"] - h / 2.0))
        return out

    def hover_signature(self, bgr, box):
        """Count the hover markers around a nameplate.

        Measured from a live hover: the model turns saturated blue (H 95-140,
        S 198-234) and the game paints a dashed red ring at its feet.  At the
        body focus point the blue mask often reads only ~20 px - under the
        floor - while the red ring reads ~50 px, so either marker confirms a
        hover.  Both are counted against a pre-hover baseline, which is what
        rules out blue scenery and red HUD elements.
        """
        cx = int(box["cx"])
        name_y = int(box["y"])
        name_h = int(box["h"])
        h, w = bgr.shape[:2]
        sx, sy = w / 1920.0, h / 1080.0
        half_w = max(28, int(52 * sx))
        y0 = max(0, name_y - int(45 * sy))
        # live at 1366: the blue body glow starts 35-70px under the name, and
        # near mobs sit lower still - the old window stopped at +39
        y1 = min(h, name_y + name_h + int(150 * sy))
        x0, x1 = max(0, cx - half_w), min(w, cx + half_w)
        if x1 <= x0 or y1 <= y0:
            return {"blue_px": 0, "red_px": 0}
        roi = bgr[y0:y1, x0:x1]
        rh = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        # measured live: the hover glow on a dark mob is S~84 V~51, so the
        # old S>=115 floor only ever counted the dashed ring - 26 px, never
        # enough.  The delta over the pre-hover baseline is what rules out
        # blue scenery; this mask just has to see the glow.
        blue_px = int(cv2.inRange(rh, np.array([95, 60, 40]),
                                  np.array([140, 255, 255])).sum()) // 255
        # the ring only, below the plate: above it sit the enemy HP bar and
        # red damage numbers, which appear during the fight and would fake a
        # hover
        r0 = max(0, name_y + name_h + int(8 * sy) - y0)
        r1 = min(rh.shape[0], name_y + name_h + int(140 * sy) - y0)
        red_px = 0
        if r1 > r0:
            ring = rh[r0:r1]
            red_px = int(cv2.inRange(ring, np.array([0, 120, 60]),
                                     np.array([10, 255, 255])).sum()) // 255
            red_px += int(cv2.inRange(ring, np.array([170, 120, 60]),
                                      np.array([180, 255, 255])).sum()) // 255
        return {"blue_px": blue_px, "red_px": red_px}

    def confirm(self, rect, inp, mob, pre=None):
        """Point at the mob's body; is the hover marker there?  (ok, blue_px)

        A baseline is measured BEFORE the cursor arrives.  Blue scenery can
        sit inside the window anyway, so a marker must both clear the absolute
        floor and raise its count by the config delta over that baseline.
        `pre` is the tick's own pre-hover frame - reusing it saves a grab.

        Two markers count as hover: the blue body glow, and the dashed red
        ring at the feet (the stronger one at the body focus point).  The
        marker has to LAST: the user asked for 0.5 s, and a single frame of
        blue is often the fade-in of a mob that is about to walk away.  Brief
        dropouts (<0.2 s) are forgiven - the client's glow flickers.
        """
        w = rect[2] - rect[0]
        h = rect[3] - rect[1]
        area = max(0.10, (w / 1920.0) * (h / 1080.0))
        min_blue = max(10, int(self.cfg["hover_blue_min"] * area))
        blue_delta = max(8, int(self.cfg["hover_delta_min"] * area))
        min_red = max(6, int(self.cfg["hover_red_min"] * area))
        red_delta = max(5, int(self.cfg["hover_red_delta_min"] * area))
        hold = max(0.10, float(self.cfg["hover_confirm_s"]))
        poll = max(0.005, float(self.cfg["hover_poll_s"]))

        base_blue = base_red = 0
        f0 = pre if pre is not None else self.grab(rect)
        if f0 is not None:
            sig0 = self.hover_signature(f0, mob)
            base_blue, base_red = sig0["blue_px"], sig0["red_px"]

        inp.move(int(rect[0] + mob["cx"]), int(rect[1] + mob["click_y"]))
        t0 = time.time()
        end = t0 + hold + 0.8                 # glow may fade in slowly
        hot_since = last_hot = None
        best_blue = best_red = 0
        tick = 0
        while True:
            # a real nudge keeps the client's hover alive; 1px is invisible
            inp.move_rel(1 if tick % 2 == 0 else -1, 0)
            tick += 1
            frame = self.grab(rect)
            if frame is not None:
                now = time.time()
                sig = self.hover_signature(frame, mob)
                blue, red = sig["blue_px"], sig["red_px"]
                best_blue, best_red = max(best_blue, blue), max(best_red, red)
                hot = ((blue >= min_blue and blue >= base_blue + blue_delta)
                       or (red >= min_red and red >= base_red + red_delta))
                if hot:
                    last_hot = now
                    if hot_since is None:
                        hot_since = now
                    if now - hot_since >= hold:
                        self.last_hover = (blue, base_blue, red, base_red)
                        self.last_confirm_s = now - t0
                        return True, blue
                elif hot_since is not None and now - last_hot > 0.2:
                    hot_since = None
            if time.time() >= end:
                break
            time.sleep(poll)
        self.last_hover = (best_blue, base_blue, best_red, base_red)
        self.last_confirm_s = time.time() - t0
        return False, best_blue

    def find_hp_plate(self, bgr):
        """Locate the red HP plate by shape, not by a hardcoded box.

        The plate moves with the HUD layout (y=743 on one character, y=1016
        on another), and a fixed box then reads the ground.
        """
        h, w = bgr.shape[:2]
        sx, sy = w / 1920.0, h / 1080.0
        y0, x1 = int(h * 0.55), int(w * 0.50)
        roi = cv2.cvtColor(bgr[y0:h, 0:x1], cv2.COLOR_BGR2HSV)
        H, S, V = roi[:, :, 0], roi[:, :, 1], roi[:, :, 2]
        red = ((H <= 12) | (H >= 168)) & (S >= 140) & (V >= 80)
        white = (S <= 70) & (V >= 190)
        mask = (red | white).astype(np.uint8)
        rows = mask.sum(axis=1)
        cand = np.nonzero(rows >= max(12, int(45 * sx)))[0]
        if len(cand) == 0:
            return None
        bands, start = [], cand[0]
        for a, b in zip(cand, cand[1:]):
            if b - a > 2:
                bands.append((start, a))
                start = b
        bands.append((start, cand[-1]))
        best = None
        for a, b in bands:
            bh = b - a + 1
            if not (max(5, int(9 * sy)) <= bh <= max(12, int(30 * sy))):
                continue
            cols = mask[a:b + 1].sum(axis=0)
            on = np.nonzero(cols >= 0.30 * bh)[0]
            if len(on) < 2:
                continue
            blocks, s2 = [], on[0]
            for p, q in zip(on, on[1:]):
                if q - p > 10:
                    blocks.append((s2, p))
                    s2 = q
            blocks.append((s2, on[-1]))
            for x0, x1b in blocks:
                bw = x1b - x0 + 1
                if not (max(24, int(55 * sx)) <= bw <= max(80, int(260 * sx))):
                    continue
                if best is None or bw * bh > best[0]:
                    best = (bw * bh, int(x0), int(y0 + a), int(bw), int(bh))
        if not best:
            return None
        return best[1:]

    def _learned_max(self):
        """The max HP to trust: most frequent recent read, ties go to newest."""
        if not self.mx_hist:
            return None
        best, best_n = self.mx_hist[-1], 0
        for v in reversed(self.mx_hist):
            n = self.mx_hist.count(v)
            if n > best_n:
                best, best_n = v, n
        return best

    def hp(self, bgr):
        """HP from the digits on the red plate.  Fast path only.

        The plate is solid red with the numbers on it, so measuring how much
        red there is says nothing about HP.  Reading '1081/1440' is exact.
        Tesseract is slow, so the digits are queued to a worker thread and
        this returns the last confirmed reading at once; `hp_valid` is False
        whenever nothing fresh has been read, so a potion is never driven by
        a stale number.
        """
        now = time.time()
        if now < self._hp_next:
            return self._hp_last, self._hp_cur
        h, w = bgr.shape[:2]
        sx, sy = w / 1920.0, h / 1080.0
        plate = None
        if self._plate_cache and now - self._plate_cache[0] < 6.0:
            plate = self._plate_cache[1]
        if plate is None and not (self._plate_miss_until
                                  and now < self._plate_miss_until):
            plate = self.find_hp_plate(bgr)
            if plate:
                self._plate_cache = (now, plate)
            else:
                self._plate_miss_until = now + 1.5
        if plate:
            x0, y0, pw, ph = plate
            pad = 3
            x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
            x1, y1 = min(w, x0 + pw + 2 * pad), min(h, y0 + ph + 2 * pad)
        else:
            x0, y0, x1, y1 = self.cfg["hp_box"]
            x0, x1 = int(x0 * sx), int(x1 * sx)
            y0, y1 = int(y0 * sy), int(y1 * sy)
        if y1 > h or x1 > w or x1 <= x0 or y1 <= y0:
            self.hp_valid = False
            self._hp_next = now + 2.0
            return self._hp_last, self._hp_cur
        # no bright, low-saturation pixels = no white HP text = no OCR needed
        rh = cv2.cvtColor(bgr[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
        digit_px = int(((rh[:, :, 1] <= 100) & (rh[:, :, 2] >= 150)).sum())
        if digit_px < 5:
            self.hp_valid = False
            self._hp_next = now + 2.0
            return self._hp_last, self._hp_cur
        crop = cv2.resize(bgr[y0:y1, x0:x1], None, fx=4, fy=4,
                          interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        # in flight - do not queue another; during a fight the read has to
        # come back fast enough to drink before the next hit
        self._hp_next = now + (0.6 if self.hp_urgent else 1.2)
        try:
            self._ocr_q.put_nowait(gray)
        except queue.Full:
            pass
        return self._hp_last, self._hp_cur

    def _ocr_loop(self):
        """Off-thread HP OCR: read, parse, publish.  Never blocks the tick."""
        while True:
            gray = self._ocr_q.get()
            cur = mx = None
            try:
                cur, mx = self._read_hp(gray)
            except Exception:
                pass
            if cur is not None:
                self.hp_now, self.hp_max = cur, mx
                self._hp_last = max(0.0, min(1.0, cur / mx))
                self._hp_cur = cur
                self.hp_valid = True
                self.read_id += 1
                # a healthy character does not need a read every 0.6 s -
                # tesseract then runs continuously and steals the CPU that
                # PrintWindow needs on a weak laptop.  While engaged it does.
                low = self._hp_last <= self.cfg["hp_potion_below"] + 0.10
                if low:
                    nxt = 0.8
                elif self.hp_urgent:
                    nxt = 1.2
                else:
                    nxt = 2.2
                self._hp_next = time.time() + nxt
            else:
                self.hp_valid = False
                self._hp_next = time.time() + 1.0

    def _read_hp(self, gray):
        cur = mx = None
        digits = ""
        for thr in (150, 170):
            _, th = cv2.threshold(gray, thr, 255, cv2.THRESH_BINARY)
            try:
                txt = (_ocr(th) or "").strip().replace(" ", "")
            except Exception:
                txt = ""
            digits = "".join(c for c in txt if c.isdigit()) or digits
            if "/" in txt:
                a, b = txt.split("/", 1)
                a = "".join(c for c in a if c.isdigit())
                b = "".join(c for c in b if c.isdigit())
                if a and b and int(b) > 0:
                    cur, mx = int(a), int(b)
                    break
        learned = self._learned_max()
        if cur is None and digits:
            if learned and int(digits) <= learned * 1.02 \
                    and len(digits) <= len(str(learned)):
                # OCR dropped the '/max' half - the current value still fits
                cur, mx = int(digits), learned
            elif len(digits) >= 6 and len(digits) % 2 == 0:
                # '10811440': only split when EVEN.  An odd count means a
                # digit was dropped and splitting invents a maximum.
                half = len(digits) // 2
                cur, mx = int(digits[:half]), int(digits[half:])
        if cur is None or not mx:
            return None, None
        if learned:
            if abs(mx - learned) <= max(2, learned // 100):
                mx = learned            # jitter around the true maximum
            elif len(str(mx)) > len(str(learned)) and cur <= learned * 1.02:
                mx = learned            # extra digit slipped into the max
        if not (200 <= mx <= 200000) or cur < 0 or cur > mx * 1.02:
            return None, None
        self.mx_hist.append(mx)
        self.mx_hist = self.mx_hist[-6:]
        return cur, mx

    def chat(self, bgr):
        """Watch the newest chat line for the kill receipt ("... experience").

        A kill always prints an exp line, so it confirms a death the pixel
        signals can miss (bar hidden by effects).  The chat block is hashed
        at ~0.4 s; only when it changes does a worker OCR the bottom-most
        line (~0.45 s, measured).  The tick never waits on Tesseract, and a
        scroll counts as a change, so two identical exp lines in a row are
        still seen as two.
        """
        if not self.cfg.get("chat_watch", True):
            return
        try:
            now = time.time()
            if now < self._chat_next:
                return
            self._chat_next = now + 0.40
            h, w = bgr.shape[:2]
            x0, y0, x1, y1 = self.cfg["chat_box"]
            x0, x1 = int(x0 * w), int(x1 * w)
            y0, y1 = int(y0 * h), int(y1 * h)
            if x1 - x0 < 40 or y1 - y0 < 20:
                return
            gray = cv2.cvtColor(bgr[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
            ink = (gray >= 90).astype(np.uint8)
            rows = ink.sum(axis=1)
            on = np.nonzero(rows >= 3)[0]
            if len(on) == 0:
                return
            bands, start = [], on[0]
            for a, b in zip(on, on[1:]):
                if b - a > 2:
                    bands.append((start, a))
                    start = b
            bands.append((start, on[-1]))
            # a chat line is ~8-10 px at 768; hairline rows are frame edges
            min_h = max(4, int(round(6 * h / 768.0)))
            bands = [(a, b) for a, b in bands if b - a + 1 >= min_h]
            if not bands:
                return
            # signature = where the text lines sit and how much ink each has.
            # A new line shifts every band, so even two identical exp lines in
            # a row register; background texture never enters the hash.
            sig = tuple((int(a), int(b), int(rows[a:b + 1].sum()))
                        for a, b in bands)
            if sig == self._chat_sig:
                return
            a, b = bands[-1]                # bottom-most = newest line
            pad = max(1, int(round(2 * h / 768.0)))
            line = gray[max(0, a - pad):min(gray.shape[0], b + 1 + pad)]
            if int((line >= 90).sum()) < max(20, int(0.01 * line.size)):
                return
            try:
                self._chat_q.put_nowait((now, line.copy()))
            except queue.Full:
                return          # worker busy; same sig is retried next poll
            self._chat_sig = sig
        except Exception:
            pass

    def _chat_loop(self):
        """Off-thread chat OCR: newest line -> remember exp kills."""
        while True:
            taken, line = self._chat_q.get()
            try:
                txt = self._read_chat(line)
            except Exception:
                continue
            if not txt:
                continue
            self.chat_text = txt
            low = txt.lower()
            if "obtain" in low and "ence" in low \
                    and any(c.isdigit() for c in low):
                self.kill_seen = taken       # NOT the ocr finish time
                self.kill_count += 1

    def _read_chat(self, line):
        """One chat line -> text.  Measured: ~0.43 s, clean at 5x."""
        up = cv2.resize(line, None, fx=5, fy=5,
                        interpolation=cv2.INTER_CUBIC)
        _, th = cv2.threshold(up, 90, 255, cv2.THRESH_BINARY_INV)
        th = cv2.copyMakeBorder(th, 16, 16, 16, 16,
                                cv2.BORDER_CONSTANT, value=255)
        return (_ocr_text(th) or "").strip()


    def damage_numbers(self, bgr, near=None, radius=260):
        """Big bright digits popping on screen == a fight is actually running.

        Damage text is near-white (S ~16, V ~234) and far taller than a
        nameplate (22-45 px vs ~10), so size separates them.
        """
        h_full, w_full = bgr.shape[:2]
        sx, sy = w_full / 1920.0, h_full / 1080.0
        cropped = bool(near)
        if cropped:
            rr = max(60, int(radius * max(sx, sy)))
            x0 = max(0, int(near[0] - rr))
            y0 = max(0, int(near[1] - rr))
            x1 = min(w_full, int(near[0] + rr))
            y1 = min(h_full, int(near[1] + rr))
        else:
            x0 = y0 = 0
            x1, y1 = w_full, h_full
        hsv = cv2.cvtColor(bgr[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
        m = cv2.inRange(hsv, np.array([0, 0, 215]), np.array([179, 45, 255]))
        m = self._cut_at(m, x0, y0)
        cut = int(h_full * 0.88) - y0
        if cut < m.shape[0]:
            m[max(0, cut):, :] = 0        # the action bar and chat are not damage
        n, lab, stats, cent = cv2.connectedComponentsWithStats(m, 8)
        h_min, h_max = max(8, int(16 * sy)), max(20, int(48 * sy))
        w_min, w_max = max(4, int(8 * sx)), max(30, int(90 * sx))
        a_min = max(12, int(40 * sx * sy))
        hits = 0
        for i in range(1, n):
            x, y, bw, bh, a = stats[i]
            if not (h_min <= bh <= h_max and w_min <= bw <= w_max
                    and a >= a_min):
                continue
            if near and not cropped:
                cx, cy = int(cent[i][0]), int(cent[i][1])
                if math.hypot(cx - near[0], cy - near[1]) > radius:
                    continue
            hits += a
        return hits

    def enemy_bar(self, bgr, near=None, half_w=95, band=55):
        """The green/blue HP bar under a mob that something has already hit.

        Measured: bright green on the left (H 55-70, S 150+), dark blue on
        the right (H 112-132, S 120-175), ~70 px wide, ~18 px under the name.
        The bar simply does not exist until the mob takes damage, so it is the
        strongest "we are fighting THIS mob" signal there is.
        """
        h_full, w_full = bgr.shape[:2]
        sx, sy = w_full / 1920.0, h_full / 1080.0
        if near:
            half_w = max(30, int(half_w * sx))
            band = max(12, int(band * sy))
            x0 = max(0, int(near[0] - half_w))
            x1 = min(w_full, int(near[0] + half_w))
            y0 = max(0, int(near[1] + 2))
            y1 = min(h_full, int(near[1] + band))
        else:
            x0, y0, x1, y1 = 0, int(h_full * 0.10), w_full, int(h_full * 0.90)
        if x1 - x0 < 12 or y1 - y0 < 3:
            return None
        hsv = cv2.cvtColor(bgr[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
        green = cv2.inRange(hsv, np.array([50, 140, 145]), np.array([78, 255, 255]))
        dark = cv2.inRange(hsv, np.array([110, 115, 80]), np.array([134, 180, 135]))
        green = self._cut_at(green, x0, y0)
        dark = self._cut_at(dark, x0, y0)

        g = green > 0
        d = dark > 0
        min_g = max(3, int(5 * sx))
        min_d = max(2, int(3 * sx))
        best = None
        for j in range(g.shape[0]):
            ng, nd = int(g[j].sum()), int(d[j].sum())
            if ng >= min_g and nd >= min_d and (best is None or ng + nd > best[1]):
                best = (j, ng + nd)
        if best is None:
            return None

        j = best[0]
        # damage digits overlap the bar and cut the green into pieces; bridge
        # gaps up to ~20 design px or every hit hides the bar we are reading
        bridge = max(10, int(20 * sx))

        def runs(row, max_gap=bridge):
            out, start, last = [], None, None
            for i, v in enumerate(row):
                if v:
                    if start is None:
                        start = i
                    elif last is not None and i - last > max_gap:
                        out.append((start, last))
                        start = i
                    last = i
                elif start is not None and last is not None and i - last > max_gap:
                    out.append((start, last))
                    start = None
            if start is not None and last is not None:
                out.append((start, last))
            return out

        gr = max(runs(g[j]), key=lambda r: r[1] - r[0], default=None)
        dr = max(runs(d[j]), key=lambda r: r[1] - r[0], default=None)
        if gr is None or dr is None:
            return None
        if gr[0] > dr[0]:                      # green must be on the left
            return None
        gap = dr[0] - gr[1]
        if gap < -3 or gap > max(8, int(22 * sx)):
            return None
        width = dr[1] - gr[0] + 1
        if not (max(15, int(35 * sx)) <= width <= max(60, int(220 * sx))):
            return None
        ng = int(g[j, gr[0]:gr[1] + 1].sum())
        nd = int(d[j, dr[0]:dr[1] + 1].sum())
        if ng + nd < max(4, int(8 * sx)):
            return None
        # fraction from the SPANS: damage digits over the bar punch holes in
        # the pixel counts, which made the same bar swing 0.6-0.8 per read
        gspan = gr[1] - gr[0] + 1
        dspan = dr[1] - dr[0] + 1
        return {"x": x0 + gr[0], "y": y0 + j, "w": width, "h": 1,
                "frac": gspan / float(gspan + dspan),
                "green_px": ng, "dark_px": nd}


# ---------------------------------------------------------------- brain
class Brain:
    def __init__(self, cfg, percept, inp, log, focused_fn=None):
        self.cfg, self.p, self.i, self.log = cfg, percept, inp, log
        self.focused = focused_fn or focused
        self.state = "idle"
        self.target = None
        self.hp_ratio = None
        self.actions = 0
        self.started = None
        self.last_attack = 0.0
        self.last_skill = 0.0
        self.skill_i = 0
        self.recent = deque(maxlen=14)
        self.low_since = None
        self.hp_hist = []
        self.hp_read = -1
        self.potions = 0
        self.logged_action = 0
        self.engage_clicks = 0
        self.engaged = False
        self.last_progress = 0.0
        self.last_potion = 0.0
        self.last_loot = 0.0
        self.last_focus_try = 0.0
        self.last_scan = 0.0
        self.last_measure = 0.0
        self.last_fight = 0.0
        self.in_fight = False
        self.fight_px = 0
        self.enemy = None
        self.enemy_hp = None
        self.enemy_prev = None
        self.target_fought = False
        self.plate_seen = 0.0
        self.engage_at = 0.0
        self.fought_at = 0.0
        self.last_frame = None
        self.last_mobs = []
        self.hover_miss = []

    # -- helpers ---------------------------------------------------------
    def note(self, s):
        self.recent.append(f"{time.strftime('%H:%M:%S')} {s}")
        self.log(s)

    def release(self):
        self.i.right_up()

    def _reset_engage(self):
        self.engaged = False
        self.engage_clicks = 0
        self.target_fought = False

    def _hover_failed_recently(self, m):
        """A name that showed no blue a moment ago is not retried at once."""
        now = time.time()
        self.hover_miss = [h for h in self.hover_miss if h[2] > now]
        return any(abs(m["cx"] - h[0]) < 55 and abs(m["cy"] - h[1]) < 55
                   for h in self.hover_miss)

    def _track(self, now, mobs):
        """Keep or drop the locked target now that its plate is gone."""
        # deliberately tight: a wide radius locks onto a neighbour's
        # plate.  A plate lost to a camera pan is covered by run-wait.
        tr = self.cfg["track_radius"]
        near_m = [m for m in mobs
                  if abs(m["cx"] - self.target["cx"]) <= tr
                  and abs(m["cy"] - self.target["cy"]) <= tr]
        if near_m:
            near_m.sort(key=lambda m: abs(m["cx"] - self.target["cx"])
                        + abs(m["cy"] - self.target["cy"]))
            self.target = dict(self.target, **{k: near_m[0][k] for k in
                                               ("cx", "cy", "click_y",
                                                "x", "y", "w", "h")})
            self.plate_seen = now
            self.state = "track"
        elif self.enemy is not None and self.enemy_hp is not None \
                and self.enemy_hp <= 0.05:
            # nameplate gone AND the bar is empty: it died.  Swap now
            # instead of holding the right button on a corpse.
            self.note(f"enemy at ({self.target['cx']},"
                      f"{self.target['click_y']}) died - swapping")
            self.target = None
            self._reset_engage()
            self.release()
        elif self.in_fight and (
                (self.enemy is not None and self.enemy_hp is not None
                 and self.enemy_hp > 0.05)
                or now - self.last_fight < 0.5):
            # nameplate hidden by effects, but the enemy bar is still up
            # or damage was seen a blink ago: still the same fight
            self.state = "track-hold"
        elif not self.target_fought \
                and now - self.plate_seen < 2.5:
            # clicked and still running: the plate left the view with the
            # camera pan, it comes back when the character arrives
            self.state = "run-wait"
        else:
            self.note(f"target gone at ({self.target['cx']},"
                      f"{self.target['cy']}) - killed or out of range")
            self.target = None
            self._reset_engage()
            self.release()

    # -- one tick --------------------------------------------------------
    def tick(self, rect):
        now = time.time()
        if not self.focused():
            self.state = "unfocused"
            self.release()
            if now - self.last_focus_try > 1.5:
                self.last_focus_try = now
                focus_game()
            return

        bgr = self.p.grab(rect)
        if bgr is None:
            self.state = "no-frame"
            self.release()
            return
        self.last_frame = bgr
        # OCR cadence: cheap while idle, quick while a target is engaged
        self.p.hp_urgent = self.target is not None
        self.hp_ratio, _hp_cur = self.p.hp(bgr)
        self.p.chat(bgr)

        # potion: the reading must be VALID and low twice, so one misread
        # digit cannot drink a potion at full health
        if self.hp_ratio is not None and self.hp_ratio < self.cfg["hp_potion_below"] \
                and getattr(self.p, "hp_valid", True):
            # count only NEW ocr results: repeating one cached number for a
            # second is not a second confirmation
            rid = getattr(self.p, "read_id", 0)
            fresh = rid != self.hp_read
            if fresh:
                self.hp_read = rid
                self.hp_hist.append(self.hp_ratio)
                self.hp_hist = self.hp_hist[-2:]
            if fresh and self.hp_ratio < 0.30 \
                    and now - self.last_potion > self.cfg["potion_cooldown_s"]:
                # critical: one trustworthy read is enough - waiting for the
                # second confirmation at 10% hp can arrive after the corpse
                self.state = "recover"
                self.i.key(self.cfg["potion_key"])
                self.note(f"hp {self.hp_ratio:.0%} critical -> potion")
                self.potions += 1
                self.last_potion = now
                self.low_since = None
                self.hp_hist = []
                return
            if self.low_since is None:
                self.low_since = now
            elif (len(self.hp_hist) >= 2
                  and max(self.hp_hist) < self.cfg["hp_potion_below"]
                  and now - self.low_since > self.cfg["potion_confirm_s"]
                  and now - self.last_potion > self.cfg["potion_cooldown_s"]):
                self.state = "recover"
                self.i.key(self.cfg["potion_key"])
                self.note(f"hp {self.hp_ratio:.0%} -> potion")
                self.potions += 1
                self.last_potion = now
                self.low_since = None
                self.hp_hist = []
                return
        else:
            self.low_since = None
            self.hp_hist = []

        # scans are throttled so one frame is not decoded three times
        if now - self.last_scan >= 0.08:
            self.last_mobs = self.p.mobs(bgr)
            self.last_scan = now
        mobs = self.last_mobs

        # ---- measure the fight, when there is one to measure --------------
        # in_fight is sticky for a moment: a fight seen a blink ago still
        # counts, so one missed frame cannot cause a pointless re-click.
        near = (self.target["cx"], self.target["cy"]) if self.target else None
        if near:
            if now - self.last_measure >= 0.06:
                self.fight_px = self.p.damage_numbers(bgr, near)
                self.enemy = self.p.enemy_bar(bgr, near=near)
                h_now = self.enemy["frac"] if self.enemy else None
                if (h_now is not None and self.enemy_prev is not None
                        and h_now < self.enemy_prev - 0.02):
                    self.last_fight = now      # the bar went DOWN: real fight
                self.enemy_hp, self.enemy_prev = h_now, h_now
                self.last_measure = now
            if self.fight_px >= self.cfg["fight_white_min"] or self.enemy:
                self.last_fight = now
                if not self.target_fought:
                    self.fought_at = now   # first real hit on this target
                self.target_fought = True
        else:
            self.fight_px, self.enemy, self.enemy_hp = 0, None, None
            self.enemy_prev = None
        self.in_fight = self.target is not None and now - self.last_fight < 1.0

        # ---- keep the target while its nameplate is there -----------------
        target = None
        if self.target is not None:
            ks = getattr(self.p, "kill_seen", 0.0)
            if self.target_fought and ks >= self.fought_at \
                    and now - ks < 3.0 and now - self.plate_seen > 0.3:
                # an exp line newer than this target's first hit is a kill
                # receipt: it died even if its bar was never seen empty.
                # Strictly newer, so exp from the PREVIOUS kill cannot
                # "kill" a target engaged a moment later; and only while the
                # plate is gone, so a delayed OCR cannot drop a live target
                # whose name is still on screen.
                self.note(f"exp line -> kill confirmed at "
                          f"({self.target['cx']},{self.target['click_y']})")
                self.target = None
                self._reset_engage()
                self.release()
            else:
                self._track(now, mobs)
                target = self.target

        # ---- otherwise: find, hover, blue? click --------------------------
        if target is None:
            if not mobs:
                self.release()
                self.state = "idle"
                return
            self.state = "confirm"
            first = True
            for m in mobs[:3]:
                if self.p.in_exclude(m["cx"], m["click_y"]):
                    continue
                if self._hover_failed_recently(m):
                    continue
                ok, blue = True, 0
                if self.cfg["require_blue"]:
                    # the tick frame is a valid pre-hover baseline only before
                    # the cursor has been sent anywhere this tick
                    ok, blue = self.p.confirm(rect, self.i, m,
                                              pre=bgr if first else None)
                first = False
                if not ok:
                    self.hover_miss.append((m["cx"], m["cy"], now + 2.5))
                    continue
                target = dict(m, blue=blue, locked=True)
                lh = getattr(self.p, "last_hover", None)
                if lh:
                    target["red"] = lh[2]
                self._reset_engage()
                self.plate_seen = now
                self.engage_at = now
                if self.cfg["mode"] != "gunner":
                    self.i.click(rect[0] + target["cx"],
                                 rect[1] + target["click_y"])
                self.engage_clicks = 1
                self.engaged = True
                self.last_attack = self.last_progress = now
                self.actions += 1
                how = "hold" if self.cfg["mode"] == "gunner" else "click"
                self.note(f"hover lock ({target['cx']},{target['click_y']}) "
                          f"b={blue} r={target.get('red', '-')} -> {how}")
                break
            if target is None:
                self.release()
                self.state = "no-confirm"
                return

        self.target = target
        now = time.time()
        mode = self.cfg["mode"]
        if self.in_fight:
            self.last_progress = now

        if mode == "gunner":
            self.state = "shooting"
            self.i.right_down(rect[0] + target["cx"],
                              rect[1] + target["click_y"])
            return self._tail(now, target)

        self.state = "attack" if mode == "melee" else "drawing"

        # ---- one click, then measure --------------------------------------
        # The click already ordered the run and the swing; clicking again while
        # the plate is moving is what used to miss.  A second click only when
        # the first produced no damage at all, at the mob's CURRENT spot.
        quiet = now - self.last_progress
        max_clicks = max(1, int(self.cfg["max_clicks"]))
        if not self.in_fight and quiet > self.cfg["no_hit_s"] \
                and self.engage_clicks < max_clicks:
            x, y = target["cx"], target["click_y"]
            self.i.click(rect[0] + x, rect[1] + y)
            self.engage_clicks += 1
            self.last_attack = self.last_progress = now
            self.note(f"no hit - re-clicked ({x},{y}) "
                      f"[{self.engage_clicks}/{max_clicks}]")
            self.state = "re-click"
            return
        if quiet > self.cfg["give_up_s"]:
            self.note("no damage for too long - giving up on this one")
            self.target = None
            self._reset_engage()
            self.release()
            self.state = "give-up"
            return
        return self._tail(now, target)

    def _tail(self, now, target):
        if now - self.last_skill >= self.cfg["skill_every_s"]:
            keys = self.cfg["skill_keys"]
            if keys:
                self.i.key(keys[self.skill_i % len(keys)])
                self.skill_i += 1
            self.last_skill = now

        if self.cfg.get("loot_after_hit") \
                and now - self.last_loot >= self.cfg["loot_every_s"]:
            self.i.key(self.cfg["loot_key"], 12)
            self.last_loot = now

        if self.actions and self.actions % 20 == 0 \
                and self.actions != self.logged_action:
            self.logged_action = self.actions
            hp = f"{self.hp_ratio:.0%}" if self.hp_ratio is not None else "-"
            self.note(f"{self.cfg['mode']} hit ({target['cx']},"
                      f"{target['click_y']}) blue={target.get('blue')} hp={hp}")

    def status(self):
        return {"state": self.state, "hp": self.hp_ratio, "target": self.target,
                "mode": self.cfg["mode"], "actions": self.actions,
                "in_fight": self.in_fight, "fight_px": self.fight_px,
                "kills": getattr(self.p, "kill_count", 0),
                "potions": self.potions,
                "chat": getattr(self.p, "chat_text", ""),
                "uptime": int(time.time() - self.started) if self.started else 0,
                "recent": list(self.recent)}


# ---------------------------------------------------------------- GUI
def run_gui(cfg, replay=None):
    import tkinter as tk
    from PIL import Image, ImageTk

    percept, inp = Perception(cfg), Input()
    q = queue.Queue()
    brain = Brain(cfg, percept, inp, q.put)

    replay_rect = None
    if replay:
        frame = cv2.imread(replay)
        if frame is None:
            print(f"cannot read {replay}")
            return 1
        replay_rect = (0, 0, frame.shape[1], frame.shape[0])
        percept.grab = lambda r: frame.copy()
        for fn in ("click", "key", "move", "right_down", "right_up"):
            setattr(inp, fn, lambda *a, **k: None)
        brain.focused = lambda: True

    def current_rect():
        return replay_rect if replay else client_rect()

    root = tk.Tk()
    root.title("OCR BOT")
    # anchor on the game window so the panel sits on the same monitor;
    # 360 wide fits a 1366x768 laptop without covering the fight
    sw, sh = u32.GetSystemMetrics(0), u32.GetSystemMetrics(1)
    gw, gh = 360, 580
    r = client_rect()
    if r and r[2] - r[0] < sw:
        gx = max(0, min(r[2] - gw - 10, sw - gw - 8))
        gy = max(0, r[1] + 30)
    else:
        gx, gy = sw - gw - 10, 40
    gh = min(gh, sh - gy - 48)
    root.geometry(f"{gw}x{gh}+{gx}+{gy}")
    root.attributes("-topmost", True)
    root.configure(bg=BG)
    root.minsize(gw, min(gh, 480))

    # ---- header
    head = tk.Frame(root, bg=BG)
    head.pack(fill="x", padx=8, pady=(6, 0))
    tk.Label(head, text="OCR BOT", bg=BG, fg=ACCENT,
             font=("Consolas", 12, "bold")).pack(side="left")
    state_lbl = tk.Label(head, text="idle", bg=BG, fg=DIM, font=("Consolas", 9))
    state_lbl.pack(side="right")

    # ---- transport
    row = tk.Frame(root, bg=BG)
    row.pack(fill="x", padx=8, pady=5)
    start_btn = tk.Button(row, text="START F8", width=9, bg="#1f6f3f",
                          fg="white", relief="flat",
                          font=("Consolas", 9, "bold"))
    start_btn.pack(side="left")
    stop_btn = tk.Button(row, text="STOP F9", width=8, bg="#5a2222",
                         fg="white", relief="flat",
                         font=("Consolas", 9, "bold"))
    stop_btn.pack(side="left", padx=4)
    quit_btn = tk.Button(row, text="X", width=3, bg="#22262d", fg=FG,
                         relief="flat", font=("Consolas", 9),
                         command=root.destroy)
    quit_btn.pack(side="right")

    # ---- mode
    mrow = tk.Frame(root, bg=BG)
    mrow.pack(fill="x", padx=8)
    tk.Label(mrow, text="MODE", bg=BG, fg=DIM,
             font=("Consolas", 8, "bold")).pack(side="left", padx=(0, 4))
    mode_var = tk.StringVar(value=cfg["mode"])
    mode_btns = {}
    for m in ("melee", "ranger", "gunner"):
        b = tk.Button(mrow, text=m.upper(), width=7, relief="flat",
                      font=("Consolas", 8, "bold"), bg="#22262d", fg=DIM,
                      command=lambda mm=m: set_mode(mm))
        b.pack(side="left", padx=1)
        mode_btns[m] = b

    def paint_mode():
        for m, b in mode_btns.items():
            on = mode_var.get() == m
            b.config(bg=ACCENT if on else "#22262d",
                     fg="#0b0f0c" if on else DIM)

    def set_mode(m):
        mode_var.set(m)
        cfg["mode"] = m
        brain.release()
        paint_mode()
        save_cfg()

    # ---- settings (every value editable, saves on change)
    box = tk.LabelFrame(root, text=" settings - saved on edit ", bg=BG,
                        fg=DIM, bd=1, relief="solid", font=("Consolas", 8),
                        highlightbackground="#22262d")
    box.pack(fill="x", padx=8, pady=4)
    grid = tk.Frame(box, bg=BG)
    grid.pack(fill="x", padx=4, pady=(1, 0))
    vars_by_key = {}

    def parse(v):
        v = v.strip()
        try:
            return int(v)
        except ValueError:
            pass
        try:
            return float(v)
        except ValueError:
            return v

    def entry(r, c, label, key, width=5):
        tk.Label(grid, text=label, bg=BG, fg=DIM, font=("Consolas", 8),
                 anchor="e").grid(row=r, column=c * 2, sticky="e",
                                  padx=(0, 3), pady=0)
        var = tk.StringVar(value=str(cfg.get(key, "")))
        vars_by_key[key] = var
        tk.Entry(grid, textvariable=var, width=width, bg="#0d0f12", fg=FG,
                 insertbackground=FG, relief="flat",
                 font=("Consolas", 8)).grid(row=r, column=c * 2 + 1,
                                            sticky="w", padx=(0, 10), pady=0)

        def commit(*_):
            try:
                cfg[key] = parse(var.get())
                save_cfg()
            except Exception:
                pass
        var.trace_add("write", commit)
        return var

    entry(0, 0, "attack s", "attack_interval_s")
    entry(0, 1, "skill s", "skill_every_s")
    entry(1, 0, "blue px", "hover_blue_min")
    entry(1, 1, "hover s", "hover_confirm_s")
    entry(2, 0, "blue +", "hover_delta_min")

    tk.Label(grid, text="skills", bg=BG, fg=DIM, font=("Consolas", 8),
             anchor="e").grid(row=3, column=0, sticky="e", padx=(0, 3))
    skills_var = tk.StringVar(value=",".join(cfg.get("skill_keys", [])))
    vars_by_key["skill_keys"] = skills_var
    tk.Entry(grid, textvariable=skills_var, width=20, bg="#0d0f12", fg=FG,
             insertbackground=FG, relief="flat",
             font=("Consolas", 8)).grid(row=3, column=1, columnspan=3,
                                        sticky="w", pady=0)

    def commit_skills(*_):
        cfg["skill_keys"] = [p.strip() for p in skills_var.get().split(",")
                             if p.strip()]
        save_cfg()
    skills_var.trace_add("write", commit_skills)

    opt = tk.Frame(box, bg=BG)
    opt.pack(fill="x", padx=4, pady=(1, 0))

    def toggle(key, text, pad=8):
        v = tk.BooleanVar(value=bool(cfg.get(key, False)))
        tk.Checkbutton(opt, text=text, variable=v, bg=BG, fg=DIM,
                       selectcolor=BG, activebackground=BG,
                       activeforeground=FG, font=("Consolas", 8),
                       command=lambda: (cfg.__setitem__(key, v.get()),
                                        save_cfg())).pack(side="left",
                                                          padx=(0, pad))
        return v

    toggle("require_blue", "require blue")
    toggle("loot_after_hit", "loot")

    brow = tk.Frame(box, bg=BG)
    brow.pack(fill="x", padx=4, pady=(0, 4))

    def reload_cfg():
        try:
            fresh = json.loads(CONFIG.read_text())
        except Exception as exc:
            brain.note(f"reload failed: {exc}")
            return
        cfg.clear()
        cfg.update(dict(DEFAULTS))
        cfg.update(fresh)
        for k, var in vars_by_key.items():
            if k == "skill_keys":
                var.set(",".join(cfg.get("skill_keys", [])))
            elif k in cfg:
                var.set(str(cfg[k]))
        brain.note("settings reloaded from agent.json")

    tk.Button(brow, text="RELOAD", width=8, bg="#22262d", fg=FG,
              relief="flat", font=("Consolas", 8),
              command=reload_cfg).pack(side="left")
    tk.Button(brow, text="DEFAULTS", width=9, bg="#22262d", fg=FG,
              relief="flat", font=("Consolas", 8),
              command=lambda: (cfg.update(dict(DEFAULTS)), save_cfg(),
                               brain.note("settings reset to defaults"))
              ).pack(side="left", padx=4)
    tk.Label(brow, text="agent.json", bg=BG, fg=DIM,
             font=("Consolas", 7)).pack(side="right")

    # ---- hp
    HP_W = 190
    hp_row = tk.Frame(root, bg=BG)
    hp_row.pack(fill="x", padx=8)
    tk.Label(hp_row, text="HP", bg=BG, fg=DIM,
             font=("Consolas", 8, "bold")).pack(side="left")
    hp_canvas = tk.Canvas(hp_row, width=HP_W, height=12, bg="#20242c",
                          highlightthickness=0)
    hp_canvas.pack(side="left", padx=6)
    hp_lbl = tk.Label(hp_row, text="-", bg=BG, fg=FG, font=("Consolas", 8))
    hp_lbl.pack(side="left")
    tgt_lbl = tk.Label(root, text="target -", bg=BG, fg=DIM,
                       font=("Consolas", 8), anchor="w")
    tgt_lbl.pack(fill="x", padx=8)

    # ---- preview
    PREVIEW_W, PREVIEW_MAX_H = 320, 210
    view = tk.Canvas(root, width=PREVIEW_W, height=180, bg="#0a0c10",
                     highlightthickness=0)
    view.pack(padx=8, pady=(3, 2))

    # ---- log
    logbox = tk.Text(root, height=4, bg="#0a0c10", fg="#9aa4b2", relief="flat",
                     font=("Consolas", 8), insertbackground=FG)
    logbox.pack(fill="both", expand=True, padx=8, pady=(0, 3))
    logbox.insert("end", "ready - press START / F8\n")

    ftr = tk.Label(root, text="", bg=BG, fg=DIM, font=("Consolas", 7), anchor="w")
    ftr.pack(fill="x", padx=8, pady=(0, 5))

    def save_cfg():
        try:
            CONFIG.write_text(json.dumps(cfg, indent=2))
        except Exception:
            pass

    running = {"on": False}

    def start():
        if running["on"]:
            return
        running["on"] = True
        brain.started = time.time()
        brain.note(f"start {cfg['mode']}")
        if not is_admin():
            brain.note("NOT ELEVATED - Windows will block clicks into the game")
        if not focused() and not replay:
            brain.note("game not focused - bringing it forward")

            def refocus(tries=0):
                if not running["on"]:
                    return
                if focus_game():
                    brain.note("focused the game window")
                elif tries < 10:
                    root.after(500, lambda: refocus(tries + 1))
                else:
                    brain.note("could not focus the game - click it once")
            root.after(150, refocus)

    def stop():
        if not running["on"]:
            return
        running["on"] = False
        brain.release()
        brain.started = None
        brain.note("stop (right button released)")

    start_btn.config(command=start)
    stop_btn.config(command=stop)
    paint_mode()

    no_game = {"warned": False}

    _rmb_prev = {"down": False}

    def _stop_hotkey():
        rmb = bool(u32.GetAsyncKeyState(0x02) & 0x8000)
        pulse = rmb and not _rmb_prev["down"]
        _rmb_prev["down"] = rmb
        return (pulse
                or ((u32.GetAsyncKeyState(0x11) & 0x8000)
                    and (u32.GetAsyncKeyState(0x10) & 0x8000)
                    and (u32.GetAsyncKeyState(0x58) & 0x8000))
                or ((u32.GetAsyncKeyState(0x11) & 0x8000)
                    and (u32.GetAsyncKeyState(0x78) & 0x8000)))

    def _emgc_poll():
        while True:
            try:
                if running["on"] and not inp.right_held and _stop_hotkey():
                    stop()
            except Exception:
                pass
            time.sleep(0.012)

    threading.Thread(target=_emgc_poll, daemon=True).start()

    def worker():
        while True:
            delay = cfg["scan_gap_s"]
            try:
                if running["on"]:
                    r = current_rect()
                    if r is None:
                        if not no_game["warned"]:
                            brain.note("waiting for the game window "
                                       "'Risk Your Life'...")
                            no_game["warned"] = True
                        brain.state = "no-game"
                        brain.release()
                        delay = 0.25
                    else:
                        no_game["warned"] = False
                        brain.tick(r)
                        if brain.state == "no-frame":
                            delay = 0.15      # do not hammer a dead capture
            except Exception as e:
                q.put(f"error: {e}")
                delay = 0.25
                try:
                    brain.release()
                except Exception:
                    pass
            time.sleep(delay)

    def drain():
        while True:
            try:
                msg = q.get_nowait()
            except queue.Empty:
                break
            logbox.insert("end", str(msg) + "\n")
            logbox.see("end")

    def update_exclude():
        """Keep the panel's rectangle out of perception - it sits over the game."""
        try:
            r = current_rect()
            if r is None:
                percept.exclude = None
                return
            wx, wy = root.winfo_rootx(), root.winfo_rooty()
            ww, wh = root.winfo_width(), root.winfo_height()
            percept.exclude = (wx - r[0], wy - r[1],
                               wx - r[0] + ww, wy - r[1] + wh)
        except Exception:
            percept.exclude = None

    preview = [0.0, None, 0.0]      # last draw, last frame, last idle grab
    psize = [PREVIEW_W, 180]

    def draw():
        drain()
        update_exclude()
        st = brain.status()
        state_lbl.config(text=st["state"],
                         fg=(ACCENT if st["state"] in
                             ("attack", "shooting", "drawing", "re-click")
                             else DIM))
        hp = st["hp"]
        hp_canvas.delete("all")
        if hp is None:
            hp_lbl.config(text="-")
        else:
            w = int(HP_W * max(0.0, min(1.0, hp)))
            col = ACCENT if hp > .55 else WARN if hp > .3 else DANGER
            hp_canvas.create_rectangle(0, 0, w, 12, fill=col, outline="")
            hp_lbl.config(text=f"{hp:.0%}")
        t = st["target"]
        tgt_lbl.config(text=(f"target ({t['cx']},{t['click_y']}) "
                             f"b={t['blue']} r={t.get('red', '-')}")
                       if t else "target -")

        # preview at ~4 fps from the bot's own last frame: the UI thread must
        # not grab the window or run detection - that is what made the panel
        # stutter on a weak machine.  When the bot is stopped the frame is
        # grabbed directly, slower, so the panel is a live game monitor.
        now = time.time()
        if now - preview[0] >= 0.25:
            bgr, plist = brain.last_frame, brain.last_mobs
            if not running["on"] or bgr is None:
                # stopped: show the live game at a cheap cadence and still
                # outline the nameplates - that is the "is it seeing them?"
                bgr, plist = None, []
                if now - preview[2] >= 0.6:
                    preview[2] = now
                    rect = current_rect()
                    if rect is not None:
                        bgr = percept.grab(rect)
                        if bgr is not None:
                            plist = percept.mobs(bgr)
            if bgr is not None and bgr is not preview[1]:
                preview[0], preview[1] = now, bgr
                try:
                    fh, fw = bgr.shape[:2]
                    sc = min(PREVIEW_W / max(1, fw),
                             PREVIEW_MAX_H / max(1, fh))
                    pw, phh = max(1, int(fw * sc)), max(1, int(fh * sc))
                    if [pw, phh] != psize:
                        psize[:] = [pw, phh]
                        view.config(width=pw, height=phh)
                    small = cv2.resize(bgr, (pw, phh),
                                       interpolation=cv2.INTER_NEAREST)
                    ph = ImageTk.PhotoImage(
                        Image.fromarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB)))
                    view.delete("all")
                    view.image = ph
                    view.create_image(0, 0, anchor="nw", image=ph)
                    # every pink nameplate found, outlined, with its click point
                    for m in plist:
                        view.create_rectangle(
                            int(m["x"] * sc), int(m["y"] * sc),
                            int((m["x"] + m["w"]) * sc),
                            int((m["y"] + m["h"]) * sc),
                            outline="#2c6e41")
                        ax, ay = int(m["cx"] * sc), int(m["click_y"] * sc)
                        view.create_line(ax - 4, ay, ax + 4, ay,
                                         fill="#3fb950")
                        view.create_line(ax, ay - 4, ax, ay + 4,
                                         fill="#3fb950")
                    # idle: show where it WOULD click, dim green
                    if not running["on"] and plist:
                        m = plist[0]
                        ax = int(m["cx"] * sc)
                        ay = int(m["click_y"] * sc)
                        view.create_oval(ax - 6, ay - 6, ax + 6, ay + 6,
                                         outline="#3fb950")
                        view.create_oval(ax - 2, ay - 2, ax + 2, ay + 2,
                                         fill="#3fb950", outline="")
                        tx = min(max(ax + 9, 4), max(4, pw - 66))
                        ty = ay - 13 if ay > 26 else ay + 15
                        tid = view.create_text(
                            tx, ty, anchor="w",
                            text=f"{m['cx']},{m['click_y']}",
                            fill="#3fb950", font=("Consolas", 8, "bold"))
                        bx0, by0, bx1, by1 = view.bbox(tid)
                        rid = view.create_rectangle(
                            bx0 - 3, by0 - 1, bx1 + 3, by1 + 1,
                            fill="#0a0c10", outline="#2c6e41")
                        view.tag_lower(rid, tid)
                    # the target: crosshair, dot on the exact click pixel and
                    # its client coordinates in a box that follows it
                    if t:
                        ax, ay = int(t["cx"] * sc), int(t["click_y"] * sc)
                        view.create_line(0, ay, pw, ay, fill="#f0883e")
                        view.create_line(ax, 0, ax, phh, fill="#f0883e")
                        view.create_oval(ax - 8, ay - 8, ax + 8, ay + 8,
                                         outline="#f0883e", width=2)
                        view.create_oval(ax - 2, ay - 2, ax + 2, ay + 2,
                                         fill="#ffd33d", outline="")
                        tx = min(max(ax + 11, 4), max(4, pw - 66))
                        ty = ay - 13 if ay > 26 else ay + 15
                        tid = view.create_text(
                            tx, ty, anchor="w",
                            text=f"{t['cx']},{t['click_y']}",
                            fill="#ffd33d", font=("Consolas", 8, "bold"))
                        bx0, by0, bx1, by1 = view.bbox(tid)
                        rid = view.create_rectangle(
                            bx0 - 3, by0 - 1, bx1 + 3, by1 + 1,
                            fill="#0a0c10", outline="#f0883e")
                        view.tag_lower(rid, tid)
                except Exception:
                    pass

        ftr.config(text=f"{st['mode']} a{st['actions']} k{st.get('kills', 0)} "
                        f"p{st.get('potions', 0)} "
                        f"{'FIGHT' if st['in_fight'] else 'calm'} "
                        f"{st['uptime']}s | F8 F9 F10 | RMB stop")
        root.after(150, draw)

    def keys():
        if inp.pressed("f8"):
            start()
        if inp.pressed("f9"):
            stop()
        if inp.pressed("f10"):
            stop()
            root.destroy()
            return
        root.after(90, keys)

    threading.Thread(target=worker, daemon=True).start()
    root.protocol("WM_DELETE_WINDOW", lambda: (stop(), root.destroy()))
    root.after(140, draw)
    root.after(140, keys)
    if replay:
        root.after(400, start)
    root.mainloop()
    stop()
    return 0


# ---------------------------------------------------------------- main
def input_test():
    """Prove where a click can die, one step at a time.

    Watch the game while this runs: it walks the cursor and clicks.  If the
    character reacts, SendInput is landing and any miss is aim, not injection.
    """
    rect = client_rect()
    if not rect:
        print("game window 'Risk Your Life' not found - start the game first")
        return 1
    inp = Input()
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    cx, cy = rect[0] + w // 2, rect[1] + h // 2

    print(f"python           : {sys.version.split()[0]} "
          f"({ctypes.sizeof(ctypes.c_void_p) * 8}-bit)")
    print(f"sizeof(INPUT)    : {ctypes.sizeof(INPUT)}  (40 = correct for x64)")
    print(f"elevated         : {is_admin()}   <- must be True")
    print(f"client rect      : {rect}   ({w}x{h})")
    print(f"game foreground  : {focused()}   <- must be True")
    print(f"cursor before    : {cursor_pos()}")

    ok = 0
    for n in range(3):
        x = cx + (n - 1) * 120
        inp.move(x, cy)
        time.sleep(0.12)
        r1 = inp._mouse(0x0002)          # LEFTDOWN
        time.sleep(0.06)
        r2 = inp._mouse(0x0004)          # LEFTUP
        time.sleep(0.45)
        ok += (r1 == 1 and r2 == 1)
        print(f"click {n + 1} at ({x},{cy})  down={r1} up={r2}  "
              f"cursor={cursor_pos()}")

    print(f"cursor after     : {cursor_pos()}")
    print()
    if ok == 3 and is_admin() and focused():
        print("SendInput accepted every click.  Did the character move/turn?")
        print("  yes -> input works; any miss now is aim, not injection")
        print("  no  -> the client is swallowing it: check Mouse Mode (ESC)")
    else:
        print("One of the gates above failed - fix it before blaming the aim.")
    return 0


def main():
    cfg = dict(DEFAULTS)
    if CONFIG.exists():
        try:
            cfg.update(json.loads(CONFIG.read_text()))
        except Exception:
            pass

    if "--inputtest" in sys.argv:
        return input_test()

    replay = None
    for i, a in enumerate(sys.argv):
        if a == "--replay" and i + 1 < len(sys.argv):
            replay = sys.argv[i + 1]

    if not replay and not client_rect():
        print("game window 'Risk Your Life' not found yet - the panel will "
              "wait for it.\nStart the client, then press START / F8.")
    return run_gui(cfg, replay)


if __name__ == "__main__":
    raise SystemExit(main())
