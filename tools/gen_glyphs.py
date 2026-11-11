"""Regenerate epro2kicad/kicad/glyphs.py: the glyph extents text fitting needs, from both programs' own font data.

    python3 tools/gen_glyphs.py [--kicad <kicad source>/common/newstroke_font.cpp] [--easyeda <EasyEDA-Pro.app or worker.js>]

EasyEDA Pro's line fonts are the `fontPath` tables in its PCB worker (pro-pcb/<version>/js/worker.js inside the
desktop client), KiCad's stroke font is common/newstroke_font.cpp.  Only the extents of each glyph are kept.
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "epro2kicad", "kicad", "glyphs.py")
# EasyEDA font family -> the worker's name for it (default = `wh`, the others are picked in its `dN` function)
EE_ALIAS = {"default": "wh", "default2": "OI", "Serif": "xI", "Sans Serif": "II"}
# code points of KiCad's font kept: Latin, Greek, Cyrillic and the usual technical signs
KICAD_RANGES = [(0x20, 0x7E), (0xA0, 0x17F), (0x370, 0x3FF), (0x400, 0x45F), (0x2010, 0x2027), (0x2030, 0x203A),
                (0x2070, 0x209C), (0x20AC, 0x20AC), (0x2103, 0x2109), (0x2126, 0x2126), (0x2190, 0x2199),
                (0x2200, 0x2265), (0x25A0, 0x25CF)]


def _object_at(s: str, i: int) -> str:
    """The JS object literal starting at s[i] == "{" (strings may contain braces)."""
    depth, q, j = 0, None, i
    while True:
        c = s[j]
        if q:
            if c == "\\":
                j += 1
            elif c == q:
                q = None
        elif c in "'\"":
            q = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[i:j + 1]
        j += 1


def _js_items(obj: str):
    """(key, value text) of a flat JS object literal whose values are numbers or {path:[...]}."""
    i, n = 1, len(obj) - 1
    while i < n:
        while i < n and obj[i] in ", \n":
            i += 1
        if i >= n:
            break
        if obj[i] in "'\"":
            q, j = obj[i], i + 1
            key = ""
            while obj[j] != q:
                if obj[j] == "\\":
                    j += 1
                key += obj[j]
                j += 1
            i = j + 1
        else:
            j = obj.index(":", i)
            key, i = obj[i:j], j
        assert obj[i] == ":", obj[i - 10:i + 10]
        i += 1
        if obj[i] == "{":
            val = _object_at(obj, i)
        else:
            j = i
            while j < n and obj[j] != ",":
                j += 1
            val = obj[i:j]
        i += len(val)
        yield key, val


def easyeda_fonts(worker: str) -> dict:
    s = open(worker, encoding="utf-8", errors="replace").read()
    classes = {}
    for m in re.finditer(r"(\w+)\.fontPath=\{", s):
        cls = m.group(1)
        table = _object_at(s, m.end() - 1)
        glyphs = {}
        for key, val in _js_items(table):
            nums = [float(v) for path in re.findall(r'"([^"]*)"', val) for v in path.split()]
            xs, ys = nums[0::2], nums[1::2]
            if xs:
                glyphs[key] = tuple(round(v, 3) for v in (min(xs), max(xs), min(ys), max(ys)))
        after = s[m.end() - 1 + len(table):]
        spacing = {}
        sm = re.match(r"," + cls + r"\.spacing=", after)
        if sm:
            spacing = {k: round(float(v), 3) for k, v in _js_items(_object_at(after, sm.end()))}
        body = s[max(s.rfind(f"class {cls}{{", 0, m.start()), 0):m.start()]
        sp = re.search(r"getSpaceWidth\(\w\)\{return ([\d.]+)\*\w\}", body)
        dflt = re.search(r"\.spacing\[\w\]\|\|([\d.]+)", body)
        classes[cls] = (glyphs, spacing, round(float(sp.group(1)), 3) if sp else None,
                        float(dflt.group(1)) if dflt else None)
    alias = dict(re.findall(r"var (\w+)=(\w+);", s))
    out = {}
    for family, var in EE_ALIAS.items():
        cls = alias.get(var)
        if cls not in classes:
            raise SystemExit(f"EasyEDA font '{family}' ({var}) not found in {worker}")
        out[family] = classes[cls]
    return out


def kicad_font(cpp: str) -> dict:
    s = open(cpp, encoding="utf-8", errors="replace").read()
    s = s[s.index("newstroke_font[] ="):]
    s = re.sub(r"/\*.*?\*/", "", s[:s.index("};")], flags=re.S)
    rows = [re.sub(r"\\(.)", r"\1", m) for m in re.findall(r'"((?:[^"\\]|\\.)*)"', s)]
    want = [cp for a, b in KICAD_RANGES for cp in range(a, b + 1)]
    out = {}
    for cp in want:
        i = cp - 0x20
        if i >= len(rows):
            continue
        g = rows[i]
        x0, x1 = ord(g[0]) - 82, ord(g[1]) - 82
        xs = [ord(g[k]) - 82 - x0 for k in range(2, len(g) - 1, 2) if g[k:k + 2] != " R"]
        ys = [ord(g[k + 1]) - 82 - 8 for k in range(2, len(g) - 1, 2) if g[k:k + 2] != " R"]
        out[chr(cp)] = (x1 - x0, min(xs), max(xs), min(ys), max(ys)) if xs else (x1 - x0, 0, 0, 0, 0)
    return out


def _fmt(d: dict, per_line: int = 4) -> str:
    items = [f"{k!r}: {v!r}" for k, v in d.items()]
    lines, cur = [], "   "
    for it in items:
        if len(cur) + len(it) + 2 > 118:
            lines.append(cur.rstrip())
            cur = "   "
        cur += " " + it + ","
    lines.append(cur.rstrip())
    return "{\n" + "\n".join(lines) + "\n}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kicad", default="")
    ap.add_argument("--easyeda", default="/Applications/EasyEDA-Pro.app")
    a = ap.parse_args()
    worker = a.easyeda
    if os.path.isdir(worker):
        hits = sorted(glob.glob(os.path.join(worker, "**", "pro-pcb", "*", "js", "worker.js"), recursive=True))
        if not hits:
            raise SystemExit(f"no pro-pcb/*/js/worker.js under {worker}")
        worker = hits[-1]
    if not os.path.isfile(a.kicad):
        raise SystemExit("--kicad: path to KiCad's common/newstroke_font.cpp")
    ee, kc = easyeda_fonts(worker), kicad_font(a.kicad)
    ver = re.search(r"pro-pcb/([^/]+)/", worker)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write('"""Glyph extents of EasyEDA Pro\'s line fonts and of KiCad\'s stroke font (generated by tools/gen_glyphs.py from\n'
                f'EasyEDA Pro\'s PCB worker {ver.group(1) if ver else ""} and KiCad\'s common/newstroke_font.cpp; do not edit)."""\n\n')
        f.write("# EasyEDA: char -> (x min, x max, y min, y max) of the strokes' centre lines in 1/32 of the glyph cell; x from\n"
                "# the pen position, y down with the cap line at -12, the baseline at 9 and the cell from -16 to 16\n")
        f.write("EE_DEFAULT = " + _fmt(ee["default"][0]) + "\n\n")
        f.write("# the newer line fonts: family -> (glyphs, gap after a glyph by char, default gap, width of a space)\n")
        f.write("EE_FONTS = {\n")
        for fam in ("default2", "Serif", "Sans Serif"):
            g, sp, space, dflt = ee[fam]
            f.write(f"    {fam!r}: (" + _fmt(g).replace("\n", "\n    ") + ", " + _fmt(sp).replace("\n", "\n    ")
                    + f", {dflt!r}, {space!r}),\n")
        f.write("}\n\n")
        f.write("# KiCad: char -> (advance, x min, x max, y min, y max) in 1/21 of the text width / height; x from the pen\n"
                "# position, y down from it with the cap line at -20 and the baseline at 1\n")
        f.write("NEWSTROKE = " + _fmt(kc) + "\n")
    print(f"{os.path.normpath(OUT)}: EasyEDA {', '.join(f'{k} {len(v[0])}' for k, v in ee.items())} glyphs; "
          f"KiCad {len(kc)} glyphs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
