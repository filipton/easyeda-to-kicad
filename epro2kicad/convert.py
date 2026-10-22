"""High level conversion: EasyEDA Pro project -> KiCad 10 project folder(s)."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import subprocess
import sys
from dataclasses import dataclass, field

from .easyeda.project import Board, Project, load
from .kicad import layers as L
from .kicad.board import BoardConverter, assign_fp_names, pcb_footprint_map
from .kicad.footprint import FpOptions, convert_footprint, footprint_lib_sexp, sane_name
from .kicad.projectfile import write_lib_table, write_project
from .kicad.schematic import SchematicConverter
from .sexp import NO, Sym, dumps
from .validate import compare, kicad_netlist


@dataclass
class Options:
    refill_zones: bool = True
    run_checks: bool = True
    boards: list | None = None  # names to convert (None = all with content)
    download_3d: bool = False
    kicad_cli: str | None = None
    kicad_symbols: bool = False  # KiCad's standard symbols for standard parts (R, C, diodes, transistors, ...)
    # progress(start, end, est_seconds, label, sub): a step covering [start, end) of the job began (sub=None) or is
    # sub*100 % done; est_seconds is its estimated duration, for interpolating and an ETA
    progress: object = None


@dataclass
class Report:
    board: str
    out_dir: str
    files: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    checks: dict = field(default_factory=dict)


def find_kicad_cli(explicit: str | None = None) -> str | None:
    cands = [explicit, shutil.which("kicad-cli"), "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
             "/usr/bin/kicad-cli", r"C:\\Program Files\\KiCad\\10.0\\bin\\kicad-cli.exe"]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def proj_name(s: str) -> str:
    s = sane_name(s)
    s = re.sub(r"[^\w.\-]+", "_", s, flags=re.UNICODE)
    return s.strip("_") or "board"


def _nostep(key, label, sub=None):
    pass


def _plan(proj: Project, b: Board, opts: Options) -> list:
    """Steps of one board with estimated seconds (measured on sample projects, scaled by document size)."""
    ns = sum(len(pg.doc.recs) for pg in proj.schematics[b.schematic].pages) if b.schematic in proj.schematics else 0
    npcb = len(proj.pcbs[b.pcb].recs) if b.pcb in proj.pcbs else 0
    cli = find_kicad_cli(opts.kicad_cli)
    steps = [("sch", 0.3 + ns * 5e-5)] if ns else []
    if ns and opts.kicad_symbols and cli:
        steps.append(("stock", 1 + ns * 1.1e-3))
    if opts.download_3d:
        steps.append(("3d", 1 + npcb * 1e-4))
    if npcb:
        steps.append(("pcb", 0.3 + npcb * 1.2e-4))
    if npcb and cli and opts.refill_zones:
        steps.append(("drc", 0.5 + npcb * 3e-4))
    if ns and cli and opts.run_checks:
        steps.append(("erc", 0.3 + ns * 1.5e-4))
    return steps


def convert_project(path: str, out_dir: str, opts: Options | None = None, log=print) -> list[Report]:
    opts = opts or Options()
    if opts.progress:
        opts.progress(0, 0.02, 1, "Reading the project", None)
    proj = load(path)
    log(f"Loaded {os.path.basename(path)}: EasyEDA Pro v{proj.fmt} project '{proj.title}', "
        f"{len(proj.boards)} board(s)")
    reports = []
    boards = [b for b in proj.boards if b.pcb or b.schematic]
    if opts.boards:
        want = {w.lower() for w in opts.boards}
        boards = [b for b in boards if b.name.lower() in want or proj_name(b.name).lower() in want]
    plan = [(i, k, e) for i, b in enumerate(boards) for k, e in _plan(proj, b, opts)]
    total = sum(e for _, _, e in plan) or 1
    names_used: set[str] = set()
    for i, b in enumerate(boards):
        name = proj_name(b.name)
        while name.lower() in names_used:
            name += "_"
        names_used.add(name.lower())

        def step(key, label, sub=None, i=i, b=b):
            if not opts.progress:
                return
            at = next((j for j, (bi, k, _) in enumerate(plan) if bi == i and k == key), None)
            if at is None:
                return
            start = sum(e for _, _, e in plan[:at])
            est = plan[at][2]
            prefix = f"{b.name} · " if len(boards) > 1 else ""
            opts.progress(0.02 + 0.98 * start / total, 0.02 + 0.98 * (start + est) / total, est, prefix + label, sub)
        reports.append(convert_board(proj, b, os.path.join(out_dir, name), name, opts, log, step=step))
    return reports


def _write(path: str, node) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(dumps(node) + "\n")


def convert_board(proj: Project, b: Board, out: str, name: str, opts: Options, log=print,
                  net_rename: dict | None = None, step=_nostep) -> Report:
    os.makedirs(out, exist_ok=True)
    rep = Report(b.name, out)
    fp_names = assign_fp_names(proj)
    lib = name
    sch_res = None
    if b.schematic and b.schematic in proj.schematics:
        step("sch", "Translating the schematic")
        placed = pcb_footprint_map(proj, proj.pcbs[b.pcb]) if b.pcb else {}
        sc = SchematicConverter(proj, proj.schematics[b.schematic], name, lib, fp_names, lib, placed_fp=placed)
        sch_res = sc.convert()
        if opts.kicad_symbols:
            sch_res = stock_schematic(proj, b, name, lib, fp_names, placed, sch_res, opts, log, rep, step) or sch_res
        for fn, node in sch_res.files.items():
            _write(os.path.join(out, fn), node)
            rep.files.append(os.path.join(out, fn))
        _write(os.path.join(out, f"{lib}.kicad_sym"),
               [Sym("kicad_symbol_lib"), [Sym("version"), 20251024], [Sym("generator"), "kicad_symbol_editor"],
                [Sym("generator_version"), "10.0"]] + sch_res.lib_symbols)
        write_lib_table(os.path.join(out, "sym-lib-table"), "sym",
                        [(lib, f"${{KIPRJMOD}}/{lib}.kicad_sym", f"Symbols imported from EasyEDA ({proj.title})")])
        rep.stats.update({f"sch_{k}": v for k, v in sch_res.stats.items()})
        rep.warnings += sch_res.warnings
    pcb_res = None
    fills_ok = False
    footprints = {}
    cli = find_kicad_cli(opts.kicad_cli)
    models = fetch_models(proj, b, fp_names, out, lib, log, rep, step) if opts.download_3d else {}
    if b.pcb:
        step("pcb", "Translating the PCB")
        doc = proj.pcbs[b.pcb]
        conv = BoardConverter(proj, doc, lib, fp_names, sch_links=sch_res.links if sch_res else None, models=models)
        pcb_res = conv.convert()
        if sch_res and cli:
            # second pass: take net names from KiCad's own reading of the converted schematic so that
            # "Update PCB from Schematic" finds nothing to change
            try:
                step("pcb", "Translating the PCB: net names from KiCad's netlist", 0.4)
                kn = kicad_netlist(cli, os.path.join(out, sch_res.root_file))
                cmp = compare(kn, pcb_res.pad_nets)
                override = {nd: name for name, nodes in kn.items() for nd in nodes}
                conv = BoardConverter(proj, doc, lib, fp_names, sch_links=sch_res.links, net_rename=cmp["rename"],
                                      pad_nets_override=override, pin_info=sch_res.pin_info, models=models)
                pcb_res = conv.convert()
                rep.checks["connectivity"] = {k: cmp[k] for k in ("nets_checked", "split", "merged", "missing", "extra")}
            except Exception as e:  # keep EasyEDA names
                rep.warnings.append(f"net name sync skipped: {e}")
        _write(os.path.join(out, f"{name}.kicad_pcb"), pcb_res.sexp)
        rep.files.append(os.path.join(out, f"{name}.kicad_pcb"))
        if pcb_res.fill_fixes:
            fills_ok = finish_fills(cli, os.path.join(out, f"{name}.kicad_pcb"), pcb_res.fill_fixes)
            if not fills_ok:
                rep.warnings.append("EasyEDA's own pour fills need KiCad's python to finish; KiCad refilled the zones "
                                    "instead (close to EasyEDA's, not identical)")
        footprints.update(pcb_res.footprints)
        if pcb_res.dru:
            with open(os.path.join(out, f"{name}.kicad_dru"), "w", encoding="utf-8") as f:
                f.write(pcb_res.dru)
        rep.stats.update(pcb_res.stats)
        rep.warnings += pcb_res.warnings
    if sch_res:
        for uuid in sch_res.footprints_used:
            nm = fp_names.get(uuid)
            if nm and nm not in footprints and uuid in proj.footprints:
                footprints[nm] = convert_footprint(proj.footprints[uuid], L.LayerMap(), FpOptions(), name=nm)
                if uuid in models:
                    footprints[nm].models = [models[uuid]]
    if footprints:
        lib_dir = os.path.join(out, f"{lib}.pretty")
        os.makedirs(lib_dir, exist_ok=True)
        for fname, fp in sorted(footprints.items()):
            _write(os.path.join(lib_dir, f"{fname}.kicad_mod"), footprint_lib_sexp(fp))
        write_lib_table(os.path.join(out, "fp-lib-table"), "fp",
                        [(lib, f"${{KIPRJMOD}}/{lib}.pretty", f"Footprints imported from EasyEDA ({proj.title})")])
    wks = None
    if sch_res:
        # EasyEDA frames have their border 2.54 mm from the paper edge (KiCad default: 10 mm)
        wks = "easyeda_frame.kicad_wks"
        shutil.copy(os.path.join(os.path.dirname(__file__), "kicad", "templates", wks), os.path.join(out, wks))
    write_project(os.path.join(out, f"{name}.kicad_pro"), name, pcb_res,
                  sheets=sch_res.sheets_meta if sch_res else None, min_text_height=0.5, drawing_sheet=wks)
    rep.sch = sch_res
    rep.pcb = pcb_res
    if b.pcb and cli and opts.refill_zones:
        step("drc", "Running KiCad's design-rule check")
        pcb_path = os.path.join(out, f"{name}.kicad_pcb")
        # pours that come with EasyEDA's own fill keep it (identical to EasyEDA's Gerber); refill only otherwise
        pours = sum(1 for r in proj.pcbs[b.pcb].of("POUR") if L.is_copper(L.LayerMap().to_kicad(r.int("layerId")) or ""))
        keep = pours and fills_ok and pcb_res.stats.get("pours_filled_as_easyeda", 0) >= pours
        args = [cli, "pcb", "drc", "--format", "json", "-o", os.path.join(out, "drc.json")]
        if not keep:
            args[3:3] = ["--refill-zones", "--save-board"]
        if sch_res:
            args.append("--schematic-parity")
        r = subprocess.run(args + [pcb_path], capture_output=True, text=True)
        if r.returncode not in (0, 5):
            rep.warnings.append(f"zone refill failed: {r.stderr.strip()[:300]}")
        rep.checks["drc"] = _read_json(os.path.join(out, "drc.json"))
        _remove(os.path.join(out, "drc.json"))
    if sch_res and cli and opts.run_checks:
        step("erc", "Running KiCad's electrical-rule check")
        ej = os.path.join(out, "erc.json")
        subprocess.run([cli, "sch", "erc", "--format", "json", "-o", ej, os.path.join(out, sch_res.root_file)],
                       capture_output=True, text=True)
        rep.checks["erc"] = _read_json(ej)
        _remove(ej)
    write_report(rep, proj, b, name)
    log(f"  {b.name} -> {out}")
    return rep


def kicad_python(cli: str | None) -> str | None:
    """KiCad's own python interpreter (the one with pcbnew), found next to kicad-cli."""
    cands = []
    if cli:
        d = os.path.dirname(os.path.realpath(cli))
        cands += [os.path.join(d, "..", "Frameworks", "Python.framework", "Versions", "Current", "bin", "python3"),
                  os.path.join(d, "python.exe"), os.path.join(d, "python3")]
    cands += [sys.executable, shutil.which("python3")]
    for c in cands:
        if c and os.path.exists(c):
            r = subprocess.run([c, "-c", "import pcbnew"], capture_output=True)
            if r.returncode == 0:
                return c
    return None


def finish_fills(cli: str | None, pcb_path: str, fixes: dict) -> bool:
    """Grow EasyEDA's pour fills by their outline stroke and merge in the thermal spokes (kicad/fillfix.py)."""
    import json
    py = kicad_python(cli)
    if not py:
        return False
    fj = pcb_path + ".fills.json"
    with open(fj, "w", encoding="utf-8") as f:
        json.dump(fixes, f)
    try:
        r = subprocess.run([py, os.path.join(os.path.dirname(__file__), "kicad", "fillfix.py"), pcb_path, fj],
                           capture_output=True, text=True, timeout=300)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        _remove(fj)


def lcsc_categories(proj: Project, log=print, done=None) -> dict:
    """LCSC code -> category of the project's parts (one search per part; tells e.g. a Zener from a diode)."""
    from .provider import _category, search
    codes = set()
    for dev in proj.devices.values():
        code = str((dev.get("attributes") or {}).get("Supplier Part") or "")
        if re.fullmatch(r"C\d+", code):
            codes.add(code)
    todo = sorted(c for c in codes if c not in _category)
    if todo:
        from concurrent.futures import ThreadPoolExecutor
        def one(code):
            try:
                search(code)
            except Exception:  # offline / unknown part: falls back to designator and part-number rules
                pass
        with ThreadPoolExecutor(8) as ex:
            for n, _ in enumerate(ex.map(one, todo), 1):
                if done:
                    done(n, len(todo))
        log(f"  LCSC categories: {sum(1 for c in codes if _category.get(c))}/{len(codes)} parts")
    return {c: _category[c] for c in codes if _category.get(c)}


def _nets(kn: dict) -> set:
    return {frozenset(nodes) for nodes in kn.values() if len(nodes) > 1}


def stock_schematic(proj, b, name, lib, fp_names, placed, plain, opts, log, rep, step=_nostep):
    """The schematic again with KiCad's standard symbols, accepted only where KiCad's netlist of it is identical to
    the one with EasyEDA's symbols; parts that would change it keep their EasyEDA symbol."""
    cli = find_kicad_cli(opts.kicad_cli)
    if not cli:
        rep.warnings.append("KiCad symbols: kicad-cli not found, so the result can't be verified; kept EasyEDA's")
        return None
    step("stock", "KiCad standard symbols: looking up LCSC categories", 0)
    cats = lcsc_categories(proj, log, lambda n, t: step("stock", f"KiCad standard symbols: LCSC categories {n}/{t}",
                                                         0.3 * n / t))
    with tempfile.TemporaryDirectory() as td:
        def netlist(res, sub):
            d = os.path.join(td, sub)
            os.makedirs(d, exist_ok=True)
            for fn, node in res.files.items():
                _write(os.path.join(d, fn), node)
            _write(os.path.join(d, f"{lib}.kicad_sym"), [Sym("kicad_symbol_lib"), [Sym("version"), 20251024],
                   [Sym("generator"), "kicad_symbol_editor"], [Sym("generator_version"), "10.0"]] + res.lib_symbols)
            write_lib_table(os.path.join(d, "sym-lib-table"), "sym", [(lib, f"${{KIPRJMOD}}/{lib}.kicad_sym", "")])
            return kicad_netlist(cli, os.path.join(d, res.root_file))
        step("stock", "KiCad standard symbols: KiCad's netlist of the original", 0.3)
        want = _nets(netlist(plain, "plain"))
        skip: set = set()
        for attempt in range(5):
            step("stock", "KiCad standard symbols: placing and checking connectivity"
                 + (f" (pass {attempt + 1})" if attempt else ""), 0.4 + 0.12 * attempt)
            sc = SchematicConverter(proj, proj.schematics[b.schematic], name, lib, fp_names, lib, placed_fp=placed,
                                    stock=True, categories=cats, stock_skip=skip)
            res = sc.convert()
            got = _nets(netlist(res, f"stock{attempt}"))
            bad = {ref for net in want ^ got for ref, _ in net}
            if not bad:
                n = res.stats.get("stock_symbols", 0)
                log(f"  KiCad standard symbols: {n} placed" + (f", {len(skip)} kept EasyEDA's" if skip else ""))
                return res
            skip |= bad
    rep.warnings.append("KiCad symbols: couldn't keep the connectivity identical, kept EasyEDA's symbols")
    return None


def _pads(fp_doc) -> list:
    """Pad numbers and positions of a footprint (KiCad mm, 0.05 mm grid), to tell whether two are the same."""
    fp = convert_footprint(fp_doc, L.LayerMap(), FpOptions())
    return sorted((p.number, round(p.at[0] / 0.05), round(p.at[1] / 0.05)) for p in fp.pads)


def _library_model(code: str, fp_doc) -> tuple:
    """(model uuid, transform) of the LCSC part's current library entry, if its footprint is this one."""
    from .lcsc import LcscError, _doc, fetch_component, fetch_device
    at = fetch_device(code).get("attributes") or {}
    if not at.get("3D Model") or not at.get("Footprint"):
        raise LcscError(f"{code}: no 3D model")
    if _pads(_doc(fetch_component(at["Footprint"]), "fp")) != _pads(fp_doc):
        raise LcscError(f"{code}: library footprint differs from the project's")
    return str(at["3D Model"]), str(at.get("3D Model Transform") or "")


def fetch_models(proj: Project, b: Board, fp_names: dict, out: str, lib: str, log, rep: Report,
                 step=_nostep) -> dict:
    """STEP models from the EasyEDA library for the footprints of this board (needs internet).

    Library models get new uuids over time, so the uuid an older project references is often gone.  Then the model
    with the same title is used (same model, so the project's transform still fits), and failing that the current
    model of the part's LCSC number, if its footprint has the same pads (EasyEDA centres models on the footprint)."""
    from .lcsc import LcscError, fetch_step, find_model, model_from_transform, step_extent
    placed = []  # (footprint uuid, device attributes); a PCB component's own footprint wins over its device's
    if b.pcb:
        comps: dict[str, dict] = {}
        for a in proj.pcbs[b.pcb].of("ATTR"):
            comps.setdefault(str(a.d.get("parentId") or ""), {})[a.str("key")] = a.str("value")
        for c in proj.pcbs[b.pcb].of("COMPONENT"):
            at = comps.get(c.id, {})
            dev = proj.devices.get(at.get("Device") or "", {}).get("attributes") or {}
            placed.append((at.get("Footprint") or str(dev.get("Footprint") or ""), dev))
    else:
        placed = [(str((d.get("attributes") or {}).get("Footprint") or ""), d.get("attributes") or {})
                  for d in proj.devices.values()]
    wanted: dict[str, list] = {}  # footprint uuid -> candidates, best first: () -> (model uuid, transform, note)
    for fu, at in placed:
        if fu not in proj.footprints:
            continue
        mu, title = str(at.get("3D Model") or ""), str(at.get("3D Model Title") or "")
        tr = str(at.get("3D Model Transform") or "")
        code = str(at.get("Supplier Part") or "").strip().upper()
        cands = wanted.setdefault(fu, [])
        if mu:
            cands.append(lambda mu=mu, tr=tr: (mu, tr, ""))
        if mu and title:
            cands.append(lambda title=title, tr=tr: (find_model(title), tr, f"model '{title}' found by name"))
        if re.fullmatch(r"C\d+", code):
            cands.append(lambda code=code, fp=proj.footprints[fu]: _library_model(code, fp)
                         + (f"model of {code} from the library",))
    models, failed, notes = {}, 0, []
    shapes = os.path.join(out, f"{lib}.3dshapes")
    todo = sorted((fu, c) for fu, c in wanted.items() if c and fp_names.get(fu))
    for n, (fu, cands) in enumerate(todo):
        step("3d", f"Downloading 3D models {n + 1}/{len(todo)}", n / len(todo))
        name = fp_names[fu]
        for cand in cands:
            try:
                mu, tr, note = cand()
                data = fetch_step({"3D Model": mu})
            except (LcscError, ValueError, OSError):
                continue
            os.makedirs(shapes, exist_ok=True)
            with open(os.path.join(shapes, f"{name}.step"), "wb") as f:
                f.write(data)
            models[fu] = model_from_transform(f"${{KIPRJMOD}}/{lib}.3dshapes/{name}.step", tr, step_extent(data))
            if note:
                notes.append(f"{name}: {note}")
            break
        else:
            failed += 1
    log(f"  3D models: {len(models)} added" + (f" ({len(notes)} replaced by the library's current one)" if notes
                                                else "") + (f", {failed} not available" if failed else ""))
    if notes:
        rep.warnings.append("3D models no longer in the EasyEDA library under the project's ids, replaced: "
                            + "; ".join(notes))
    if failed:
        rep.warnings.append(f"{failed} 3D model(s) could not be downloaded from the EasyEDA library")
    return models


def _read_json(path: str):
    import json
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _remove(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


# violations that come from the source design itself rather than from the translation
DESIGN_NOTES = {
    "pin_not_connected": "EasyEDA has no no-connect flags in these sheets; add KiCad no-connect markers where intended",
    "power_pin_not_driven": "KiCad wants a PWR_FLAG on nets fed from connectors/regulators (EasyEDA has no such check)",
    "missing_footprint": "symbol exists in the EasyEDA schematic but was never placed on the EasyEDA PCB",
    "net_conflict": "symbol pin has no matching pad in the footprint (as in the EasyEDA project)",
}


def write_report(rep: Report, proj: Project, b: Board, name: str) -> str:
    """Human readable summary written next to the converted project."""
    from collections import Counter
    L_ = [f"# {name}: EasyEDA Pro → KiCad translation report", "",
          f"Source: `{os.path.basename(proj.source or '')}` (EasyEDA Pro v{proj.fmt}), board **{b.name}**", ""]
    st = rep.stats
    if st:
        L_ += ["## Contents", ""]
        L_ += [f"- {k.replace('_', ' ')}: {v}" for k, v in sorted(st.items())]
        L_.append("")
    con = rep.checks.get("connectivity")
    if con:
        ok = not (con["split"] or con["merged"] or con["missing"])
        L_ += ["## Schematic ↔ PCB connectivity", "",
               f"{con['nets_checked']} PCB nets compared with KiCad's netlist of the translated schematic: "
               + ("**identical**." if ok else "**differences found**:"), ""]
        for en, ks in con["split"]:
            L_.append(f"- EasyEDA net `{en}` maps to several schematic nets: {ks}")
        for k, ens in con["merged"].items():
            L_.append(f"- schematic net `{k}` joins PCB nets {ens}")
        for nd, en in con["missing"][:50]:
            L_.append(f"- pad {nd[0]}.{nd[1]} (net `{en}`) has no schematic pin")
        L_.append("")
    drc = rep.checks.get("drc")
    if drc:
        cnt = Counter(v["type"] for v in drc.get("violations", []))
        par = Counter(v["type"] for v in drc.get("schematic_parity", []))
        L_ += ["## PCB design rule check (KiCad)", "",
               f"- unconnected items: {len(drc.get('unconnected_items', []))}"]
        L_ += [f"- {k}: {v}" for k, v in cnt.most_common()]
        if par:
            L_ += ["", "Schematic parity:"]
            for k, v in par.most_common():
                note = DESIGN_NOTES.get(k)
                L_.append(f"- {k}: {v}" + (f" — {note}" if note else ""))
                for x in [x for x in drc["schematic_parity"] if x["type"] == k][:12]:
                    L_.append(f"    - {x['description']} ({', '.join(i['description'] for i in x['items'])})")
        L_.append("")
    erc = rep.checks.get("erc")
    if erc:
        cnt = Counter(v["type"] for s in erc.get("sheets", []) for v in s.get("violations", []))
        L_ += ["## Electrical rule check (KiCad)", ""]
        L_ += [f"- {k}: {v}" + (f" — {DESIGN_NOTES[k]}" if k in DESIGN_NOTES else "") for k, v in cnt.most_common()]
        if not cnt:
            L_.append("- no violations")
        L_.append("")
    if rep.warnings:
        L_ += ["## Translation notes", ""]
        for w, n in Counter(rep.warnings).most_common():
            L_.append(f"- {w}" + (f" (×{n})" if n > 1 else ""))
        L_.append("")
    path = os.path.join(rep.out_dir, "conversion-report.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L_))
    return path
