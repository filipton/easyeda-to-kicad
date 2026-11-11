"""Small Gerber (RS-274X) rasteriser for the dialects EasyEDA Pro and KiCad write.

    g = Gerber.load(path)         # g.ops (mm), g.bbox = (x0, y0, x1, y1)
    img = g.raster(x0, y1, W, H, dpmm)   # numpy bool, top-left pixel at (x0, y1) mm, Y up

Supported: FS/MO, apertures C R O P and macros (primitives 1, 4, 5, 20, 21 with expressions), G01/G02/G03 in
multi-quadrant mode, regions, D01/D02/D03 (also modal and G54Dnn), LPD/LPC.  Anything else raises or is counted
in g.ignored so that a silent mis-render can't happen.
"""
from __future__ import annotations

import math
import re
from collections import Counter

import numpy as np
from PIL import Image, ImageDraw

ARC_ERR = 0.002  # mm, chord error when flattening arcs
_CMD = re.compile(r"^(?:G(\d+))?(?:X([+-]?\d+))?(?:Y([+-]?\d+))?(?:I([+-]?\d+))?(?:J([+-]?\d+))?(?:D(\d+))?$")


def _circle_pts(cx, cy, r, err=ARC_ERR):
    n = max(12, int(math.ceil(math.pi / math.acos(max(-1.0, 1 - err / r)))) if r > err else 12)
    return [(cx + r * math.cos(2 * math.pi * k / n), cy + r * math.sin(2 * math.pi * k / n)) for k in range(n)]


def _arc_pts(a, b, c, cw: bool):
    """Points from a to b around centre c (excluding a)."""
    r = math.hypot(a[0] - c[0], a[1] - c[1])
    if r < 1e-9:
        return [b]
    a0 = math.atan2(a[1] - c[1], a[0] - c[0])
    a1 = math.atan2(b[1] - c[1], b[0] - c[0])
    if abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9:
        sweep = -2 * math.pi if cw else 2 * math.pi
    elif cw:
        sweep = a1 - a0
        if sweep >= 0:
            sweep -= 2 * math.pi
    else:
        sweep = a1 - a0
        if sweep <= 0:
            sweep += 2 * math.pi
    step = 2 * math.acos(max(-1.0, 1 - ARC_ERR / r)) if r > ARC_ERR else math.pi / 4
    n = max(1, int(math.ceil(abs(sweep) / step)))
    pts = [(c[0] + r * math.cos(a0 + sweep * k / n), c[1] + r * math.sin(a0 + sweep * k / n)) for k in range(1, n)]
    return pts + [b]


def _rot(p, deg):
    if not deg:
        return p
    t = math.radians(deg)
    return (p[0] * math.cos(t) - p[1] * math.sin(t), p[0] * math.sin(t) + p[1] * math.cos(t))


def _eval(expr: str, var: dict) -> float:
    e = expr.strip().replace("x", "*").replace("X", "*")
    e = re.sub(r"\$(\d+)", lambda m: repr(float(var.get(int(m.group(1)), 0.0))), e)
    if not re.fullmatch(r"[0-9eE.+\-*/() ]*", e) or not e:
        raise ValueError(f"macro expression {expr!r}")
    return float(eval(e, {"__builtins__": {}}, {}))  # noqa: S307 - digits and operators only


class Gerber:
    def __init__(self, text: str):
        self.ops = []  # (kind "poly" | "stroke", pts, width, dark, section)
        self.section = ""  # EasyEDA marks what it plots: "G04 Text Start*" ... "G04 Text End*"
        self.ignored = Counter()
        self.apertures = {}  # code -> list of ("poly", pts) / ("circle", cx, cy, r) / ("clear-...")
        self.macros = {}
        self.circle_d = {}  # code -> diameter for stroking
        self._parse(text)
        xs, ys = [], []
        for op in self.ops:
            pad = op[2] / 2 if op[0] == "stroke" else 0.0
            for x, y in op[1]:
                xs += [x - pad, x + pad]
                ys += [y - pad, y + pad]
        self.bbox = (min(xs), min(ys), max(xs), max(ys)) if xs else None

    @classmethod
    def load(cls, path: str) -> "Gerber":
        with open(path, encoding="utf-8", errors="replace") as f:
            return cls(f.read())

    # ------------------------------------------------------------------ apertures
    def _define(self, code: int, name: str, params: list):
        shapes = []
        if name == "C":
            d = params[0]
            shapes.append(("poly", _circle_pts(0, 0, d / 2) if d > 0 else [], True))
            self.circle_d[code] = d
            if len(params) > 1 and params[1] > 0:
                shapes.append(("poly", _circle_pts(0, 0, params[1] / 2), False))
        elif name == "R":
            w, h = params[0], params[1]
            shapes.append(("poly", [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)], True))
            self.circle_d[code] = min(w, h)
        elif name == "O":
            w, h = params[0], params[1]
            r = min(w, h) / 2
            a, b = ((-(w / 2 - r), 0), (w / 2 - r, 0)) if w >= h else ((0, -(h / 2 - r)), (0, h / 2 - r))
            shapes.append(("stroke", [a, b], 2 * r, True))
            self.circle_d[code] = 2 * r
        elif name == "P":
            d, n = params[0], int(params[1])
            rot = params[2] if len(params) > 2 else 0.0
            shapes.append(("poly", [_rot((d / 2 * math.cos(2 * math.pi * k / n), d / 2 * math.sin(2 * math.pi * k / n)),
                                         rot) for k in range(n)], True))
        elif name in self.macros:
            var = {i + 1: v for i, v in enumerate(params)}
            for prim in self.macros[name]:
                if re.match(r"^\$\d+\s*=", prim):
                    k, e = prim.split("=", 1)
                    var[int(k.strip()[1:])] = _eval(e, var)
                    continue
                f = [x for x in prim.split(",")]
                kind = int(_eval(f[0], var))
                v = [_eval(x, var) for x in f[1:]]
                if kind == 1:  # circle: exposure, diameter, cx, cy [, rotation]
                    c = _rot((v[2], v[3]), v[4] if len(v) > 4 else 0.0)
                    shapes.append(("poly", _circle_pts(c[0], c[1], v[1] / 2), v[0] != 0))
                elif kind == 4:  # outline: exposure, n, points..., rotation
                    n = int(v[1])
                    pts = [(v[2 + 2 * i], v[3 + 2 * i]) for i in range(n + 1)]
                    rot = v[2 + 2 * (n + 1)] if len(v) > 2 + 2 * (n + 1) else 0.0
                    shapes.append(("poly", [_rot(p, rot) for p in pts], v[0] != 0))
                elif kind == 5:  # polygon: exposure, vertices, cx, cy, diameter, rotation
                    n, d = int(v[1]), v[4]
                    pts = [(v[2] + d / 2 * math.cos(2 * math.pi * k / n), v[3] + d / 2 * math.sin(2 * math.pi * k / n))
                           for k in range(n)]
                    shapes.append(("poly", [_rot(p, v[5] if len(v) > 5 else 0.0) for p in pts], v[0] != 0))
                elif kind == 20:  # vector line: exposure, width, x0, y0, x1, y1, rotation (square ends)
                    w, (x0, y0, x1, y1) = v[1], v[2:6]
                    ln = math.hypot(x1 - x0, y1 - y0) or 1.0
                    nx, ny = -(y1 - y0) / ln * w / 2, (x1 - x0) / ln * w / 2
                    pts = [(x0 + nx, y0 + ny), (x1 + nx, y1 + ny), (x1 - nx, y1 - ny), (x0 - nx, y0 - ny)]
                    shapes.append(("poly", [_rot(p, v[6] if len(v) > 6 else 0.0) for p in pts], v[0] != 0))
                elif kind == 21:  # centre line: exposure, w, h, cx, cy, rotation
                    w, h, cx, cy = v[1:5]
                    pts = [(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2), (cx + w / 2, cy + h / 2),
                           (cx - w / 2, cy + h / 2)]
                    shapes.append(("poly", [_rot(p, v[5] if len(v) > 5 else 0.0) for p in pts], v[0] != 0))
                else:
                    raise NotImplementedError(f"macro primitive {kind} in {name}")
        else:
            raise NotImplementedError(f"aperture template {name}")
        self.apertures[code] = shapes

    # ------------------------------------------------------------------ parser
    def _parse(self, text: str):
        dec = 5
        unit = 1.0
        cur = (0.0, 0.0)
        mode = 1  # 1 linear, 2 cw, 3 ccw
        dcode = 2
        ap = None
        dark = True
        region = False
        contours, contour = [], []
        stroke = []  # running polyline of consecutive D01 with one aperture

        def flush():
            nonlocal stroke
            if len(stroke) > 1 and ap in self.circle_d:
                self.ops.append(("stroke", stroke, self.circle_d[ap] * 1.0, dark, self.section))
            elif len(stroke) > 1:
                self.ignored["draw with a non-round aperture"] += 1
            stroke = []

        i, n = 0, len(text)
        while i < n:
            ch = text[i]
            if ch in " \r\n\t":
                i += 1
                continue
            if ch == "%":
                j = text.find("%", i + 1)
                block = text[i + 1:j if j >= 0 else n]
                i = (j if j >= 0 else n) + 1
                parts = [p.strip() for p in block.replace("\n", "").replace("\r", "").split("*") if p.strip()]
                if not parts:
                    continue
                head = parts[0]
                if head.startswith("FS"):
                    m = re.match(r"FS([LT])([AI])X(\d)(\d)Y(\d)(\d)", head)
                    if not m or m.group(1) != "L" or m.group(2) != "A":
                        raise NotImplementedError(head)
                    dec = int(m.group(4))
                elif head.startswith("MO"):
                    unit = 25.4 if head[2:4] == "IN" else 1.0
                elif head.startswith("AM"):
                    self.macros[head[2:]] = [p for p in parts[1:] if not p.startswith("0 ") and p != "0"]
                elif head.startswith("AD"):
                    m = re.match(r"ADD(\d+)([^,]+)(?:,(.*))?$", head)
                    params = [float(x) * unit for x in m.group(3).split("X")] if m.group(3) else []
                    if m.group(2) == "P" and len(params) > 1:  # vertex count and rotation carry no unit
                        params[1:3] = [p / unit for p in params[1:3]]
                    self._define(int(m.group(1)), m.group(2), params)
                elif head.startswith("LP"):
                    flush()
                    dark = head[2] == "D"
                elif head[:2] in ("TF", "TA", "TO", "TD", "LN", "IN", "IP", "OF"):
                    pass
                elif head[:2] in ("LM", "LR", "LS", "SR", "AB", "MI", "SF", "AS", "IR"):
                    if head not in ("LMN", "LR0", "LR0.0", "LS1", "LS1.0", "SRX1Y1I0J0", "SR", "MIA0B0", "SFA1B1",
                                    "SFA1.0B1.0", "ASAXBY", "IR0"):
                        raise NotImplementedError(head)
                else:
                    self.ignored[head[:2]] += 1
                continue
            j = text.find("*", i)
            if j < 0:
                break
            cmd = text[i:j].strip().replace("\n", "").replace("\r", "")
            i = j + 1
            if not cmd or cmd.startswith("G04") or cmd.startswith("G4 "):
                m = re.match(r"G04 (\w+) (Start|End)$", cmd)
                if m:
                    flush()
                    self.section = m.group(1) if m.group(2) == "Start" else ""
                continue
            if cmd in ("M02", "M00", "M2"):
                break
            m = _CMD.match(cmd)
            if not m:
                self.ignored[cmd[:6]] += 1
                continue
            g, x, y, ii, jj, d = m.groups()
            if g is not None:
                g = int(g)
                if g in (1, 2, 3):
                    mode = g
                elif g == 36:
                    flush()
                    region, contours, contour = True, [], []
                elif g == 37:
                    if len(contour) > 2:
                        contours.append(contour)
                    for c in contours:
                        self.ops.append(("poly", c, 0.0, dark, self.section))
                    region, contours, contour = False, [], []
                elif g == 74:
                    raise NotImplementedError("single-quadrant arcs (G74)")
                elif g in (75, 54, 55, 70, 71, 90, 91):
                    if g == 70:
                        unit = 25.4
                    elif g == 91:
                        raise NotImplementedError("incremental coordinates")
                else:
                    self.ignored[f"G{g}"] += 1
            if d is not None and int(d) >= 10:
                flush()
                ap = int(d)
                if ap not in self.apertures:
                    raise ValueError(f"undefined aperture D{ap}")
                continue
            if x is None and y is None and d is None:
                continue
            if d is not None:
                dcode = int(d)
            nx = int(x) / 10 ** dec * unit if x is not None else cur[0]
            ny = int(y) / 10 ** dec * unit if y is not None else cur[1]
            new = (nx, ny)
            if dcode == 1:
                if mode == 1:
                    pts = [new]
                else:
                    c = (cur[0] + (int(ii) / 10 ** dec * unit if ii else 0.0), cur[1] + (int(jj) / 10 ** dec * unit if jj else 0.0))
                    pts = _arc_pts(cur, new, c, mode == 2)
                if region:
                    if not contour:
                        contour = [cur]
                    contour += pts
                else:
                    if not stroke:
                        stroke = [cur]
                    stroke += pts
            elif dcode == 2:
                if region:
                    if len(contour) > 2:
                        contours.append(contour)
                    contour = []
                else:
                    flush()
            elif dcode == 3:
                flush()
                for kind, pts, *rest in self.apertures.get(ap, []):
                    if kind == "poly":
                        if pts:
                            self.ops.append(("poly", [(px + nx, py + ny) for px, py in pts], 0.0, dark == rest[0], self.section))
                    else:  # stroke (obround)
                        self.ops.append(("stroke", [(px + nx, py + ny) for px, py in pts], rest[0], dark == rest[1], self.section))
            cur = new
        flush()

    # ------------------------------------------------------------------ raster
    def raster(self, x0: float, y1: float, W: int, H: int, dpmm: float, sections=None) -> np.ndarray:
        """`sections`: only the ops EasyEDA wrote in these sections (e.g. {"Text"})."""
        img = Image.new("L", (W, H), 0)
        dr = ImageDraw.Draw(img)
        T = lambda p: ((p[0] - x0) * dpmm, (y1 - p[1]) * dpmm)  # noqa: E731
        for op in self.ops:
            kind, pts, width, dark, sec = op
            if sections is not None and sec not in sections:
                continue
            fill = 255 if dark else 0
            if kind == "poly":
                if len(pts) >= 3:
                    dr.polygon([T(p) for p in pts], fill=fill)
                continue
            r = width / 2 * dpmm
            q = [T(p) for p in pts]
            if r <= 0.35:  # thinner than a pixel: a hairline
                dr.line(q, fill=fill, width=1)
                continue
            for a, b in zip(q, q[1:]):
                ln = math.hypot(b[0] - a[0], b[1] - a[1])
                if ln > 1e-9:
                    nx, ny = -(b[1] - a[1]) / ln * r, (b[0] - a[0]) / ln * r
                    dr.polygon([(a[0] + nx, a[1] + ny), (b[0] + nx, b[1] + ny), (b[0] - nx, b[1] - ny),
                                (a[0] - nx, a[1] - ny)], fill=fill)
            for c in q:
                dr.ellipse([c[0] - r, c[1] - r, c[0] + r, c[1] + r], fill=fill)
        return np.asarray(img) > 127
