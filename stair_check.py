#!/usr/bin/env python3
"""stair_check.py - run every quarter-turn stair of an ArchLang plan through the winder
construction of stair_quarter_turn.py before it is drawn.

For each stair with one winder landing the storey height, flight width, the run before
the turn (run 1), the run after it (run 2) and the riser count are taken from the plan
(`describe --json`, the # @stair goings directive, RISER_MAX otherwise).  numpy then
computes every variant from --min-winders to --max-winders tapered steps, with run 2 as
drawn and with run 2 = 0 (the winding ends at the stair eye, the arrival floor is the
last tread), and prints per variant:

  h / a      riser and going on the walking line (b / 2 from the inner stringer)
  2h + a     step formula, comfortable between 590 and 650 mm
  zone       winding zone over its minimum 1.5 * arc (below 1 the inner stringer runs back)
  inner      smallest tapered going at the inner stringer (>= --inner-min wanted)
  outer      largest going at the outer stringer

The authored variant is marked with `*`.  The check is advisory: it never changes the
plan and exits 0 unless --strict is given and the authored stair fails a test.

Usage:
  stair_check.py plan.arch [--min-winders 3] [--max-winders 6] [--inner-min 100] [--strict]
"""
import argparse
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import arch_section as AS  # noqa: E402

STEP_LO, STEP_HI = 590.0, 650.0      # 2h + a, mm


def construct(H, n, b, run1, run2, straight, top_straight=0, w=None):
    """Edges of a quarter-turn stair as stair_quarter_turn.construct(), vectorised.
    Returns a dict with h, a, zone ratio, inner goings and outer goings (mm)."""
    w = b / 2 if w is None else w
    arc = math.pi * w / 2
    L = run1 + arc + run2
    h, a = H / n, L / (n - 1)
    sigma = np.arange(n) * a
    s0, s1 = straight * a, L - top_straight * a
    zone = (s1 - s0) / (1.5 * arc)
    u = np.clip((sigma - s0) / max(s1 - s0, 1e-9), 0, 1)
    tau = sigma - arc * (3 * u**2 - 2 * u**3)
    inner = np.diff(tau)
    # walking-line point P and inner-stringer point Q of every edge
    phi = np.clip((sigma - run1) / w, 0, math.pi / 2)
    on_arc = (sigma > run1) & (sigma < run1 + arc)
    P = np.where(sigma <= run1, np.vstack([sigma - run1, np.full(n, w)]),
                 np.where(on_arc, np.vstack([w * np.sin(phi), w * np.cos(phi)]),
                          np.vstack([np.full(n, w), -(sigma - run1 - arc)])))
    Q = np.where(tau <= run1, np.vstack([tau - run1, np.zeros(n)]), np.vstack([np.zeros(n), -(tau - run1)]))
    d = P - Q
    with np.errstate(divide="ignore", invalid="ignore"):
        tN = np.where(d[1] > 1e-9, (b - Q[1]) / d[1], np.inf)          # outer stringer of run 1: y = b
        tN = np.where(Q[0] + tN * d[0] <= b + 1e-6, tN, np.inf)
        tE = np.where(d[0] > 1e-9, (b - Q[0]) / d[0], np.inf)          # outer stringer of run 2: x = b
        tE = np.where(Q[1] + tE * d[1] <= b + 1e-6, tE, np.inf)
    O = Q + np.minimum(tN, tE) * d
    station = np.where(O[1] >= b - 1e-6, O[0] + run1, run1 + b + (b - O[1]))
    outer = np.diff(station)
    return {"h": h, "a": a, "zone": zone, "inner": inner, "outer": outer}


def quarter_turns(level, directives):
    """The stairs of a level with exactly one winder landing, as (id, H, b, run1, run2,
    straight goings, winders, top goings, risers)."""
    out = []
    for v in level["verticals"]:
        if v.get("kind") != "stair" or "bbox" not in v:
            continue
        pieces = AS.stair_pieces(v)
        turns = [i for i, p in enumerate(pieces) if p["axis"] is None and p.get("winders")]
        if len(turns) != 1:
            continue
        i = turns[0]
        before = [p for p in pieces[:i] if p["axis"] is not None]
        after = [p for p in pieces[i + 1:] if p["axis"] is not None]
        if len(before) != 1 or len(after) > 1 or any(p["axis"] is None for p in pieces[:i] + pieces[i + 1:]):
            print(f"stair {v.get('id')}: more than one flight or landing on a side of the turn, not checked",
                  file=sys.stderr)
            continue
        goings = directives["stair_goings"].get(v.get("id"))
        winders = pieces[i]["winders"]
        if goings:
            straight, top = goings[0], (goings[1] if len(goings) > 1 else 0)
            risers = straight + winders + top + 1
        else:
            risers = max(2, math.ceil(level["height"] / AS.RISER_MAX))
            # share the free goings like arch_section.stair_treads: by run
            free = max(len(before) + len(after), risers - 1 - winders)
            runs = [before[0]["run"]] + [p["run"] for p in after]
            straight = max(1, round(free * runs[0] / sum(runs)))
            top = free - straight
            risers = straight + winders + top + 1
        out.append({"id": v.get("id"), "H": level["height"], "b": v.get("flight_width") or v.get("width") or v["bbox"]["w"],
                    "run1": before[0]["run"], "run2": after[0]["run"] if after else 0.0,
                    "straight": straight, "winders": winders, "top": top, "risers": risers,
                    "authored": bool(goings)})
    return out


def verdict(c, inner_min):
    notes = []
    if c["zone"] < 1:
        notes.append("zone too short")
    if c["inner"].min() < inner_min:
        notes.append(f"inner < {inner_min:.0f}")
    if not STEP_LO <= 2 * c["h"] + c["a"] <= STEP_HI:
        notes.append(f"2h+a outside {STEP_LO:.0f}-{STEP_HI:.0f}")
    return notes


def check_stair(s, lo, hi, inner_min):
    """Print the variant table of one stair; return the notes of the authored variant."""
    n, b = s["risers"], s["b"]
    src = "@stair goings" if s["authored"] else f"RISER_MAX {AS.RISER_MAX}"
    print(f"stair {s['id']}: H {s['H']:.0f}, width {b:.0f}, run1 {s['run1']:.0f}, run2 {s['run2']:.0f}, "
          f"{n} risers ({src}); authored {s['straight']} straight + {s['winders']} winders + {s['top']} top")
    print("   run2  straight  winders  top    h    a  2h+a  zone  inner  outer  notes")
    authored_notes = None
    for run2 in sorted({s["run2"], 0.0}, reverse=True):
        for m in range(lo, hi + 1):
            variants = [(n - 1 - m, m, 0)]
            if run2 == s["run2"] and m == s["winders"] and s["top"]:
                variants.append((s["straight"], m, s["top"]))     # the authored split with its top going(s)
            for straight, wind, top in variants:
                if straight < 1:
                    continue
                c = construct(s["H"], n, b, s["run1"], run2, straight, top)
                notes = verdict(c, inner_min)
                mark = "*" if (run2 == s["run2"] and straight == s["straight"] and wind == s["winders"]
                               and top == s["top"]) else " "
                if mark == "*":
                    authored_notes = notes
                print(f" {mark} {run2:4.0f}  {straight:8d}  {wind:7d}  {top:3d}  {c['h']:3.0f}  {c['a']:3.0f}  "
                      f"{2 * c['h'] + c['a']:4.0f}  {c['zone']:4.2f}  {c['inner'].min():5.0f}  {c['outer'].max():5.0f}  "
                      f"{'; '.join(notes) or 'ok'}")
    if authored_notes is None:
        print(f"   authored variant has {s['winders']} winders, outside {lo}-{hi}: not marked")
        c = construct(s["H"], n, b, s["run1"], s["run2"], s["straight"], s["top"])
        authored_notes = verdict(c, inner_min)
    return authored_notes


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("arch")
    ap.add_argument("--min-winders", type=int, default=3)
    ap.add_argument("--max-winders", type=int, default=6)
    ap.add_argument("--inner-min", type=float, default=100, help="least tapered going at the inner stringer, mm")
    ap.add_argument("--strict", action="store_true", help="exit 1 when an authored stair fails a test")
    a = ap.parse_args()
    levels = AS.load_plan(a.arch)
    d = AS.parse_directives(a.arch)
    failed, seen = 0, 0
    for level in levels:
        for s in quarter_turns(level, d):
            seen += 1
            notes = check_stair(s, a.min_winders, a.max_winders, a.inner_min)
            if notes:
                failed += 1
                sys.stdout.flush()
                print(f"   -> authored stair {s['id']} on level {level['n']}: {'; '.join(notes)}", file=sys.stderr)
    if not seen:
        print("no quarter-turn stair with winders in the plan")
    sys.exit(1 if a.strict and failed else 0)


if __name__ == "__main__":
    main()
