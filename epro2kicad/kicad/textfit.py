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

from .glyphs import HERSHEY, NEWSTROKE

CAP = 0.5695  # cap height / fontSize
UNIT = CAP / 21  # one Hershey unit / fontSize
GAP = 0.264  # space between the ink of two characters / fontSize
BASE = 0.2555  # baseline above the bottom of the line box / fontSize
KICAD_CAP_CENTRE = 0.0498  # KiCad centre-justified: cap box centre above the anchor / text height
DEFAULT_W = 0.55  # KiCad width / fontSize when the text can't be fitted


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
    g = [NEWSTROKE.get(c, NEWSTROKE["?"]) for c in text]
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
    a = math.radians(angle)
    ux, uy = math.cos(a), math.sin(a)
    vx, vy = -math.sin(a), math.cos(a)
    if back:  # bottom-side text is mirrored left-right as seen from the top
        ux, vx = -ux, -vx
    return x + (u * ux + v * vx) * font, y + (u * uy + v * vy) * font, w * font, h * font
