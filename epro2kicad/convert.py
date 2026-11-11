"""High level conversion: EasyEDA Pro project -> KiCad 10 project folder(s)."""

from __future__ import annotations

import functools
import math
import os
import re
import shutil
import tempfile
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .easyeda.project import Board, Project, load
from .kicad import layers as L
from .kicad.board import BoardConverter, assign_fp_names, pcb_footprint_map
from .kicad.footprint import FpOptions, convert_footprint, footprint_lib_sexp, sane_name
from .kicad.projectfile import write_lib_table, write_project
from .kicad.schematic import SchematicConverter
from .sexp import Sym, dumps
from .validate import compare, identical, kicad_netlist, pcb_net_names


@dataclass
class Options:
    refill_zones: bool = True
    run_checks: bool = True
    boards: list | None = None  # names to convert (None = all with content)
    download_3d: bool = False
    kicad_cli: str | None = None
    kicad_symbols: bool = False  # KiCad's standard symbols for standard parts (R, C, diodes, transistors, ...)
    jobs: int = 0  # boards translated side by side (0: up to 4)
    # a limit missed by no more than this (mm) is rounding between EasyEDA's units and KiCad's, not a design
    # error: the board's rules are then written that little lower (0: rules exactly as in EasyEDA)
    tolerance: float = 0.003
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
    # seconds per record measured on the 7 projects of easyeda/easyeda-viewer (2 to 71k PCB records); kicad-cli
    # needs about a second to start
    steps = [("sch", 0.3 + ns * 5e-5)] if ns else []
    if ns and opts.kicad_symbols and cli:
        steps.append(("stock", 1 + ns * 1.1e-3))
    if opts.download_3d:
        steps.append(("3d", 1 + npcb * 1e-4))
    if npcb:
        steps.append(("pcb", 0.3 + npcb * 2.6e-4))
    if npcb and cli and opts.refill_zones:
        steps.append(("drc", 1.0 + npcb * 3.8e-4))
    if ns and cli and opts.run_checks:
        steps.append(("erc", 1.0 + ns * 2.2e-4))
    return steps


class _Progress:
    """One progress bar for boards that are translated side by side: a step counts as done when its board moves on
    to the next one; the bar is at the share of the estimated time of all finished steps."""

    def __init__(self, cb, plan: list, f0: float, many: bool):
        self.cb, self.f0, self.many = cb, f0, many
        self.est = {(i, k): e for i, k, e in plan}
        self.total = sum(self.est.values()) or 1
        self.done = 0.0
        self.open: dict = {}  # board -> step it is in
        self.lock = threading.Lock()

    def step(self, i: int, board: str, key: str, label: str, sub=None) -> None:
        if not self.cb or (i, key) not in self.est:
            return
        with self.lock:
            cur = self.open.get(i)
            if cur != key:
                if cur is not None:
                    self.done += self.est[(i, cur)]
                self.open[i] = key
            est = self.est[(i, key)]
            a = self.f0 + (1 - self.f0) * self.done / self.total
            self.cb(a, min(1.0, a + (1 - self.f0) * est / self.total), est,
                    (f"{board} · " if self.many else "") + label, sub)

    def finish(self, i: int) -> None:
        with self.lock:
            cur = self.open.pop(i, None)
            if cur is not None:
                self.done += self.est[(i, cur)]


def convert_project(path: str, out_dir: str, opts: Options | None = None, log=print) -> list[Report]:
    opts = opts or Options()
    t0 = time.monotonic()
    if opts.progress:  # about a quarter second per MB of project
        opts.progress(0, 0.02, 0.5 + os.path.getsize(path) / 4e6, "Reading the project", None)
    proj = load(path)
    read_s = time.monotonic() - t0
    log(f"Loaded {os.path.basename(path)}: EasyEDA Pro v{proj.fmt} project '{proj.title}', "
        f"{len(proj.boards)} board(s)")
    boards = [b for b in proj.boards if b.pcb or b.schematic]
    if opts.boards:
        want = {w.lower() for w in opts.boards}
        boards = [b for b in boards if b.name.lower() in want or proj_name(b.name).lower() in want]
    plan = [(i, k, e) for i, b in enumerate(boards) for k, e in _plan(proj, b, opts)]
    total = sum(e for _, _, e in plan) or 1
    f0 = max(0.02, min(0.3, read_s / (read_s + total)))  # the bar's share of the reading that's done now
    prog = _Progress(opts.progress, plan, f0, len(boards) > 1)
    names_used: set[str] = set()
    jobs = []
    for i, b in enumerate(boards):
        name = proj_name(b.name)
        while name.lower() in names_used:
            name += "_"
        names_used.add(name.lower())
        jobs.append((i, b, name))
    many = len(boards) > 1
    lock = threading.Lock()

    def one(job):
        i, b, name = job

        def blog(msg):  # lines of boards that run side by side are told apart by the board's name
            with lock:
                log(f"  [{b.name}]{msg[1:] if msg.startswith('  ') else ' ' + msg}" if many and msg.startswith("  ")
                    and not msg.lstrip().startswith(b.name) else msg)
        try:
            return convert_board(proj, b, os.path.join(out_dir, name), name, opts, blog,
                                 step=lambda key, label, sub=None: prog.step(i, b.name, key, label, sub))
        finally:
            prog.finish(i)
    # most of a board's time is KiCad checking the result (separate processes) and downloads: boards overlap well
    workers = max(1, min(len(jobs), opts.jobs or min(4, os.cpu_count() or 2)))
    if workers == 1:
        return [one(j) for j in jobs]
    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(one, jobs))


def _write(path: str, node) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(dumps(node) + "\n")


def convert_board(proj: Project, b: Board, out: str, name: str, opts: Options, log=print,
                  net_rename: dict | None = None, step=_nostep) -> Report:
    os.makedirs(out, exist_ok=True)
    rep = Report(b.name, out)
    fp_names = assign_fp_names(proj)
    lib = name
    cli = find_kicad_cli(opts.kicad_cli)
    # downloads, KiCad's netlist and its ERC don't depend on each other: they run while the translation goes on
    side = ThreadPoolExecutor(3)
    try:
        return _convert_board(proj, b, out, name, opts, log, step, rep, fp_names, lib, cli, side)
    finally:
        side.shutdown(wait=True)


def _convert_board(proj, b, out, name, opts, log, step, rep, fp_names, lib, cli, side) -> Report:
    sch_res = kn = None
    models_job = side.submit(fetch_models, proj, b, fp_names, out, lib, log, rep, step) if opts.download_3d else None
    erc_job = net_job = None
    if b.schematic and b.schematic in proj.schematics:
        step("sch", "Translating the schematic")
        placed = pcb_footprint_map(proj, proj.pcbs[b.pcb]) if b.pcb else {}
        sc = SchematicConverter(proj, proj.schematics[b.schematic], name, lib, fp_names, lib, placed_fp=placed)
        sch_res = sc.convert()
        if opts.kicad_symbols:
            stock = stock_schematic(proj, b, name, lib, fp_names, placed, sch_res, opts, log, rep, step)
            if stock:
                sch_res, kn = stock  # its netlist was read to check it: no need to ask KiCad again
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
        root = os.path.join(out, sch_res.root_file)
        if cli and b.pcb and kn is None:
            net_job = side.submit(kicad_netlist, cli, root)
    pcb_res = None
    fills_ok = False
    footprints = {}
    models = {}
    if b.pcb:
        step("pcb", "Translating the PCB")
        doc = proj.pcbs[b.pcb]
        if sch_res and cli:
            # a first pass for the pads' EasyEDA nets, then the real one with the net names of KiCad's own reading
            # of the converted schematic, so that "Update PCB from Schematic" finds nothing to change
            pcb_res = BoardConverter(proj, doc, lib, fp_names, sch_links=sch_res.links).convert()
        models = models_job.result() if models_job else {}
        if sch_res and cli:
            try:
                step("pcb", "Translating the PCB: net names from KiCad's netlist", 0.4)
                if kn is None:
                    kn = net_job.result()
                cmp = compare(kn, pcb_res.pad_nets)
                override, rename = pcb_net_names(kn, pcb_res.pad_nets, cmp)
                conv = BoardConverter(proj, doc, lib, fp_names, sch_links=sch_res.links, net_rename=rename,
                                      pad_nets_override=override, pin_info=sch_res.pin_info, models=models)
                pcb_res = conv.convert()
                rep.checks["connectivity"] = {k: cmp[k] for k in ("nets_checked", "split", "merged", "missing", "extra",
                                                                  "open")}
                rep.checks["connectivity"]["identical"] = identical(cmp)
            except Exception as e:  # keep EasyEDA names
                rep.warnings.append(f"net name sync skipped: {e}")
                pcb_res = None
        if pcb_res is None or (models and not (sch_res and cli)):
            pcb_res = BoardConverter(proj, doc, lib, fp_names, sch_links=sch_res.links if sch_res else None,
                                     models=models).convert()
        _write(os.path.join(out, f"{name}.kicad_pcb"), pcb_res.sexp)
        rep.files.append(os.path.join(out, f"{name}.kicad_pcb"))
        if pcb_res.fill_fixes or pcb_res.unfilled:
            fills_ok = finish_fills(cli, os.path.join(out, f"{name}.kicad_pcb"), pcb_res.fill_fixes,
                                    pcb_res.unfilled)
            if not fills_ok:
                rep.warnings.append("EasyEDA's own pour fills need KiCad's python to finish; KiCad refilled the zones "
                                    "instead (close to EasyEDA's, not identical)")
            elif pcb_res.unfilled:
                rep.warnings.append(f"{len(pcb_res.unfilled)} pour(s) were never built in EasyEDA: filled by KiCad")
        else:
            fills_ok = True  # nothing to finish
        footprints.update(pcb_res.footprints)
        if pcb_res.dru:
            with open(os.path.join(out, f"{name}.kicad_dru"), "w", encoding="utf-8") as f:
                f.write(pcb_res.dru)
        rep.stats.update(pcb_res.stats)
        rep.warnings += pcb_res.warnings
    elif models_job:
        models = models_job.result()
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
    def write_pro():
        write_project(os.path.join(out, f"{name}.kicad_pro"), name, pcb_res,
                      sheets=sch_res.sheets_meta if sch_res else None, min_text_height=0.5, drawing_sheet=wks)
    write_pro()
    rep.sch = sch_res
    rep.pcb = pcb_res
    if sch_res and cli and opts.run_checks:
        # the ERC needs the whole project in place (library tables, footprints, project file): it runs beside
        # the DRC, each in its own KiCad process
        ej = os.path.join(out, "erc.json")
        erc_job = side.submit(subprocess.run, [cli, "sch", "erc", "--format", "json", "-o", ej,
                                               os.path.join(out, sch_res.root_file)], capture_output=True, text=True)
    # a PCB document without anything on it (a project that is a schematic so far) has nothing to check
    drawn = pcb_res is not None and any(pcb_res.stats.get(k) for k in ("footprints", "tracks", "vias", "zones",
                                                                         "graphics"))
    if drawn and cli and opts.refill_zones:
        step("drc", "Running KiCad's design-rule check")
        pcb_path = os.path.join(out, f"{name}.kicad_pcb")
        # pours keep EasyEDA's own fill (identical to its Gerber), the ones it never built were filled by KiCad's
        # python above; only without that python everything is refilled here
        args = [cli, "pcb", "drc", "--format", "json", "-o", os.path.join(out, "drc.json")]
        if not fills_ok:
            args[3:3] = ["--refill-zones", "--save-board"]
        if sch_res:
            args.append("--schematic-parity")
        r = subprocess.run(args + [pcb_path], capture_output=True, text=True)
        if r.returncode not in (0, 5):
            rep.warnings.append(f"zone refill failed: {r.stderr.strip()[:300]}")
        rep.checks["drc"] = _read_json(os.path.join(out, "drc.json"))
        near = near_misses(rep.checks["drc"], opts.tolerance) if pcb_res.retune else []
        if near:
            # as little as it takes, in whole um; only the rules change, the board stays as it is
            tol = math.ceil((max(near) + 0.0002) * 1000 - 1e-9) / 1000
            pcb_res.netclasses, pcb_res.design_rules, pcb_res.dru = pcb_res.retune(tol)
            if pcb_res.dru:
                with open(os.path.join(out, f"{name}.kicad_dru"), "w", encoding="utf-8") as f:
                    f.write(pcb_res.dru)
            write_pro()
            subprocess.run([a for a in args if a not in ("--refill-zones", "--save-board")] + [pcb_path],
                           capture_output=True, text=True)
            rep.checks["drc"] = _read_json(os.path.join(out, "drc.json")) or rep.checks["drc"]
            rep.warnings.append(
                f"{len(near)} place(s) miss a clearance or width limit by {min(near) * 1000:.1f} to "
                f"{max(near) * 1000:.1f} um (rounding between EasyEDA's units and KiCad's): this board's rules are "
                f"written {tol * 1000:g} um lower so that they pass. --tolerance 0 keeps the rules exact")
        _remove(os.path.join(out, "drc.json"))
    if erc_job:
        step("erc", "Running KiCad's electrical-rule check")
        erc_job.result()
        ej = os.path.join(out, "erc.json")
        rep.checks["erc"] = _read_json(ej)
        _remove(ej)
    write_report(rep, proj, b, name)
    log(f"  {b.name} -> {out}")
    return rep


_LIMIT = re.compile(r"([\d.]+) mm; actual ([\d.]+) mm")


def near_misses(drc: dict | None, tolerance: float) -> list:
    """By how much (mm) the DRC errors that miss a minimum by `tolerance` or less miss it."""
    out = []
    if tolerance <= 0:
        return out
    for v in (drc or {}).get("violations", []):
        m = _LIMIT.search(v.get("description") or "")
        if m and v.get("severity") == "error":
            d = float(m.group(1)) - float(m.group(2))
            if 0 < d <= tolerance + 1e-9:
                out.append(d)
    return out


@functools.lru_cache(maxsize=None)
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


def finish_fills(cli: str | None, pcb_path: str, fixes: dict, unfilled: list | None = None) -> bool:
    """Grow EasyEDA's pour fills by their outline stroke and merge in the thermal spokes, and let KiCad fill the
    pours EasyEDA never built (kicad/fillfix.py)."""
    import json
    py = kicad_python(cli)
    if not py:
        return False
    fj = pcb_path + ".fills.json"
    with open(fj, "w", encoding="utf-8") as f:
        json.dump({"fixes": fixes, "fill": unfilled or []}, f)
    try:
        r = subprocess.run([py, os.path.join(os.path.dirname(__file__), "kicad", "fillfix.py"), pcb_path, fj],
                           capture_output=True, text=True, timeout=300)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        _remove(fj)


_categories_lock = threading.Lock()


def lcsc_categories(proj: Project, log=print, done=None) -> dict:
    """LCSC code -> category of the project's parts (tells e.g. a Zener from a diode).

    One search per part the first time; the answers are kept for good in the cache folder (what a part is doesn't
    change), apart from the searches of the parts browser, which go stale with stock and prices."""
    from .lcsc import CACHE
    codes = set()
    for dev in proj.devices.values():
        code = str((dev.get("attributes") or {}).get("Supplier Part") or "")
        if re.fullmatch(r"C\d+", code):
            codes.add(code)
    path = os.path.join(CACHE, "categories.json")
    with _categories_lock:  # boards translated side by side ask for the same parts
        known = _read_json(path) or {}
        todo = sorted(c for c in codes if c not in known)
        if todo:
            from .provider import _category, search

            def one(code):
                try:
                    search(code)
                except Exception:  # offline / LCSC error: asked again next time; designator and part-number rules
                    return code, None
                return code, _category.get(code) or ""
            with ThreadPoolExecutor(8) as ex:
                for n, (code, cat) in enumerate(ex.map(one, todo), 1):
                    if cat is not None:
                        known[code] = cat
                    if done:
                        done(n, len(todo))
            try:
                os.makedirs(CACHE, exist_ok=True)
                merged = dict(_read_json(path) or {}, **known)
                tmp = f"{path}.{os.getpid()}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    import json
                    json.dump(merged, f)
                os.replace(tmp, path)
            except OSError:
                pass
            log(f"  LCSC categories: {sum(1 for c in codes if known.get(c))}/{len(codes)} parts")
    return {c: known[c] for c in codes if known.get(c)}


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
        with ThreadPoolExecutor(1) as bg:  # KiCad reads the original while the first attempt is made
            plain_job = bg.submit(netlist, plain, "plain")
            want = None
            skip: set = set()
            for attempt in range(5):
                step("stock", "KiCad standard symbols: placing and checking connectivity"
                     + (f" (pass {attempt + 1})" if attempt else ""), 0.4 + 0.12 * attempt)
                sc = SchematicConverter(proj, proj.schematics[b.schematic], name, lib, fp_names, lib, placed_fp=placed,
                                        stock=True, categories=cats, stock_skip=skip)
                res = sc.convert()
                kn = netlist(res, f"stock{attempt}")
                got = _nets(kn)
                if want is None:
                    want = _nets(plain_job.result())
                bad = {ref for net in want ^ got for ref, _ in net}
                if not bad:
                    n = res.stats.get("stock_symbols", 0)
                    log(f"  KiCad standard symbols: {n} placed" + (f", {len(skip)} kept EasyEDA's" if skip else ""))
                    return res, kn
                skip |= bad
    rep.warnings.append("KiCad symbols: couldn't keep the connectivity identical, kept EasyEDA's symbols")
    return None


def _pads(fp_doc) -> list:
    """Pad numbers and positions of a footprint (KiCad mm, 0.05 mm grid), to tell whether two are the same."""
    fp = convert_footprint(fp_doc, L.LayerMap(), FpOptions())
    return sorted((p.number, round(p.at[0] / 0.05), round(p.at[1] / 0.05)) for p in fp.pads)


def _library_model(code: str, fp_doc) -> tuple:
    """(model uuid, transform) of the LCSC part's current library entry, if its footprint is this one."""
    from .lcsc import LcscError, _doc, _known_missing, _mark_missing, fetch_component, fetch_device
    # what the library answered is remembered for a week: a part without a model costs a request per import otherwise
    key = f"model-of-{code}-{fp_doc.uuid}"
    if _known_missing(key, 7 * 86400):
        raise LcscError(f"{code}: no usable 3D model in the library")
    at = fetch_device(code).get("attributes") or {}
    if not at.get("3D Model") or not at.get("Footprint"):
        _mark_missing(key)
        raise LcscError(f"{code}: no 3D model")
    if _pads(_doc(fetch_component(at["Footprint"]), "fp")) != _pads(fp_doc):
        _mark_missing(key)
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
    overrides = []  # (component id, footprint uuid, device attributes, the instance's own 3D model attributes)
    if b.pcb:
        comps: dict[str, dict] = {}
        for a in proj.pcbs[b.pcb].of("ATTR"):
            comps.setdefault(str(a.d.get("parentId") or ""), {})[a.str("key")] = a.str("value")
        for c in proj.pcbs[b.pcb].of("COMPONENT"):
            at = comps.get(c.id, {})
            dev = proj.devices.get(at.get("Device") or "", {}).get("attributes") or {}
            fu = at.get("Footprint") or str(dev.get("Footprint") or "")
            placed.append((fu, dev))
            own = {k: str(v) for k, v in (c.d.get("attrs") or {}).items()
                   if k in ("3D Model", "3D Model Title", "3D Model Transform") and v}
            if any(own.get(k) != str(dev.get(k) or "") for k in own):
                overrides.append((c.id, fu, dev, own))
    else:
        placed = [(str((d.get("attributes") or {}).get("Footprint") or ""), d.get("attributes") or {})
                  for d in proj.devices.values()]
    wanted: dict[str, list] = {}  # footprint uuid -> candidates, best first: () -> (model uuid, transform, note)
    named: dict[str, str] = {}  # footprint uuid -> title of the model the project asks for
    for fu, at in placed:
        if fu not in proj.footprints:
            continue
        mu, title = str(at.get("3D Model") or ""), str(at.get("3D Model Title") or "")
        tr = str(at.get("3D Model Transform") or "")
        code = str(at.get("Supplier Part") or "").strip().upper()
        cands = wanted.setdefault(fu, [])
        if mu:
            named.setdefault(fu, title or mu)
            cands.append(lambda mu=mu, tr=tr: (mu, tr, ""))
        if mu and title:
            cands.append(lambda title=title, tr=tr: (find_model(title), tr, f"model '{title}' found by name"))
        if re.fullmatch(r"C\d+", code):
            cands.append(lambda code=code, fp=proj.footprints[fu]: _library_model(code, fp)
                         + (f"model of {code} from the library",))
    models, failed, notes, lost = {}, 0, [], []
    shapes = os.path.join(out, f"{lib}.3dshapes")
    todo = sorted((fu, c) for fu, c in wanted.items() if c and fp_names.get(fu))

    def resolve(cands):
        for cand in cands:
            try:
                mu, tr, note = cand()
                return tr, note, fetch_step({"3D Model": mu})
            except (LcscError, ValueError, OSError):
                continue
        return None

    # the lookups are independent web requests: run them side by side
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as pool:
        for n, ((fu, _), got) in enumerate(zip(todo, pool.map(lambda t: resolve(t[1]), todo))):
            step("3d", f"Downloading 3D models {n + 1}/{len(todo)}", n / len(todo))
            if got is None:
                if fu in named:  # a part EasyEDA has no model for either isn't a failure
                    failed += 1
                    lost.append(f"{fp_names[fu]} ({named[fu]})")
                continue
            tr, note, data = got
            name = fp_names[fu]
            os.makedirs(shapes, exist_ok=True)
            with open(os.path.join(shapes, f"{name}.step"), "wb") as f:
                f.write(data)
            models[fu] = model_from_transform(f"${{KIPRJMOD}}/{lib}.3dshapes/{name}.step", tr, step_extent(data))
            if note:
                notes.append(f"{name}: {note}")
    # components with their own model or transform (instance attributes override the device's)
    for cid, fu, dev, own in overrides:
        name = fp_names.get(fu)
        if not name or fu not in proj.footprints:
            continue
        mu = own.get("3D Model") or str(dev.get("3D Model") or "")
        title = own.get("3D Model Title") or str(dev.get("3D Model Title") or "")
        tr = own.get("3D Model Transform") or str(dev.get("3D Model Transform") or "")
        same = mu == str(dev.get("3D Model") or "") and fu in models
        path = os.path.join(shapes, f"{name}.step" if same else f"{name}__{cid}.step")
        try:
            if same:
                with open(path, "rb") as f:
                    data = f.read()
            else:
                try:
                    data = fetch_step({"3D Model": mu})
                except (LcscError, ValueError, OSError):
                    if not title:
                        raise
                    data = fetch_step({"3D Model": find_model(title)})
                os.makedirs(shapes, exist_ok=True)
                with open(path, "wb") as f:
                    f.write(data)
        except (LcscError, ValueError, OSError):
            failed += 1
            continue
        models[f"comp:{cid}"] = model_from_transform(f"${{KIPRJMOD}}/{lib}.3dshapes/{os.path.basename(path)}", tr,
                                                     step_extent(data))
    log(f"  3D models: {len(models)} added" + (f" ({len(notes)} replaced by the library's current one)" if notes
                                                else "") + (f", {failed} not available" if failed else ""))
    if notes:
        by_name = sum(1 for x in notes if "found by name" in x)
        rep.warnings.append(f"{len(notes)} 3D model(s) linked to the EasyEDA library's current version (the project's "
                            f"ids are gone): {by_name} by model name, {len(notes) - by_name} by LCSC number")
    if failed:
        rep.warnings.append(f"{failed} 3D model(s) are no longer in the EasyEDA library (not under their id, their "
                            f"name or the part's LCSC number)" + (": " + ", ".join(sorted(lost)) if lost else ""))
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
    "net_conflict": "the EasyEDA PCB and its schematic disagree here (a pad on other copper than its pin's net, or a "
                    "pad without a pin): the board keeps the copper it has",
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
        ok = identical(con)
        L_ += ["## Schematic ↔ PCB connectivity", "",
               f"{con['nets_checked']} PCB nets compared with KiCad's netlist of the translated schematic: "
               + ("**identical**." if ok else "**differences found**:"), ""]
        for en, ks in con["split"]:
            L_.append(f"- EasyEDA net `{en}` maps to several schematic nets: {ks}")
        for k, ens in con["merged"].items():
            L_.append(f"- schematic net `{k}` joins PCB nets {ens}")
        for nd, en in con["missing"][:50]:
            L_.append(f"- pad {nd[0]}.{nd[1]} (net `{en}`) has no schematic pin")
        for nd, en in (con.get("open") or [])[:50]:
            L_.append(f"- pad {nd[0]}.{nd[1]} (net `{en}`) is not connected in the schematic")
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
