"""Check a translation against EasyEDA's own manufacturing output.

    uv run --with numpy --with pillow tools/gerber_check.py <project.epro2> <EasyEDA Gerber .zip or folder>
                                                             [--board NAME] [--out DIR] [--dpmm 40]

Export the Gerber of a board from EasyEDA ("Export > PCB Fabrication File"), then run this on the project and that
zip.  The board is translated, KiCad plots it (kicad-cli), and both plots are compared:

- every layer, as pixels: <out>/<layer>.png shows what both have in dark, only EasyEDA in red, only KiCad in blue;
- every silkscreen text: EasyEDA's Gerber marks its text, KiCad's is plotted from a copy of the board with nothing
  but texts; reported is how far each text's ink is off, along and across the text;
- the drill files, hole by hole;
- KiCad's DRC of the result.

This is how the text placement, pour clearances, image transforms and hole handling of the converter were verified.
"""
from __future__ import annotations

import argparse
import collections
import glob
import math
import os
import re
import subprocess
import sys
import tempfile
import zipfile

import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from gerber_render import Gerber  # noqa: E402

from epro2kicad.convert import Options, convert_project, proj_name  # noqa: E402
from epro2kicad.easyeda.project import load  # noqa: E402
from epro2kicad.kicad import textfit  # noqa: E402
from epro2kicad.sexp import dumps, loads  # noqa: E402

MIL = 0.0254

# name -> (EasyEDA file suffix, KiCad layer, KiCad file name part)
LAYERS = {
    "fsilk": ("TopSilkscreenLayer.GTO", "F.SilkS", "F_Silkscreen"),
    "bsilk": ("BottomSilkscreenLayer.GBO", "B.SilkS", "B_Silkscreen"),
    "fcu": ("TopLayer.GTL", "F.Cu", "F_Cu"),
    "bcu": ("BottomLayer.GBL", "B.Cu", "B_Cu"),
    "in1": ("InnerLayer1.G1", "In1.Cu", "In1_Cu"),
    "in2": ("InnerLayer2.G2", "In2.Cu", "In2_Cu"),
    "in3": ("InnerLayer3.G3", "In3.Cu", "In3_Cu"),
    "in4": ("InnerLayer4.G4", "In4.Cu", "In4_Cu"),
    "fmask": ("TopSolderMaskLayer.GTS", "F.Mask", "F_Mask"),
    "bmask": ("BottomSolderMaskLayer.GBS", "B.Mask", "B_Mask"),
    "fpaste": ("TopPasteMaskLayer.GTP", "F.Paste", "F_Paste"),
    "bpaste": ("BottomPasteMaskLayer.GBP", "B.Paste", "B_Paste"),
    "edge": ("BoardOutlineLayer.GKO", "Edge.Cuts", "Edge_Cuts"),
}


def find(directory: str, suffix: str) -> str | None:
    hits = glob.glob(os.path.join(directory, "*" + suffix))
    return hits[0] if hits else None


def compare(pcb: str, ee_dir: str, out: str, dpmm: float = 40, layers: list | None = None, quiet: bool = False):
    """-> {layer: dict(ee, kicad, only_ee, only_kicad, mismatch, iou)}; images and masks written to `out`."""
    os.makedirs(out, exist_ok=True)
    want = [k for k in (layers or list(LAYERS)) if find(ee_dir, LAYERS[k][0])]
    if "edge" not in want:
        want.append("edge")
    kdir = os.path.join(out, "kicad-gerber")
    os.makedirs(kdir, exist_ok=True)
    for f in glob.glob(os.path.join(kdir, "*")):
        os.remove(f)
    r = subprocess.run(["kicad-cli", "pcb", "export", "gerbers", "-o", kdir + "/", "--no-x2", "--no-netlist",
                        "--layers", ",".join(LAYERS[k][1] for k in want), pcb], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stdout + r.stderr)
    kfile = {k: (glob.glob(os.path.join(kdir, f"*-{LAYERS[k][2]}.*")) or [None])[0] for k in want}
    ee_edge, k_edge = Gerber.load(find(ee_dir, LAYERS["edge"][0])), Gerber.load(kfile["edge"])
    eb, kb = ee_edge.bbox, k_edge.bbox
    dx_raw, dy_raw = (kb[0] + kb[2]) / 2 - (eb[0] + eb[2]) / 2, (kb[1] + kb[3]) / 2 - (eb[1] + eb[3]) / 2
    dx, dy = round(dx_raw), round(dy_raw)
    if not quiet:
        print(f"offset KiCad - EasyEDA: ({dx_raw:.4f}, {dy_raw:.4f}) mm -> ({dx}, {dy})")
        print(f"{'layer':8} {'EE px':>9} {'KiCad px':>9} {'only EE':>9} {'only Ki':>9} {'mismatch':>9}  IoU")
    res = {}
    for k in want:
        ef = find(ee_dir, LAYERS[k][0])
        if not ef or not kfile.get(k):
            continue
        e = ee_edge if k == "edge" else Gerber.load(ef)
        q = k_edge if k == "edge" else Gerber.load(kfile[k])
        if e.bbox is None and q.bbox is None:
            continue
        boxes = [b for b in (e.bbox, (q.bbox[0] - dx, q.bbox[1] - dy, q.bbox[2] - dx, q.bbox[3] - dy) if q.bbox else None) if b]
        x0, y0 = min(b[0] for b in boxes) - 0.5, min(b[1] for b in boxes) - 0.5
        x1, y1 = max(b[2] for b in boxes) + 0.5, max(b[3] for b in boxes) + 0.5
        W, H = int(round((x1 - x0) * dpmm)), int(round((y1 - y0) * dpmm))
        A = e.raster(x0, y1, W, H, dpmm)
        B = q.raster(x0 + dx, y1 + dy, W, H, dpmm)
        only_e, only_k, both = A & ~B, B & ~A, A & B
        uni = int((A | B).sum()) or 1
        res[k] = dict(ee=int(A.sum()), kicad=int(B.sum()), only_ee=int(only_e.sum()), only_kicad=int(only_k.sum()),
                      mismatch=100 * (int(only_e.sum()) + int(only_k.sum())) / uni, iou=int(both.sum()) / uni,
                      ignored=dict(e.ignored) | dict(q.ignored))
        if not quiet:
            v = res[k]
            print(f"{k:8} {v['ee']:9d} {v['kicad']:9d} {v['only_ee']:9d} {v['only_kicad']:9d} {v['mismatch']:8.2f}%  "
                  f"{v['iou']:.4f}" + (f"  ignored: {v['ignored']}" if v["ignored"] else ""))
        rgb = np.full((H, W, 3), 255, np.uint8)
        rgb[both] = (40, 40, 40)
        rgb[only_e] = (230, 30, 30)
        rgb[only_k] = (30, 90, 230)
        Image.fromarray(rgb).save(os.path.join(out, f"{k}.png"))
        np.save(os.path.join(out, f"{k}.npy"), np.stack([A, B]))
        with open(os.path.join(out, f"{k}.origin"), "w") as f:
            f.write(f"{x0} {y1} {dpmm} {dx} {dy}\n")  # top-left pixel in EasyEDA mm, page offset
    return res


SILK = {3: ("TopSilkscreenLayer.GTO", "F.SilkS", "F_Silkscreen", False),
        4: ("BottomSilkscreenLayer.GBO", "B.SilkS", "B_Silkscreen", True)}


def strip_board(src: str, dst: str) -> None:
    """Copy of the board with only its texts (board texts, footprint fields and footprint texts)."""
    tree = loads(open(src, encoding="utf-8").read())
    keep_top = {"version", "generator", "generator_version", "general", "paper", "layers", "setup", "gr_text"}
    keep_fp = {"layer", "uuid", "at", "property", "fp_text", "attr"}
    out = [tree[0]]
    for n in tree[1:]:
        if not isinstance(n, list) or not n:
            continue
        head = str(n[0])
        if head in keep_top:
            out.append(n)
        elif head == "footprint":
            out.append([x for x in n if not isinstance(x, list) or str(x[0]) in keep_fp])
    with open(dst, "w", encoding="utf-8") as f:
        f.write(dumps(out) + "\n")


def records(proj, doc):
    """(label, layer id, x, y, angle, origin, font size, stroke, text, font family) of the board's own texts."""
    comp = {c.id for c in doc.of("COMPONENT")}
    for r in doc.of("STRING"):
        if r.str("text"):
            yield (f"STRING {r.str('text')[:14]!r}", r.int("layerId"), r.num("x"), r.num("y"), r.num("angle"),
                   r.str("origin"), r.num("fontSize", 45), r.num("strokeWidth", 6), r.str("text"),
                   r.str("fontFamily", "default") or "default", r)
    for a in doc.of("ATTR"):
        if a.d.get("x") is None or not a.d.get("valueVisible") or str(a.d.get("parentId") or "") not in comp:
            continue
        txt = a.str("value")
        if a.d.get("keyVisible"):
            txt = f"{a.str('key')}:{txt}"
        if txt:
            yield (f"ATTR {a.str('key')[:10]}={txt[:10]!r}", a.int("layerId"), a.num("x"), a.num("y"), a.num("angle"),
                   a.str("origin"), a.num("fontSize", 45), a.num("strokeWidth", 6), txt,
                   a.str("fontFamily", "default") or "default", a)




def texts(proj, doc, pcb: str, ee_dir: str, out: str, dx: float, dy: float, dpmm: float = 80.0) -> list:
    """How far each silkscreen text of the PCB document is off: [(worst offset mm, along, across, length ratio,
    height ratio, label, layer)], and the labels of texts one side didn't draw."""
    os.makedirs(out, exist_ok=True)
    stripped = os.path.join(out, "texts-only.kicad_pcb")
    strip_board(pcb, stripped)
    kdir = os.path.join(out, "kicad-gerber")
    os.makedirs(kdir, exist_ok=True)
    r = subprocess.run(["kicad-cli", "pcb", "export", "gerbers", "-o", kdir + "/", "--no-x2", "--no-netlist",
                        "--layers", "F.SilkS,B.SilkS", stripped], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stdout + r.stderr)
    rows, missing = [], []
    for lid, (ee_suffix, klayer, kpart, back) in SILK.items():
        ef, kf = find(ee_dir, ee_suffix), glob.glob(os.path.join(kdir, f"*-{kpart}.*"))
        recs = [t for t in records(proj, doc) if t[1] == lid]
        if not ef or not recs:
            continue
        e = Gerber.load(ef)
        q = Gerber.load(kf[0]) if kf else None
        if e.bbox is None:
            continue
        boxes = [e.bbox] + ([(q.bbox[0] - dx, q.bbox[1] - dy, q.bbox[2] - dx, q.bbox[3] - dy)] if q and q.bbox else [])
        x0, y0 = min(b[0] for b in boxes) - 1, min(b[1] for b in boxes) - 1
        x1, y1 = max(b[2] for b in boxes) + 1, max(b[3] for b in boxes) + 1
        W, H = int((x1 - x0) * dpmm), int((y1 - y0) * dpmm)
        A = e.raster(x0, y1, W, H, dpmm, sections={"Text"})
        B = q.raster(x0 + dx, y1 + dy, W, H, dpmm) if q and q.bbox else np.zeros((H, W), bool)
        for label, _, x, y, ang, origin, font, stroke, text, fam, rec in recs:
            h_al, v_al = (origin or "LEFT_BOTTOM").upper().split("_", 1) if "_" in (origin or "") else ("LEFT", "BOTTOM")
            if fam in textfit.LINE_FONTS:  # the text box as EasyEDA lays it out, plus a margin
                L, Hb, _ = textfit.ee_layout(text, font, stroke, fam)
            else:
                L, Hb = len(text.split("\n")[0]) * font * 0.7, font * 1.2 * (text.count("\n") + 1)
            m = 0.12 * font + stroke
            u0 = {"LEFT": 0.0, "RIGHT": -L}.get(h_al, -L / 2) - m
            v0 = {"BOTTOM": 0.0, "TOP": -Hb}.get(v_al, -Hb / 2) - m
            u1, v1 = u0 + L + 2 * m, v0 + Hb + 2 * m
            a = math.radians(ang)
            ux, uy, vx, vy = math.cos(a), math.sin(a), -math.sin(a), math.cos(a)
            if back != rec.bool("mirror"):
                ux, vx = -ux, -vx
            cor = [(x + u * ux + v * vx, y + u * uy + v * vy) for u, v in ((u0, v0), (u1, v0), (u1, v1), (u0, v1))]
            px = [((cx * MIL - x0) * dpmm, (y1 - cy * MIL) * dpmm) for cx, cy in cor]
            c0, c1 = max(0, int(min(p[0] for p in px)) - 1), min(W, int(max(p[0] for p in px)) + 2)
            r0, r1 = max(0, int(min(p[1] for p in px)) - 1), min(H, int(max(p[1] for p in px)) + 2)
            if c1 <= c0 or r1 <= r0:
                continue
            mask = Image.new("1", (c1 - c0, r1 - r0), 0)
            ImageDraw.Draw(mask).polygon([(p[0] - c0, p[1] - r0) for p in px], fill=1)
            rr, cc = np.nonzero(np.asarray(mask, bool))
            gx = x0 + (cc + c0 + 0.5) / dpmm - x * MIL
            gy = y1 - (rr + r0 + 0.5) / dpmm - y * MIL
            U, V = gx * ux + gy * uy, gx * vx + gy * vy

            def ext(M):
                sel = M[r0:r1, c0:c1][rr, cc]
                return (U[sel].min(), U[sel].max(), V[sel].min(), V[sel].max()) if sel.any() else None
            ea, kb = ext(A), ext(B)
            if ea is None or kb is None:
                missing.append(f"{label} on {klayer}: {'EasyEDA' if ea is None else 'KiCad'} drew nothing there")
                continue
            du = (kb[0] + kb[1]) / 2 - (ea[0] + ea[1]) / 2
            dv = (kb[2] + kb[3]) / 2 - (ea[2] + ea[3]) / 2
            rows.append((max(abs(du), abs(dv)), du, dv, (kb[1] - kb[0]) / max(ea[1] - ea[0], 1e-6),
                         (kb[3] - kb[2]) / max(ea[3] - ea[2], 1e-6), label, klayer))
    return sorted(rows, reverse=True), missing


def _drill_file(path: str) -> list:
    """[(x, y, diameter, x2, y2)] of an Excellon file, mm; slots have a second point."""
    tools, cur, hits, scale = {}, None, [], 1.0
    for line in open(path, errors="replace"):
        line = line.strip()
        if line.startswith("INCH"):
            scale = 25.4
        m = re.match(r"T(\d+)C([\d.]+)", line)
        if m:
            tools[int(m.group(1))] = float(m.group(2)) * scale
            continue
        m = re.match(r"T(\d+)$", line)
        if m:
            cur = int(m.group(1))
            continue
        m = re.match(r"X(-?[\d.]+)Y(-?[\d.]+)(?:G85X(-?[\d.]+)Y(-?[\d.]+))?$", line)
        if m and cur in tools:
            x, y = float(m.group(1)) * scale, float(m.group(2)) * scale
            x2, y2 = (float(m.group(3)) * scale, float(m.group(4)) * scale) if m.group(3) else (x, y)
            hits.append((x, y, tools[cur], x2, y2))
    return hits


def drills(pcb: str, ee_dir: str, dx: float, dy: float) -> dict:
    """{"PTH" / "NPTH": (holes in EasyEDA's files, in KiCad's, only EasyEDA, only KiCad)}."""
    td = tempfile.mkdtemp()
    subprocess.run(["kicad-cli", "pcb", "export", "drill", "--format", "excellon", "--excellon-units", "mm",
                    "--excellon-zeros-format", "decimal", "--excellon-separate-th", "--excellon-oval-format",
                    "alternate", "-o", td + "/", pcb], capture_output=True, text=True)
    k, e = {"PTH": [], "NPTH": []}, {"PTH": [], "NPTH": []}
    for f in glob.glob(os.path.join(td, "*.drl")):
        k["NPTH" if "NPTH" in os.path.basename(f) else "PTH"] += _drill_file(f)
    for f in glob.glob(os.path.join(ee_dir, "Drill_*.DRL")):
        if not f.endswith("_Via.DRL"):  # that file repeats the vias of the plated one
            e["NPTH" if "NPTH" in os.path.basename(f) else "PTH"] += _drill_file(f)

    def key(h, ox=0.0, oy=0.0):
        ln = math.hypot(h[3] - h[0], h[4] - h[1])
        return (round(((h[0] + h[3]) / 2 - ox) / 0.02), round(((h[1] + h[4]) / 2 - oy) / 0.02), round(h[2] / 0.011),
                round(ln / 0.05))
    out = {}
    for kind in ("PTH", "NPTH"):
        pool = collections.Counter(key(h, dx, dy) for h in k[kind])
        miss = 0
        for a in (key(h) for h in e[kind]):
            c = next((c for c in ((a[0] + i, a[1] + j, a[2] + d, a[3]) for i in (-1, 0, 1) for j in (-1, 0, 1)
                                  for d in (-1, 0, 1)) if pool.get(c, 0) > 0), None)
            if c:
                pool[c] -= 1
            else:
                miss += 1
        out[kind] = (len(e[kind]), len(k[kind]), miss, sum(n for n in pool.values() if n > 0))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("project")
    ap.add_argument("gerber", help="EasyEDA's Gerber export of the board (.zip or the unpacked folder)")
    ap.add_argument("--board", help="board of the project (needed when it has several)")
    ap.add_argument("--out", help="where to put the translation and the comparison (default: a temporary folder)")
    ap.add_argument("--dpmm", type=float, default=40, help="pixels per mm of the layer comparison")
    a = ap.parse_args()
    out = a.out or tempfile.mkdtemp(prefix="gerber-check-")
    ee_dir = a.gerber
    if os.path.isfile(ee_dir):
        ee_dir = os.path.join(out, "easyeda-gerber")
        zipfile.ZipFile(a.gerber).extractall(ee_dir)
    proj = load(a.project)
    boards = [b for b in proj.boards if b.pcb and (not a.board or b.name == a.board)]
    if len(boards) != 1:
        print(f"pick one board with --board: {[b.name for b in proj.boards if b.pcb]}")
        return 2
    b = boards[0]
    reps = convert_project(a.project, os.path.join(out, "kicad"), Options(boards=[b.name]), log=lambda *_: None)
    pcb = os.path.join(reps[0].out_dir, proj_name(b.name) + ".kicad_pcb")
    res = compare(pcb, ee_dir, os.path.join(out, "layers"), a.dpmm, quiet=True)
    dx, dy = (float(v) for v in open(os.path.join(out, "layers", "edge.origin")).read().split()[3:5])
    print(f"{b.name}: {pcb}")
    print("layers differing (share of the pixels either side has):")
    for k, v in res.items():
        print(f"  {LAYERS[k][1]:10} {v['mismatch']:6.2f} %   only EasyEDA {v['only_ee'] / a.dpmm ** 2:8.2f} mm2, "
              f"only KiCad {v['only_kicad'] / a.dpmm ** 2:8.2f} mm2")
    rows, missing = texts(proj, proj.pcbs[b.pcb], pcb, ee_dir, os.path.join(out, "text"), dx, dy)
    if rows:
        arr = np.array([r[1:5] for r in rows])
        print(f"silkscreen texts: {len(rows)} measured, {sum(1 for r in rows if r[0] > 0.1)} off by more than 0.1 mm; "
              f"along the text rms {np.sqrt((arr[:, 0] ** 2).mean()):.3f} max {np.abs(arr[:, 0]).max():.3f} mm, "
              f"across rms {np.sqrt((arr[:, 1] ** 2).mean()):.3f} max {np.abs(arr[:, 1]).max():.3f} mm")
        for r in rows[:5]:
            print(f"  {r[5]:28} {r[6]}  along {r[1]:+.3f}  across {r[2]:+.3f} mm  length x{r[3]:.3f}  height x{r[4]:.3f}")
    for m in missing[:10]:
        print("  " + m)
    for kind, (ne, nk, miss, extra) in drills(pcb, ee_dir, dx, dy).items():
        print(f"drills {kind}: EasyEDA {ne}, KiCad {nk}, only EasyEDA {miss}, only KiCad {extra}")
    drc = reps[0].checks.get("drc") or {}
    c = collections.Counter((v["severity"], v["type"]) for v in drc.get("violations", []))
    print(f"KiCad DRC: {len(drc.get('unconnected_items', []))} unconnected, errors "
          f"{dict((t, n) for (s, t), n in c.items() if s == 'error')}, warnings "
          f"{dict((t, n) for (s, t), n in c.items() if s != 'error')}")
    print(f"pictures and files: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
