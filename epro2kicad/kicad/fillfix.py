"""Finish the pour fills inside a .kicad_pcb with KiCad's own engine (run with KiCad's python).

    <kicad python> fillfix.py board.kicad_pcb fixes.json

fixes.json: {"fixes": {zone uuid: {"layer": "F.Cu", "grow": mm, "spokes": [[[x, y], ...], ...]}}, "fill": [uuids]}.
"fixes" are pours that carry EasyEDA's own fill.  EasyEDA's Gerber strokes each fill outline with the pour width and
draws thermal spokes as separate lines; KiCad's fill is the final copper and must be one polygon set (separate
overlapping pieces don't connect), so: grow by half the stroke, add the spokes, merge, fracture.
"fill" are pours EasyEDA never built: KiCad fills those, around the others as they are.
"""

import json
import sys

import pcbnew


def main(board_path: str, fixes_path: str) -> int:
    with open(fixes_path, encoding="utf-8") as f:
        job = json.load(f)
    fixes, to_fill = job.get("fixes") or {}, set(job.get("fill") or [])
    board = pcbnew.LoadBoard(board_path)
    round_all = getattr(pcbnew, "CORNER_STRATEGY_ROUND_ALL_CORNERS", 2)
    done, pending = 0, []
    for zone in board.Zones():
        uid = zone.m_Uuid.AsString()
        if uid in to_fill:
            pending.append(zone)
        fx = fixes.get(uid)
        if not fx:
            continue
        layer = board.GetLayerID(fx["layer"])
        polys = zone.GetFilledPolysList(layer)
        polys.Unfracture()
        polys.Simplify()
        if fx.get("grow"):
            polys.Inflate(pcbnew.FromMM(fx["grow"]), round_all, pcbnew.FromMM(0.002))
        spokes = pcbnew.SHAPE_POLY_SET()
        for pts in fx.get("spokes") or []:
            spokes.NewOutline()
            for x, y in pts:
                spokes.Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
        if spokes.OutlineCount():
            polys.BooleanAdd(spokes)
        polys.Fracture()
        zone.SetIsFilled(True)
        done += 1
    if pending:
        zones = pcbnew.ZONES()
        for z in pending:
            zones.append(z)
        pcbnew.ZONE_FILLER(board).Fill(zones)
    pcbnew.SaveBoard(board_path, board)
    print(f"fills finished: {done}, filled by KiCad: {len(pending)}")
    return 0 if done == len(fixes) and len(pending) == len(to_fill) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
