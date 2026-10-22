"""EasyEDA Pro schematic -> KiCad 10 hierarchical schematic."""

from __future__ import annotations

import datetime
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

from ..easyeda.project import Doc, Page, Project, Schematic
from ..easyeda.records import Rec
from ..sexp import NO, YES, Sym, stable_uuid
from .board import part_fields, resolve_attr, resolve_value
from .footprint import sane_name
from .symbol import (U, LibSymbol, _record_points, align_justify, body_end, convert_symbol, effects, lib_symbol_sexp,
                     make_power_symbol, power_body_dir, r4, text_size)

PAPERS = {"A5": (210, 148), "A4": (297, 210), "A3": (420, 297), "A2": (594, 420), "A1": (841, 594),
          "A0": (1189, 841), "A": (279.4, 215.9), "B": (431.8, 279.4), "C": (558.8, 431.8),
          "D": (863.6, 558.8), "E": (1117.6, 863.6)}
GRID = 1.27
AUTO_NET = re.compile(r"^\$\d*N\d+$|^\$[0-9A-Za-z]+N\d+$")


def snap(v: float, g: float = GRID) -> float:
    return r4(round(v / g) * g)


def rot_pt(p, deg):
    if not deg:
        return p
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return (p[0] * c - p[1] * s, p[0] * s + p[1] * c)


@dataclass
class SchResult:
    files: dict  # filename -> sexp
    lib_symbols: list  # symbol definitions for the project library
    links: dict  # unique id / designator -> (path, sheetname, sheetfile)
    footprints_used: set
    warnings: list
    stats: dict
    pin_info: dict  # (ref, pin number) -> (name, type)
    sheets_meta: list  # for .kicad_pro "sheets"
    root_file: str = ""


@dataclass
class PageCtx:
    page: Page
    uuid: str  # sheet uuid (sheet symbol in root) or root uuid for single page
    file: str
    path: str  # instance path, e.g. "/root/sheet"
    ox: float = 0.0
    oy: float = 0.0
    paper: object = "A4"
    title: dict = field(default_factory=dict)
    items: list = field(default_factory=list)
    lib_ids: dict = field(default_factory=dict)  # lib_id -> symbol def
    pins: list = field(default_factory=list)  # (point, kind)
    wires: list = field(default_factory=list)  # (a, b, wire_id)
    labels_net: dict = field(default_factory=dict)
    extra_segs: list = field(default_factory=list)  # (a, b, id) wires synthesised by the converter
    obstacles: tuple | None = None  # (points, segments) a stock symbol's new pins / wires must not touch


class SchematicConverter:
    def __init__(self, proj: Project, sch: Schematic, name: str, lib_nick: str, fp_names: dict, fp_lib_nick: str,
                 placed_fp: dict | None = None, stock: bool = False, categories: dict | None = None,
                 stock_skip: set | None = None):
        self.proj = proj
        self.sch = sch
        self.name = name
        self.lib = lib_nick
        self.fp_names = fp_names
        self.fp_lib = fp_lib_nick
        self.placed_fp = placed_fp or {}
        self.root_uuid = stable_uuid("root", sch.uuid)
        self.symbols: dict[str, LibSymbol] = {}  # easyeda symbol uuid -> LibSymbol
        self.sym_names: set[str] = set()
        self.custom_power: dict[str, list] = {}
        self.warn: list[str] = []
        self.links: dict = {}
        self.fp_used: set[str] = set()
        self.pin_info: dict = {}
        self.stats = defaultdict(int)
        self.pwr_counter = 0
        # KiCad standard symbols instead of EasyEDA's drawings (see ..stock); categories: LCSC code -> category
        self.stock = stock
        self.categories = categories or {}
        self.stock_skip = stock_skip or set()  # designators that keep EasyEDA's symbol
        self.stock_defs: dict = {}  # (symbol uuid, device uuid) -> stock definition or None
        self.stock_nodes: dict[str, list] = {}  # project library name -> symbol node
        self.stock_names: dict = {}  # (lib, name, pin map) -> project library name

    # ------------------------------------------------------------------ helpers
    def lib_symbol(self, sym_uuid: str) -> LibSymbol | None:
        if sym_uuid in self.symbols:
            return self.symbols[sym_uuid]
        doc = self.proj.symbols.get(sym_uuid)
        if doc is None:
            return None
        base = sane_name(doc.title)
        nm = base
        k = 2
        while nm.lower() in self.sym_names:
            nm = f"{base}_{k}"
            k += 1
        self.sym_names.add(nm.lower())
        ls = convert_symbol(doc, nm)
        self.symbols[sym_uuid] = ls
        return ls

    def P(self, ctx: PageCtx, x, y):
        return (r4(ctx.ox + x * U), r4(ctx.oy - y * U))

    # ------------------------------------------------------------------ main
    def convert(self) -> SchResult:
        pages = self.sch.pages
        multi = len(pages) > 1
        files = {}
        ctxs = []
        used_files = set()
        root_file = f"{self.name}.kicad_sch"
        for i, pg in enumerate(pages):
            if multi:
                fn = sane_name(pg.name) or f"page{i + 1}"
                fn = re.sub(r"[^\w.\-]+", "_", fn, flags=re.UNICODE)
                base = fn
                k = 2
                while fn.lower() in used_files or f"{fn}.kicad_sch" == root_file:
                    fn = f"{base}_{k}"
                    k += 1
                used_files.add(fn.lower())
                sheet_uuid = stable_uuid("sheet", pg.uuid)
                ctx = PageCtx(pg, sheet_uuid, f"{fn}.kicad_sch", f"/{self.root_uuid}/{sheet_uuid}")
            else:
                ctx = PageCtx(pg, self.root_uuid, root_file, f"/{self.root_uuid}")
            ctxs.append(ctx)
        # global net usage across pages (EasyEDA nets are global by name)
        self.net_pages = defaultdict(set)
        for ctx in ctxs:
            for name in self._page_net_names(ctx.page.doc):
                self.net_pages[name].add(ctx.page.uuid)
        for i, ctx in enumerate(ctxs):
            self._convert_page(ctx, i + 1, len(ctxs))
            files[ctx.file] = self._page_sexp(ctx, i + 1, is_root=not multi)
        sheets_meta = [[self.root_uuid, "Root"]]
        if multi:
            files[root_file] = self._root_sexp(ctxs)
            for ctx in ctxs:
                sheets_meta.append([ctx.uuid, ctx.page.name])
        lib_syms = []
        for ls in sorted(self.symbols.values(), key=lambda s: s.name):
            if ls.kind in (18, 19, 20):
                continue
            lib_syms.append(lib_symbol_sexp(ls))
        for nm, node in sorted(self.custom_power.items()):
            lib_syms.append(node)
        for nm, node in sorted(self.stock_nodes.items()):
            lib_syms.append(node)
        self.stats["pages"] = len(ctxs)
        return SchResult(files, lib_syms, self.links, self.fp_used, self.warn, dict(self.stats), self.pin_info,
                         sheets_meta, root_file)

    def _page_net_names(self, doc: Doc) -> set:
        out = set()
        attrs = defaultdict(dict)
        for a in doc.of("ATTR"):
            attrs[a.d.get("parentId")][a.str("key")] = a
        for w in doc.of("WIRE"):
            a = attrs[w.id].get("NET")
            if a and a.str("value"):
                out.add(a.str("value"))
        for c in doc.of("COMPONENT"):
            at = attrs[c.id]
            st = self._sym_type(at)
            if st in (18, 19):
                out.add(self._port_name(at))
        return out

    def _sym_uuid(self, at: dict) -> str:
        s = at.get("Symbol")
        if s is not None and s.str("value"):
            return s.str("value")
        dev = at.get("Device")
        if dev is not None:
            return str(self.proj.devices.get(dev.str("value"), {}).get("attributes", {}).get("Symbol") or "")
        return ""

    def _sym_type(self, at: dict) -> int:
        return self.proj.symbol_type(self._sym_uuid(at))

    def _port_name(self, at: dict) -> str:
        g = at.get("Global Net Name")
        if g is not None and g.str("value"):
            return g.str("value")
        n = at.get("Name")
        v = n.str("value") if n is not None else ""
        if v.startswith("="):
            dev = at.get("Device")
            dattrs = self.proj.devices.get(dev.str("value"), {}).get("attributes", {}) if dev else {}
            v = resolve_attr(dict(dattrs, **{k: a.str("value") for k, a in at.items()}), "Name")
        return v

    # ------------------------------------------------------------------ page
    def _frame_box(self, frame: Rec, at: dict):
        """Absolute extent (x0, y0, x1, y1) of an EasyEDA sheet frame, EasyEDA units, Y-up."""
        fx, fy = frame.num("x"), frame.num("y")
        w, h = at.get("Width"), at.get("Height")
        try:
            W, H = float(w.str("value")), float(h.str("value"))
            if W > 0 and H > 0:
                return (fx, fy, fx + W, fy + H)  # v3: anchored bottom-left
        except (AttributeError, ValueError):
            pass
        doc = self.proj.symbols.get(self._sym_uuid(at))
        pts = []
        if doc is not None:
            for r in doc.recs:
                if r.type in ("RECT", "POLY", "LINE"):
                    pts += _record_points(r)
        if not pts:
            return None
        return (fx + min(p[0] for p in pts), fy + min(p[1] for p in pts),
                fx + max(p[0] for p in pts), fy + max(p[1] for p in pts))

    def _setup_page(self, ctx: PageCtx, comps: list, attrs: dict):
        frame = None
        for c in comps:
            if self._sym_type(attrs[c.id]) == 20:
                frame = c
                break
        box = None
        if frame is not None:
            at = attrs[frame.id]
            ctx.title = {k: at[k].str("value") for k in at}
            box = self._frame_box(frame, at)
            if box is None:
                ps = at.get("Page Size")
                name = ps.str("value").upper() if ps is not None else ""
                pw, ph = PAPERS.get(name, PAPERS["A4"])
                fx, fy = frame.num("x"), frame.num("y")
                box = (fx, fy, fx + pw / U, fy + ph / U)
        if box is None:
            pts = []
            for r in ctx.page.doc.recs:
                if r.type == "COMPONENT":
                    pts.append((r.num("x"), r.num("y")))
                elif r.type == "WIRE":
                    for sg in r.d.get("segments") or []:
                        pts += [(sg[0], sg[1]), (sg[2], sg[3])]
            if pts:
                x0, y0 = min(p[0] for p in pts), min(p[1] for p in pts)
                x1, y1 = max(p[0] for p in pts), max(p[1] for p in pts)
                mx, my = 40 / U, 60 / U  # room for the title block
                box = (x0 - mx / 2, y0 - my, x1 + mx / 2, y1 + my / 3)
            else:
                box = (0, 0, PAPERS["A4"][0] / U, PAPERS["A4"][1] / U)
        fw, fh = (box[2] - box[0]) * U, (box[3] - box[1]) * U
        portrait = fh > fw * 1.05
        lw, lh = (fh, fw) if portrait else (fw, fh)
        # smallest standard sheet the frame fits on (2 % tolerance: EasyEDA frames are ~297.2 mm wide)
        fits = [k for k in ("A5", "A4", "A3", "A2", "A1", "A0") if PAPERS[k][0] >= lw * 0.98 and PAPERS[k][1] >= lh * 0.98]
        paper = fits[0] if fits else None
        if paper and (PAPERS[paper][0] > lw * 1.3 and PAPERS[paper][1] > lh * 1.3):
            paper = None  # odd size: use a user sheet instead of floating in a big one
        if paper:
            pw, ph = PAPERS[paper]
            if portrait:
                pw, ph = ph, pw
            ctx.paper = [paper, Sym("portrait")] if portrait else paper
        else:
            pw, ph = math.ceil(fw / GRID) * GRID, math.ceil(fh / GRID) * GRID
            ctx.paper = ["User", r4(pw), r4(ph)]
        # centre the frame on the sheet, grid-aligned
        ctx.ox = snap((pw - fw) / 2 - box[0] * U)
        ctx.oy = snap((ph - fh) / 2 + box[3] * U)

    def _convert_page(self, ctx: PageCtx, page_no: int, page_count: int):
        doc = ctx.page.doc
        attrs: dict[str, dict] = defaultdict(dict)
        for a in doc.of("ATTR"):
            attrs[a.d.get("parentId")][a.str("key")] = a
        comps = doc.of("COMPONENT")
        self._setup_page(ctx, comps, attrs)
        for c in comps:
            at = attrs[c.id]
            st = self._sym_type(at)
            try:
                if st == 20:
                    continue
                if st == 22:
                    self._short(ctx, c, at)
                elif st == 18:
                    self._power(ctx, c, at)
                elif st == 19:
                    self._netport(ctx, c, at)
                else:
                    self._symbol(ctx, c, at)
            except Exception as e:  # pragma: no cover - keep converting
                self.warn.append(f"{ctx.page.name}: component {c.id}: {e!r}")
        self._wires(ctx, doc, attrs)
        for r in doc.recs:
            if r.type in ("TEXT", "RECT", "POLY", "LINE", "CIRCLE", "ELLIPSE", "ARC", "BEZIER"):
                g = self._graphic(ctx, r)
                if g is not None:
                    ctx.items.append(g)

    # ------------------------------------------------------------------ transforms
    def _xf(self, c: Rec):
        """Local symbol point (EasyEDA units, Y-up) -> absolute EasyEDA point."""
        ang = c.num("rotation")
        mir = c.d.get("isMirror") in (True, 1)
        cx, cy = c.num("x"), c.num("y")

        # rotate, then mirror about the symbol's vertical axis (verified against wire endpoints)
        def f(p):
            x, y = rot_pt(p, ang)
            if mir:
                x = -x
            return (cx + x, cy + y)

        def fdir(d):
            d = (d + ang) % 360
            return (180 - d) % 360 if mir else d

        return f, fdir

    # ------------------------------------------------------------------ components
    def _symbol(self, ctx: PageCtx, c: Rec, at: dict):
        sym_uuid = self._sym_uuid(at)
        ls = self.lib_symbol(sym_uuid)
        if ls is None:
            self.warn.append(f"{ctx.page.name}: symbol {sym_uuid} missing for component {c.id}")
            return
        dev_uuid = at["Device"].str("value") if "Device" in at else ""
        dev = self.proj.devices.get(dev_uuid, {})
        dattrs = dict(dev.get("attributes", {}))
        cattrs = {k: a.str("value") for k, a in at.items()}
        merged = dict(dattrs)
        merged.update({k: v for k, v in cattrs.items() if v})
        ref = cattrs.get("Designator") or (ls.ref_prefix + "?")
        value = resolve_value(dattrs, {k: v for k, v in cattrs.items() if k in ("Value", "Name") and v}, dev.get("title", ""))
        fp_uuid = self.placed_fp.get(cattrs.get("Unique ID")) or self.placed_fp.get(cattrs.get("Channel ID")) \
            or cattrs.get("Footprint") or dattrs.get("Footprint") or ""
        fp = ""
        if fp_uuid and fp_uuid in self.fp_names:
            fp = f"{self.fp_lib}:{self.fp_names[fp_uuid]}"
            self.fp_used.add(fp_uuid)
        unit = ls.unit_of(c.str("partId"))
        uid = stable_uuid(ctx.page.uuid, "sym", c.id)
        pos = self.P(ctx, c.num("x"), c.num("y"))
        mir = c.d.get("isMirror") in (True, 1)
        # EasyEDA and KiCad both rotate first and then mirror (x -> -x): same angle
        ang = int(round(c.num("rotation"))) % 360
        f, fdir = self._xf(c)
        plan = self._stock_plan(ctx, c, ls, sym_uuid, dev_uuid, merged, ref, unit, f) if self.stock else None
        if plan:
            lib_id, unit, pos, ang, mir, spins = plan
        else:
            lib_id = f"{self.lib}:{ls.name}"
            ctx.lib_ids[lib_id] = ls
        n = [Sym("symbol"), [Sym("lib_id"), lib_id], [Sym("at"), pos[0], pos[1], ang]]
        if mir:
            n.append([Sym("mirror"), Sym("y")])
        in_bom = str(merged.get("Add into BOM", "yes")).lower() not in ("no", "false", "0")
        on_board = str(merged.get("Convert to PCB", "yes")).lower() not in ("no", "false", "0")
        n += [[Sym("unit"), unit], [Sym("body_style"), 1], [Sym("exclude_from_sim"), NO],
              [Sym("in_bom"), YES if in_bom else NO], [Sym("on_board"), YES if on_board else NO],
              [Sym("in_pos_files"), YES if on_board else NO], [Sym("dnp"), NO], [Sym("uuid"), uid]]
        bb = ls.bbox.get(unit, (-5, -5, 5, 5))
        # default field spots (above / below the body) if EasyEDA has no position
        top = self.P(ctx, *f(((bb[0] + bb[2]) / 2, bb[3] + 5)))
        bot = self.P(ctx, *f(((bb[0] + bb[2]) / 2, bb[1] - 5)))
        ref_at = val_at = None
        if plan:  # KiCad-style spots next to the stock drawing: right of an upright part, above / below a lying one
            xs, ys = [q[0][0] for q in spins], [q[0][1] for q in spins]
            cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
            column = len(spins) >= 3 and (len(set(xs)) == 1 or len(set(ys)) == 1)  # connectors: body beside pins
            if max(ys) - min(ys) >= max(xs) - min(xs) and not column:
                ref_at = ((r4(max(xs) + 2.54), r4(cy - 1.27)), (0, 1))
                val_at = ((r4(max(xs) + 2.54), r4(cy + 1.27)), (0, 1))
            elif len(spins) == 2:  # lying two-pin part: "R1 10k" on one line above it, like EasyEDA lays them out
                ref_at = ((r4(cx - 0.635), r4(min(ys) - 2.54)), (2, 2))
                val_at = ((r4(cx + 0.635), r4(min(ys) - 2.54)), (0, 2))
            else:
                ref_at = ((r4(cx), r4(min(ys) - 2.54)), (1, 2))
                val_at = ((r4(cx), r4(max(ys) + 2.54)), (1, 0))
                if column and len(set(xs)) == 1:  # over the body, which is beside the pin column (at the origin)
                    ref_at = ((r4(pos[0]), r4(min(ys) - 2.54)), (1, 2))
                    val_at = ((r4(pos[0]), r4(max(ys) + 2.54)), (1, 0))
        # instance attributes without a position are auto-placed by EasyEDA (the symbol's own attribute
        # is hidden inside the body) -> centred above / below the body
        vattr = at.get("Value") if _visible(at.get("Value")) else at.get("Name")
        if plan:  # EasyEDA's spots were laid out by the designer: keep them unless they'd sit on the new body
            x0, x1 = min(q[0][0] for q in spins) - 1.27, max(q[0][0] for q in spins) + 1.27
            y0, y1 = min(q[0][1] for q in spins) - 1.27, max(q[0][1] for q in spins) + 1.27
            for a_, nm_ in ((at.get("Designator"), "ref"), (vattr, "val")):
                if a_ is not None and a_.d.get("x") is not None and a_.d.get("y") is not None:
                    px, py = self.P(ctx, a_.num("x"), a_.num("y"))
                    if not (x0 <= px <= x1 and y0 <= py <= y1):
                        ref_at, val_at = (None, val_at) if nm_ == "ref" else (ref_at, None)
        n.append(self._field(ctx, "Reference", ref, at.get("Designator"), top, ang, mirror=mir, place=ref_at))
        n.append(self._field(ctx, "Value", value, vattr, bot, ang, mirror=mir, place=val_at))
        n.append(self._field(ctx, "Footprint", fp, None, pos, ang, hide=True))
        ds = dattrs.get("Datasheet", "")
        n.append(self._field(ctx, "Datasheet", ds if not ds.startswith("=") else "", None, pos, ang, hide=True))
        desc = dev.get("description") or dattrs.get("Description", "")
        n.append(self._field(ctx, "Description", desc if not desc.startswith("=") else "", None, pos, ang, hide=True))
        extra = part_fields(merged)
        for k, v in extra.items():
            n.append(self._field(ctx, k, v, None, pos, ang, hide=True))
        pins = ls.pins_of(unit)
        numbers = {q[1] for q in spins} if plan else {p.number for p in pins if p.number}
        for p in sorted(numbers, key=_natkey):
            n.append([Sym("pin"), p, [Sym("uuid"), stable_uuid(uid, "pin", p)]])
        n.append([Sym("instances"), [Sym("project"), self.name,
                                      [Sym("path"), ctx.path,
                                       [Sym("reference"), ref], [Sym("unit"), unit]]]])
        ctx.items.append(n)
        if plan:
            for pt, num, pname, etype in spins:
                ctx.pins.append((pt, "pin"))
                for pad in num.strip("[]").split(","):
                    if ref and pad:
                        self.pin_info[(ref, pad)] = (pname, etype)
        else:
            for p in pins:
                ctx.pins.append((self.P(ctx, *f((p.x, p.y))), "pin"))
                if ref and p.number:
                    self.pin_info[(ref, p.number)] = (p.name, p.etype)
        # link for the PCB
        sheet_path = "/" + "/".join(x for x in ctx.path.split("/")[2:] if x)  # drop root uuid
        pcb_path = (sheet_path.rstrip("/") + "/" + uid) if sheet_path != "/" else "/" + uid
        link = (pcb_path, f"/{ctx.page.name}/" if ctx.uuid != self.root_uuid else "/", ctx.file)
        fields = {"Reference": ref, "Value": value, "Datasheet": ds if not ds.startswith("=") else "",
                  "Description": desc if not desc.startswith("=") else ""}
        fields.update(extra)
        for key in (cattrs.get("Unique ID"), cattrs.get("Channel ID"), ref):
            if key and (unit == 1 or key not in self.links):
                self.links[key] = link + (fields,)
        self.stats["symbols"] += 1

    def _stock_def(self, ls, sym_uuid: str, dev_uuid: str, merged: dict):
        """KiCad stock symbol for this EasyEDA symbol + device: (lib_id, {unit: [(x, y, number, name, type)]},
        pads it maps), or None."""
        key = (sym_uuid, dev_uuid)
        if key in self.stock_defs:
            return self.stock_defs[key]
        from ..stock import choose, stock_symbol  # noqa: PLC0415 - optional feature
        attrs = {k: ("" if v is None else str(v)) for k, v in merged.items()}
        cat = self.categories.get(attrs.get("Supplier Part", ""))
        if cat:
            attrs["LCSC Category"] = cat
        doc = self.proj.symbols.get(sym_uuid)
        res = None
        choice = choose(attrs, ls, doc)
        if choice:
            sig = (choice[0], choice[1], tuple(sorted(choice[2].items())))
            node = stock_symbol(attrs, ls, lib_symbol_sexp(ls), doc, choice)
            if node is not None:
                nm = self.stock_names.get(sig)
                if nm is None:
                    base = sane_name(choice[1])
                    nm, k = base, 2
                    while nm.lower() in self.sym_names:
                        nm, k = f"{base}_{k}", k + 1
                    self.sym_names.add(nm.lower())
                    self.stock_names[sig] = nm
                    old = node[1]
                    node = [node[0], nm] + [[x[0], nm + x[1][len(old):]] + x[2:] if isinstance(x, list) and x
                                            and x[0] == "symbol" else x for x in node[2:]]
                    node = [[x[0], x[1], choice[1]] + x[3:] if isinstance(x, list) and x[:2] == [Sym("property"), "Value"]
                            else x for x in node]
                    self.stock_nodes[nm] = node
                node = self.stock_nodes[nm]
                units: dict[int, list] = defaultdict(list)
                for sub in node:
                    if not (isinstance(sub, list) and sub and sub[0] == "symbol"):
                        continue
                    u, style = (int(v) for v in sub[1].rsplit("_", 2)[-2:])
                    if style not in (0, 1):
                        continue
                    for pin in sub:
                        if isinstance(pin, list) and pin and pin[0] == "pin":
                            at = next(x for x in pin if isinstance(x, list) and x[0] == "at")
                            nme = next(x[1] for x in pin if isinstance(x, list) and x[0] == "name")
                            num = next(x[1] for x in pin if isinstance(x, list) and x[0] == "number")
                            units[u].append((float(at[1]), float(at[2]), str(num), str(nme), str(pin[1])))
                common = units.pop(0, [])
                per_unit = {u: v + common for u, v in units.items()} or {1: common}
                pads = {x for v in choice[2].values() for x in v.strip("[]").split(",")}
                res = (f"{self.lib}:{nm}", per_unit, pads)
                full = [Sym("symbol"), res[0]] + node[2:]
                self.stock_nodes_full = getattr(self, "stock_nodes_full", {})
                self.stock_nodes_full[res[0]] = full
        self.stock_defs[key] = res
        return res

    def _stock_plan(self, ctx, c, ls, sym_uuid, dev_uuid, merged, ref, unit, f):
        """Place KiCad's stock symbol where EasyEDA's was: best rotation / mirror / position for the old pin
        spots, with short wires from each old pin spot to the new pin.  None = keep EasyEDA's symbol."""
        if ref in self.stock_skip:
            return None
        sd = self._stock_def(ls, sym_uuid, dev_uuid, merged)
        if sd is None:
            return None
        lib_id, per_unit, pads = sd
        old = {}  # pad -> absolute point of EasyEDA's pin
        for p in ls.pins_of(unit):
            if p.number:
                old[p.number] = self.P(ctx, *f((p.x, p.y)))
        mine = set(old) & pads
        # the stock unit that has exactly this EasyEDA unit's (mapped) pins
        cand = [u for u, pins in per_unit.items()
                if {x for q in pins for x in q[2].strip("[]").split(",")} == mine]
        if len(cand) != 1 or not mine:
            return None
        su = cand[0]
        pins = per_unit[su]
        if any(p.number not in pads and not _norm_nc(p.name) for p in ls.pins_of(unit) if p.number):
            return None  # an EasyEDA pin with a function the stock symbol doesn't have
        ang0 = int(round(c.num("rotation"))) % 360
        mir0 = c.d.get("isMirror") in (True, 1)
        tries = []
        for a in (ang0, (ang0 + 90) % 360, (ang0 + 180) % 360, (ang0 + 270) % 360):
            for m in (mir0, not mir0):
                loc = []
                for x, y, num, _, _ in pins:
                    lx, ly = rot_pt((x, y), a)
                    loc.append((-lx if m else lx, -ly))  # schematic offset (y down)
                tgt = []
                for x, y, num, _, _ in pins:
                    pts = [old[n] for n in num.strip("[]").split(",") if n in old]
                    tgt.append((sum(q[0] for q in pts) / len(pts), sum(q[1] for q in pts) / len(pts)))
                X = snap(sum(t[0] - l[0] for t, l in zip(tgt, loc)) / len(loc))
                Y = snap(sum(t[1] - l[1] for t, l in zip(tgt, loc)) / len(loc))
                new = [(r4(X + l[0]), r4(Y + l[1])) for l in loc]
                moves = [math.hypot(q[0] - old[n][0], q[1] - old[n][1]) for (x, y, num, _, _), q in zip(pins, new)
                         for n in num.strip("[]").split(",") if n in old]
                if max(moves) <= 7.62:  # beyond that the drawing doesn't fit the existing wiring
                    tries.append((sum(moves), len(tries), a, m, (X, Y), new))
        for _, _, a, m, pos, new in sorted(tries):
            paths, mine = [], []
            for (x, y, num, _, _), q in zip(pins, new):
                olds = {old[n] for n in num.strip("[]").split(",") if n in old}
                mine.append((q, olds))
                for o in olds:
                    if o == q:
                        continue
                    if o[0] == q[0] or o[1] == q[1]:
                        paths.append((len(mine) - 1, [[o, q]]))
                    else:  # an L going around the body (corner farther from the symbol centre) or the other one
                        c1, c2 = (o[0], q[1]), (q[0], o[1])
                        d = lambda p: (p[0] - pos[0]) ** 2 + (p[1] - pos[1]) ** 2  # noqa: E731
                        paths.append((len(mine) - 1, [[o, c1, q], [o, c2, q]] if d(c1) >= d(c2) else
                                      [[o, c2, q], [o, c1, q]]))
            chosen = self._route(ctx, mine, paths)
            if chosen is not None:
                break
        else:
            return None
        spins = [(q, num, nme, etype) for (x, y, num, nme, etype), q in zip(pins, new)]
        for k, path in enumerate(chosen):
            for i, (s0, s1) in enumerate(zip(path, path[1:])):
                ctx.extra_segs.append((s0, s1, f"stk-{c.id}-{k}-{i}"))
        pts, segs = ctx.obstacles
        pts |= set(new) | {p for path in chosen for p in path}
        segs += [(a0, a1) for path in chosen for a0, a1 in zip(path, path[1:])]
        ctx.lib_ids[lib_id] = self.stock_nodes_full[lib_id]
        self.stats["stock_symbols"] += 1
        return lib_id, su, pos, a, m, spins

    def _page_obstacles(self, ctx):
        """Every wire end / wire run and every EasyEDA pin spot of the page (computed once per page)."""
        if ctx.obstacles is None:
            doc = ctx.page.doc
            segs, pts = [], set()
            for w in doc.of("WIRE"):
                for sg in w.d.get("segments") or []:
                    a, b = self.P(ctx, sg[0], sg[1]), self.P(ctx, sg[2], sg[3])
                    segs.append((a, b))
                    pts |= {a, b}
            attrs: dict[str, dict] = defaultdict(dict)
            for a in doc.of("ATTR"):
                attrs[a.d.get("parentId")][a.str("key")] = a
            for comp in doc.of("COMPONENT"):
                ls = self.lib_symbol(self._sym_uuid(attrs[comp.id]))
                if ls is None:
                    continue
                f, _ = self._xf(comp)
                for p in ls.pins_of(ls.unit_of(comp.str("partId"))):
                    pts.add(self.P(ctx, *f((p.x, p.y))))
            ctx.obstacles = (pts, segs)
        return ctx.obstacles

    def _route(self, ctx, mine: list, paths: list):
        """Pick wire paths (old pin spot -> new pin) that touch nothing they shouldn't; None if impossible.
        mine: per stock pin (new spot, its pads' old spots); paths: (stock pin index, alternative paths).
        A new pin may only land on its own old spot; a path may only touch its own old spot and its own new pin,
        not the page's wires / pins, nor this symbol's other pins and paths."""
        pts, segs = self._page_obstacles(ctx)
        for q, olds in mine:
            if q not in olds and (q in pts or any(_on_segment(q, a, b) for a, b in segs)):
                return None
        chosen, taken = [], defaultdict(list)  # stock pin -> its chosen paths

        def clear(i, path):
            ends = {path[0], path[-1]}
            others = {q for j, (q, _) in enumerate(mine) if j != i and q != mine[i][0]}
            others |= {o for j, (_, olds) in enumerate(mine) if j != i for o in olds}
            other_segs = [(a, b) for j, ps in taken.items() if j != i for pth in ps for a, b in zip(pth, pth[1:])]
            for a0, a1 in zip(path, path[1:]):
                if any(p not in ends and _on_segment(p, a0, a1) for p in pts | others):
                    return False  # runs over another wire end / pin
                if any(_on_segment(p, a0, a1) or _on_segment(a0, *sg) or _on_segment(a1, *sg)
                       for sg in other_segs for p in sg):
                    return False  # touches a path to another pin of this symbol
            return not any(_on_segment(p, a, b) for p in path[1:-1] for a, b in segs)  # corner on a wire
        for i, alts in paths:
            ok = next((pth for pth in alts if clear(i, pth)), None)
            if ok is None:
                return None
            chosen.append(ok)
            taken[i].append(ok)
        return chosen

    def _field(self, ctx, name, value, attr: Rec | None, default_pos, sym_ang, hide=False, mirror=False, place=None):
        """Symbol field at the EasyEDA attribute position (absolute alignment kept); place=(pos, (h, v)) puts it
        there instead (EasyEDA's spot was laid out for another drawing), keeping EasyEDA's visibility."""
        pos = default_pos
        hv = (1, 1)  # centred
        txt_ang = 0
        size = 1.27
        visible = not hide
        src = None
        if place is not None:
            pos, hv = place
            if attr is not None:
                visible = visible and _visible(attr)
        elif attr is not None:
            visible = visible and _visible(attr)
            if attr.d.get("x") is not None and attr.d.get("y") is not None:
                pos = self.P(ctx, attr.num("x"), attr.num("y"))
                src = attr
        elif not hide:
            visible = True
        if src is not None:
            hv = _align_hv(src, attr)
            txt_ang = int(round(src.num("rotation") or 0)) % 360
            fs = src.d.get("fontSize")
            if fs:
                size = text_size(float(fs))
        if txt_ang in (180, 270):  # KiCad keeps text readable: same box, opposite anchor
            hv = (2 - hv[0], 2 - hv[1])
        abs_ang = txt_ang % 180
        fa, just = _kicad_field_orient(abs_ang, hv, sym_ang, mirror)
        n = [Sym("property"), name, value or "", [Sym("at"), r4(pos[0]), r4(pos[1]), fa]]
        if not visible or not value:
            n.append([Sym("hide"), YES])
        n += [[Sym("show_name"), NO], [Sym("do_not_autoplace"), NO], effects(size, just)]
        return n

    def _power(self, ctx: PageCtx, c: Rec, at: dict):
        name = self._port_name(at)
        if not name:
            return
        sym_uuid = self._sym_uuid(at)
        ls = self.lib_symbol(sym_uuid)
        f, fdir = self._xf(c)
        if ls is not None and ls.pins:
            p = ls.pins[0]
            pin_abs = f((p.x, p.y))
            body = fdir(p.rot)
            ground = (p.rot % 360) == 270 or "gnd" in name.lower() or "ground" in (ls.name or "").lower()
        else:
            pin_abs = (c.num("x"), c.num("y"))
            body = (270 + c.num("rotation")) % 360
            ground = "gnd" in name.lower()
        lib_id, node = make_power_symbol(name, ground)
        if not lib_id.startswith("power:"):
            if name not in self.custom_power:
                self.custom_power[name] = node
            node = self.custom_power[name]
            full = f"{self.lib}:{name}"
            import copy
            n2 = copy.deepcopy(node)
            n2[1] = full
            ctx.lib_ids[full] = n2
            lib_id = full
        else:
            ctx.lib_ids[lib_id] = node
        kd = power_body_dir(node)
        ang = int(round((body - kd) / 90.0)) * 90 % 360
        pos = self.P(ctx, *pin_abs)
        pos = (snap(pos[0]), snap(pos[1]))
        uid = stable_uuid(ctx.page.uuid, "pwr", c.id)
        self.pwr_counter += 1
        ref = f"#PWR{self.pwr_counter:03d}"
        # value label offset like KiCad (3.81 mm beyond the pin, in the body direction)
        off = {0: (3.81, 0), 90: (0, -3.81), 180: (-3.81, 0), 270: (0, 3.81)}[int(round(body / 90.0)) * 90 % 360]
        vpos = (r4(pos[0] + off[0]), r4(pos[1] + off[1]))
        n = [Sym("symbol"), [Sym("lib_id"), lib_id], [Sym("at"), pos[0], pos[1], ang], [Sym("unit"), 1],
             [Sym("body_style"), 1], [Sym("exclude_from_sim"), NO], [Sym("in_bom"), YES], [Sym("on_board"), YES],
             [Sym("in_pos_files"), YES], [Sym("dnp"), NO], [Sym("uuid"), uid],
             [Sym("property"), "Reference", ref, [Sym("at"), pos[0], pos[1], 0], [Sym("hide"), YES],
              [Sym("show_name"), NO], [Sym("do_not_autoplace"), NO], effects()],
             [Sym("property"), "Value", name, [Sym("at"), vpos[0], vpos[1], 0], [Sym("show_name"), NO],
              [Sym("do_not_autoplace"), NO], effects()],
             [Sym("property"), "Footprint", "", [Sym("at"), pos[0], pos[1], 0], [Sym("hide"), YES],
              [Sym("show_name"), NO], [Sym("do_not_autoplace"), NO], effects()],
             [Sym("property"), "Datasheet", "", [Sym("at"), pos[0], pos[1], 0], [Sym("hide"), YES],
              [Sym("show_name"), NO], [Sym("do_not_autoplace"), NO], effects()],
             [Sym("property"), "Description", f'Power symbol creates a global label with name "{name}"',
              [Sym("at"), pos[0], pos[1], 0], [Sym("hide"), YES], [Sym("show_name"), NO],
              [Sym("do_not_autoplace"), NO], effects()],
             [Sym("pin"), "1", [Sym("uuid"), stable_uuid(uid, "pin")]],
             [Sym("instances"), [Sym("project"), self.name, [Sym("path"), ctx.path, [Sym("reference"), ref],
                                                              [Sym("unit"), 1]]]]]
        ctx.items.append(n)
        ctx.pins.append((pos, "power"))
        ctx.labels_net.setdefault(pos, set()).add(name)
        self.stats["power"] += 1

    def _short(self, ctx: PageCtx, c: Rec, at: dict):
        """EasyEDA "net short" symbol: merges two nets.  KiCad equivalent that keeps the
        schematic and the (single-net) PCB copper in sync is a plain wire between its pins."""
        ls = self.lib_symbol(self._sym_uuid(at))
        if ls is None or len(ls.pins) < 2:
            return
        f, _ = self._xf(c)
        pts = [self.P(ctx, *f((p.x, p.y))) for p in ls.pins[:2]]
        if pts[0] != pts[1]:
            ctx.extra_segs.append((pts[0], pts[1], f"short-{c.id}"))
        self.warn.append(f"{ctx.page.name}: net-short symbol replaced by a wire (nets merged, as on the EasyEDA PCB)")
        self.stats["net_shorts"] += 1

    def _netport(self, ctx: PageCtx, c: Rec, at: dict):
        name = self._port_name(at)
        if not name:
            return
        ls = self.lib_symbol(self._sym_uuid(at))
        f, fdir = self._xf(c)
        if ls is not None and ls.pins:
            p = ls.pins[0]
            pin_abs = f((p.x, p.y))
            body = fdir(p.rot)
        else:
            pin_abs = (c.num("x"), c.num("y"))
            body = c.num("rotation")
        title = (ls.name if ls else "").upper()
        shape = "bidirectional" if "BI" in title else "input" if "IN" in title else "output" if "OUT" in title \
            else "passive"
        pos = self.P(ctx, *pin_abs)
        pos = (snap(pos[0]), snap(pos[1]))
        # body direction in EasyEDA (Y-up) == label angle in KiCad (screen CCW)
        ang = int(round(body / 90.0)) * 90 % 360
        ctx.items.append(self._global_label(name, shape, pos, ang, stable_uuid(ctx.page.uuid, "port", c.id)))
        ctx.pins.append((pos, "label"))
        ctx.labels_net.setdefault(pos, set()).add(name)
        self.stats["global_labels"] += 1

    def _global_label(self, name, shape, pos, ang, uid):
        just = "left" if ang in (0, 90) else "right"
        return [Sym("global_label"), name, [Sym("shape"), Sym(shape)], [Sym("at"), pos[0], pos[1], ang],
                [Sym("fields_autoplaced"), YES], effects(1.27, [just]), [Sym("uuid"), uid],
                [Sym("property"), "Intersheetrefs", "${INTERSHEET_REFS}", [Sym("at"), pos[0], pos[1], 0],
                 [Sym("hide"), YES], [Sym("show_name"), NO], [Sym("do_not_autoplace"), NO], effects(1.27, [just])]]

    # ------------------------------------------------------------------ wires
    def _wires(self, ctx: PageCtx, doc: Doc, attrs: dict):
        segs = []
        for w in doc.of("WIRE"):
            for s in w.d.get("segments") or []:
                a, b = self.P(ctx, s[0], s[1]), self.P(ctx, s[2], s[3])
                if a != b:
                    segs.append((a, b, w.id))
        segs += ctx.extra_segs
        points = {p for p, _ in ctx.pins}
        segs = _prune_stock_stubs(segs, points)
        ends = defaultdict(int)
        for a, b, _ in segs:
            ends[a] += 1
            ends[b] += 1
        cand = set(ends) | points
        # split segments at T-junctions / pins lying on them
        out = []
        for a, b, wid in segs:
            mids = [p for p in cand if p != a and p != b and _on_segment(p, a, b)]
            if not mids:
                out.append((a, b, wid))
                continue
            mids.sort(key=lambda p: (p[0] - a[0]) ** 2 + (p[1] - a[1]) ** 2)
            chain = [a] + mids + [b]
            for p, q in zip(chain, chain[1:]):
                if p != q:
                    out.append((p, q, wid))
        # dedupe identical segments
        seen = set()
        segs = []
        for a, b, wid in out:
            k = (min(a, b), max(a, b))
            if k in seen:
                continue
            seen.add(k)
            segs.append((a, b, wid))
        ends = defaultdict(int)
        for a, b, _ in segs:
            ends[a] += 1
            ends[b] += 1
        for p, nwire in ends.items():
            npin = sum(1 for q, _ in ctx.pins if q == p)
            if nwire >= 3 or (nwire >= 2 and npin >= 1):
                ctx.items.append([Sym("junction"), [Sym("at"), p[0], p[1]], [Sym("diameter"), 0],
                                  [Sym("color"), 0, 0, 0, 0], [Sym("uuid"), stable_uuid(ctx.page.uuid, "j", p)]])
                self.stats["junctions"] += 1
        for i, (a, b, wid) in enumerate(segs):
            ctx.items.append([Sym("wire"), [Sym("pts"), [Sym("xy"), a[0], a[1]], [Sym("xy"), b[0], b[1]]],
                              [Sym("stroke"), [Sym("width"), 0], [Sym("type"), Sym("default")]],
                              [Sym("uuid"), stable_uuid(ctx.page.uuid, "w", wid, a, b)]])
        self.stats["wires"] += len(segs)
        ctx.wires = segs
        self._net_labels(ctx, doc, attrs, segs)

    def _net_labels(self, ctx: PageCtx, doc: Doc, attrs: dict, segs):
        # connected groups of wire segments (union-find on endpoints)
        parent = {}

        def find(x):
            while parent.setdefault(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        ends = defaultdict(int)
        for a, b, _ in segs:
            union(a, b)
            ends[a] += 1
            ends[b] += 1
        pin_pts = {p for p, _ in ctx.pins}
        group_names = defaultdict(set)
        for p, names in ctx.labels_net.items():
            if p in parent:
                group_names[find(p)] |= names
        wire_segs = defaultdict(list)
        for a, b, wid in segs:
            wire_segs[wid].append((a, b))
        done_groups = set()
        for w in doc.of("WIRE"):
            na = attrs[w.id].get("NET")
            if na is None:
                continue
            name = na.str("value")
            if not name or AUTO_NET.match(name):
                continue
            ss = wire_segs.get(w.id)
            if not ss:
                continue
            g = find(ss[0][0])
            if name in group_names.get(g, set()) or (g, name) in done_groups:
                continue
            done_groups.add((g, name))
            # label position: projection of the visible NET attribute, else middle of the longest segment
            if na.d.get("x") is not None and _visible(na):
                tgt = self.P(ctx, na.num("x"), na.num("y"))
            else:
                a, b = max(ss, key=lambda s: abs(s[0][0] - s[1][0]) + abs(s[0][1] - s[1][1]))
                tgt = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            # EasyEDA shows the net name on a wire that simply ends; KiCad style is a label sitting on the
            # free end of the wire (otherwise ERC reports a dangling wire) -> prefer the nearest free end
            free = []
            for a, b in ss:
                for p_, o_ in ((a, b), (b, a)):
                    if ends[p_] == 1 and p_ not in pin_pts:
                        free.append((p_, o_))
            if free:
                q, o = min(free, key=lambda t: (t[0][0] - tgt[0]) ** 2 + (t[0][1] - tgt[1]) ** 2)
                if abs(q[1] - o[1]) < 1e-6:
                    ang = 0 if q[0] > o[0] else 180
                else:
                    ang = 90 if q[1] < o[1] else 270
            else:
                best = None
                for a, b in ss:
                    q = _project(tgt, a, b)
                    d = (q[0] - tgt[0]) ** 2 + (q[1] - tgt[1]) ** 2
                    if best is None or d < best[0]:
                        best = (d, q, a, b)
                _, q, a, b = best
                q = _snap_on_segment(q, a, b)
                ang = 0 if abs(a[1] - b[1]) < 1e-6 else 90
            pin_pts.add(q)
            ends[q] += 1
            uid = stable_uuid(ctx.page.uuid, "lbl", w.id, name)
            if len(self.net_pages.get(name, ())) > 1:
                ctx.items.append(self._global_label(name, "passive", q, ang, uid))
                self.stats["global_labels"] += 1
            else:
                just = ["left", "bottom"] if ang in (0, 90) else ["right", "bottom"]
                ctx.items.append([Sym("label"), name, [Sym("at"), q[0], q[1], ang],
                                  [Sym("fields_autoplaced"), YES], effects(1.27, just),
                                  [Sym("uuid"), uid]])
                self.stats["labels"] += 1
            group_names[g].add(name)

    # ------------------------------------------------------------------ graphics
    def _graphic(self, ctx: PageCtx, r: Rec):
        P = lambda x, y: self.P(ctx, x, y)  # noqa: E731
        st = [Sym("stroke"), [Sym("width"), 0], [Sym("type"), Sym("default")]]
        fill = [Sym("fill"), [Sym("type"), Sym("none")]]
        uid = [Sym("uuid"), stable_uuid(ctx.page.uuid, "g", r.id)]
        t = r.type
        if t == "TEXT":
            txt = r.str("value")
            if not txt:
                return None
            a = P(r.num("x"), r.num("y"))
            size = text_size(r.num("fontSize", 7) or 7)
            just = align_justify(r) or ["left", "bottom"]
            return [Sym("text"), txt, [Sym("exclude_from_sim"), NO],
                    [Sym("at"), a[0], a[1], int(round(r.num("rotation"))) % 360],
                    effects(size, just, bold=r.d.get("bold") in (1, True) or r.d.get("fontWeight") == "bold"), uid]
        if t == "RECT":
            a = P(r.num("dotX1"), r.num("dotY1"))
            b = P(r.num("dotX2"), r.num("dotY2"))
            return [Sym("rectangle"), [Sym("start"), *a], [Sym("end"), *b], st, fill, uid]
        if t == "POLY":
            pts = [P(*p) for p in r.d.get("points") or []]
            if r.d.get("closed") in (True, 1) and pts and pts[0] != pts[-1]:
                pts.append(pts[0])
            if len(pts) < 2:
                return None
            return [Sym("polyline"), [Sym("pts")] + [[Sym("xy"), *p] for p in pts], st, fill, uid]
        if t == "LINE":
            a, b = P(r.num("startX"), r.num("startY")), P(r.num("endX"), r.num("endY"))
            if a == b:
                return None
            return [Sym("polyline"), [Sym("pts"), [Sym("xy"), *a], [Sym("xy"), *b]], st, fill, uid]
        if t == "CIRCLE":
            c = P(r.num("centerX"), r.num("centerY"))
            return [Sym("circle"), [Sym("center"), *c], [Sym("radius"), r4(abs(r.num("radius")) * U)], st, fill, uid]
        if t == "ARC":
            s, m, e = P(r.num("startX"), r.num("startY")), P(r.num("referenceX"), r.num("referenceY")), \
                P(r.num("endX"), r.num("endY"))
            if s == e or s == m or m == e:
                return None
            return [Sym("arc"), [Sym("start"), *s], [Sym("mid"), *m], [Sym("end"), *e], st, fill, uid]
        return None

    # ------------------------------------------------------------------ files
    def _title_block(self, ctx: PageCtx | None, page_title: str):
        t = ctx.title if ctx else {}
        date = t.get("@Update Date") or datetime.date.today().isoformat()
        tb = [Sym("title_block"), [Sym("title"), page_title], [Sym("date"), date]]
        if t.get("Version"):
            tb.append([Sym("rev"), t["Version"]])
        comp = t.get("Company")
        if comp and comp not in ("EasyEDA.com", "嘉立创EDA"):
            tb.append([Sym("company"), comp])
        k = 1
        for key in ("Drawn", "Drawed", "Reviewed", "Part Number"):
            v = t.get(key)
            if v and v not in ("EasyEDA",):
                tb.append([Sym("comment"), k, f"{key}: {v}"])
                k += 1
        return tb

    def _page_sexp(self, ctx: PageCtx, page_no: int, is_root: bool) -> list:
        title = self.proj.title if is_root else ctx.page.name
        n = [Sym("kicad_sch"), [Sym("version"), 20260306], [Sym("generator"), "eeschema"],
             [Sym("generator_version"), "10.0"], [Sym("uuid"), ctx.uuid if not is_root else self.root_uuid],
             [Sym("paper"), *(ctx.paper if isinstance(ctx.paper, list) else [ctx.paper])], self._title_block(ctx, title if is_root else f"{self.proj.title} - {ctx.page.name}")]
        libs = [Sym("lib_symbols")]
        for lib_id, s in sorted(ctx.lib_ids.items()):
            if isinstance(s, LibSymbol):
                libs.append(lib_symbol_sexp(s, self.lib))
            else:
                libs.append(s)
        n.append(libs)
        order = {"junction": 0, "no_connect": 1, "wire": 2, "text": 3, "label": 4, "global_label": 5,
                 "rectangle": 6, "polyline": 6, "circle": 6, "arc": 6, "symbol": 8}
        n += sorted(ctx.items, key=lambda x: order.get(str(x[0]), 7))
        if is_root:
            n.append([Sym("sheet_instances"), [Sym("path"), "/", [Sym("page"), "1"]]])
        n.append([Sym("embedded_fonts"), NO])
        return n

    def _root_sexp(self, ctxs: list[PageCtx]) -> list:
        n = [Sym("kicad_sch"), [Sym("version"), 20260306], [Sym("generator"), "eeschema"],
             [Sym("generator_version"), "10.0"], [Sym("uuid"), self.root_uuid], [Sym("paper"), "A4"],
             self._title_block(ctxs[0] if ctxs else None, self.proj.title), [Sym("lib_symbols")]]
        # sheet symbols in a grid
        w, h = 40.64, 25.4
        x0, y0 = 25.4, 30.48
        per_row = 5
        for i, ctx in enumerate(ctxs):
            x = x0 + (i % per_row) * (w + 12.7)
            y = y0 + (i // per_row) * (h + 17.78)
            n.append([Sym("sheet"), [Sym("at"), r4(x), r4(y)], [Sym("size"), w, h], [Sym("exclude_from_sim"), NO],
                      [Sym("in_bom"), YES], [Sym("on_board"), YES], [Sym("dnp"), NO],
                      [Sym("fields_autoplaced"), YES],
                      [Sym("stroke"), [Sym("width"), 0.1524], [Sym("type"), Sym("solid")]],
                      [Sym("fill"), [Sym("color"), 0, 0, 0, 0.0]], [Sym("uuid"), ctx.uuid],
                      [Sym("property"), "Sheetname", ctx.page.name, [Sym("at"), r4(x), r4(y - 0.7116), 0],
                       [Sym("show_name"), NO], [Sym("do_not_autoplace"), NO], effects(1.27, ["left", "bottom"])],
                      [Sym("property"), "Sheetfile", ctx.file, [Sym("at"), r4(x), r4(y + h + 0.5846), 0],
                       [Sym("show_name"), NO], [Sym("do_not_autoplace"), NO], effects(1.27, ["left", "top"])],
                      [Sym("instances"), [Sym("project"), self.name,
                                          [Sym("path"), f"/{self.root_uuid}", [Sym("page"), str(i + 2)]]]]])
        n.append([Sym("sheet_instances"), [Sym("path"), "/", [Sym("page"), "1"]]])
        n.append([Sym("embedded_fonts"), NO])
        return n


# ---------------------------------------------------------------------------


def _visible(a) -> bool:
    if a is None:
        return False
    v = a.d.get("valueVisible")
    return bool(v)


def _align_hv(src: Rec, inst: Rec | None = None) -> tuple:
    """EasyEDA alignment as (h, v) with 0 = left/top ... 2 = right/bottom.  Unset alignment is
    left-bottom (EasyEDA default; checked against label/body overlaps on real projects)."""
    h = v = None
    for r in (inst, src):
        if r is None:
            continue
        al = r.d.get("align")
        if isinstance(al, str) and "_" in al:
            hs, vs = al.upper().split("_", 1)
            h = {"LEFT": 0, "CENTER": 1, "RIGHT": 2}.get(hs, 1) if h is None else h
            v = {"TOP": 0, "MIDDLE": 1, "CENTER": 1, "BOTTOM": 2}.get(vs, 1) if v is None else v
        if h is None and r.d.get("hAlign") is not None:
            h = int(r.d["hAlign"])
        if v is None and r.d.get("vAlign") is not None:
            v = int(r.d["vAlign"])
    return (0 if h is None else h, 2 if v is None else v)


def _kicad_field_orient(abs_ang: int, hv: tuple, sym_ang: int, mirror: bool):
    """Find the stored (relative) field angle and justification that make KiCad draw the field at
    absolute angle ``abs_ang`` (0/90) with absolute alignment ``hv``.  KiCad applies the symbol
    rotation, then the mirror, then turns the text so it stays readable."""
    fa = (abs_ang - sym_ang) % 180
    # mirroring across the vertical axis keeps horizontal/vertical, so fa needs no mirror correction

    def frame(a):  # reading direction / up vector in screen coords (y down)
        return ((1, 0), (0, -1)) if a % 180 == 0 else ((0, -1), (-1, 0))

    def xform(v):
        x, y = v
        for _ in range((sym_ang // 90) % 4):
            x, y = y, -x
        if mirror:
            x = -x
        return (x, y)

    dl, ul = frame(fa)
    da, ua = frame(abs_ang)
    want = (1 - hv[0], hv[1] - 1)  # +1 = left/bottom (box lies along +d / +u from the anchor)
    for sh in (1, 0, -1):
        for sv in (1, 0, -1):
            o = xform((sh * dl[0] + sv * ul[0], sh * dl[1] + sv * ul[1]))
            if (o[0] * da[0] + o[1] * da[1], o[0] * ua[0] + o[1] * ua[1]) == want:
                just = [{1: "left", -1: "right"}[sh]] if sh else []
                if sv:
                    just.append({1: "bottom", -1: "top"}[sv])
                return (90 if fa == 90 else 0), (just or None)
    return (90 if fa == 90 else 0), None


def _natkey(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def _on_segment(p, a, b, eps=1e-4) -> bool:
    if min(a[0], b[0]) - eps <= p[0] <= max(a[0], b[0]) + eps and min(a[1], b[1]) - eps <= p[1] <= max(a[1], b[1]) + eps:
        cross = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
        return abs(cross) < eps * max(1.0, abs(b[0] - a[0]) + abs(b[1] - a[1]))
    return False


def _project(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = dx * dx + dy * dy
    if L == 0:
        return a
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L))
    return (a[0] + t * dx, a[1] + t * dy)


def _snap_on_segment(q, a, b):
    if abs(a[1] - b[1]) < 1e-6:  # horizontal
        x = snap(q[0])
        x = min(max(x, min(a[0], b[0])), max(a[0], b[0]))
        return (r4(x), a[1])
    if abs(a[0] - b[0]) < 1e-6:
        y = snap(q[1])
        y = min(max(y, min(a[1], b[1])), max(a[1], b[1]))
        return (a[0], r4(y))
    return (r4(q[0]), r4(q[1]))


def _norm_nc(name: str) -> bool:
    return re.sub(r"[\s_.]", "", str(name or "")).upper() in ("NC", "DNC", "N/C")


def _prune_stock_stubs(segs: list, points: set) -> list:
    """Drop the wires added to reach a stock symbol's pin from an EasyEDA pin spot that nothing was attached to."""
    while True:
        ends = defaultdict(int)
        for a, b, _ in segs:
            ends[a] += 1
            ends[b] += 1
        plain = [(a, b) for a, b, w in segs if not str(w).startswith("stk-")]

        def attached(p):
            return p in points or ends[p] >= 2 or any(_on_segment(p, a, b) for a, b in plain)
        keep = [s for s in segs if not str(s[2]).startswith("stk-") or (attached(s[0]) and attached(s[1]))]
        if len(keep) == len(segs):
            return keep
        segs = keep
