"""Place KiCad text where EasyEDA prints it.

EasyEDA's default PCB font is Hershey Simplex; KiCad uses its own stroke font with other glyph widths.  Measured
from EasyEDA's own Gerber (a whole board of designators): glyphs are the Hershey shapes with a cap height of
0.5695 x fontSize, characters follow each other at ink width + 0.264 x fontSize, the anchor is the corner of a
fontSize-high line box with the outer edge of the ink starting right at it and the baseline 0.2555 x fontSize
above its bottom.
KiCad text is centre-justified and stretched so its ink covers the same box.
"""

from __future__ import annotations

import math
import unicodedata

from .glyphs import HERSHEY, NEWSTROKE

WIDE = (31, 2, 28)  # KiCad's CJK / full-width glyphs: (advance, ink start, ink end), 21 = text width
WIDE_TOP = 1.143  # their top above the baseline / cap height (measured from kicad-cli output)
CAP = 0.5695  # cap height / fontSize
UNIT = CAP / 21  # one Hershey unit / fontSize
GAP = 0.264  # space between the ink of two characters / fontSize
BASE = 0.2555  # baseline above the bottom of the line box / fontSize
KICAD_CAP_CENTRE = 0.0498  # KiCad centre-justified: cap box centre above the anchor / text height
DEFAULT_W = 0.55  # KiCad width / fontSize when the text can't be fitted
# TrueType text without EasyEDA's rendering of it (measured on SAO UI, Noto and YaHei renderings in projects)
TTF_CAP = 0.78  # cap height / fontSize
TTF_BASE = 0.17  # baseline above the bottom of the text box / fontSize
TTF_LINE = 1.2  # text box height / fontSize
TTF_WIDTH = 0.9  # ink length / KiCad's at the same cap height


def _ee_ink(text: str) -> float:
    """Length of EasyEDA's ink (stroke centrelines) / fontSize."""
    widths = []
    for c in text:
        if c == " ":
            widths.append(12 * UNIT - GAP)  # Hershey space advance
        elif c in HERSHEY:
            a, b = HERSHEY[c]
            widths.append((b - a) * UNIT)
        else:
            widths.append(10 * UNIT)
    return sum(widths) + GAP * (len(widths) - 1)


def _kicad_ink(text: str) -> tuple[float, float]:
    """(ink length, ink centre offset of a centre-justified text) in units of the KiCad text width."""
    g = [NEWSTROKE.get(c) or (WIDE if unicodedata.east_asian_width(c) in "WF" else NEWSTROKE["?"]) for c in text]
    ink = sum(x[0] for x in g[:-1]) + g[-1][2] - g[0][1]
    shift = (g[0][1] - (g[-1][0] - g[-1][2])) / 2  # left bearing of the first vs right bearing of the last
    return ink / 21, shift / 21


def fit(text: str, x: float, y: float, angle: float, origin: str | None, font: float, stroke: float, back: bool):
    """EasyEDA text (anchor x/y, fontSize and stroke width in mil, Y up, angle in degrees) ->
    (anchor x, anchor y in mil, KiCad width, KiCad height in mil) for a centre-justified KiCad text."""
    text = text.split("\n")[0] or " "
    h_al, v_al = (origin or "LEFT_BOTTOM").upper().split("_", 1) if "_" in (origin or "") else ("LEFT", "BOTTOM")
    ee = _ee_ink(text)
    k_ink, k_shift = _kicad_ink(text)
    w = ee / k_ink if k_ink > 1e-6 and ee > 1e-6 else DEFAULT_W
    h = CAP
    # centre of EasyEDA's ink along the text, from the anchor (fontSize units)
    half = ee / 2 + stroke / 2 / font  # the outer edge of the stroke touches the anchor
    u = {"LEFT": half, "RIGHT": -half}.get(h_al, 0.0) - k_shift * w
    # centre of the cap box across the text; the line box is fontSize high
    cap_mid = BASE + CAP / 2
    v = {"BOTTOM": cap_mid, "TOP": cap_mid - 1}.get(v_al, cap_mid - 0.5) - KICAD_CAP_CENTRE * h
    px, py = _place(x, y, angle, back, u * font, v * font)
    return px, py, w * font, h * font


def fit_ttf(text: str, x: float, y: float, angle: float, origin: str | None, font: float, back: bool, box=None):
    """Like fit, for text EasyEDA draws with a TrueType font.  `box` is EasyEDA's rendering of it from the project
    (x0, x1, baseline, top, box w, box h in mil, see easyeda.project._ttf_boxes); without it the font is assumed
    to be about as wide as KiCad's."""
    text = text.split("\n")[0] or " "
    h_al, v_al = (origin or "LEFT_BOTTOM").upper().split("_", 1) if "_" in (origin or "") else ("LEFT", "BOTTOM")
    k_ink, k_shift = _kicad_ink(text)
    if box:
        x0, x1, base, top, bw, bh = box
    else:
        cap = TTF_CAP * font
        ink = k_ink * cap * TTF_WIDTH
        bw, bh, base = ink + 0.1 * font, TTF_LINE * font, TTF_BASE * font
        x0, x1, top = 0.05 * font, 0.05 * font + ink, base + cap
    rise = WIDE_TOP if any(unicodedata.east_asian_width(c) in "WF" for c in text) else 1.0  # ink top / cap height
    h = max(top - base, 1e-3) / rise
    w = (x1 - x0) / k_ink if k_ink > 1e-6 else h
    # the text box (bw x bh) hangs off the anchor as `origin` says; the ink sits in it where EasyEDA drew it
    u = {"LEFT": 0.0, "RIGHT": -bw}.get(h_al, -bw / 2) + (x0 + x1) / 2 - k_shift * w
    v = {"BOTTOM": 0.0, "TOP": -bh}.get(v_al, -bh / 2) + (base + top) / 2 - (KICAD_CAP_CENTRE + (rise - 1) / 2) * h
    px, py = _place(x, y, angle, back, u, v)
    return px, py, w, h


def _place(x: float, y: float, angle: float, back: bool, u: float, v: float) -> tuple[float, float]:
    """Anchor + offset u along / v across the text (mil, before rotation)."""
    a = math.radians(angle)
    ux, uy = math.cos(a), math.sin(a)
    vx, vy = -math.sin(a), math.cos(a)
    if back:  # bottom-side text is mirrored left-right as seen from the top
        ux, vx = -ux, -vx
    return x + u * ux + v * vx, y + u * uy + v * vy
