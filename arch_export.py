#!/usr/bin/env python3
"""arch_export.py - export every sheet of an ArchLang plan into one folder, each with a footer.

Writes, for plan.arch into OUTDIR:
  plan.arch                             a copy of the source
  plan_arch.L<n>.svg|png|pdf|dxf        floor plans, one per level (ArchLang compile)
  plan_schnitt_<name>.svg|png|dxf       sections (arch_section.py, one per # @section)

Every sheet (SVG, PNG, PDF) carries a footer line at the bottom with the command
that produced it, i.e. this script's invocation with absolute paths, plus the
sheet's own file name. DXF has no sheet and gets no footer. The npm package is
untouched: the floor-plan footer is stamped onto ArchLang's output afterwards.

Usage:
  arch_export.py plan.arch -o OUTDIR

Requires: node + `npm install` in this directory (ArchLang), Python 3 with
Pillow (PNG), pypdf + reportlab (PDF), plus what arch_section.py needs.
"""
import argparse
import glob
import os
import re
import shlex
import shutil
import subprocess
import sys
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.realpath(__file__))
ARCHLANG_DIR = os.environ.get("ARCHLANG_DIR", HERE)
CLI = os.environ.get("ARCHLANG_CLI", os.path.join(ARCHLANG_DIR, "node_modules/@chanmeng666/archlang/dist/cli.js"))
FORMATS = ("svg", "png", "pdf", "dxf")
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"   # has the arrow; Helvetica in PDF has not

# footer geometry on the sheet, mm
FOOT_X = 12.0          # from the left edge
FOOT_Y = 6.0           # baseline above the bottom edge
FOOT_EM = 3.0          # nominal font size (em); shrunk to fit the sheet width, not below FOOT_EM_MIN
FOOT_EM_MIN = 1.6
FOOT_GREY = (85, 85, 85)


def fit_em(text, paper_w_mm):
    """Font size in mm so the line fits between the margins (DejaVu Sans metrics)."""
    from PIL import ImageFont
    per_em = ImageFont.truetype(FONT, 100).getlength(text) / 100     # line width in em
    return max(FOOT_EM_MIN, min(FOOT_EM, (paper_w_mm - 2 * FOOT_X) / per_em))


def footer_text(cmd, sheet):
    return f"created by: {cmd}   →   {sheet}"


# ------------------------------------------------------------- stamping ---
def stamp_svg(path, text):
    s = open(path, encoding="utf-8").read()
    m = re.search(r'<svg\b[^>]*\bwidth="([\d.]+)mm"[^>]*\bviewBox="([-\d.]+) ([-\d.]+) ([\d.]+) ([\d.]+)"', s)
    if not m:
        sys.exit(f"{path}: no width in mm / viewBox on the root element, cannot place the footer")
    w_mm = float(m.group(1))
    vx, vy, vw, vh = map(float, m.group(2, 3, 4, 5))
    u = vw / w_mm                                   # user units per paper mm
    x, y = vx + FOOT_X * u, vy + vh - FOOT_Y * u
    em = fit_em(text, w_mm)
    g = "#%02x%02x%02x" % FOOT_GREY
    tag = (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{em * u:.1f}" fill="{g}" '
           f'font-family="DejaVu Sans, Helvetica, Arial, sans-serif">{escape(text)}</text>\n')
    i = s.rindex("</svg>")
    open(path, "w", encoding="utf-8").write(s[:i] + tag + s[i:])


def stamp_png(path, text, paper_w_mm):
    from PIL import Image, ImageDraw, ImageFont
    im = Image.open(path).convert("RGB")
    px = im.width / paper_w_mm                      # pixels per paper mm
    font = ImageFont.truetype(FONT, max(6, round(fit_em(text, paper_w_mm) * px)))
    ImageDraw.Draw(im).text((FOOT_X * px, im.height - FOOT_Y * px), text, font=font, fill=FOOT_GREY, anchor="ls")
    im.save(path)


def stamp_pdf(path, text):
    import io
    from pypdf import PdfReader, PdfWriter
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas
    pdfmetrics.registerFont(TTFont("DejaVuSans", FONT))
    reader, writer = PdfReader(path), PdfWriter()
    for page in reader.pages:
        w, h = float(page.mediabox.width), float(page.mediabox.height)
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=(w, h))
        c.setFont("DejaVuSans", fit_em(text, w / mm) * mm)
        c.setFillColorRGB(*(v / 255 for v in FOOT_GREY))
        c.drawString(float(page.mediabox.left) + FOOT_X * mm, float(page.mediabox.bottom) + FOOT_Y * mm, text)
        c.save()
        buf.seek(0)
        page.merge_page(PdfReader(buf).pages[0])
        writer.add_page(page)
    with open(path, "wb") as f:
        writer.write(f)


# --------------------------------------------------------------- export ---
def run(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        sys.exit(f"{shlex.join(cmd)} failed:\n{r.stderr[-2000:]}")
    return r


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("arch")
    ap.add_argument("-o", "--outdir", required=True)
    a = ap.parse_args()
    arch, outdir = os.path.abspath(a.arch), os.path.abspath(a.outdir)
    os.makedirs(outdir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(arch))[0]
    cmd = shlex.join([os.path.realpath(__file__), arch, "-o", outdir])

    if os.path.realpath(os.path.join(outdir, os.path.basename(arch))) != os.path.realpath(arch):
        shutil.copy2(arch, outdir)

    # floor plans: one compile per format writes every level (<stem>_arch.L<n>.<fmt>)
    for fmt in FORMATS:
        out = os.path.join(outdir, f"{stem}_arch.{fmt}")
        r = run(["node", CLI, "compile", arch, "-f", fmt, "-o", out], cwd=ARCHLANG_DIR)
        for line in r.stderr.splitlines():
            if line.strip() and not line.startswith("✓"):
                print(line, file=sys.stderr)          # compiler warnings, shown once per format
    paper_w = {}
    for svg in sorted(glob.glob(os.path.join(outdir, f"{stem}_arch.L*.svg"))):
        m = re.search(r'\bwidth="([\d.]+)mm"', open(svg, encoding="utf-8").read(2000))
        paper_w[os.path.splitext(svg)[0]] = float(m.group(1))
        stamp_svg(svg, footer_text(cmd, os.path.basename(svg)))
    for base, w_mm in paper_w.items():
        stamp_png(base + ".png", footer_text(cmd, os.path.basename(base) + ".png"), w_mm)
        stamp_pdf(base + ".pdf", footer_text(cmd, os.path.basename(base) + ".pdf"))

    # sections: arch_section.py writes the footer itself, showing this command
    run([sys.executable, os.path.join(HERE, "arch_section.py"), arch, "-o", outdir, "--footer", cmd])

    made = sorted(os.listdir(outdir))
    print(f"{len(made)} files in {outdir}")


if __name__ == "__main__":
    main()
