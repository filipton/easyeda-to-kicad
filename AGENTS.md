# epro2kicad

Translates EasyEDA Pro projects (`.epro`, `.epro2`, `.zip`) into native KiCad 10 projects, and puts LCSC
parts into KiCad's Remote Symbols panel. Plain Python 3, standard library only. The result has to look as
if it was drawn in KiCad and match what EasyEDA prints.

## Commands

```sh
python3 -m epro2kicad <project> -o <out>            # convert every board; --list, -b NAME, --3d, --kicad-symbols
python3 -m epro2kicad.test_convert                   # translation rules, no files needed
python3 -m epro2kicad.test_stock                     # stock-symbol pin mapping; needs KiCad's symbol libraries
uvx ruff check --select F,E9 .                       # unused names and syntax; none allowed
uv run --with numpy --with pillow tools/gerber_check.py <project> <Gerber.zip>   # against EasyEDA's own Gerbers
python3 -m epro2kicad.install                        # copy the plugin into KiCad (also restarts the parts provider)
python3 tools/gen_glyphs.py                          # regenerate kicad/glyphs.py from both programs' font data
```

Before committing: both self-checks on Python 3 and on KiCad's own Python (3.9, the one the plugin runs
on), ruff, and a conversion of at least one v2 and one v3 project with 0 new DRC or net differences.

## Layout

| path | what |
|---|---|
| `epro2kicad/easyeda/` | reading: `project.py` loads v2 and v3 archives, `records.py` turns both into one record model, `rules.py` the design rules |
| `epro2kicad/kicad/` | writing: `schematic.py`, `symbol.py`, `board.py`, `footprint.py`, `layers.py`, `projectfile.py`; `textfit.py` + `glyphs.py` place text where EasyEDA prints it; `fillfix.py` finishes pour fills with KiCad's engine |
| `epro2kicad/convert.py` | one project in, one KiCad folder per board out; runs kicad-cli's ERC, DRC and netlist; writes the report |
| `epro2kicad/validate.py` | schematic nets against PCB nets |
| `epro2kicad/geom.py`, `sexp.py` | geometry helpers; KiCad file writer |
| `epro2kicad/lcsc.py`, `stock.py` | one LCSC part by number; KiCad's standard symbols for standard parts |
| `epro2kicad/provider.py`, `panel.html` | the local server and page behind the LCSC parts panel |
| `epro2kicad/kicad_plugin.py`, `install.py`, `kicad_plugin/` | toolbar actions, import window, installer, plugin manifest and icons |
| `tools/` | Gerber comparison and glyph table generation; not shipped with the plugin |

## Boundaries

- Standard library only in `epro2kicad/`, and it runs on Python 3.9. `tools/` may use numpy and pillow.
- `easyeda/` knows nothing about KiCad; `kicad/` reads only the normalized records. A field name that
  differs between v2 and v3 is settled once in `records.py`, never where it is read.
- EasyEDA is the reference. Its file format schema ships with the desktop app
  (`assets/pro-api/*/assets/format-types.schema-*.json`); read it before guessing what a field means.
  A value EasyEDA does not check or show (a `0` rule cell, a leftover record) is not translated.
- The board is never changed to pass a check. Rules may be loosened only as `--tolerance` says, and the
  report states it.
- Everything a conversion decided that the user should know goes into `conversion-report.md`.
- Network answers are cached, including "not found"; a conversion repeated offline does not wait.

## Code

- Comments are short and factual: what a thing is, or why a non-obvious choice was made. No history.
- Fix the cause, then look for the same cause everywhere else it can occur.
- No dead code, no unused imports, no option without a caller.
- User-facing words are short and plain; the README stays terse.

## Tests

- A rule measured against EasyEDA's output gets a case in `test_convert.py`; a bug fix starts with a
  case that fails on the bug.
- Test headless with `kicad-cli`. Never open KiCad or EasyEDA windows without the owner's say.
- Real projects are private: they stay outside the repository and never appear in tests, docs or
  screenshots. Screenshots show public boards only.

## Commits

One line, conventional: `<type>: <what is different now>`, type in `feat fix perf refactor docs build
test chore`, lowercase, no full stop, under 72 chars. No body, no trailers, no `Co-Authored-By`, even if
your tool asks for one. One commit per piece of work, straight to main. Fixing your own unpushed work:
amend it into the commit it fixes; never stack "fix the fix" commits. `feat`, `fix` and `perf` subjects
are written for users.
