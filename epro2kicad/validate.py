"""Cross-checks between the converted schematic (via kicad-cli netlist) and the EasyEDA PCB."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections import defaultdict

from .sexp import loads


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
    # pads the PCB connects to others whose pin hangs loose in the schematic (a net of one pad connects nothing)
    open_ = sorted((nd, en) for en, nodes in e_groups.items() if len(nodes) > 1 for nd in nodes
                   if (node_k.get(nd) or "").startswith("unconnected-"))
    return {"rename": rename, "split": split, "merged": merged, "missing": missing, "extra": extra, "open": open_,
            "nets_checked": len(e_groups)}


def identical(cmp: dict) -> bool:
    """The PCB's nets and the schematic's connect the same pins."""
    return not (cmp["split"] or cmp["merged"] or cmp["missing"] or cmp.get("open"))


def pcb_net_names(kicad_nets: dict[str, set], pad_nets: dict, cmp: dict) -> tuple[dict, dict]:
    """Net names for the board: (pad -> net, EasyEDA net -> net).

    The EasyEDA PCB decides which pads share a net: that is the copper that exists.  The schematic only gives the
    nets their KiCad names (so that updating the PCB from the schematic changes nothing), and only where it agrees.
    Where it doesn't (a pin wired differently, a net split in two), giving each pad its schematic net would turn a
    correct board into one full of shorts; the pads keep their PCB net and the report lists the difference."""
    node_k = {nd: name for name, nodes in kicad_nets.items() for nd in nodes}
    votes: dict = defaultdict(lambda: defaultdict(int))  # schematic net -> EasyEDA net -> pads
    for nd, en in pad_nets.items():
        k = node_k.get(nd)
        if en and k:
            votes[k][en] += 1
    final, taken = {}, set()
    for en, k in sorted(cmp["rename"].items(), key=lambda kv: -votes[kv[1]][kv[0]]):
        if k not in taken:  # one PCB net per schematic name: the one with most pads on it
            final[en] = k
            taken.add(k)
    alone: dict = defaultdict(list)
    for nd, en in pad_nets.items():
        if en:
            alone[en].append(nd)
    for en in sorted({en for en in pad_nets.values() if en} - set(final)):
        k = node_k.get(alone[en][0]) if len(alone[en]) == 1 else None
        if k and k not in taken and len(kicad_nets[k]) == 1:
            # a net of one pad whose pin is loose in the schematic (what is left of an older schematic): no
            # connection either way, so it takes the schematic's name, with whatever copper hangs on it
            final[en] = k
            taken.add(k)
            continue
        name, n = en, 2
        while name in taken:  # keeps its EasyEDA name, unless another net just got that one
            name, n = f"{en}_{n}", n + 1
        final[en] = name
        taken.add(name)
    pads = {}
    for nd, en in pad_nets.items():
        if en:
            pads[nd] = final[en]
        else:
            # no net on the PCB yet (it wasn't updated from the schematic since that pin was wired): the schematic's
            # net, as updating the PCB would give it.  Nothing is joined that isn't joined already: the pad has
            # no copper net, and what is missing shows as an unrouted connection
            pads[nd] = node_k.get(nd) or ""
    return pads, final
