"""Finish EasyEDA's own pour fills inside a .kicad_pcb with KiCad's polygon engine (run with KiCad's python).

    <kicad python> fillfix.py board.kicad_pcb fixes.json

fixes.json: zone uuid -> {"layer": "F.Cu", "grow": mm, "spokes": [[[x, y], ...], ...]}.  EasyEDA's Gerber strokes
each fill outline with the pour width and draws thermal spokes as separate lines; KiCad's fill is the final copper
and must be one polygon set (separate overlapping pieces don't connect), so: grow by half the stroke, add the
spokes, merge, fracture.
"""

import json
import sys

import pcbnew


def main(board_path: str, fixes_path: str) -> int:
    with open(fixes_path, encoding="utf-8") as f:
        fixes = json.load(f)
    board = pcbnew.LoadBoard(board_path)
    round_all = getattr(pcbnew, "CORNER_STRATEGY_ROUND_ALL_CORNERS", 2)
    done = 0
    for zone in board.Zones():
        fx = fixes.get(zone.m_Uuid.AsString())
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
    pcbnew.SaveBoard(board_path, board)
    print(f"fills finished: {done}")
    return 0 if done == len(fixes) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
