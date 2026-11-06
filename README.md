# archlang-section

Rudimentary building sections (Schnitte) for [ArchLang](https://github.com/ChanMeng666/archlang) floor plans.

ArchLang 1.55 draws floor plans only — heights are a datum that "draws nothing", and sections/elevations
are planned upstream for a later `archlang3d` package. Until then this tool reads what ArchLang itself
exports (Plan JSON per storey and `describe --json`) and draws a vertical section along an axis-aligned
cut line as **SVG, PNG and DXF**. The ArchLang package is not modified.

![Schnitt A-A of the demo plan](example/demo_schnitt_A.png)

`example/haus.arch` is a real three-storey plan (Keller/EG/OG, identical floor plan) whose slab build-ups are not measured yet, so its sections show the red TODO slabs.

## Install

```sh
git clone https://github.com/gerontec/archlang-section && cd archlang-section
npm install                      # ArchLang (@chanmeng666/archlang)
pip install --user ezdxf cairosvg
```

## Use

```sh
./arch_section.py example/haus.arch            # all @section lines
./arch_section.py plan.arch --section A -o out/ --scale 50 --no-dxf
# demo.arch and haus_winders.arch use `landing … winders <n>` (ArchLang branch feat/stair-winders):
ARCHLANG_CLI=/path/to/archlang/dist/cli.js ./arch_section.py example/demo.arch
```

Output: `<plan>_schnitt_<name>.svg|png|dxf` next to the plan (or in `-o`). The SVG is to scale (default 1:50).
Set `ARCHLANG_DIR` to use an ArchLang install in another directory.

## Directives

Section data lives in **comments** of the `.arch` file, so ArchLang still compiles it unchanged:

| Directive | Meaning |
|---|---|
| `# @section A x=4300 look east` | cut along x = 4300 mm, looking east (`x=` needs east/west, `y=` north/south) |
| `# @floor 0 Fliesen:15 Estrich:65 Trittschall:30 Stahlbeton:180 Putz:15` | floor build-up **on** level 0, top → bottom, mm; quote names with spaces: `"Holzbalken mit Daemmung":200` |
| `# @floor -1 …` / `# @floor <top+1> …` | lowest level = ground slab; one above the top storey = top ceiling |
| `# @ground -300` | terrain line, mm relative to the datum |
| `# @datum 0` | level whose finished floor is ±0,00 (default 0) |
| `# @stairwell closed` | keep the slab above a cut stair (default: open) |
| `# @stair tr goings 9 1` | goings per flight of stair `tr` (default: shared by run); risers = goings + winders + 1 |

A level without `@floor` gets an assumed 250 mm slab, drawn red and marked TODO.

## What it draws

* walls cut by the line, thickness from `walls from rooms` (exterior / partition / `between … thickness`)
* doors as gaps with lintel, windows with sill, head and glazing (`sill` / `head` from the plan)
* slabs layer by layer, hatched by material keyword (Beton, Estrich, Dämmung, Holz, Putz, …)
* level marks OK FFB (finished floor) and OK RFB (top of the first structural layer), ±0,00 at the datum
* clear height (LH) per room, overall dimension, terrain line
* stairs, constructed as a draftsman does it in 2D:
  * **every riser the same height**: the storey height in risers of at most 180 mm (2660 mm → 15 × 177)
  * every tread an area in plan: rectangles in a flight, on a quarter-turn landing with
    `winders <n>` the winder fan of ArchLang (equal angles θ = k·90°/n from the inner corner, so a tread
    edge meets the far side at W·tan θ, then W·cot θ)
  * **cut**: each tread area on the cut line, from its soffit to its tread, hatched as concrete with
    wide edges; the winders come out with their real, unequal goings and a curved soffit
  * **view**: the stair behind the cut plane, edges facing the viewer only, hidden edges removed
    (thin lines); the part in front of the cut is left out
  * goings: winders as planned, a level landing one, the flights share the rest by run, or as
    written with `# @stair <id> goings 9 1` (one count per flight)
  * `winders` needs the ArchLang branch `feat/stair-winders` (see `example/haus_winders.arch`,
    `example/poc_winders.png`); run against a local build with `ARCHLANG_CLI=/path/to/archlang/dist/cli.js`

Storey height (finished floor to finished floor) comes from ArchLang's `level … height`; clear height is
storey height minus the `@floor` build-up of the level above.

## Limits

Only what the cut plane hits — nothing behind it is projected except the cut stair, no roofs, axis-aligned cuts only,
rooms/walls from `walls from rooms` (authored `wall` statements are not read),
and the cut line is not drawn in the floor plan (ArchLang has no element for it).

## License

MIT, see [LICENSE](LICENSE). ArchLang itself is a separate project under its own license.
