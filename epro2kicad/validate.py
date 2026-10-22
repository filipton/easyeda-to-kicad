"""Cross-checks between the converted schematic (via kicad-cli netlist) and the EasyEDA PCB."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections import defaultdict

from .sexp import find_all, loads


def kicad_netlist(cli: str, sch_path: str) -> dict[str, set]:
    """net name -> {(ref, pin)} using `kicad-cli sch export netlist`."""
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "net.net")
        r = subprocess.run([cli, "sch", "export", "netlist", "--format", "kicadsexpr", "-o", out, sch_path],
                           capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(out):
            raise RuntimeError(f"netlist export failed: {r.stderr.strip()[:300]}")
        tree = loads(open(out, encoding="utf-8").read())
    nets = {}
    netsec = next(x for x in tree if isinstance(x, list) and x and x[0] == "nets")
    for n in netsec[1:]:
        name = next(x[1] for x in n if isinstance(x, list) and x[0] == "name")
        nodes = set()
        for nd in n:
            if isinstance(nd, list) and nd and nd[0] == "node":
                ref = next(x[1] for x in nd if isinstance(x, list) and x[0] == "ref")
                pin = next(x[1] for x in nd if isinstance(x, list) and x[0] == "pin")
                nodes.add((str(ref), str(pin)))
        nets[str(name)] = nodes
    return nets


def compare(kicad_nets: dict[str, set], pad_nets: dict) -> dict:
    """Compare connectivity.  pad_nets: (ref, pad) -> easyeda net name ('' = unconnected).

    Returns dict with rename map (easyeda net -> kicad net) and lists of problems.
    """
    node_k = {}
    for name, nodes in kicad_nets.items():
        for nd in nodes:
            node_k[nd] = name
    e_groups = defaultdict(set)
    for nd, en in pad_nets.items():
        if en:
            e_groups[en].add(nd)
    rename = {}
    split = []  # easyeda net spread over several kicad nets
    merged = defaultdict(set)  # kicad net -> easyeda nets
    missing = []  # pads with a net on the PCB but no pin in the schematic
    for en, nodes in e_groups.items():
        ks = defaultdict(int)
        for nd in nodes:
            k = node_k.get(nd)
            if k is None:
                missing.append((nd, en))
                continue
            ks[k] += 1
        real = {k: c for k, c in ks.items() if not k.startswith("unconnected-")}
        if not real:
            continue
        best = max(real, key=real.get)
        rename[en] = best
        if len(real) > 1:
            split.append((en, dict(real)))
        for k in real:
            merged[k].add(en)
    merged = {k: sorted(v) for k, v in merged.items() if len(v) > 1}
    # pins connected in the schematic but unconnected on the PCB
    extra = []
    for name, nodes in kicad_nets.items():
        if name.startswith("unconnected-") or len(nodes) < 2:
            continue
        for nd in nodes:
            if nd in pad_nets and not pad_nets[nd]:
                extra.append((nd, name))
    return {"rename": rename, "split": split, "merged": merged, "missing": missing, "extra": extra,
            "nets_checked": len(e_groups)}
