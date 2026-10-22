"""Writers for .kicad_pro, sym-lib-table and fp-lib-table."""

from __future__ import annotations

import copy
import json
import os

TEMPLATES = os.path.join(os.path.dirname(__file__), "templates")


def _netclass(base: dict, nc) -> dict:
    c = copy.deepcopy(base)
    c["name"] = nc.name
    c["clearance"] = nc.clearance
    c["track_width"] = nc.track_width
    c["via_diameter"] = nc.via_diameter
    c["via_drill"] = nc.via_drill
    if nc.diff_pair_width:
        c["diff_pair_width"] = nc.diff_pair_width
    if nc.diff_pair_gap:
        c["diff_pair_gap"] = nc.diff_pair_gap
        c["diff_pair_via_gap"] = nc.diff_pair_gap
    if nc.name != "Default":
        c["priority"] = 0
    return c


def write_project(path: str, name: str, board=None, sheets: list | None = None, text_vars: dict | None = None,
                  drawing_sheet: str | None = None,
                  min_text_height: float | None = None) -> None:
    with open(os.path.join(TEMPLATES, "project.kicad_pro"), encoding="utf-8") as f:
        pro = json.load(f)
    pro["meta"]["filename"] = os.path.basename(path)
    if board is not None:
        ds = pro["board"]["design_settings"]
        ds["rules"].update(board.design_rules)
        if min_text_height is not None:
            ds["rules"]["min_text_height"] = min_text_height
        ds["track_widths"] = [0.0] + [w for w in board.track_widths if w > 0]
        ds["via_dimensions"] = [{"diameter": 0.0, "drill": 0.0}] + [{"diameter": d, "drill": h}
                                                                   for d, h in board.via_dims if d > 0]
        ns = pro["net_settings"]
        base = ns["classes"][0]
        classes = []
        prio = 0
        for nc in board.netclasses:
            c = _netclass(base, nc)
            if nc.name != "Default":
                c["priority"] = prio
                prio += 1
            classes.append(c)
        ns["classes"] = classes
        patterns = []
        for nc in board.netclasses:
            for n in sorted(nc.nets):
                patterns.append({"netclass": nc.name, "pattern": n})
        ns["netclass_patterns"] = patterns
        if getattr(board, "net_colors", None):
            ns["net_colors"] = dict(sorted(board.net_colors.items()))
    if sheets:
        pro["sheets"] = sheets
    if drawing_sheet:
        pro.setdefault("schematic", {})["page_layout_descr_file"] = drawing_sheet
    if text_vars:
        pro["text_variables"] = text_vars
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pro, f, indent=2, ensure_ascii=False)
        f.write("\n")


def write_lib_table(path: str, kind: str, libs: list[tuple[str, str, str]]) -> None:
    """kind: 'sym' or 'fp'.  libs: [(nickname, uri, descr)]"""
    head = "sym_lib_table" if kind == "sym" else "fp_lib_table"
    lines = [f"({head}", "\t(version 7)"]
    for nick, uri, descr in libs:
        typ = "KiCad"
        lines.append(f'\t(lib (name "{nick}")(type "{typ}")(uri "{uri}")(options "")(descr "{descr}"))')
    lines.append(")")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
