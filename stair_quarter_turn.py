#!/usr/bin/env python3
"""stair_quarter_turn.py - 2D drawing of a quarter-turn stair (viertelgewendelte Treppe)
derived from storey height, riser count, stair width and the two run lengths.

Construction (as a draftsman does it, one formula per step):

  risers      n = ceil(H / h_max)           (or given), riser h = H / n: every step the same height
  walking line at w from the inner stringer (default w = b / 2): straight run 1, a quarter
              circle of radius w round the inner corner, straight run 2
              L = run1 + pi * w / 2 + run2
  going       a = L / (n - 1): every going the same on the walking line
              edge k lies on the walking line at sigma_k = k * a, k = 0 .. n - 1
  winding     the inner stringer has no length at the corner, so over the winding zone
              [sigma_s, sigma_e] it must give up the arc length pi * w / 2.  Its station is
                  tau(sigma) = sigma - pi * w / 2 * S(u),   u = (sigma - sigma_s) / (sigma_e - sigma_s)
                  S(u) = 3 u^2 - 2 u^3      (smooth: S'(0) = S'(1) = 0)
              Outside the zone tau = sigma (run 1) or sigma - pi * w / 2 (run 2), so the treads
              there stay rectangular; inside it the treads grow tapered towards the corner and
              back.  tau must not run backwards: the zone needs at least 1.5 * pi * w / 2.
  tread edge  the line from the inner stringer point Q(tau_k) through the walking line point
              P(sigma_k), up to the outer stringer.

Drawn: plan (Grundriss) with walking line, development of the walking line (all steps h / a)
and development of the inner stringer (the tapered goings and the curved stringer line).

Plan coordinates: inner corner at (0, 0), run 1 climbs east along the north wall
(outer stringer y = b), run 2 turns south (outer stringer x = b).
"""
import argparse
import math

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def construct(H, n, b, run1, run2, straight, top_straight, w):
    h = H / n
    arc = math.pi * w / 2
    L = run1 + arc + run2
    a = L / (n - 1)
    s0, s1 = straight * a, L - top_straight * a          # winding zone on the walking line
    if s1 - s0 < 1.5 * arc:
        raise SystemExit(f"winding zone {s1 - s0:.0f} mm is too short, needs >= {1.5 * arc:.0f} mm "
                         "(fewer rectangular steps)")

    def walk(s):
        if s <= run1:
            return (s - run1, w)
        if s <= run1 + arc:
            phi = (s - run1) / w
            return (w * math.sin(phi), w * math.cos(phi))
        return (w, -(s - run1 - arc))

    def tau(s):
        if s <= s0:
            return s
        if s >= s1:
            return s - arc
        u = (s - s0) / (s1 - s0)
        return s - arc * (3 * u * u - 2 * u ** 3)

    def inner(t):
        return (t - run1, 0.0) if t <= run1 else (0.0, -(t - run1))

    def outer(q, p):
        dx, dy = p[0] - q[0], p[1] - q[1]
        best = None
        if dy > 1e-9:                                    # outer stringer of run 1: y = b, x <= b
            t = (b - q[1]) / dy
            if q[0] + t * dx <= b + 1e-6:
                best = t
        if dx > 1e-9:                                    # outer stringer of run 2: x = b, y <= b
            t = (b - q[0]) / dx
            if q[1] + t * dy <= b + 1e-6 and (best is None or t < best):
                best = t
        return (q[0] + best * dx, q[1] + best * dy)

    edges = []
    for k in range(n):
        s = k * a
        p, t = walk(s), tau(s)
        q = inner(t)
        edges.append({"k": k, "sigma": s, "tau": t, "P": p, "Q": q, "O": outer(q, p)})
    return {"h": h, "a": a, "L": L, "arc": arc, "edges": edges, "walk": walk, "tau": tau,
            "zone": (s0, s1)}


def draw(c, b, run1, run2, H, out):
    h, a, E = c["h"], c["a"], c["edges"]
    n = len(E)
    fig = plt.figure(figsize=(11.7, 16.5))               # A3 portrait
    ax1 = fig.add_axes([0.08, 0.50, 0.84, 0.44])
    ax2 = fig.add_axes([0.08, 0.27, 0.84, 0.19])
    ax3 = fig.add_axes([0.08, 0.04, 0.84, 0.19])
    thick, thin = 1.4, 0.5

    # --- plan
    ax1.plot([-run1, 0, 0], [0, 0, -run2], "k", lw=thick)                 # inner stringer
    ax1.plot([-run1, b, b], [b, b, -run2], "k", lw=thick)                 # outer stringer (walls)
    ax1.plot([-run1, -run1], [0, b], "k", lw=thin)
    ax1.plot([0, b], [-run2, -run2], "k", lw=thick)                      # arrival: floor edge
    for e in E[1:-1]:
        ax1.plot([e["Q"][0], e["O"][0]], [e["Q"][1], e["O"][1]], "k", lw=thin)
    ws = [c["walk"](s) for s in [i * c["L"] / 400 for i in range(401)]]
    ax1.plot([p[0] for p in ws], [p[1] for p in ws], "k", lw=thin, dashes=(6, 2, 1, 2))
    p0 = E[0]["P"]
    ax1.add_patch(plt.Circle((p0[0] + 0.25 * a, p0[1]), 40, fill=False, lw=thin))   # walking line: start
    pe, pb = c["walk"](c["L"]), c["walk"](c["L"] - 0.6 * a)
    ax1.annotate("", xy=pe, xytext=pb, arrowprops={"arrowstyle": "-|>", "lw": thin, "color": "k"})
    for k in range(n - 1):                                                # step numbers on the walking line
        p = c["walk"]((k + 0.5) * a)
        ax1.text(p[0], p[1] + (60 if k < 9 else 0), str(k + 1), ha="center", va="bottom", fontsize=7)
    ax1.set_title(f"Grundriss  —  {n} Stg. {h:.1f}/{a:.1f}  (2h + a = {2 * h + a:.0f} mm), "
                  f"Laufbreite {b:.0f}, Lauflinie {c['L']:.0f} mm", fontsize=10)

    # --- development of the walking line: every step h / a
    def saw(ax, xs, label):
        z = 0.0
        xx, zz = [xs[0]], [0.0]
        for k in range(n - 1):
            z += h
            xx += [xs[k], xs[k + 1]]
            zz += [z, z]
        xx.append(xs[-1])
        zz.append(H)
        ax.plot(xx, zz, "k", lw=thick)
        ax.plot([xs[0] - 300, xs[0]], [0, 0], "k", lw=thin)               # floor below
        ax.plot([xs[-1], xs[-1] + 300], [H, H], "k", lw=thin)              # floor above
        ax.set_title(label, fontsize=10)
    saw(ax2, [e["sigma"] for e in E],
        f"Abwicklung der Lauflinie  —  alle Steigungen {h:.1f} mm, alle Auftritte {a:.1f} mm")
    s0, s1 = c["zone"]
    ax2.axvspan(s0, s1, color="0.92", zorder=0)
    ax2.text((s0 + s1) / 2, 0.1 * H, "Verziehungsbereich", ha="center", fontsize=8)

    taus = [e["tau"] for e in E]
    saw(ax3, taus, "Abwicklung der Innenwange  —  verzogene Stufen: tau(sigma) = sigma - "
                   "pi*w/2 * (3u^2 - 2u^3)")
    ss = [i * c["L"] / 400 for i in range(401)]                           # stringer line through the nosings
    ax3.plot([c["tau"](s) for s in ss], [h + h * s / a for s in ss], "k", lw=thin)
    ax3.plot([c["tau"](s) for s in ss], [h * s / a - 250 for s in ss], "k", lw=thin)
    ax3.axvline(run1, color="k", lw=thin, dashes=(4, 3))
    ax3.text(run1, 0.95 * H, " Innenecke", fontsize=8, va="top")

    for ax in (ax1, ax2, ax3):
        ax.set_aspect("equal")
        ax.tick_params(labelsize=7)
    fig.savefig(out + ".png", dpi=150)
    fig.savefig(out + ".pdf")


# ------------------------------------------------------------------ sections ---
def treads_from_edges(c, b, straight):
    """Tread areas between consecutive edges, in the form arch_section.py cuts and views."""
    E, eps = c["edges"], 1e-6
    out = []
    for k, (e, f) in enumerate(zip(E, E[1:])):
        Qa, Oa, Qb, Ob = e["Q"], e["O"], f["Q"], f["O"]
        outer = [Oa] + ([(b, b)] if Oa[1] > b - eps and Oa[0] < b - eps and Ob[0] > b - eps
                        and Ob[1] < b - eps else []) + [Ob]
        inner = [Qb] + ([(0.0, 0.0)] if Qa[1] > -eps and Qa[0] < -eps and Qb[0] > -eps
                        and Qb[1] < -eps else []) + [Qa]

        def lam(q, Qa=Qa, Oa=Oa, Qb=Qb, Ob=Ob):
            def dist(p, a, d):
                return abs((d[0] - a[0]) * (p[1] - a[1]) - (d[1] - a[1]) * (p[0] - a[0])) / math.dist(a, d)
            df, db = dist(q, Qa, Oa), dist(q, Qb, Ob)
            return df / (df + db) if df + db > 1e-9 else 0.0
        out.append({"poly": [Qa] + outer + inner[:-1], "front": (Qa, Oa), "back": (Qb, Ob),
                    "sides": list(zip(outer, outer[1:])) + list(zip(inner, inner[1:])),
                    "lam": lam, "winder": False, "tapered": k >= straight})
    return out


def draw_sections(c, A, out):
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    import arch_section as AS
    b, H, h, a = A.width, A.height, c["h"], c["a"]
    treads = treads_from_edges(c, b, A.straight)
    slab, wall_n, wall_e = A.slab, A.wall_north, A.wall_east
    fig, axes = plt.subplots(1, 2, figsize=(16.5, 11.7))   # A3 landscape
    specs = [  # name, axis, pos, plan look vector, U(c)
        (f"Schnitt A-A  (x = {A.cut_east:g}, Blick nach Osten)", "x", A.cut_east, (1, 0), lambda c_: -c_),
        (f"Schnitt B-B  (y = {A.cut_south:g}, Blick nach Süden)", "y", A.cut_south, (0, -1), lambda c_: -c_),
    ]
    spans = []
    for ax, (title, axis, pos, grad, U) in zip(axes, specs):
        g = AS.tread_section_view(treads, h, a, 0.0, axis, pos, grad, U)
        for poly in g["cut"]:
            ax.add_patch(plt.Polygon(poly, closed=True, facecolor="0.85", edgecolor="k", hatch="////", lw=0))
        for (p, q) in g["cut_lines"]:
            ax.plot([p[0], q[0]], [p[1], q[1]], "k", lw=1.4, solid_capstyle="round")
        for (p, q) in g["view_lines"]:
            ax.plot([p[0], q[0]], [p[1], q[1]], "k", lw=0.5)
        for (p, q) in g["hidden_lines"]:
            ax.plot([p[0], q[0]], [p[1], q[1]], "k", lw=0.5, dashes=(4, 2))
        us = [p[0] for poly in g["cut"] for p in poly] + [p[0] for ln in g["view_lines"] for p in ln]
        lo, hi = min(us) - 600, max(us) + 600
        cu = [p[0] for poly in g["cut"] for p in poly] or [lo]
        # walls the line cuts: the north wall behind the outer stringer of run 1 (in A),
        # the east wall behind the outer stringer of run 2 (in B)
        if axis == "x":
            wu = sorted((U(b), U(b + wall_n)))
        else:
            wu = sorted((U(b), U(b + wall_e)))
        ax.add_patch(plt.Rectangle((wu[0], -slab), wu[1] - wu[0], H + 2 * slab, facecolor="0.3", edgecolor="k", lw=1.4))
        lo, hi = min(lo, wu[0] - 300), max(hi, wu[1] + 300)
        # floor slabs, the upper one open over the cut stair
        ax.add_patch(plt.Rectangle((lo, -slab), hi - lo, slab, facecolor="0.85", edgecolor="k", hatch="////", lw=1.4))
        ca, cb = min(cu), max(cu)
        for x0, x1 in ((lo, ca), (cb, hi)):
            if x1 - x0 > 1:
                ax.add_patch(plt.Rectangle((x0, H - slab), x1 - x0, slab, facecolor="0.85", edgecolor="k",
                                           hatch="////", lw=1.4))
        for z, t in ((0, "±0,00 OKF"), (H, f"+{H / 1000:.2f} OKF".replace(".", ","))):
            ax.plot([hi, hi + 500], [z, z], "k", lw=0.5)
            ax.text(hi + 520, z, t, va="center", fontsize=8)
        ax.text((lo + hi) / 2, -slab - 300, f"{len(treads) + 1} Stg. {h:.1f}/{a:.1f}  (2h + a = {2 * h + a:.0f})",
                ha="center", va="top", fontsize=9)
        # height dimension: storey height in equal risers
        xd = lo - 250
        ax.plot([xd, xd], [0, H], "k", lw=0.5)
        for z in (0, H):
            ax.plot([xd - 60, xd + 60], [z - 60, z + 60], "k", lw=1.0)
        ax.text(xd - 80, H / 2, f"{len(treads) + 1} × {h:.1f} = {H:.0f}", rotation=90, ha="right", va="center", fontsize=8)
        ax.set_title(title, fontsize=11)
        ax.axis("off")
        spans.append((lo - 1200, hi + 1600, -slab - 700, H + 500))
    # one scale for both sections (DIN: drawn to scale), side by side on the sheet
    fw, fh = fig.get_size_inches()
    x = 0.6
    for ax, (x0, x1, z0, z1) in zip(axes, spans):
        w, hgt = (x1 - x0) / A.scale / 25.4, (z1 - z0) / A.scale / 25.4
        ax.set_xlim(x0, x1)
        ax.set_ylim(z0, z1)
        ax.set_position([x / fw, (fh - hgt) / 2 / fh, w / fw, hgt / fh])
        x += w + 0.4
    fig.text(0.5, 0.06, f"M 1:{A.scale:g} (A3) · stair_quarter_turn.py", ha="center", fontsize=8)
    fig.savefig(out + "_schnitte.png", dpi=150)
    fig.savefig(out + "_schnitte.pdf")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--height", type=float, default=2660, help="storey height H, floor to floor (mm)")
    ap.add_argument("--risers", type=int, help="riser count n (default ceil(H / max-riser))")
    ap.add_argument("--max-riser", type=float, default=180)
    ap.add_argument("--width", type=float, default=1000, help="stair width b (mm)")
    ap.add_argument("--run1", type=float, default=2700, help="inner stringer of run 1, start to inner corner (mm)")
    ap.add_argument("--run2", type=float, default=560, help="inner stringer of run 2, inner corner to floor edge (mm)")
    ap.add_argument("--straight", type=int, default=9, help="rectangular steps before the winders")
    ap.add_argument("--top-straight", type=int, default=0, help="rectangular steps after the winders")
    ap.add_argument("--walk", type=float, help="walking line from the inner stringer (default b / 2)")
    ap.add_argument("--cut-east", type=float, default=405, help="section A: x of the cut, looking east")
    ap.add_argument("--cut-south", type=float, default=500, help="section B: y of the cut, looking south")
    ap.add_argument("--slab", type=float, default=320, help="slab incl. floor finish (mm)")
    ap.add_argument("--wall-north", type=float, default=290, help="wall behind the outer stringer of run 1")
    ap.add_argument("--wall-east", type=float, default=150, help="wall behind the outer stringer of run 2")
    ap.add_argument("--scale", type=float, default=50, help="drawing scale of the sections, 1:<scale> on A3")
    ap.add_argument("-o", default="treppe_viertelwendung")
    A = ap.parse_args()
    n = A.risers or math.ceil(A.height / A.max_riser)
    c = construct(A.height, n, A.width, A.run1, A.run2, A.straight, A.top_straight, A.walk or A.width / 2)
    E = c["edges"]
    print(f"{n} risers of {c['h']:.1f} mm, goings {c['a']:.1f} mm on the walking line, "
          f"2h + a = {2 * c['h'] + c['a']:.0f} mm")
    print("step  going inner  walking  outer   (mm)")
    for e, f in zip(E, E[1:]):
        gi = f["tau"] - e["tau"]
        go = outer_station(f["O"], A.width, A.run1) - outer_station(e["O"], A.width, A.run1)
        print(f"{e['k'] + 1:4d}  {gi:11.0f}  {c['a']:7.0f}  {go:5.0f}")
    draw(c, A.width, A.run1, A.run2, A.height, A.o)
    draw_sections(c, A, A.o)
    print(f"written {A.o}.png/.pdf, {A.o}_schnitte.png/.pdf")


def outer_station(p, b, run1):
    """Distance along the outer stringer from the start of run 1 (the wall turns at (b, b))."""
    return p[0] + run1 if p[1] >= b - 1e-6 else run1 + b + (b - p[1])


if __name__ == "__main__":
    main()
