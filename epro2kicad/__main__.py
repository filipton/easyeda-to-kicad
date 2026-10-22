"""Command line: python -m epro2kicad <project.epro|.epro2|.zip> [-o OUT] [--board NAME ...]"""

from __future__ import annotations

import argparse
import os
import sys

from .convert import Options, convert_project, proj_name
from .easyeda.project import load


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="epro2kicad",
        description="Translate an EasyEDA Pro project (v2 .epro/.zip or v3 .epro2) into native KiCad 10 projects "
                    "(schematic + PCB + project libraries), one folder per board.")
    ap.add_argument("project", help="EasyEDA Pro project export (.epro, .epro2 or .zip)")
    ap.add_argument("-o", "--out", help="output folder (default: <project name>-kicad next to the input)")
    ap.add_argument("-b", "--board", action="append", help="only this board (repeatable; see --list)")
    ap.add_argument("--list", action="store_true", help="list the boards in the project and exit")
    ap.add_argument("--no-refill", action="store_true", help="do not fill zones / run DRC with kicad-cli")
    ap.add_argument("--no-checks", action="store_true", help="do not run ERC")
    ap.add_argument("--3d", dest="models", action="store_true",
                    help="download STEP models for the footprints from the EasyEDA library (internet)")
    ap.add_argument("--kicad-symbols", action="store_true",
                    help="use KiCad's standard symbols for standard parts (resistors, capacitors, diodes, transistors, "
                         "op-amps, connectors, ICs in KiCad's libraries ...); verified against the netlist")
    ap.add_argument("--kicad-cli", help="path to kicad-cli (auto-detected otherwise)")
    a = ap.parse_args(argv)

    if a.list:
        proj = load(a.project)
        print(f"{proj.title}  (EasyEDA Pro v{proj.fmt})")
        for b in proj.boards:
            parts = [p for p, ok in (("schematic", b.schematic), ("pcb", b.pcb)) if ok]
            print(f"  {b.name:30} {' + '.join(parts) or 'empty'}")
        return 0

    out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.project)),
                                proj_name(os.path.splitext(os.path.basename(a.project))[0]) + "-kicad")
    opts = Options(refill_zones=not a.no_refill, run_checks=not a.no_checks, boards=a.board, kicad_cli=a.kicad_cli,
                   download_3d=a.models, kicad_symbols=a.kicad_symbols)
    reps = convert_project(a.project, out, opts)
    if not reps:
        print("nothing converted (check --list / --board)", file=sys.stderr)
        return 1
    print()
    for r in reps:
        con = r.checks.get("connectivity")
        drc = r.checks.get("drc") or {}
        line = f"{r.board}: {r.out_dir}"
        if con:
            same = not (con["split"] or con["merged"] or con["missing"])
            line += f"\n    schematic/PCB nets: {'identical' if same else 'DIFFER (see report)'} ({con['nets_checked']} nets)"
        if drc:
            line += f"\n    unconnected items: {len(drc.get('unconnected_items', []))}"
        line += f"\n    report: {os.path.join(r.out_dir, 'conversion-report.md')}"
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
