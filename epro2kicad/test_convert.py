"""Self-check of the translation rules that were measured against EasyEDA's own output (no files needed).

    python3 -m epro2kicad.test_convert
"""

from . import geom
from .easyeda.records import Rec
from .easyeda.rules import parse_rules
from .kicad import textfit
from .validate import compare, identical, pcb_net_names

MIL = 0.0254


def close(a, b, tol, what):
    assert abs(a - b) <= tol, f"{what}: {a} != {b} (+-{tol})"


def text_layout():
    # the "+" of a board's bottom silkscreen, fontSize 236.2205 mil, stroke 31.4961 mil: EasyEDA's Gerber draws its
    # bar 2.92608 mm long, centred 2.99974 mm above the anchor, the stroke's outer edge on the anchor
    F, t = 236.2205, 31.4961
    w, h, lines = textfit.ee_layout("+", F, t)
    x0, x1, y0, y1, base = lines[0]
    close((x1 - x0) * MIL, 2.92608, 0.002, "ink length of +")
    close((y0 + y1) / 2 * MIL, 2.99974, 0.002, "centre of + above the box")
    close(x0, t / 2, 1e-9, "the pen starts half a stroke inside the box")
    close(h, F, 1e-9, "one line is fontSize high")
    close(w, x1 + t / 2, 1e-9, "the box ends half a stroke after the ink")
    # the cap height follows the stroke width: (F - t) * 21 / 32
    close(textfit.fit("AB", 0, 0, 0, "LEFT_BOTTOM", 100, 8, False).h, 92 * 21 / 32, 1e-9, "cap height, 100/8")
    close(textfit.fit("AB", 0, 0, 0, "LEFT_BOTTOM", 45, 3, False).h, 42 * 21 / 32, 1e-9, "cap height, 45/3")
    # KiCad draws with at most a quarter of the text size: a bolder EasyEDA text grows by what the pen loses
    bold = textfit.fit("AB", 0, 0, 0, "LEFT_BOTTOM", 100, 30, False)
    pen = textfit.K_MAX_PEN * min(bold.w, bold.h)
    assert pen < 30, bold
    close(bold.h + pen, 70 * 21 / 32 + 30, 1e-3, "outer height of bold text")
    # lines are fontSize apart and the first is on top: an empty last line lifts the text by one line
    one = textfit.fit("AB", 0, 0, 0, "LEFT_BOTTOM", 50, 5, False)
    two = textfit.fit("AB\n", 0, 0, 0, "LEFT_BOTTOM", 50, 5, False)
    close(two.y - one.y, 50, 1e-6, "trailing newline")
    assert two.text == "AB" and one.hjust == "" and two.line_spacing == 1.0, two
    multi = textfit.fit("AB\nCDE", 0, 0, 0, "LEFT_BOTTOM", 50, 5, False)
    assert multi.text == "AB\nCDE" and multi.hjust == "left", multi
    close(multi.line_spacing * textfit.K_INTERLINE * multi.h, 50, 1e-6, "KiCad's line pitch is EasyEDA's")
    # the box hangs off the anchor as the origin says; bottom-side text is mirrored
    left = textfit.fit("R10", 100, 0, 0, "LEFT_BOTTOM", 45, 6, False)
    right = textfit.fit("R10", 100, 0, 0, "RIGHT_BOTTOM", 45, 6, False)
    box = textfit.ee_layout("R10", 45, 6)[0]
    close(left.x - right.x, box, 1e-6, "LEFT vs RIGHT origin")
    back = textfit.fit("R10", 100, 0, 0, "LEFT_BOTTOM", 45, 6, True)
    close(left.x - 100, 100 - back.x, 1e-6, "mirrored about the anchor")
    # signs sit at other heights in KiCad's font: the ink is matched, not the baseline
    minus = textfit.fit("-", 0, 0, 0, "LEFT_BOTTOM", 100, 10, False)
    m = 90 / 32
    ink_y = 5 + 16 * m  # EasyEDA's bar
    k = textfit.kicad_line("-")
    k_y = minus.y - (minus.h / 21 + minus.h - textfit.K_PEN_UP * 10 - textfit.K_FIRST_LINE * minus.h / 2) \
        + (1 - k[3]) * minus.h / 21  # KiCad's bar: baseline of a centred line + the glyph's height above it
    close(k_y, ink_y, 1e-6, "the minus sign's bar")


def polygons():
    # a keyhole bridge that lands on a corner repeats it: cleaning up must not cut the corner off
    sq = [(0, 0), (10, 0), (10, 0), (10, 10), (0, 10)]
    close(abs(geom.signed_area(geom.simplify_collinear(sq))), 100, 1e-9, "repeated corner")
    hole = [(4, 4), (6, 4), (6, 6), (4, 6)]
    pts = geom.simplify_collinear(geom.fracture([(0, 0), (10, 0), (10, 10), (0, 10)], [hole]))
    close(abs(geom.signed_area(pts)), 96, 1e-9, "outline with a hole")
    line = [(0, 0), (5, 0), (5, 0), (10, 0), (10, 10), (0, 10)]
    assert len(geom.simplify_collinear(line)) == 4, geom.simplify_collinear(line)


def rules():
    # 3.x spacing table (13 rows): row 7 is the copper pour, row 6 solid fills, 11 the outline, 12 holes
    table = [[4.0157], [5.9843] * 2, [5.9843] * 3, [5.9843] * 4, [5.9843] * 5, [5.9843] * 6,
             [11.811] + [5.9843] * 6, [10] * 8, [5.9843] * 6 + [11.811, 10, 5.9843],
             [5.9843] * 7 + [10, 5.9843, 5.9843], [5.9843] * 7 + [10, 5.9843, 5.9843, 5.9843],
             [11.811] * 7 + [10] + [11.811] * 3, [6.9] * 7 + [10] + [6.9] * 4]
    R = parse_rules([Rec("RULE", "SAFE:a", {"ruleType": "SAFE", "name": "a", "isDefault": True,
                                             "context": {"unit": "mm", "safeSpacing": [{"content": table}]}})])
    safe = R.default("SAFE")
    assert safe.value("zone", "track") == 10 and safe.value("fill", "track") == 11.811, "pour row"
    assert safe.value("zone", "outline") == 10 and safe.value("hole", "track") == 6.9, "outline / hole rows"
    # a project migrated from the old format: every rule typed OTHER, told apart by name and shape
    old = [Rec("RULE", "OTHER:safeClearance", {"ruleType": "OTHER", "name": "safeClearance", "isDefault": True, "context": {
        "unit": "mil", "deviceClearance": [[5.9] * 8 + [9.8, 5.9]] + [[5.9] + [0] * 7 + [9.8, 0]] * 7
        + [[9.8] * 10, [5.9] + [0] * 7 + [9.8, 0]], "thru2SmdClearance": 0, "holeClearance": 11.8}}),
           Rec("RULE", "OTHER:trackWidth", {"ruleType": "OTHER", "name": "trackWidth", "isDefault": True, "context": {
               "unit": "mil", "deviceClearance": 5.9, "thru2SmdClearance": 6, "holeClearance": 100}}),
           Rec("RULE", "OTHER:CopperRegion", {"ruleType": "OTHER", "name": "CopperRegion", "isDefault": True, "context": {
               "unit": "mil", "deviceClearance": 10, "thru2SmdClearance": 10, "holeClearance": "0"}}),
           Rec("RULE", "OTHER:Common", {"ruleType": "OTHER", "name": "Common", "isDefault": True, "context": {
               "unit": "mil", "deviceClearance": 0, "thru2SmdClearance": 0, "holeClearance": 11.8}})]
    R = parse_rules(old)
    assert R.default("SAFE").value("track", "via") == 5.9 and R.default("SAFE").value("outline", "track") == 9.8, R.safe
    assert R.default("TRACK") == (5.9, 6, 100) and R.default("COPPER").clearance == 10 and R.hole_to_hole == 11.8, R


def net_names():
    # the PCB decides which pads share a net; the schematic names the nets where it agrees
    pads = {("U1", "1"): "A", ("R1", "1"): "A", ("R1", "2"): "B", ("C1", "1"): "B", ("C1", "2"): "", ("J1", "1"): "A"}
    sch = {"VCC": {("U1", "1"), ("R1", "1"), ("J1", "1")}, "Net-(C1-Pad1)": {("R1", "2"), ("C1", "1")},
           "unconnected-(C1-Pad2)": {("C1", "2")}}
    cmp = compare(sch, pads)
    assert identical(cmp), cmp
    names, rename = pcb_net_names(sch, pads, cmp)
    assert rename == {"A": "VCC", "B": "Net-(C1-Pad1)"} and names[("C1", "2")] == "unconnected-(C1-Pad2)", (names, rename)
    # a pin wired to another net in the schematic: the pad keeps the net of the copper it sits on
    sch2 = {"VCC": {("U1", "1"), ("R1", "1")}, "Net-(C1-Pad1)": {("R1", "2"), ("C1", "1"), ("J1", "1")},
            "unconnected-(C1-Pad2)": {("C1", "2")}}
    cmp = compare(sch2, pads)
    assert not identical(cmp) and cmp["split"], cmp
    names, _ = pcb_net_names(sch2, pads, cmp)
    assert names[("J1", "1")] == names[("U1", "1")] == "VCC", names
    # a pad on copper whose pin hangs loose in the schematic is a difference too
    sch3 = dict(sch, VCC={("U1", "1"), ("R1", "1")}, **{"unconnected-(J1-Pad1)": {("J1", "1")}})
    cmp = compare(sch3, pads)
    assert cmp["open"] == [(("J1", "1"), "A")] and not identical(cmp), cmp
    # ... also when none of the net's pins is wired in the schematic
    pads4 = {**pads, ("J2", "1"): "X", ("J3", "1"): "X"}
    sch4 = dict(sch, **{"unconnected-(J2-Pad1)": {("J2", "1")}, "unconnected-(J3-Pad1)": {("J3", "1")}})
    cmp = compare(sch4, pads4)
    assert [nd for nd, _ in cmp["open"]] == [("J2", "1"), ("J3", "1")] and not identical(cmp), cmp
    # a net of a single pad connects nothing: it takes the name its pin has in the schematic, nothing differs
    pads5 = {**pads, ("J2", "1"): "OLD"}
    sch5 = dict(sch, **{"unconnected-(J2-Pad1)": {("J2", "1")}})
    cmp = compare(sch5, pads5)
    names, rename = pcb_net_names(sch5, pads5, cmp)
    assert identical(cmp) and names[("J2", "1")] == rename["OLD"] == "unconnected-(J2-Pad1)", (cmp, names)
    # a pad the PCB has no net for yet takes the schematic's, as updating the PCB from the schematic would
    pads6 = {**pads, ("J1", "1"): ""}
    cmp = compare(sch, pads6)
    names, _ = pcb_net_names(sch, pads6, cmp)
    assert identical(cmp) and cmp["extra"] == [(("J1", "1"), "VCC")] and names[("J1", "1")] == "VCC", (cmp, names)


def v3_records():
    from .easyeda.records import assemble_v3_wires, flip_v3_sch_y, norm_v3
    # an arc's point on the curve is referX/referY in v3 and flips with the rest
    arc = norm_v3({"type": "ARC", "id": "a"}, {"startX": -4, "startY": 0, "referX": 0, "referY": -4,
                                               "endX": 4, "endY": 0}, "SYMBOL")
    flip_v3_sch_y([arc])
    assert (arc.num("referenceX"), arc.num("referenceY")) == (0, 4), arc.d
    # a LINE belongs to its WIRE; one whose wire is gone is a leftover EasyEDA does not show
    rows = [norm_v3({"type": "WIRE", "id": "w1"}, {}, "SCH_PAGE"),
            norm_v3({"type": "LINE", "id": "l1"}, {"startX": 0, "startY": 0, "endX": 5, "endY": 0, "lineGroup": "w1"}, "SCH_PAGE"),
            norm_v3({"type": "LINE", "id": "l2"}, {"startX": 0, "startY": 9, "endX": 5, "endY": 9, "lineGroup": "gone"}, "SCH_PAGE")]
    out = assemble_v3_wires(rows)
    assert [r.id for r in out] == ["w1"] and out[0].d["segments"] == [(0, 0, 5, 0)], out


def main():
    for check in (text_layout, polygons, rules, net_names, v3_records):
        check()
    print("translation rules: ok")


if __name__ == "__main__":
    main()
