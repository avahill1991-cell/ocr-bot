# OCR BOT

A screen-only farm bot for RYL1. It reads the game off the screen: pink
nameplates, the client's own hover marker, damage, the enemy HP bar, the HP
digits and the chat exp line. Nothing is injected, hooked or attached — this
client kills the game if it sees Cheat Engine, x64dbg, IDA or an injected DLL.
**Screen-only, on purpose.**

## The loop

```
pink name found -> cursor to the body (30 px under the plate)
  -> blue glow OR red feet ring held 0.5 s?
  yes -> click once (gunner: hold the right button) -> damage / enemy HP bar?
         yes -> keep swinging on cadence
                name gone + bar empty (or a fresh exp line) -> died -> next mob
                name hidden but bar/damage still there -> keep the lock
                name lost while running there -> wait 2.5 s for it to return
         no  -> one re-click -> nothing after 15s -> next mob
  no  -> skip that name for a moment, try the next
```

Each of the three classes uses that same loop:

| mode | behaviour |
|---|---|
| `melee` | left-click, cycle skill keys on a cadence |
| `ranger` | same click, separate cadence |
| `gunner` | no left click at all: holds the RIGHT button on the locked target until it dies |

Between targets, on a timer, the bot also takes a **buffer break**: every
`buffer_every_s` (default 60 s) it points 30 px under the nearest **white**
nameplate — players and the buffer are white, mobs are pink — and holds the
RIGHT button for `buffer_hold_s` (5 s), then resumes farming. It never
interrupts an engaged target. Toggle it with `buffer_mode` (on by default in
`agent.json`), and the hold/every values are in the panel.

Loot uses the **E** key (`loot_key`); looting is off unless `loot_after_hit`
is ticked.

## Run (Windows)

```bat
Install.bat      # once: python packages, checks Tesseract
Start.bat        # elevated, opens the panel; F8 start, F9 stop, F10 quit
```

Run **elevated** (the game runs elevated; Windows drops clicks from a lower
process, which looks exactly like a broken bot). Python 3.10+ and
[Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) are required;
`requirements.txt` lists the pip packages. Tesseract is found automatically in
Program Files, Program Files (x86) or on PATH.

## The panel

360 px wide, docked on the right edge. The preview is the live game (the bot's
own `PrintWindow` frame while running, a slower direct grab while stopped):
green boxes are pink nameplates it can see, the orange crosshair is the target,
and the box beside it is the exact client coordinate it will click. HP bar and
log below; the footer counts actions, kills (`k`), potions (`p`) and uptime.
F8 / F9 / F10 or the buttons; a right-click pulse also stops it.

## Measurement (what makes it work)

* **Aim**: the plate floats above the model, so the focus point is 30 px under
  it (1366×768; scales with frame height). Aiming right under the text used to
  hit empty air and the click selected nothing. The pointer glides in small
  relative steps: Windows runs the mouse at speed 20 here and amplifies an
  injected step ~8× (24 px arrived as ~195 px), which is what looked like the
  pointer jumping. 5 px steps land as ~12 px and are corrected every step.
* **Hover**: two client markers count — the model turning saturated blue, and
  the dashed red ring at its feet. At the body focus point the blue mask often
  reads only ~20 px (under its floor); the red ring reads ~50 px there, so
  either one can confirm. What stops scenery faking a lock is the delta over
  the baseline measured *before* the cursor arrives, and the marker has to
  persist for `hover s` (0.5 s) before the click — a single bright frame is not
  a lock. Tunables: `hover_blue_min`, `hover_delta_min`, `hover_red_min`,
  `hover_red_delta_min`, `hover_confirm_s`.
* **Fight**: damage numbers, or the green/blue enemy HP bar — the bar does not
  exist until the mob takes damage, so it is the strongest "this fight is
  real" signal. The "in fight" flag is sticky for a second and also trips when
  the enemy bar goes *down*, so one missed frame cannot cause a pointless
  re-click.
* **Death**: the nameplate disappearing is the primary signal; an empty enemy
  bar or 0.5 s without damage confirm it. A hidden nameplate with the bar
  still up or damage still popping keeps the lock instead. In gunner mode the
  right button is released the moment the kill is confirmed, and the next
  blue hover starts in the same tick (measured ~0.1 s).
* **Kill receipt**: the chat prints `Obtained N experience` on every kill, so
  the newest chat line is also watched: the chat block is fingerprinted every
  0.4 s and only a new bottom line is OCR'd, off-thread (~0.45 s measured).
  A fresh exp line **newer than the target's first hit** confirms the death
  even when the enemy bar was hidden by effects; older lines cannot "kill" a
  target engaged a moment later. It counts kills in the footer, but it is not
  on the fast path — pixels still swap targets in ~0.1 s.
* **Buffer**: white nameplates are found with a second half-resolution scan
  (S ≤ 70, V ≥ 185), with the chat box and the bot's own panel cut out; mobs
  stay pink. The right-hold keeps the cursor on the same client point for the
  whole hold, and a missing white name means it retries on the next scan —
  no target is ever dropped for a buffer break.
* **HP**: OCR of the digits on the red plate (`1081/1440`). The max is stable
  for a session, so it is learned; a stray digit read is rejected instead of
  triggering a potion. Low HP must be read twice before drinking — except
  below 30 %, where waiting for the second read can arrive after the corpse.
  Reads run every ~2.2 s while idle and every ~1.2 s while a target is
  engaged, so Tesseract does not steal capture CPU in between.

## Fast on a weak laptop

One grab per tick, and nothing is decoded twice:

| work | how often | cost (measured, weak x64 CPU) |
|---|---|---|
| window grab (`PrintWindow`) | every tick | the one unavoidable cost |
| nameplate scan | at most every 80 ms, at **half resolution** | ~15 ms |
| fight scans (damage + bar) | at most every 60 ms, only near the target | ~6.5 + 1.9 ms |
| HP digits | **worker thread** — the tick reads a cached number | ~0 ms |
| chat line | worker thread, only when the chat block changes | ~0.45 s off-thread |
| white names | at most every 0.4 s while a buffer break is due | ~15 ms |
| GUI preview | 4 fps, nearest-neighbour downscale, no detection on the UI thread | ~1 ms |

No full-frame HSV conversion exists anywhere: nameplates convert the
half-size frame, HP converts only the plate crop, the fight scans only their
window, chat only its crop. If a frame ever fails to grab, the loop backs off
instead of spinning.

`agent.json` is edited from the GUI and saved in place. `DEFAULTS` restores,
`RELOAD FILE` re-reads it.

## Tuning from a screenshot

No game needed and no input is sent:

```bat
python agent.py --replay path\to\screen.png
```

The panel draws what it detects in that frame and where it would click, then
runs the loop on the still frame. Chat kill detection also works on a replay
frame, which is the quickest way to check the exp line and the thresholds.

## Reusing on another PC

| thing | what to do |
|---|---|
| `hp_box` | the red HP plate sits at a different height per character; if HP reads blank, set the new box |
| `chat_box` | the chat text block is a fraction of the frame; adjust if the UI is laid out differently |
| resolution | everything scales from 1920×1080, but the bands were measured at 1366×768 |
| Tesseract | install it — pip does not provide it; the bot finds it in Program Files or PATH |
| elevation | needed, see above |

## Two things that cost days, so you don't repeat them

**The screen grab is dark.** A fullscreen client is an exclusive surface:
`mss` / `ImageGrab` / BitBlt return a frame ~3× too dark to read a HUD from.
Never fully black, so no "did it fail" test catches it. The bot renders with
`PrintWindow` + `PW_RENDERFULLCONTENT`. Do not "optimise" this back.

**Input structs.** `SendInput` with the old 32-bit 28-byte `INPUT` layout is
silently ignored on a 64-bit OS — clicks do nothing and nothing errors. The
struct is 40 bytes on x64.

## Files

| file | what it does |
|---|---|
| `agent.py` | the whole bot: `Input`, `Perception`, `Brain`, the GUI |
| `winutil.py` | window rendering (`PrintWindow`) |
| `agent.json` | settings, saved by the GUI |
| `diag.py` | live measurement run: stage timings, hover/kill/potion stats |
| `Install.bat` / `Start.bat` | Windows setup and elevated launch |
| `requirements.txt` | pip packages |
