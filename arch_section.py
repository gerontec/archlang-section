#!/usr/bin/env python3
"""arch_section.py - rudimentary building section (Schnitt) for ArchLang plans.

ArchLang 1.55 draws floor plans only ("heights draw nothing"). This tool reads
what ArchLang itself exports (Plan JSON per level + `describe --json`) and draws
a vertical section along an axis-aligned cut line. The npm package is untouched.

Extra data lives in COMMENT directives inside the .arch file, so the ArchLang
compiler still accepts the file unchanged:

  # @section A x=4300 look east      cut along x = 4300 mm, view towards east
  # @section B y=6000 look north     cut along y = 6000 mm, view towards north
  # @floor 0 Parkett:15 Estrich:60 Daemmung:40 Stahlbeton:180 Putz:15
        floor build-up ON level 0, top (finished floor) to bottom, mm.
        The lowest level's @floor is the ground slab, @floor <top+1> is the
        ceiling above the top storey. Missing -> 250 mm "assumed" (red, TODO).
        Names may be quoted: "Holzbalken mit Fuellung":220
  # @ground -200                     terrain line, mm relative to the datum
  # @datum 0                         level whose finished floor is +-0.00 (default 0)
  # @stairwell closed                default open: slab above a cut stair is left out

Usage:
  arch_section.py plan.arch [--section A] [-o outdir] [--scale 50] [--no-png] [--no-dxf]

Requires: node + `npm install` in this directory (ArchLang), Python 3 with ezdxf (DXF) and cairosvg (PNG).

Limits (rudimentary): only rooms + walls derived by `walls from rooms`, openings
cut in walls crossing the line, straight stairs; nothing beyond the cut plane
is projected; roofs are not modelled.
"""
import argparse
import json
import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape

# directory whose node_modules holds @chanmeng666/archlang (npm install there); override with $ARCHLANG_DIR
ARCHLANG_DIR = os.environ.get("ARCHLANG_DIR", os.path.dirname(os.path.realpath(__file__)))
RISER_MAX = 180          # mm, for the drawn step count
STAIR_WAIST = 160        # mm, flight slab thickness
TREAD_GOING_MM = 280     # ArchLang's drawn tread going (src/elements/vertical-glyphs.ts)
MIN_TREADS = 2
DEFAULT_SLAB = 250       # mm, used when a level has no @floor
STRUCTURAL = re.compile(r"beton|rohdecke|bodenplatte|holzbalken|balken|decke|ziegel|hourdis|filigran|concrete|slab|timber",
                        re.I)


# ---------------------------------------------------------------- input ---
def run_archlang(args):
    cli = os.environ.get("ARCHLANG_CLI")       # e.g. a local build: .../archlang/dist/cli.js
    cmd = (["node", cli] if cli else ["npx", "--no-install", "archlang"]) + args
    r = subprocess.run(cmd, cwd=ARCHLANG_DIR, capture_output=True, text=True, timeout=120)
    if r.returncode not in (0,):
        sys.exit(f"archlang {' '.join(args)} failed:\n{r.stderr[-2000:]}")
    return r.stdout


def load_plan(arch):
    describe = json.loads(run_archlang(["describe", arch, "--json"]))
    levels = describe.get("levels") or [dict(describe, level=0, name=describe.get("plan", ""))]
    tmp = tempfile.mkdtemp(prefix="arch_section_")
    run_archlang(["compile", arch, "-f", "json", "-o", os.path.join(tmp, "plan.json")])
    out = []
    for lv in levels:
        n = lv.get("level", 0)
        f = os.path.join(tmp, f"plan.L{n}.json") if describe.get("levels") else os.path.join(tmp, "plan.json")
        pj = json.load(open(f))
        h = lv.get("heights") or {}
        out.append({
            "n": n,
            "name": lv.get("name") or f"Ebene {n}",
            "height": h.get("storey_height", pj.get("storey_height", 3000)),
            "elev": h.get("elevation", 0),
            "plan": pj,
            "verticals": lv.get("verticals", []),
        })
    out.sort(key=lambda l: l["n"])
    if not describe.get("heights") and not any(l["elev"] for l in out):   # no height clause anywhere
        z = 0
        for l in out:
            l["elev"] = z
            z += l["height"]
    return out


def parse_directives(arch):
    d = {"sections": {}, "floors": {}, "ground": None, "datum": 0, "stairwell": "open"}
    for no, line in enumerate(open(arch, encoding="utf-8"), 1):
        m = re.match(r"\s*#\s*@(\w+)\s*(.*)$", line)
        if not m:
            continue
        key, rest = m.group(1).lower(), m.group(2).split(" #")[0].strip()
        try:
            if key == "section":
                sm = re.match(r"(\S+)\s+([xy])\s*=\s*(-?[\d.]+)\s+look\s+(north|south|east|west)$", rest, re.I)
                if not sm:
                    raise ValueError("expected: @section <name> x=<mm>|y=<mm> look north|south|east|west")
                name, axis, pos, look = sm.group(1), sm.group(2).lower(), float(sm.group(3)), sm.group(4).lower()
                if (axis == "x") != (look in ("east", "west")):
                    raise ValueError("x=... needs look east|west, y=... needs look north|south")
                d["sections"][name] = (axis, pos, look)
            elif key == "floor":
                tok = shlex.split(rest)
                lvl, layers = int(tok[0]), []
                for t in tok[1:]:
                    nm, mm = t.rsplit(":", 1)
                    layers.append((nm.replace("_", " "), float(mm)))
                d["floors"][lvl] = layers
            elif key == "ground":
                d["ground"] = float(rest)
            elif key == "datum":
                d["datum"] = int(rest)
            elif key == "stairwell":
                d["stairwell"] = rest.lower()
        except (ValueError, IndexError) as e:
            sys.exit(f"{arch}:{no}: bad @{key}: {e}")
    return d


# ------------------------------------------------------------- geometry ---
def poly_intervals(poly, axis, pos):
    """Intervals of a rectilinear/simple polygon on the line axis=pos (axis 'x': x=pos -> y-intervals)."""
    a, b = ("x", "y") if axis == "x" else ("y", "x")
    hits = []
    n = len(poly)
    for i in range(n):
        p, q = poly[i], poly[(i + 1) % n]
        if (p[a] <= pos < q[a]) or (q[a] <= pos < p[a]):
            t = (pos - p[a]) / (q[a] - p[a])
            hits.append(p[b] + t * (q[b] - p[b]))
    hits.sort()
    return [(hits[i], hits[i + 1]) for i in range(0, len(hits) - 1, 2)]


def stair_pieces(v):
    """Flights and landings of a stair in climbing order, each with the climbing
    axis ('x'/'y' = axis the flight runs along, None = landing), low/high end
    coordinate and the storey-height fractions f0..f1 it covers. Rise is shared
    by the flights in proportion to their run; landings stay flat."""
    flights, landings = v.get("flights"), v.get("landings") or []
    if not flights:                                   # straight stair: entry at the larger coordinate
        bb = v["bbox"]
        flights = [{"bbox": bb, "entry": "right" if bb["w"] >= bb["h"] else "bottom"}]
    pieces = []
    for f in flights:
        bb = f["bbox"]
        x0, y0, x1, y1 = bb["x"], bb["y"], bb["x"] + bb["w"], bb["y"] + bb["h"]
        low, high, ax = {"left": (x0, x1, "x"), "right": (x1, x0, "x"),
                         "top": (y0, y1, "y"), "bottom": (y1, y0, "y")}[f.get("entry", "right")]
        pieces.append({"bbox": bb, "axis": ax, "low": low, "high": high, "run": abs(high - low)})
    # landings sit between flights; order them by touching the end of a flight
    ordered = []
    for i, p in enumerate(pieces):
        ordered.append(p)
        if i + 1 < len(pieces):
            for lg in landings:
                b = lg["bbox"]
                lx0, ly0, lx1, ly1 = b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]
                hx, hy = (p["high"], None) if p["axis"] == "x" else (None, p["high"])
                if (hx is not None and (abs(hx - lx0) < 1 or abs(hx - lx1) < 1)) or \
                   (hy is not None and (abs(hy - ly0) < 1 or abs(hy - ly1) < 1)):
                    if lg.get("winders"):
                        ordered.append({"bbox": b, "axis": None, "winders": lg["winders"],
                                        "run": 0, "before": p})
                    else:
                        ordered.append({"bbox": b, "axis": None, "run": 0})
                    landings = [x for x in landings if x is not lg]
                    break
    # Equal risers over the whole stair: share the rise by step count, a flight's steps as
    # ArchLang draws its treads (round(run / TREAD_GOING_MM), at least MIN_TREADS), a winder
    # landing's steps as authored, a level landing none.
    for p in ordered:
        if p["axis"] is not None:
            p["steps"] = max(MIN_TREADS, round(p["run"] / TREAD_GOING_MM))
        else:
            p["steps"] = p.get("winders", 0)
    if len(ordered) == 1:          # straight stair: risers come from the storey height
        ordered[0]["steps"] = None
    # Each going is entered by a riser, and the head of the last piece adds the arrival riser
    # onto the floor above: n goings, n + 1 risers.
    if ordered[0]["steps"] is None:
        for p in ordered:
            p["f0"], p["f1"], p["head"] = 0.0, 1.0, True
        return ordered
    risers = sum(p["steps"] for p in ordered) + 1
    acc = 0
    for i, p in enumerate(ordered):
        p["head"] = i == len(ordered) - 1
        p["f0"] = acc / risers
        acc += p["steps"] + (1 if p["head"] else 0)
        p["f1"] = min(1.0, acc / risers)
    return ordered


def stair_profile(ul, uh, za, zb, steps, head):
    """Step line of one stair piece along the cut: a riser before every going and, at the head
    of the last piece, the arrival riser onto the floor.  steps None (straight stair): risers
    from the rise.  Returns (points, risers, rise, going)."""
    risers = max(1, math.ceil((zb - za) / RISER_MAX)) if steps is None else steps + (1 if head else 0)
    goings = risers - (1 if head else 0) if steps is None else steps
    goings = max(1, goings)
    run, rise = (uh - ul) / goings, (zb - za) / risers
    pts, z = [(ul, za)], za
    for i in range(goings):
        z += rise
        pts += [(ul + i * run, z), (ul + (i + 1) * run, z)]
    if risers > goings:
        pts.append((uh, zb))
    return pts, risers, rise, run


def winder_ends(p, pieces, axis):
    """Low and high end of a winder landing along a cut line.  The cut 'y=pos' runs along x.
    Along the entry flight's axis the winders rise away from the entry side; along the exit
    flight's axis they rise toward the exit side."""
    b = p["bbox"]
    x0, y0, x1, y1 = b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]
    before, after = p["before"], pieces[[q is p for q in pieces].index(True) + 1]
    run_axis = "x" if axis == "y" else "y"            # coordinate the cut runs along
    lo_c, hi_c = (x0, x1) if run_axis == "x" else (y0, y1)
    if before["axis"] == run_axis:                    # entered across a side normal to the cut
        low = before["high"]
        return low, (hi_c if abs(low - lo_c) < 1 else lo_c)
    high = after["low"]
    return (hi_c if abs(high - lo_c) < 1 else lo_c), high


def level_cut(level, axis, pos):
    """Rooms, walls and openings of one level on the cut line, in plan coordinate c along the line."""
    pj = level["plan"]
    rooms = []
    for r in pj["rooms"]:
        for c0, c1 in poly_intervals(r["floor_polygon"], axis, pos):
            rooms.append({"id": r["id"], "label": r.get("label", r["id"]), "c0": c0, "c1": c1})
    wf = pj.get("walls_from_rooms") or {}
    ext_t, part_t = wf.get("exterior", 250), wf.get("partition", 120)
    pair_t = {frozenset(x["between"]): x["thickness"] for x in wf.get("between", [])}
    edges = sorted({round(v, 1) for r in rooms for v in (r["c0"], r["c1"])})
    walls = []
    for e in edges:
        left = [r["id"] for r in rooms if abs(r["c1"] - e) < 1]
        right = [r["id"] for r in rooms if abs(r["c0"] - e) < 1]
        if not left or not right:
            t, kind = ext_t, "exterior"
        else:
            t, kind = part_t, "partition"
            for a in left:
                for b in right:
                    if frozenset((a, b)) in pair_t:
                        t = pair_t[frozenset((a, b))]
        ops = []
        for o in pj.get("openings", []):
            oc, oa = (o["y"], o["x"]) if axis == "x" else (o["x"], o["y"])
            if abs(oc - e) < 1 and abs(oa - pos) <= o["width"] / 2:
                ops.append(o)
        walls.append({"c": e, "t": t, "kind": kind, "openings": ops})
    stairs = []
    for v in level["verticals"]:
        if v.get("kind") != "stair" or "bbox" not in v:
            continue
        pieces_all = stair_pieces(v)
        for p in pieces_all:
            bb = p["bbox"]
            x0, y0, x1, y1 = bb["x"], bb["y"], bb["x"] + bb["w"], bb["y"] + bb["h"]
            if axis == "x" and not (x0 < pos < x1) or axis == "y" and not (y0 < pos < y1):
                continue
            a0, a1 = (y0, y1) if axis == "x" else (x0, x1)      # piece extent along the cut
            if p["axis"] is None and p.get("winders"):          # winders: rise across the cut
                lo, hi = winder_ends(p, pieces_all, axis)
                stairs.append({"mode": "along", "c_low": lo, "c_high": hi, "steps": p["winders"],
                               "head": p["head"], "f0": p["f0"], "f1": p["f1"], "dir": v.get("dir", "up")})
            elif p["axis"] is None:                             # landing: flat
                stairs.append({"mode": "flat", "c0": a0, "c1": a1, "f": p["f0"], "dir": v.get("dir", "up")})
            elif p["axis"] != axis:                              # cut runs along the flight
                stairs.append({"mode": "along", "c_low": p["low"], "c_high": p["high"], "steps": p["steps"],
                               "head": p["head"], "f0": p["f0"], "f1": p["f1"], "dir": v.get("dir", "up")})
            else:                                                # cut crosses the flight
                t = (pos - p["low"]) / (p["high"] - p["low"])
                stairs.append({"mode": "across", "c0": a0, "c1": a1,
                               "f": p["f0"] + t * (p["f1"] - p["f0"]), "dir": v.get("dir", "up")})
    return {"rooms": rooms, "walls": walls, "stairs": stairs}


# ---------------------------------------------------------------- model ---
def fmt_m(mm):
    v = mm / 1000.0
    if abs(v) < 0.0005:
        return "±0,00"
    return f"{v:+.2f}".replace(".", ",")


def fmt_len(mm):
    return f"{mm / 1000:.2f}".replace(".", ",")


def build_section(levels, d, name):
    axis, pos, look = d["sections"][name]
    # u runs left->right on the sheet: look east -> north (small y) left, look north -> west (small x) left
    sign = 1 if look in ("east", "north") else -1
    datum = next((l["elev"] for l in levels if l["n"] == d["datum"]), levels[0]["elev"])
    top = levels[-1]
    cuts = {l["n"]: level_cut(l, axis, pos) for l in levels}
    if not any(cuts[n]["rooms"] for n in cuts):
        sys.exit(f"section {name}: line {axis}={pos:g} crosses no room")

    def U(c):
        return sign * c

    # floor slabs: one per level + ceiling above the top storey
    slab_levels = [l["n"] for l in levels] + [top["n"] + 1]
    elev = {l["n"]: l["elev"] - datum for l in levels}
    elev[top["n"] + 1] = top["elev"] + top["height"] - datum
    floors = {}
    for n in slab_levels:
        layers = d["floors"].get(n)
        floors[n] = {"layers": layers or [("Decke (Annahme)", DEFAULT_SLAB)], "assumed": not layers}
    tot = {n: sum(t for _, t in floors[n]["layers"]) for n in slab_levels}

    def extent(n):
        """Outer faces of the walls of level n on the cut, as (u_min, u_max)."""
        c = cuts.get(n)
        if not c or not c["walls"]:
            return None
        return (min(U(w["c"]) - w["t"] / 2 for w in c["walls"]), max(U(w["c"]) + w["t"] / 2 for w in c["walls"]))

    els = {"walls": [], "slabs": [], "glass": [], "stairs": [], "texts": [], "dims": [], "marks": []}
    stair_voids = {}   # slab level -> list of (u0,u1) left open
    for l in levels:
        n = l["n"]
        z0 = elev[n]                       # finished floor
        z1 = elev[n + 1] - tot[n + 1]      # underside of the slab above
        c = cuts[n]
        for w in c["walls"]:
            u, t = U(w["c"]), w["t"]
            segs = [(z0, z1)]
            for o in w["openings"]:
                sill = o.get("sill", 0) if o["kind"] == "window" else 0
                head = min(o.get("head", 2100), z1 - z0)
                new = []
                for a, b in segs:
                    oa, ob = z0 + sill, z0 + head
                    if ob <= a or oa >= b:
                        new.append((a, b))
                        continue
                    if oa > a:
                        new.append((a, oa))
                    if ob < b:
                        new.append((ob, b))
                segs = new
                if o["kind"] == "window":
                    els["glass"].append((u, t, z0 + sill, z0 + head))
            for a, b in segs:
                els["walls"].append((u - t / 2, a, u + t / 2, b, w["kind"]))
        taken = [tuple(sorted((U(st["c_low"]), U(st["c_high"])) if st["mode"] == "along"
                              else (U(st["c0"]), U(st["c1"])))) for st in c["stairs"]]
        for r in c["rooms"]:
            ua, ub = sorted((U(r["c0"]), U(r["c1"])))
            # label in the widest part of the room no stair of this level runs through
            free = [(ua, ub)]
            for ta, tb in taken:
                free = [seg for a, b in free for seg in ((a, min(b, ta)), (max(a, tb), b)) if seg[1] - seg[0] > 1]
            fa, fb = max(free, key=lambda f: f[1] - f[0]) if free else (ua, ub)
            uc = (fa + fb) / 2 if fb - fa >= 800 else (ua + ub) / 2
            # upper part of the room; lower down where a stair climbs to the ceiling
            stair_here = any(ta < ub and tb > ua for ta, tb in taken)
            zt = z0 + (0.5 if stair_here else 0.78) * (z1 - z0)
            els["texts"].append((uc, zt + 120, r["label"], "room"))
            els["texts"].append((uc, zt - 180, f"LH {fmt_len(z1 - z0)}", "small"))
        # one clear-height dimension per level, in the widest room
        if c["rooms"]:
            def clear(r):
                ua, ub = sorted((U(r["c0"]), U(r["c1"])))
                return not any(ta < ub and tb > ua for ta, tb in taken)
            r = max(c["rooms"], key=lambda r: (clear(r), r["c1"] - r["c0"]))
            ua, ub = sorted((U(r["c0"]), U(r["c1"])))
            els["dims"].append(("v", ua + 0.2 * (ub - ua), z0, z1))
        for s in c["stairs"]:
            H = l["height"]
            zl = z0 if s["dir"] == "up" else z0 - next((x["height"] for x in levels if x["n"] == n - 1), H)
            Hs = H if s["dir"] == "up" else z0 - zl
            vl = n + 1 if s["dir"] == "up" else n
            if s["mode"] == "along":
                ul, uh = U(s["c_low"]), U(s["c_high"])
                els["stairs"].append(("along", ul, uh, zl + s["f0"] * Hs, zl + s["f1"] * Hs, s.get("steps"), s.get("head", True)))
                stair_voids.setdefault(vl, []).append(tuple(sorted((ul, uh))))
            else:
                ua, ub = sorted((U(s["c0"]), U(s["c1"])))
                label = "Podest/Wendelung" if s["mode"] == "flat" else "Treppe (quer)"
                els["stairs"].append(("across", ua, ub, zl + s["f"] * Hs, label))
                stair_voids.setdefault(vl, []).append((ua, ub))

    # slabs
    for n in slab_levels:
        ex = [e for e in (extent(n), extent(n - 1)) if e]
        if not ex:
            continue
        ua, ub = min(e[0] for e in ex), max(e[1] for e in ex)
        spans = [(ua, ub)]
        if d["stairwell"] != "closed":
            for va, vb in stair_voids.get(n, []):
                new = []
                for a, b in spans:
                    if vb <= a or va >= b:
                        new.append((a, b))
                        continue
                    if va > a:
                        new.append((a, va))
                    if vb < b:
                        new.append((vb, b))
                spans = new
        ztop = elev[n]
        z = ztop
        for lname, t in floors[n]["layers"]:
            for a, b in spans:
                els["slabs"].append((a, z - t, b, z, lname, floors[n]["assumed"]))
            z -= t
        lvl = next((l for l in levels if l["n"] == n), None)
        label = lvl["name"] if lvl else "Decke oben"
        rfb = ztop
        for lname, t in floors[n]["layers"]:
            if STRUCTURAL.search(lname):
                break
            rfb -= t
        els["marks"].append((ztop, f"OK FFB {label}" if lvl else "OK Decke", fmt_m(ztop), ub))
        if not floors[n]["assumed"] and rfb != ztop:
            els["marks"].append((rfb, f"OK RFB {label}" if lvl else "OK Rohdecke", fmt_m(rfb), ub))
        if floors[n]["assumed"]:
            lay_txt = f"Aufbau unbekannt: Annahme {tot[n]:g} mm (TODO @floor {n})"
        else:
            lay_txt = " / ".join(f"{nm} {t:g}" for nm, t in floors[n]["layers"]) + f" = {tot[n]:g} mm"
        els["texts"].append((ua - 400, ztop - tot[n] / 2 - 60, lay_txt,
                             "layers_todo" if floors[n]["assumed"] else "layers"))
    allx = [e[0] for e in els["walls"]] + [e[2] for e in els["walls"]] + [s[0] for s in els["slabs"]] + [s[2] for s in els["slabs"]]
    allz = [e[1] for e in els["walls"]] + [e[3] for e in els["walls"]] + [s[1] for s in els["slabs"]] + [s[3] for s in els["slabs"]]
    bb = (min(allx), min(allz), max(allx), max(allz))
    els["dims"].append(("h", bb[0], bb[2], bb[1] - 700))
    look_de = {"north": "Norden", "south": "Süden", "east": "Osten", "west": "Westen"}[look]
    title = f"Schnitt {name}-{name}  ({axis} = {fmt_len(pos)} m, Blick nach {look_de})"
    return {"els": els, "bb": bb, "ground": d["ground"], "title": title, "floors": floors}


# ----------------------------------------------------------------- SVG ---
PATTERNS = """
<pattern id="concrete" patternUnits="userSpaceOnUse" width="120" height="120" patternTransform="rotate(45)">
  <rect width="120" height="120" fill="#c8c8c8"/><line x1="0" y1="0" x2="0" y2="120" stroke="#555" stroke-width="10"/></pattern>
<pattern id="screed" patternUnits="userSpaceOnUse" width="80" height="80">
  <rect width="80" height="80" fill="#e8e8e8"/><circle cx="40" cy="40" r="8" fill="#777"/></pattern>
<pattern id="insulation" patternUnits="userSpaceOnUse" width="120" height="60">
  <rect width="120" height="60" fill="#fff6d0"/><path d="M0 60 L30 0 L60 60 L90 0 L120 60" fill="none" stroke="#a08020" stroke-width="6"/></pattern>
<pattern id="wood" patternUnits="userSpaceOnUse" width="200" height="40">
  <rect width="200" height="40" fill="#e9cf9f"/><line x1="0" y1="20" x2="200" y2="20" stroke="#8a6a30" stroke-width="5"/></pattern>
<pattern id="plaster" patternUnits="userSpaceOnUse" width="50" height="50"><rect width="50" height="50" fill="#f7f7f7"/></pattern>
<pattern id="todo" patternUnits="userSpaceOnUse" width="150" height="150" patternTransform="rotate(45)">
  <rect width="150" height="150" fill="#ffe0e0"/><line x1="0" y1="0" x2="0" y2="150" stroke="#e05050" stroke-width="8"/></pattern>
"""


def layer_fill(name, assumed):
    if assumed:
        return "url(#todo)"
    n = name.lower()
    for key, pat in (("dämm", "insulation"), ("daemm", "insulation"), ("trittschall", "insulation"), ("insul", "insulation"),
                     ("estrich", "screed"), ("screed", "screed"), ("parkett", "wood"), ("dielen", "wood"),
                     ("holz", "wood"), ("balken", "wood"), ("putz", "plaster"), ("fliese", "plaster")):
        if key in n:
            return f"url(#{pat})"
    if STRUCTURAL.search(n):
        return "url(#concrete)"
    return "#dddddd"


def render_svg(sec, scale):
    els, (x0, z0, x1, z1) = sec["els"], sec["bb"]
    pad_l, pad_r, pad_t, pad_b = 9000, 4200, 1600, 2200
    vx, vy = x0 - pad_l, -(z1 + pad_t)
    vw, vh = (x1 - x0) + pad_l + pad_r, (z1 - z0) + pad_t + pad_b
    S = scale / 1.0                     # model mm per paper mm
    txt = 3.0 * S
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{vw / S:.1f}mm" height="{vh / S:.1f}mm" '
           f'viewBox="{vx:.0f} {vy:.0f} {vw:.0f} {vh:.0f}" font-family="DejaVu Sans, sans-serif">',
           f"<defs>{PATTERNS}</defs>", f'<rect x="{vx}" y="{vy}" width="{vw}" height="{vh}" fill="white"/>']
    for a, b, c, d_, lname, assumed in els["slabs"]:
        out.append(f'<rect x="{a:.0f}" y="{-d_:.0f}" width="{c - a:.0f}" height="{d_ - b:.0f}" fill="{layer_fill(lname, assumed)}" '
                   f'stroke="{"#d03030" if assumed else "#000"}" stroke-width="{0.18 * S:.0f}"'
                   f'{" stroke-dasharray=\"%d %d\"" % (2 * S, 1 * S) if assumed else ""}/>')
    for a, b, c, d_, kind in els["walls"]:
        out.append(f'<rect x="{a:.0f}" y="{-d_:.0f}" width="{c - a:.0f}" height="{d_ - b:.0f}" '
                   f'fill="{"#222" if kind == "exterior" else "#555"}" stroke="#000" stroke-width="{0.25 * S:.0f}"/>')
    for u, t, a, b in els["glass"]:
        for off in (-t / 6, t / 6):
            out.append(f'<line x1="{u + off:.0f}" y1="{-a:.0f}" x2="{u + off:.0f}" y2="{-b:.0f}" stroke="#1060c0" stroke-width="{0.18 * S:.0f}"/>')
        out.append(f'<rect x="{u - t / 2:.0f}" y="{-a - 0.4 * S:.0f}" width="{t:.0f}" height="{0.4 * S:.0f}" fill="#1060c0"/>')
    for s in els["stairs"]:
        if s[0] == "along":
            _, ul, uh, za, zb, steps, head = s
            pts, n, rise, run = stair_profile(ul, uh, za, zb, steps, head)
            slope = math.hypot(run, rise)
            off = STAIR_WAIST * slope / abs(run) if run else STAIR_WAIST
            pts += [(uh, zb - off), (ul, za - off)]
            out.append('<polygon points="' + " ".join(f"{p:.0f},{-q:.0f}" for p, q in pts)
                       + f'" fill="#9a9a9a" stroke="#000" stroke-width="{0.25 * S:.0f}"/>')
            # label centred under the waist, dropped by the slope over half the text length
            label = f"{n} Stg. {rise:.0f}/{abs(run):.0f}"
            half = 0.8 * txt * 0.55 * len(label) / 2
            lx = (ul + uh) / 2
            ly = (za + zb) / 2 - off - half * abs(rise / run if run else 0) - 1.0 * txt
            out.append(f'<text x="{lx:.0f}" y="{-ly:.0f}" font-size="{txt * 0.8:.0f}" '
                       f'text-anchor="middle">{label}</text>')
        else:
            _, ua, ub, z, _n = s
            out.append(f'<rect x="{ua:.0f}" y="{-z:.0f}" width="{ub - ua:.0f}" height="{STAIR_WAIST:.0f}" fill="#9a9a9a" '
                       f'stroke="#000" stroke-width="{0.25 * S:.0f}"/>')
            out.append(f'<text x="{(ua + ub) / 2:.0f}" y="{-z + STAIR_WAIST + 1.0 * txt:.0f}" font-size="{txt * 0.7:.0f}" text-anchor="middle">{_n}</text>')
    if sec["ground"] is not None:
        g = sec["ground"]
        out.append(f'<line x1="{x0 - 2500:.0f}" y1="{-g:.0f}" x2="{x1 + 2500:.0f}" y2="{-g:.0f}" stroke="#3a7a20" stroke-width="{0.5 * S:.0f}"/>')
        out.append(f'<text x="{x1 + 2500:.0f}" y="{-g - 0.6 * txt:.0f}" font-size="{txt * 0.8:.0f}" text-anchor="end" fill="#3a7a20">Gelände {fmt_m(g)}</text>')
    for u, z, s, cls in els["texts"]:
        if cls in ("layers", "layers_todo"):
            out.append(f'<text x="{u:.0f}" y="{-z:.0f}" font-size="{txt * 0.7:.0f}" text-anchor="end" '
                       f'fill="{"#c02020" if cls == "layers_todo" else "#000"}">{escape(s)}</text>')
        else:
            out.append(f'<text x="{u:.0f}" y="{-z:.0f}" font-size="{txt * (1 if cls == "room" else 0.8):.0f}" text-anchor="middle">{escape(s)}</text>')
    tri = 1.6 * S
    for z, label, val, ub in els["marks"]:
        mx = x1 + 600
        out.append(f'<path d="M{mx:.0f} {-z:.0f} l{-tri / 2:.0f} {-tri:.0f} h{tri:.0f} z" fill="#000"/>')
        out.append(f'<line x1="{mx - tri:.0f}" y1="{-z:.0f}" x2="{mx + 3200:.0f}" y2="{-z:.0f}" stroke="#000" stroke-width="{0.13 * S:.0f}"/>')
        below = "RFB" in label or "Rohdecke" in label          # raw-floor mark: text under its line
        ty = -z + 1.2 * txt if below else -z - 0.5 * txt
        out.append(f'<text x="{mx + tri:.0f}" y="{ty:.0f}" font-size="{txt * 0.8:.0f}">{val}  {escape(label)}</text>')
    for dm in els["dims"]:
        sw = 0.13 * S
        if dm[0] == "v":
            _, u, a, b = dm
            out.append(f'<line x1="{u:.0f}" y1="{-a:.0f}" x2="{u:.0f}" y2="{-b:.0f}" stroke="#000" stroke-width="{sw:.0f}"/>')
            for z in (a, b):
                out.append(f'<line x1="{u - 1 * S:.0f}" y1="{-z + 1 * S:.0f}" x2="{u + 1 * S:.0f}" y2="{-z - 1 * S:.0f}" stroke="#000" stroke-width="{2 * sw:.0f}"/>')
            out.append(f'<text x="{u - 0.6 * txt:.0f}" y="{-(a + b) / 2:.0f}" font-size="{txt * 0.8:.0f}" text-anchor="middle" '
                       f'transform="rotate(-90 {u - 0.6 * txt:.0f} {-(a + b) / 2:.0f})">{fmt_len(b - a)}</text>')
        else:
            _, a, b, z = dm
            out.append(f'<line x1="{a:.0f}" y1="{-z:.0f}" x2="{b:.0f}" y2="{-z:.0f}" stroke="#000" stroke-width="{sw:.0f}"/>')
            for u in (a, b):
                out.append(f'<line x1="{u - 1 * S:.0f}" y1="{-z + 1 * S:.0f}" x2="{u + 1 * S:.0f}" y2="{-z - 1 * S:.0f}" stroke="#000" stroke-width="{2 * sw:.0f}"/>')
            out.append(f'<text x="{(a + b) / 2:.0f}" y="{-z - 0.5 * txt:.0f}" font-size="{txt * 0.8:.0f}" text-anchor="middle">{fmt_len(b - a)}</text>')
    out.append(f'<text x="{x0:.0f}" y="{vy + 1.6 * txt:.0f}" font-size="{txt * 1.4:.0f}" font-weight="bold">{escape(sec["title"])}</text>')
    out.append(f'<text x="{x0:.0f}" y="{vy + vh - 0.8 * txt:.0f}" font-size="{txt * 0.7:.0f}">M 1:{scale:g} · arch_section.py '
               f'(rudimentär: nur Schnittebene, keine Ansicht dahinter, Dach nicht modelliert)</text>')
    out.append("</svg>")
    return "\n".join(out)


def render_dxf(sec, path):
    import ezdxf
    doc = ezdxf.new("R2010", setup=True)
    doc.units = ezdxf.units.MM
    msp = doc.modelspace()
    for lay, col in (("S_WAND", 7), ("S_DECKE", 8), ("S_DECKE_ANNAHME", 1), ("S_GLAS", 5), ("S_TREPPE", 9), ("S_TEXT", 7), ("S_GELAENDE", 3)):
        doc.layers.add(lay, color=col)

    def box(a, b, c, d_, layer, hatch=None):
        pts = [(a, b), (c, b), (c, d_), (a, d_)]
        msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": layer})
        if hatch:
            h = msp.add_hatch(dxfattribs={"layer": layer})
            h.set_pattern_fill(hatch, scale=10) if hatch != "SOLID" else h.set_solid_fill()
            h.paths.add_polyline_path(pts, is_closed=True)

    els = sec["els"]
    for a, b, c, d_, lname, assumed in els["slabs"]:
        pat = "ANSI31" if STRUCTURAL.search(lname) else ("AR-SAND" if "estrich" in lname.lower() else None)
        box(a, b, c, d_, "S_DECKE_ANNAHME" if assumed else "S_DECKE", pat)
    for a, b, c, d_, _k in els["walls"]:
        box(a, b, c, d_, "S_WAND", "SOLID")
    for u, t, a, b in els["glass"]:
        for off in (-t / 6, t / 6):
            msp.add_line((u + off, a), (u + off, b), dxfattribs={"layer": "S_GLAS"})
    for s in els["stairs"]:
        if s[0] == "along":
            _, ul, uh, za, zb, steps, head = s
            pts = stair_profile(ul, uh, za, zb, steps, head)[0]
            msp.add_lwpolyline(pts, dxfattribs={"layer": "S_TREPPE"})
        else:
            _, ua, ub, z, _n = s
            box(ua, z - STAIR_WAIST, ub, z, "S_TREPPE")
    for u, z, s, cls in els["texts"]:
        msp.add_text(s, height=120 if cls == "room" else 90, dxfattribs={"layer": "S_TEXT"}).set_placement(
            (u, z), align=ezdxf.enums.TextEntityAlignment.MIDDLE_CENTER if cls not in ("layers", "layers_todo")
            else ezdxf.enums.TextEntityAlignment.MIDDLE_RIGHT)
    x1 = sec["bb"][2]
    for z, label, val, _ub in els["marks"]:
        msp.add_line((x1 + 400, z), (x1 + 3800, z), dxfattribs={"layer": "S_TEXT"})
        msp.add_text(f"{val}  {label}", height=90, dxfattribs={"layer": "S_TEXT"}).set_placement((x1 + 700, z + 60))
    if sec["ground"] is not None:
        msp.add_line((sec["bb"][0] - 2500, sec["ground"]), (x1 + 2500, sec["ground"]), dxfattribs={"layer": "S_GELAENDE"})
    msp.add_text(sec["title"], height=200, dxfattribs={"layer": "S_TEXT"}).set_placement((sec["bb"][0], sec["bb"][3] + 900))
    doc.saveas(path)


# ----------------------------------------------------------------- main ---
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("arch")
    ap.add_argument("--section", help="only this @section name")
    ap.add_argument("-o", "--outdir", help="default: next to the .arch file")
    ap.add_argument("--scale", type=float, default=50)
    ap.add_argument("--no-png", action="store_true")
    ap.add_argument("--no-dxf", action="store_true")
    a = ap.parse_args()
    arch = os.path.abspath(a.arch)
    d = parse_directives(arch)
    if not d["sections"]:
        sys.exit("no '# @section <name> x=<mm>|y=<mm> look <dir>' directive in " + arch)
    names = [a.section] if a.section else list(d["sections"])
    for n in names:
        if n not in d["sections"]:
            sys.exit(f"unknown section {n}; known: {', '.join(d['sections'])}")
    levels = load_plan(arch)
    outdir = a.outdir or os.path.dirname(arch)
    stem = os.path.splitext(os.path.basename(arch))[0]
    for n in names:
        sec = build_section(levels, d, n)
        base = os.path.join(outdir, f"{stem}_schnitt_{n}")
        svg = render_svg(sec, a.scale)
        open(base + ".svg", "w", encoding="utf-8").write(svg)
        made = [base + ".svg"]
        if not a.no_png:
            try:
                import cairosvg
                cairosvg.svg2png(bytestring=svg.encode(), write_to=base + ".png", output_width=2400)
                made.append(base + ".png")
            except Exception as e:  # noqa: BLE001 - PNG is optional
                print(f"PNG skipped: {e}", file=sys.stderr)
        if not a.no_dxf:
            render_dxf(sec, base + ".dxf")
            made.append(base + ".dxf")
        todo = [k for k, f in sec["floors"].items() if f["assumed"]]
        print(f"{sec['title']}: " + ", ".join(made))
        if todo:
            print(f"  assumed slab build-up (no @floor) for levels: {', '.join(map(str, todo))}")


if __name__ == "__main__":
    main()
