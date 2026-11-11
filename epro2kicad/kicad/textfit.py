"""Place KiCad text where EasyEDA prints it.

EasyEDA Pro's line fonts and KiCad's stroke font are both Hershey fonts, with different glyph widths and their own
layout rules.  Both layouts are reproduced here from the programs' own code (EasyEDA: `Io` / `dN` / `pN` in its PCB
worker; KiCad: common/font/stroke_font.cpp and font.cpp) with the glyph extents of kicad/glyphs.py, and the KiCad
text is sized and placed so that its ink covers EasyEDA's.

EasyEDA, default font, size F and stroke width t: glyphs are drawn in a 32 unit cell of (F - t) / 32 per unit with
the cap line 28 and the baseline 7 units above its bottom; the first pen position is t / 2 right and above the
corner of the text box; a glyph is followed by t + 0.15 (F - t), a space is 0.7 (F - t) wide, lines are F apart.
The box is as wide as the ink and F high per line, and hangs off the anchor as `origin` says.
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass

from .glyphs import EE_DEFAULT, EE_FONTS, NEWSTROKE

LINE_FONTS = ("default", "default2", "Serif", "Sans Serif")  # EasyEDA draws these itself, as strokes
WIDE = (31, 2, 28, -22, 3)  # KiCad's CJK / full-width glyphs, like a NEWSTROKE entry
WIDE_TOP = 1.143  # their top above the baseline / cap height (measured from kicad-cli output)
# KiCad's stroke font layout
K_INTERLINE = 1.68 * 0.9583  # line pitch / text height at line spacing 1 (font_metrics.h, STROKE_FONT::GetInterline)
K_FIRST_LINE = 1.17  # share of the text height the first line counts for when centring (FONT::getLinePositions)
K_PEN_UP = 0.052  # the text sits this share of the pen width higher (same place)
K_PEN_RIGHT = 1 / 1.52  # left-justified text starts this share of the pen width right of its anchor
K_MAX_PEN = 0.25  # the pen is clamped to this share of the smaller text size (ClampTextPenSize)
# TrueType text without EasyEDA's rendering of it (measured on SAO UI, Noto and YaHei renderings in projects)
TTF_CAP = 0.78  # cap height / fontSize
TTF_BASE = 0.17  # baseline above the bottom of the text box / fontSize
TTF_LINE = 1.2  # text box height / fontSize
TTF_WIDTH = 0.9  # ink length / KiCad's at the same cap height


@dataclass
class Fit:
    x: float  # anchor of the KiCad text, EasyEDA coordinates (mil)
    y: float
    w: float  # KiCad text width and height (mil)
    h: float
    text: str  # what to write: EasyEDA's empty first and last lines carry no ink
    hjust: str = ""  # "" = centred on the anchor, "left" for several lines
    line_spacing: float = 1.0


def _align(origin: str | None) -> tuple[float, float]:
    """Where the anchor sits in the text box: (0 left .. 1 right, 0 bottom .. 1 top)."""
    h_al, v_al = (origin or "LEFT_BOTTOM").upper().split("_", 1) if "_" in (origin or "") else ("LEFT", "BOTTOM")
    return {"LEFT": 0.0, "RIGHT": 1.0}.get(h_al, 0.5), {"BOTTOM": 0.0, "TOP": 1.0}.get(v_al, 0.5)


def ee_layout(text: str, font: float, stroke: float, family: str = "default"):
    """EasyEDA's layout of line-font text: (box width, box height, lines), x right and y up from the bottom-left
    corner of the text box.  A line is None (no ink) or (x0, x1, y0, y1, baseline) of its strokes' centre lines."""
    rows = text.split("\n")
    n = len(rows)
    if family == "default" or family not in EE_FONTS:
        glyphs, gaps, gap0, space = EE_DEFAULT, None, 0.0, 0.0
        size, x_start, y0, pitch = font - stroke, stroke / 2, stroke / 2, font
    else:
        glyphs, gaps, gap0, space = EE_FONTS[family]
        size, x_start, y0, pitch = font, 0.0, 0.0, font + stroke
    m = size / 32
    lines, x_max, y_max = [], 0.0, 0.0
    for i, row in enumerate(rows):
        bottom = font * (n - 1) - i * pitch + y0  # of this line's glyph cell
        pen, ink = x_start, None
        for c in row:
            if c == " ":
                pen += 0.7 * size if gaps is None else space * m
                continue
            g = glyphs.get(c)
            if g is None:  # EasyEDA draws nothing for a character its font doesn't have
                continue
            a, b = pen + m * g[0], pen + m * g[1]
            lo, hi = bottom + m * (16 - g[3]), bottom + m * (16 - g[2])
            ink = (a, b, lo, hi) if ink is None else (min(ink[0], a), max(ink[1], b), min(ink[2], lo), max(ink[3], hi))
            pen = max(pen, b) + (stroke + 0.15 * size if gaps is None else gaps.get(c, gap0) * m)
        if ink is None:
            lines.append(None)
            continue
        lines.append(ink + (bottom + 7 * m,))
        x_max, y_max = max(x_max, ink[1]), max(y_max, ink[3])
    width = x_max + stroke / 2
    height = font * n if gaps is None else y_max + stroke / 2
    return width, height, lines


def _kglyph(c: str):
    g = NEWSTROKE.get(c)
    if g is None:
        g = WIDE if unicodedata.east_asian_width(c) in "WF" else NEWSTROKE["?"]
    return g


def kicad_line(row: str):
    """KiCad's layout of one line in 1/21 of the text size: (advance, ink x0, ink x1, ink y0, ink y1), x from the
    start of the line and y down from the pen position (baseline at 1, cap line at -20); ink is None without any."""
    pen, ink = 0, None
    for c in row:
        adv, x0, x1, y0, y1 = _kglyph(c)
        if c != " " and (x0, x1, y0, y1) != (0, 0, 0, 0):
            a, b = pen + x0, pen + x1
            ink = (a, b, y0, y1) if ink is None else (min(ink[0], a), max(ink[1], b), min(ink[2], y0), max(ink[3], y1))
        pen += adv
    return (pen,) + (ink or (None, None, None, None))


def fit(text: str, x: float, y: float, angle: float, origin: str | None, font: float, stroke: float, back: bool,
        family: str = "default") -> Fit:
    """EasyEDA line-font text (anchor, fontSize and stroke width in mil, Y up, angle in degrees) -> the KiCad stroke
    font text that covers the same ink (centred on its anchor; left-justified when it has several lines)."""
    font = font if font > 0 else 45.0
    stroke = max(0.0, min(stroke, font * 0.9))
    bw, bh, lines = ee_layout(text, font, stroke, family)
    fx, fy = _align(origin)
    inked = [i for i, ln in enumerate(lines) if ln is not None]
    cap = 21 * ((font - stroke) if family == "default" or family not in EE_FONTS else font) / 32
    if not inked:  # nothing EasyEDA draws: keep the text, at the anchor
        px, py = _place(x, y, angle, back, (0.5 - fx) * bw, (0.5 - fy) * bh)
        return Fit(px, py, cap, cap, text)
    rows = text.split("\n")
    first, last = inked[0], inked[-1]
    krows = rows[first:last + 1]
    kl = [kicad_line(r) for r in krows]
    # the longest line decides the width
    j = max(inked, key=lambda i: lines[i][1] - lines[i][0])
    e, k = lines[j], kl[j - first]
    h = cap
    base = lines[first][4]  # of the first line that is written
    if len(krows) == 1 and k[1] is not None and not any(c.isalnum() for c in krows[0]):
        # signs only ("+", "-", "->"): they sit at other heights in the two fonts, so match the ink itself
        e_h, k_h = e[3] - e[2], k[4] - k[3]
        if e_h > 1e-6 and k_h > 0:
            h = min(max(e_h * 21 / k_h, 0.5 * cap), 2 * cap)
        base = (e[2] + e[3]) / 2 - (1 - (k[3] + k[4]) / 2) * h / 21
    e_len = e[1] - e[0]
    k_len = (k[2] - k[1]) / 21 if k[1] is not None else 0.0
    # KiCad draws with a thinner pen when EasyEDA's is over a quarter of the text size.  The text then grows by the
    # difference, so that the outer edges of the ink stay where they are; the pen that comes out of it:
    fits = e_len > 1e-6 and k_len > 1e-6
    q = K_MAX_PEN
    pen = min([stroke, q * (h + stroke) / (1 + q)] + ([q * (e_len + stroke) / (k_len + q)] if fits else []))
    h += stroke - pen
    w = (e_len + stroke - pen) / k_len if fits else h  # no length to match ("I", "|"): KiCad's own shape
    w = min(max(w, 0.4 * h), 2.5 * h)
    pen = min(stroke, K_MAX_PEN * min(w, h))
    pitch = font if family == "default" or family not in EE_FONTS else font + stroke
    n = len(krows)
    # across: KiCad centres the block of lines on the anchor; its first baseline has to be EasyEDA's
    v = base + h / 21 + h - K_PEN_UP * pen - (K_FIRST_LINE * h + (n - 1) * pitch) / 2
    # along: the ink of the longest line where EasyEDA has it
    centre = (e[0] + e[1]) / 2
    if n == 1:
        u = centre - w * (k[1] + k[2] - k[0]) / 42 if k[1] is not None else centre
    else:
        u = centre - K_PEN_RIGHT * pen - (w * (k[1] + k[2]) / 42 if k[1] is not None else 0.0)
    px, py = _place(x, y, angle, back, u - fx * bw, v - fy * bh)
    return Fit(px, py, w, h, "\n".join(krows), "" if n == 1 else "left",
               1.0 if n == 1 else pitch / (K_INTERLINE * h))


def fit_ttf(text: str, x: float, y: float, angle: float, origin: str | None, font: float, back: bool,
            box=None, stroke: float = 0.0) -> Fit:
    """Like fit, for text EasyEDA draws with a TrueType font.  `box` is EasyEDA's rendering of it from the project
    (x0, x1, baseline, top, box w, box h in mil, see easyeda.project._ttf_boxes); without it the font is assumed
    to be about as wide as KiCad's.  `stroke`: pen width the KiCad text gets."""
    line = text.split("\n")[0] or " "
    fx, fy = _align(origin)
    k = kicad_line(line)
    k_ink = (k[2] - k[1]) / 21 if k[1] is not None else 0.0
    k_shift = (k[1] + k[2] - k[0]) / 42 if k[1] is not None else 0.0
    if box:
        x0, x1, base, top, bw, bh = box
    else:
        cap = TTF_CAP * font
        ink = k_ink * cap * TTF_WIDTH
        bw, bh, base = ink + 0.1 * font, TTF_LINE * font, TTF_BASE * font
        x0, x1, top = 0.05 * font, 0.05 * font + ink, base + cap
    rise = WIDE_TOP if any(unicodedata.east_asian_width(c) in "WF" for c in line) else 1.0  # ink top / cap height
    h = max(top - base, 1e-3) / rise
    w = (x1 - x0) / k_ink if k_ink > 1e-6 else h
    # the text box (bw x bh) hangs off the anchor as `origin` says; the ink sits in it where EasyEDA drew it
    u = -fx * bw + (x0 + x1) / 2 - k_shift * w
    # KiCad's cap box is centred (K_FIRST_LINE / 2 - 1/2 - 1/21) of the height above the anchor
    v = -fy * bh + (base + top) / 2 - (K_FIRST_LINE / 2 - 0.5 - 1 / 21 + (rise - 1) / 2) * h \
        - K_PEN_UP * min(stroke, K_MAX_PEN * min(w, h))
    px, py = _place(x, y, angle, back, u, v)
    return Fit(px, py, w, h, line)


def _place(x: float, y: float, angle: float, back: bool, u: float, v: float) -> tuple[float, float]:
    """Anchor + offset u along / v across the text (mil, before rotation)."""
    a = math.radians(angle)
    ux, uy = math.cos(a), math.sin(a)
    vx, vy = -math.sin(a), math.cos(a)
    if back:  # bottom-side text is mirrored left-right as seen from the top
        ux, vx = -ux, -vx
    return x + u * ux + v * vx, y + u * uy + v * vy
