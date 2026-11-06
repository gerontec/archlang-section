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
  # @stair tr goings 9 1             goings per flight (default: shared by run); winders as planned

Usage:
  arch_section.py plan.arch [--section A] [-o outdir] [--scale 50] [--no-png] [--no-dxf]
                  [--footer TEXT | --no-footer]

Every sheet gets a footer line with the command that made it (default: this
invocation, all paths absolute; `--footer` replaces the command, e.g. when a
wrapper such as arch_export.py runs this script).

Requires: node + `npm install` in this directory (ArchLang), Python 3 with ezdxf (DXF) and cairosvg (PNG).

Limits (rudimentary): only rooms + walls derived by `walls from rooms`, openings
cut in walls crossing the line, stairs (flights, landings, winders) cut and in view;
nothing else beyond the cut plane is projected; roofs are not modelled.
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
    d = {"sections": {}, "floors": {}, "ground": None, "datum": 0, "stairwell": "open", "stair_goings": {}}
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
            elif key == "stair":                      # @stair <id> goings <n> [<n> ...]
                tok = rest.split()
                if len(tok) < 3 or tok[1] != "goings":
                    raise ValueError("expected: @stair <id> goings <n> [<n> ...] (one count per flight)")
                d["stair_goings"][tok[0]] = [int(x) for x in tok[2:]]
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


# ------------------------------------------------------------ stair treads ---
# The stair is constructed the way a draftsman does it in 2D: every riser the same
# height, every tread an area in plan (rectangles in a flight, the winder fan on a
# quarter-turn landing), and the section and the view derived from those areas.
#
# Winders fan from the landing's inner corner C at equal angles theta_k = k * 90 deg / n
# (as ArchLang draws them). A tread edge meets the far side at W * tan(theta_k) from the
# corner on the entry side and at W * cot(theta_k) on the exit side, so the goings along
# any line through the landing change by that function, and the soffit (constant height
# along each ray from C) is a helical ramp.

def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def _lerp(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def _near_seg(p, a, b, tol=1.0):
    ab, ap = _sub(b, a), _sub(p, a)
    L2 = _dot(ab, ab) or 1e-9
    t = max(0.0, min(1.0, _dot(ap, ab) / L2))
    q = _lerp(a, b, t)
    return math.hypot(p[0] - q[0], p[1] - q[1]) <= tol


def stair_treads(v, H, goings=None):
    """Treads of a stair in climbing order, all risers H / R with R = ceil(H / RISER_MAX).
    The goings are R - 1: a winder landing keeps its n winders, a level landing is one
    going, the flights share the rest in proportion to their run.
    Returns (treads, rise, going of the longest flight)."""
    pieces = stair_pieces(v)
    flights = [p for p in pieces if p["axis"] is not None]
    fixed = sum((p.get("winders") or 1) for p in pieces if p["axis"] is None)
    R = max(2, math.ceil(H / RISER_MAX))
    free = max(len(flights), R - 1 - fixed)
    R = free + fixed + 1
    runs = [p["run"] for p in flights]
    quota = [free * r / sum(runs) for r in runs]
    cnt = [max(1, int(q)) for q in quota]
    while sum(cnt) < free:
        cnt[max(range(len(cnt)), key=lambda i: quota[i] - cnt[i])] += 1
    while sum(cnt) > free:
        cnt[max(range(len(cnt)), key=lambda i: (cnt[i] > 1, cnt[i] - quota[i]))] -= 1
    if goings:                                          # the draftsman's counts, per flight
        if len(goings) != len(flights):
            sys.exit(f"@stair {v.get('id')} goings: {len(flights)} flight(s), {len(goings)} count(s)")
        cnt = list(goings)
        R = sum(cnt) + fixed + 1
    for p, c in zip(flights, cnt):
        p["goings"] = c
    rise = H / R
    main = max(flights, key=lambda p: p["run"])
    treads = []
    for i, p in enumerate(pieces):
        bb = p["bbox"]
        x0, y0, x1, y1 = bb["x"], bb["y"], bb["x"] + bb["w"], bb["y"] + bb["h"]
        if p["axis"] is not None:
            sg = 1 if p["high"] > p["low"] else -1
            g = p["run"] / p["goings"]
            t0, t1 = (y0, y1) if p["axis"] == "x" else (x0, x1)

            def pt(s, t, p=p, sg=sg):
                a = p["low"] + sg * s
                return (a, t) if p["axis"] == "x" else (t, a)
            for k in range(p["goings"]):
                s0, s1 = k * g, (k + 1) * g
                a, b, c, d = pt(s0, t0), pt(s1, t0), pt(s1, t1), pt(s0, t1)

                def lam(q, p=p, sg=sg, s0=s0, g=g):
                    s = ((q[0] if p["axis"] == "x" else q[1]) - p["low"]) * sg
                    return max(0.0, min(1.0, (s - s0) / g))
                treads.append({"poly": [a, b, c, d], "front": (a, d), "back": (b, c),
                               "sides": [(a, b), (d, c)], "lam": lam, "winder": False})
        elif p.get("winders"):
            before, after = pieces[i - 1], pieces[i + 1]
            ab, aa = before["axis"], after["axis"]          # entry side: ab = before.high
            hb, la = before["high"], after["low"]

            def P2(on_b, on_a, ab=ab):                     # point from (coord on ab, coord on aa)
                return (on_b, on_a) if ab == "x" else (on_a, on_b)
            C = P2(hb, la)
            ra = (x0, x1) if aa == "x" else (y0, y1)
            rb = (x0, x1) if ab == "x" else (y0, y1)
            oa = ra[1] if abs(la - ra[0]) < 1 else ra[0]
            ob = rb[1] if abs(hb - rb[0]) < 1 else rb[0]
            e = _sub(P2(hb, oa), C)
            e = (e[0] / abs(e[0] + e[1]), e[1] / abs(e[0] + e[1]))
            xv = _sub(P2(ob, la), C)
            xv = (xv[0] / abs(xv[0] + xv[1]), xv[1] / abs(xv[0] + xv[1]))
            W = bb["w"]
            n = p["winders"]

            def Pt(th, C=C, e=e, xv=xv, W=W):
                r = W / max(math.cos(th), math.sin(th))
                return (C[0] + r * (math.cos(th) * e[0] + math.sin(th) * xv[0]),
                        C[1] + r * (math.cos(th) * e[1] + math.sin(th) * xv[1]))
            F = (C[0] + W * (e[0] + xv[0]), C[1] + W * (e[1] + xv[1]))
            for j in range(n):
                ta, tb = j * math.pi / 2 / n, (j + 1) * math.pi / 2 / n
                rim = [Pt(ta)] + ([F] if ta < math.pi / 4 - 1e-9 and tb > math.pi / 4 + 1e-9 else []) + [Pt(tb)]

                def lam(q, C=C, e=e, xv=xv, ta=ta, tb=tb):
                    w = _sub(q, C)
                    th = math.atan2(_dot(w, xv), _dot(w, e))
                    return max(0.0, min(1.0, (th - ta) / (tb - ta)))
                treads.append({"poly": [C] + rim, "front": (C, rim[0]), "back": (C, rim[-1]), "rim": rim,
                               "sides": list(zip(rim, rim[1:])), "lam": lam, "winder": True, "C": C})
        else:                                               # level landing: one going
            rect = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            treads.append({"poly": rect, "front": treads[-1]["back"] if treads else None, "back": None,
                           "sides": list(zip(rect, rect[1:] + rect[:1])), "lam": lambda q: 0.5,
                           "winder": False})
    return treads, rise, main["run"] / main["goings"]


def _outward(edge, poly):
    (ax, ay), (bx, by) = edge
    n = (by - ay, -(bx - ax))
    L = math.hypot(*n) or 1.0
    n = (n[0] / L, n[1] / L)
    cx, cy = sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly)
    return n if _dot(n, ((ax + bx) / 2 - cx, (ay + by) / 2 - cy)) > 0 else (-n[0], -n[1])


def stair_faces(treads, zl, rise, waist):
    """Boundary faces of the stair: risers, stringer sides, soffit.  Each face is
    (vertices [(x, y, z)], flags [edge i drawn?], outward normal (nx, ny, nz) or None = soffit)."""
    faces, K = [], len(treads)
    for k, t in enumerate(treads):
        zt = zl + (k + 1) * rise
        first, last = k == 0, k == K - 1

        def soff(q, t=t, zt=zt):
            return zt - rise - waist + rise * t["lam"](q)

        def role(q, t=t):
            if t["front"] and _near_seg(q, *t["front"]):
                return "front"
            if t["back"] and _near_seg(q, *t["back"]):
                return "back"
            return None
        if t["front"]:
            a, b = t["front"]
            n = _outward(t["front"], t["poly"])
            faces.append(([(*a, zt - rise), (*b, zt - rise), (*b, zt), (*a, zt)], [True] * 4, (*n, 0.0), "riser"))
        if last and t["back"]:                               # arrival riser onto the floor above
            a, b = t["back"]
            n = _outward(t["back"], t["poly"])
            faces.append(([(*a, zt), (*b, zt), (*b, zt + rise), (*a, zt + rise)], [True] * 4, (-n[0], -n[1], 0.0), "riser"))
        for a, b in t["sides"]:
            m = 8 if t["winder"] or t.get("tapered") else 1
            V = [(*q, soff(q)) for q in (_lerp(a, b, i / m) for i in range(m + 1))]
            Fl = [True] * m

            def up(q):
                """Vertical edge at q as (z_from, drawn) pieces, bottom to top.  Below the
                riser of a tread's front edge and along its back edge it is a seam."""
                r = role(q)
                if r == "back":
                    return [(soff(q), last)]
                if r == "front":
                    return [(soff(q), first), (zt - rise, True)]
                return [(soff(q), True)]
            segb = up(b)
            for i, (z_, f) in enumerate(segb):
                if i:
                    V.append((*b, z_))
                Fl.append(f)
            V.append((*b, zt))
            Fl.append(True)                                  # tread line b -> a
            V.append((*a, zt))
            sega = up(a)
            for i, (z_, f) in enumerate(reversed(sega)):
                Fl.append(f)
                if i < len(sega) - 1:
                    V.append((*a, z_))
            n = _outward((a, b), t["poly"])
            faces.append((V, Fl, (*n, 0.0), "side"))
        if t["winder"]:                                      # helical soffit, one strip per rim step
            C, rim = t["C"], t["rim"]
            pts = []
            for a, b in zip(rim, rim[1:]):
                pts += [_lerp(a, b, i / 8) for i in range(8)]
            pts.append(rim[-1])
            for i, (a, b) in enumerate(zip(pts, pts[1:])):
                za, zb = soff(a), soff(b)
                fa = first if i == 0 else False
                fb = last if i == len(pts) - 2 else False
                faces.append(([(*C, za), (*a, za), (*b, zb)], [fa, True, False], None, "soffit"))
                faces.append(([(*C, za), (*b, zb), (*C, zb)], [False, fb, False], None, "soffit"))
        else:
            P = t["poly"]
            cx, cy = sum(p[0] for p in P) / len(P), sum(p[1] for p in P) / len(P)
            M = (cx, cy, soff((cx, cy)))
            for a, b in zip(P, P[1:] + P[:1]):
                if t["front"] and _near_seg(a, *t["front"]) and _near_seg(b, *t["front"]):
                    f = first
                elif t["back"] and _near_seg(a, *t["back"]) and _near_seg(b, *t["back"]):
                    f = last
                else:
                    f = True
                faces.append(([(*a, soff(a)), (*b, soff(b)), M], [f, False, False], None, "soffit"))
    return faces


# ------------------------------------------------------- section and view ---
def _newell(P):
    nx = ny = nz = 0.0
    for i in range(len(P)):
        a, b = P[i], P[(i + 1) % len(P)]
        nx += (a[1] - b[1]) * (a[2] + b[2])
        ny += (a[2] - b[2]) * (a[0] + b[0])
        nz += (a[0] - b[0]) * (a[1] + b[1])
    L = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
    return nx / L, ny / L, nz / L


def _clip_front(V, Fl, axis=2, lo=0.0):
    """Keep the part of a face with coordinate [axis] >= lo (default: behind the cut plane,
    d >= 0), carrying the edge flags; edges made by the clip are not drawn."""
    if lo:
        V = [tuple(p[i] - (lo if i == axis else 0) for i in range(3)) for p in V]
    out, of = [], []
    n = len(V)
    for i in range(n):
        A, B, f = V[i], V[(i + 1) % n], Fl[i]
        ina, inb = A[axis] >= 0, B[axis] >= 0
        if ina:
            out.append(A)
            of.append(f)
        if ina != inb:
            t = A[axis] / (A[axis] - B[axis])
            I = tuple(A[j] + (B[j] - A[j]) * t for j in range(3))
            out.append(I)
            of.append(f if inb else False)
            if ina:
                of[-2] = f
                of[-1] = False
    if lo:
        out = [tuple(p[i] + (lo if i == axis else 0) for i in range(3)) for p in out]
    return out, of


def _inside(poly, p, tol=0.5):
    x, y = p
    inside = False
    n = len(poly)
    for i in range(n):
        (ax, ay), (bx, by) = poly[i], poly[(i + 1) % n]
        if (ay > y) != (by > y) and x < ax + (y - ay) * (bx - ax) / (by - ay):
            inside = not inside
    if not inside:
        return False
    for i in range(n):
        (ax, ay), (bx, by) = poly[i], poly[(i + 1) % n]
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy or 1e-9
        t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / L2))
        if math.hypot(x - ax - t * dx, y - ay - t * dy) < tol:
            return False
    return True


def _seg_t(a, b, c, d):
    """Parameter on a-b where it crosses c-d, or None."""
    r = (b[0] - a[0], b[1] - a[1])
    s = (d[0] - c[0], d[1] - c[1])
    den = r[0] * s[1] - r[1] * s[0]
    if abs(den) < 1e-9:
        return None
    q = (c[0] - a[0], c[1] - a[1])
    t = (q[0] * s[1] - q[1] * s[0]) / den
    w = (q[0] * r[1] - q[1] * r[0]) / den
    return t if 1e-6 < t < 1 - 1e-6 and -1e-6 <= w <= 1 + 1e-6 else None


def stair_section_view(v, zl, H, axis, pos, look, U, goings=None):
    """Section and view of one stair storey of an ArchLang stair (plan y points south)."""
    treads, rise, going = stair_treads(v, H, goings)
    grad = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}[look]
    return tread_section_view(treads, rise, going, zl, axis, pos, grad, U)


def tread_section_view(treads, rise, going, zl, axis, pos, grad, U):
    """Section and view of a stair given as tread areas (see stair_treads) on the cut line
    axis = pos ('x': x = pos), looking along grad (plan vector), U(c) = sheet position of the
    plan coordinate c along the line.  Returns dict with cut: [polygon (u, z)],
    cut_lines / view_lines / hidden_lines: [((u, z), (u, z))], risers, rise, going."""
    waist = STAIR_WAIST * math.hypot(going, rise) / going
    gx, gy = grad

    def to_view(x, y, z):
        if axis == "x":
            return (U(y), z, (x - pos) * gx)
        return (U(x), z, (y - pos) * gy)
    # --- cut: each tread area on the cut line, from its soffit up to its tread
    cut, verticals, cut_lines = [], {}, []
    for k, t in enumerate(treads):
        cs = []
        P = t["poly"]
        for a, b in zip(P, P[1:] + P[:1]):
            ia, ib = (a[1], b[1]) if axis == "y" else (a[0], b[0])
            ca, cb = (a[0], b[0]) if axis == "y" else (a[1], b[1])
            if (ia - pos) * (ib - pos) < 0:
                cs.append(ca + (cb - ca) * (pos - ia) / (ib - ia))
            elif abs(ia - pos) < 1e-6:
                cs.append(ca)
        if len(cs) < 2 or max(cs) - min(cs) < 1:
            continue
        c0, c1 = min(cs), max(cs)
        zt = zl + (k + 1) * rise
        q = (lambda c: (c, pos)) if axis == "y" else (lambda c: (pos, c))
        bottom = [(U(c), max(zl, zt - rise - waist + rise * t["lam"](q(c))))
                  for c in (c1 + (c0 - c1) * i / 8 for i in range(9))]
        poly = [(U(c0), zt), (U(c1), zt)] + bottom
        cut.append(poly)
        for a, b in zip(poly, poly[1:] + poly[:1]):
            if abs(a[0] - b[0]) < 0.5:
                verticals.setdefault(round(a[0]), []).append(tuple(sorted((a[1], b[1]))))
            else:
                cut_lines.append((a, b))
    for u, iv in verticals.items():                        # a riser between two cut treads is drawn once,
        zs = sorted({z for seg in iv for z in seg})        # the seam below it not at all
        for za, zb in zip(zs, zs[1:]):
            m = (za + zb) / 2
            if sum(1 for a, b in iv if a <= m <= b) == 1:
                cut_lines.append(((u, za), (u, zb)))
    # --- view: faces behind the cut plane.  Faces turned to the viewer hide what lies behind
    # them; their edges are drawn where nothing nearer covers them.  The tread edges (top and
    # bottom of every riser) that are covered are drawn dashed, as hidden edges (DIN 1356-1),
    # so a flight seen from its head still shows its steps.
    occ, risers_all = [], []
    for V, Fl, nrm, kind in stair_faces(treads, zl, rise, waist):
        if nrm is None:                                    # soffit: faces down
            nx, ny, nz = _newell(V)
            if nz > 0:
                nx, ny, nz = -nx, -ny, -nz
        else:
            nx, ny, nz = nrm
        front = nx * gx + ny * gy < -1e-6                  # turned to the viewer
        if not front and kind != "riser":
            continue
        W, Fw = _clip_front([to_view(*p) for p in V], Fl)
        if len(W) >= 3:
            W, Fw = _clip_front(W, Fw, axis=1, lo=zl)       # below its own floor: inside the slab
        if len(W) < 3:
            continue
        nu, nzv, nd = _newell(W)
        if abs(nd) < 1e-9:                                 # edge-on
            continue
        u0, z0_, d0 = W[0]
        poly2 = [(p[0], p[1]) for p in W]
        F = {"poly": poly2, "flags": Fw if front else [False] * len(Fw), "cut": False,
             "depth": (lambda u, z, nu=nu, nzv=nzv, nd=nd, u0=u0, z0_=z0_, d0=d0:
                       d0 - (nu * (u - u0) + nzv * (z - z0_)) / nd),
             "bb": (min(p[0] for p in poly2), min(p[1] for p in poly2),
                    max(p[0] for p in poly2), max(p[1] for p in poly2))}
        if front:
            occ.append(F)
        if kind == "riser":
            risers_all.append(F)
    for poly in cut:
        occ.append({"poly": poly, "flags": [False] * len(poly), "depth": lambda u, z: 0.0, "cut": True,
                    "bb": (min(p[0] for p in poly), min(p[1] for p in poly),
                           max(p[0] for p in poly), max(p[1] for p in poly))})

    def parts(F, a, b):
        """Pieces of edge a-b of face F as (p0, p1, 'visible' | 'hidden' | 'cut')."""
        lo = (min(a[0], b[0]), min(a[1], b[1]))
        hi = (max(a[0], b[0]), max(a[1], b[1]))
        near = [Q for Q in occ if Q is not F and Q["bb"][0] <= hi[0] + 1 and Q["bb"][2] >= lo[0] - 1
                and Q["bb"][1] <= hi[1] + 1 and Q["bb"][3] >= lo[1] - 1]
        ts = {0.0, 1.0}
        for Q in near:
            QP = Q["poly"]
            for j in range(len(QP)):
                t = _seg_t(a, b, QP[j], QP[(j + 1) % len(QP)])
                if t is not None:
                    ts.add(t)
        ts = sorted(ts)
        out = []
        for t0, t1 in zip(ts, ts[1:]):
            m = _lerp(a, b, (t0 + t1) / 2)
            dF = F["depth"](*m)
            cover = [Q for Q in near if Q["bb"][0] <= m[0] <= Q["bb"][2] and Q["bb"][1] <= m[1] <= Q["bb"][3]
                     and Q["depth"](*m) < dF - 1 and _inside(Q["poly"], m)]
            state = "visible" if not cover else ("cut" if any(Q["cut"] for Q in cover) else "hidden")
            out.append((_lerp(a, b, t0), _lerp(a, b, t1), state))
        return out

    def key(p0, p1):
        return tuple(sorted(((round(p0[0]), round(p0[1])), (round(p1[0]), round(p1[1])))))
    seen, view_lines, hidden_lines = set(), [], []
    for F in occ:
        P = F["poly"]
        for i, f in enumerate(F["flags"]):
            if f:
                for p0, p1, st in parts(F, P[i], P[(i + 1) % len(P)]):
                    if st == "visible" and key(p0, p1) not in seen:
                        seen.add(key(p0, p1))
                        view_lines.append((p0, p1))
    for F in risers_all:                                   # covered tread edges, dashed
        P = F["poly"]
        for i in range(len(P)):
            a, b = P[i], P[(i + 1) % len(P)]
            if abs(a[1] - b[1]) > 0.5 or abs(a[0] - b[0]) < 1:   # horizontal edges only
                continue
            for p0, p1, st in parts(F, a, b):
                k = key(p0, p1)
                if st == "hidden" and k not in seen:
                    seen.add(k)
                    hidden_lines.append((p0, p1))
    return {"cut": cut, "cut_lines": cut_lines, "view_lines": view_lines, "hidden_lines": hidden_lines,
            "risers": len(treads) + 1, "rise": rise, "going": going}


def level_cut(level, axis, pos, look="north"):
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
        n_lo, n_hi = (("x", "w") if axis == "x" else ("y", "h"))
        # a stair the cut passes through, or one wholly behind the cut plane (drawn in view)
        behind = (lambda p: p["bbox"][n_lo] + p["bbox"][n_hi] > pos) if look in ("east", "south") \
            else (lambda p: p["bbox"][n_lo] < pos)
        if any(behind(p) for p in pieces_all):
            stairs.append({"v": v, "dir": v.get("dir", "up")})
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
    cuts = {l["n"]: level_cut(l, axis, pos, look) for l in levels}
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

    els = {"walls": [], "slabs": [], "glass": [], "stair_cut": [], "stair_cut_lines": [], "views": [], "hidden": [],
           "stair_labels": [], "texts": [], "dims": [], "marks": []}
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
        taken = []
        for st in c["stairs"]:
            H = l["height"]
            zl = z0 if st["dir"] == "up" else z0 - next((x["height"] for x in levels if x["n"] == n - 1), H)
            Hs = H if st["dir"] == "up" else z0 - zl
            vl = n + 1 if st["dir"] == "up" else n
            g = stair_section_view(st["v"], zl, Hs, axis, pos, look, U, d["stair_goings"].get(st["v"].get("id")))
            els["stair_cut"] += g["cut"]
            els["stair_cut_lines"] += g["cut_lines"]
            els["views"] += g["view_lines"]
            els["hidden"] += g["hidden_lines"]
            us = [p[0] for poly in g["cut"] for p in poly] + [p[0] for ln in g["view_lines"] for p in ln]
            if not us:
                continue
            taken.append((min(us), max(us)))
            if g["cut"]:
                cu = [p[0] for poly in g["cut"] for p in poly]
                stair_voids.setdefault(vl, []).append((min(cu), max(cu)))
            els["stair_labels"].append(((min(us) + max(us)) / 2, zl + 150,
                                        f'{g["risers"]} Stg. {g["rise"]:.0f}/{g["going"]:.0f}'))
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


def render_svg(sec, scale, footer=None):
    els, (x0, z0, x1, z1) = sec["els"], sec["bb"]
    pad_l, pad_r, pad_t, pad_b = 9000, 4200, 1600, 2200
    if footer:
        pad_b += 2.5 * scale            # one more line below the caption
    vx, vy = x0 - pad_l, -(z1 + pad_t)
    vw, vh = (x1 - x0) + pad_l + pad_r, (z1 - z0) + pad_t + pad_b
    S = scale / 1.0                     # model mm per paper mm
    txt = 3.0 * S
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{vw / S:.1f}mm" height="{vh / S:.1f}mm" '
           f'viewBox="{vx:.0f} {vy:.0f} {vw:.0f} {vh:.0f}" font-family="DejaVu Sans, sans-serif">',
           f"<defs>{PATTERNS}</defs>", f'<rect x="{vx}" y="{vy}" width="{vw}" height="{vh}" fill="white"/>']
    for (ua, za), (ub, zb) in els["views"]:            # stair behind the cut plane: visible edges, thin
        out.append(f'<line x1="{ua:.0f}" y1="{-za:.0f}" x2="{ub:.0f}" y2="{-zb:.0f}" stroke="#000" '
                   f'stroke-width="{0.25 * S:.0f}" stroke-linecap="round"/>')
    for a, b, c, d_, lname, assumed in els["slabs"]:
        out.append(f'<rect x="{a:.0f}" y="{-d_:.0f}" width="{c - a:.0f}" height="{d_ - b:.0f}" fill="{layer_fill(lname, assumed)}" '
                   f'stroke="{"#d03030" if assumed else "#000"}" stroke-width="{0.18 * S:.0f}"'
                   f'{" stroke-dasharray=\"%d %d\"" % (2 * S, 1 * S) if assumed else ""}/>')
    for a, b, c, d_, kind in els["walls"]:
        out.append(f'<rect x="{a:.0f}" y="{-d_:.0f}" width="{c - a:.0f}" height="{d_ - b:.0f}" '
                   f'fill="{"#222" if kind == "exterior" else "#555"}" stroke="#000" stroke-width="{0.5 * S:.0f}"/>')
    for u, t, a, b in els["glass"]:
        for off in (-t / 6, t / 6):
            out.append(f'<line x1="{u + off:.0f}" y1="{-a:.0f}" x2="{u + off:.0f}" y2="{-b:.0f}" stroke="#1060c0" stroke-width="{0.18 * S:.0f}"/>')
        out.append(f'<rect x="{u - t / 2:.0f}" y="{-a - 0.4 * S:.0f}" width="{t:.0f}" height="{0.4 * S:.0f}" fill="#1060c0"/>')
    for (ua, za), (ub, zb) in els["hidden"]:           # hidden tread edges: dashed, thin
        out.append(f'<line x1="{ua:.0f}" y1="{-za:.0f}" x2="{ub:.0f}" y2="{-zb:.0f}" stroke="#000" '
                   f'stroke-width="{0.25 * S:.0f}" stroke-dasharray="{2 * S:.0f} {1 * S:.0f}"/>')
    for poly in els["stair_cut"]:                      # cut stair: reinforced concrete
        out.append('<polygon points="' + " ".join(f"{p:.0f},{-q:.0f}" for p, q in poly)
                   + '" fill="url(#concrete)" stroke="none"/>')
    for (ua, za), (ub, zb) in els["stair_cut_lines"]:  # cut edges, wide
        out.append(f'<line x1="{ua:.0f}" y1="{-za:.0f}" x2="{ub:.0f}" y2="{-zb:.0f}" stroke="#000" '
                   f'stroke-width="{0.5 * S:.0f}" stroke-linecap="round"/>')
    for u, z, label in els["stair_labels"]:
        w_ = 0.8 * txt * 0.6 * len(label)                  # white ground under the label
        out.append(f'<rect x="{u - w_ / 2:.0f}" y="{-z - 0.8 * txt:.0f}" width="{w_:.0f}" height="{1.05 * txt:.0f}" fill="white"/>')
        out.append(f'<text x="{u:.0f}" y="{-z:.0f}" font-size="{txt * 0.8:.0f}" text-anchor="middle">{label}</text>')
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
               f'(rudimentär: nur Schnittebene, Ansicht dahinter nur Treppen, verdeckte Stufenkanten gestrichelt, Dach nicht modelliert)</text>')
    if footer:                          # the caption above moved up by the extra padding
        out[-1] = out[-1].replace(f'y="{vy + vh - 0.8 * txt:.0f}"', f'y="{vy + vh - 0.8 * txt - 2.5 * scale:.0f}"')
        out.append(f'<text x="{x0:.0f}" y="{vy + vh - 0.8 * txt:.0f}" font-size="{txt * 0.6:.0f}" fill="#555">{escape(footer)}</text>')
    out.append("</svg>")
    return "\n".join(out)


def render_dxf(sec, path):
    import ezdxf
    doc = ezdxf.new("R2010", setup=True)
    doc.units = ezdxf.units.MM
    msp = doc.modelspace()
    for lay, col in (("S_WAND", 7), ("S_DECKE", 8), ("S_DECKE_ANNAHME", 1), ("S_GLAS", 5), ("S_TREPPE", 9), ("S_TREPPE_ANSICHT", 8), ("S_TREPPE_VERDECKT", 8), ("S_TEXT", 7), ("S_GELAENDE", 3)):
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
    for a, b in els["views"]:
        msp.add_line(a, b, dxfattribs={"layer": "S_TREPPE_ANSICHT", "lineweight": 25})
    for a, b in els["hidden"]:
        msp.add_line(a, b, dxfattribs={"layer": "S_TREPPE_VERDECKT", "linetype": "DASHED", "lineweight": 25})
    for poly in els["stair_cut"]:
        h = msp.add_hatch(dxfattribs={"layer": "S_TREPPE"})
        h.set_pattern_fill("ANSI31", scale=10)
        h.paths.add_polyline_path(poly, is_closed=True)
    for a, b in els["stair_cut_lines"]:
        msp.add_line(a, b, dxfattribs={"layer": "S_TREPPE", "lineweight": 50})
    for u, z, label in els["stair_labels"]:
        msp.add_text(label, height=90, dxfattribs={"layer": "S_TEXT"}).set_placement(
            (u, z), align=ezdxf.enums.TextEntityAlignment.BOTTOM_CENTER)
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
def own_command(a, arch, outdir):
    """This invocation as one shell line, paths absolute, so it reruns from any directory."""
    cmd = [os.path.realpath(__file__), arch, "-o", os.path.abspath(outdir)]
    if a.section:
        cmd += ["--section", a.section]
    if a.scale != 50:
        cmd += ["--scale", f"{a.scale:g}"]
    cmd += [f for f, on in (("--no-png", a.no_png), ("--no-dxf", a.no_dxf)) if on]
    return shlex.join(cmd)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("arch")
    ap.add_argument("--section", help="only this @section name")
    ap.add_argument("-o", "--outdir", help="default: next to the .arch file")
    ap.add_argument("--scale", type=float, default=50)
    ap.add_argument("--no-png", action="store_true")
    ap.add_argument("--no-dxf", action="store_true")
    ap.add_argument("--footer", help="command shown in the footer (default: this invocation)")
    ap.add_argument("--no-footer", action="store_true")
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
    cmd = a.footer or own_command(a, arch, outdir)
    for n in names:
        sec = build_section(levels, d, n)
        base = os.path.join(outdir, f"{stem}_schnitt_{n}")
        footer = None if a.no_footer else f"created by: {cmd}   →   {os.path.basename(base)}.svg/.png/.dxf"
        svg = render_svg(sec, a.scale, footer)
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
