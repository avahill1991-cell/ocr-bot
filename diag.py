"""Live measurement run: per-stage timing, hover, kill and potion stats.

    python diag.py [seconds]
"""
import json
import sys
import time
from collections import Counter

import cv2
import agent

DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0

cfg = dict(agent.DEFAULTS)
try:
    cfg.update(json.loads(agent.CONFIG.read_text()))
except Exception:
    pass

p = agent.Perception(cfg)
inp = agent.Input()
logs = []
T0 = time.time()
ms = {k: [] for k in ("tick", "grab", "mobs", "damage", "bar", "hp")}
confirms = []
actions = []
states = Counter()
episode = None
episodes = []
shot = {"n": 0}


def log(s):
    global episode
    logs.append((time.time() - T0, s))
    print(f"  [{time.time() - T0:6.2f}] {s}", flush=True)
    if "hover lock" in s:
        if episode:
            episodes.append(episode)
        episode = {"t0": time.time() - T0, "click": s, "fp": [], "bar": [],
                   "dur": None, "drop": None}
        if brain.last_frame is not None:
            cv2.imwrite(f"diag_click_{len(episodes) + 1}.png",
                        brain.last_frame)
    elif ("target gone" in s or "died" in s or "giving up" in s
          or "re-clicked" in s):
        if episode:
            episode["dur"] = time.time() - T0 - episode["t0"]
            episode["drop"] = s
            if brain.last_frame is not None:
                cv2.imwrite(f"diag_drop_{len(episodes) + 1}.png",
                            brain.last_frame)
            episodes.append(episode)
            episode = None


brain = agent.Brain(cfg, p, inp, log)


def timed(name, fn):
    def f(*a, **k):
        t = time.perf_counter()
        try:
            return fn(*a, **k)
        finally:
            ms[name].append((time.perf_counter() - t) * 1000)
    return f


p.grab = timed("grab", p.grab)
p.mobs = timed("mobs", p.mobs)
p.hp = timed("hp", p.hp)
p.damage_numbers = timed("damage", p.damage_numbers)
p.enemy_bar = timed("bar", p.enemy_bar)

_confirm = p.confirm


def confirm(rect, inp_, m, pre=None):
    t = time.perf_counter()
    ok, blue = _confirm(rect, inp_, m, pre=pre)
    confirms.append(((time.perf_counter() - t) * 1000, ok, blue,
                     m["cx"], m["cy"]))
    return ok, blue


p.confirm = confirm

for name in ("click", "key", "right_down", "right_up"):
    orig = getattr(inp, name)

    def make(name, orig):
        def f(*a, **k):
            out = orig(*a, **k)
            actions.append((time.time() - T0, name, a))
            return out
        return f
    setattr(inp, name, make(name, orig))

_tick = brain.tick


def tick(rect):
    t = time.perf_counter()
    try:
        _tick(rect)
    finally:
        ms["tick"].append((time.perf_counter() - t) * 1000)
        states[brain.state] += 1
        if episode is not None and brain.target is not None:
            episode["fp"].append(brain.fight_px)
            episode["bar"].append(None if brain.enemy_hp is None
                                  else round(brain.enemy_hp, 2))
            age = time.time() - T0 - episode["t0"]
            if age > 1.2 and not episode.get("saved") \
                    and brain.last_frame is not None:
                episode["saved"] = True
                cv2.imwrite(f"diag_fight_{len(episodes) + 1}.png",
                            brain.last_frame)


brain.tick = tick

rect = agent.client_rect()
if not rect:
    print("game window not found")
    sys.exit(1)

print(f"game {rect} | mode {cfg['mode']} | hover {cfg['hover_confirm_s']}s "
      f"| run {DUR:.0f}s | admin {agent.is_admin()} | focused {agent.focused()}")

n = 0
try:
    end = time.time() + DUR
    while time.time() < end:
        brain.tick(rect)
        time.sleep(cfg["scan_gap_s"])
        n += 1
        if n % 100 == 0:
            st = brain.status()
            tg = st["target"]
            print(f"  [{time.time() - T0:6.1f}] t#{n} {st['state']:10s} "
                  f"tgt={None if not tg else (tg['cx'], tg['click_y'])} "
                  f"fight={int(st['in_fight'])} px={st['fight_px']} "
                  f"hp={st['hp']}", flush=True)
finally:
    inp.right_up()

total = time.time() - T0


def q(a, pct=0.95):
    b = sorted(a)
    return b[min(len(b) - 1, int(len(b) * pct))]


def line(name, a):
    if not a:
        print(f"  {name:11s} -")
    else:
        print(f"  {name:11s} n={len(a):5d}  avg {sum(a) / len(a):6.1f} ms  "
              f"p50 {q(a, 0.5):6.1f}  p95 {q(a):6.1f}  max {max(a):6.1f}")


print("\n--- timing")
for k in ("tick", "grab", "mobs", "damage", "bar", "hp"):
    line(k, ms[k])
print(f"  loop rate   {n / total:.1f} ticks/s over {total:.1f}s")

print("\n--- hover")
ok_c = [c for c in confirms if c[1]]
bad_c = [c for c in confirms if not c[1]]
if ok_c:
    d = [c[0] for c in ok_c]
    print(f"  confirmed {len(ok_c)}  avg {sum(d) / len(d):.0f} ms  "
          f"min {min(d):.0f}  max {max(d):.0f}  "
          f"blue {min(c[2] for c in ok_c)}-{max(c[2] for c in ok_c)}")
print(f"  rejected  {len(bad_c)}")

print("\n--- episodes")
for i, e in enumerate(episodes, 1):
    fp = e["fp"]
    bars = [b for b in e["bar"] if b is not None]
    print(f"  {i}. dur {e['dur']:.1f}s  maxfightpx {max(fp) if fp else 0}  "
          f"barhr {min(bars) if bars else '-'}  n={len(fp)}  {e['drop']}")

print("\n--- clicks / keys")
for t, nm, a in actions:
    if nm == "right_up":
        continue
    print(f"  {t:6.2f}s {nm}{a[:2]}")

print("\n--- states")
for k, v in states.most_common():
    print(f"  {k:12s} {v:5d} ticks  {100 * v / max(1, len(ms['tick'])):.0f}%")

print("\n--- log")
for t, s in logs:
    print(f"  {t:6.2f}s {s}")
