"""EasyEDA Pro PCB document -> native KiCad 10 board."""

from __future__ import annotations

import datetime
import math
import os
import re
from dataclasses import dataclass, field

from .. import geom
from ..easyeda.project import Doc, Project
from ..easyeda.records import Rec
from ..easyeda.rules import Rules, parse_rules
from ..sexp import NO, YES, Sym, loads, stable_uuid
from . import layers as L
from . import textfit
from .footprint import (MIL, Footprint, FpOptions, Graphic, Pad, Text, convert_footprint, footprint_board_sexp,
                        graphic_sexp, krot, mm, norm_deg, origin_justify, pad_sexp, rnd, sane_name, zone_sexp,
                        _region_zone, _shape_graphics)

TEMPLATES = os.path.join(os.path.dirname(__file__), "templates")

PAGES = [("A4", 297, 210), ("A3", 420, 297), ("A2", 594, 420), ("A1", 841, 594), ("A0", 1189, 841)]


@dataclass
class NetClass:
    name: str
    clearance: float  # mm
    track_width: float
    via_diameter: float
    via_drill: float
    diff_pair_width: float | None = None
    diff_pair_gap: float | None = None
    nets: list = field(default_factory=list)


@dataclass
class BoardResult:
    sexp: list
    footprints: dict  # lib name -> Footprint (library form)
    netclasses: list
    design_rules: dict  # .kicad_pro rules section overrides
    dru: str
    nets: set
    warnings: list
    stats: dict
    pad_nets: dict  # (designator, pad number) -> easyeda net name
    track_widths: list
    via_dims: list
    page: str = "A4"
    # zone uuid -> {"layer", "grow" (mm), "spokes" [[(x, y)...]]}: finishing of EasyEDA's own pour fills,
    # applied by kicad/fillfix.py with KiCad's polygon engine
    fill_fixes: dict = field(default_factory=dict)


def _sel_value(rules: dict, typ: str):
    return rules.get(typ) if rules else None


class BoardConverter:
    def __init__(self, proj: Project, doc: Doc, lib_nick: str, fp_names: dict[str, str],
                 sch_links: dict | None = None, net_rename: dict | None = None, flip_angle_offset: float = 0.0,
                 pad_nets_override: dict | None = None, pin_info: dict | None = None, models: dict | None = None):
        self.proj = proj
        self.doc = doc
        self.lib_nick = lib_nick
        self.fp_names = fp_names
        self.sch_links = sch_links or {}
        self.net_rename = net_rename or {}
        self.pad_override = pad_nets_override or {}
        self.models = models or {}
        self.pin_info = pin_info or {}
        self.flip_offset = flip_angle_offset
        self.warn: list[str] = []
        self.rules: Rules = parse_rules(doc.recs)
        inner = []
        for r in doc.of("LAYER"):
            lid = r.int("layerId")
            if L.INNER_FIRST <= lid <= L.INNER_LAST and r.d.get("use") in (True, 1):
                inner.append(lid)
        # include inner layers that carry objects even if flagged unused
        used = {r.int("layerId") for r in doc.recs if r.type in ("LINE", "ARC", "POUR", "FILL", "POLY")}
        for lid in used:
            if L.INNER_FIRST <= lid <= L.INNER_LAST and lid not in inner:
                inner.append(lid)
        self.inner_types = {}
        for r in doc.of("LAYER"):
            if r.int("layerId") in inner and str(r.d.get("layerType", "")).upper() == "PLANE":
                self.inner_types[r.int("layerId")] = "power"
        self.lm = L.LayerMap(inner)
        self.fp_opts = FpOptions(default_mask_mil=self.rules.mask, default_paste_mil=self.rules.paste)
        self.offset = (0.0, 0.0)
        self.items: list = []
        self.nets: set[str] = set()
        self.lib_fps: dict[str, Footprint] = {}
        self.pad_nets: dict = {}
        self.stats: dict = {}

    # ------------------------------------------------------------------ utils
    def net(self, name) -> str:
        name = str(name or "")
        name = self.net_rename.get(name, name)
        if name:
            self.nets.add(name)
        return name

    def P(self, x, y):
        return (rnd(x * MIL + self.offset[0]), rnd(-y * MIL + self.offset[1]))

    def uid(self, *parts) -> str:
        return stable_uuid(self.doc.uuid, *parts)

    # ------------------------------------------------------------------ main
    def convert(self) -> BoardResult:
        doc = self.doc
        self._compute_offset()
        comps = doc.of("COMPONENT")
        attrs_by_parent: dict[str, list[Rec]] = {}
        for a in doc.of("ATTR"):
            attrs_by_parent.setdefault(str(a.d.get("parentId") or ""), []).append(a)
        padnets: dict[str, dict] = {}
        for r in doc.of("PAD_NET"):
            padnets.setdefault(str(r.d.get("parentId")), {})[str(r.d.get("padId") or r.d.get("num"))] = r.d.get("padNet") or ""
            padnets[str(r.d.get("parentId"))]["#" + str(r.d.get("num"))] = r.d.get("padNet") or ""
        fps_sexp = []
        for c in comps:
            n = self._component(c, attrs_by_parent.get(c.id, []), padnets.get(c.id, {}))
            if n is not None:
                fps_sexp.append(n)
        for r in doc.of("PAD"):
            n = self._free_pad(r)
            if n is not None:
                fps_sexp.append(n)
        gr, segs, vias, zones = [], [], [], []
        for r in doc.recs:
            t = r.type
            try:
                if t == "LINE":
                    self._line(r, gr, segs)
                elif t == "ARC":
                    self._arc(r, gr, segs)
                elif t == "VIA":
                    vias.append(self._via(r))
                elif t == "POLY":
                    self._poly(r, gr, segs)
                elif t == "FILL":
                    self._fill(r, gr)
                elif t == "POUR":
                    z = self._pour(r)
                    if z:
                        zones.append(z)
                elif t == "REGION":
                    z = self._region(r)
                    if z:
                        zones.append(z)
                elif t == "TEARDROP":
                    z = self._teardrop(r)
                    if z:
                        zones.append(z)
                elif t == "STRING":
                    self._string(r, gr)
                elif t == "IMAGE":
                    self._image(r, gr)
                elif t == "DIMENSION":
                    self.stats["dimensions_skipped"] = self.stats.get("dimensions_skipped", 0) + 1
            except Exception as e:  # keep going, report
                self.warn.append(f"{t} {r.id}: {e!r}")
        self.stats.update(footprints=len(fps_sexp), tracks=len(segs), vias=len(vias), zones=len(zones),
                          graphics=len(gr))
        head = self._header()
        body = head + fps_sexp + gr + segs + vias + zones + [[Sym("embedded_fonts"), NO]]
        board = [Sym("kicad_pcb")] + body
        netclasses, rules_cfg, dru, tw, vd = self._design_rules()
        return BoardResult(board, self.lib_fps, netclasses, rules_cfg, dru, self.nets, self.warn, self.stats,
                           self.pad_nets, tw, vd, self.page, getattr(self, "fill_fixes", {}))

    # ------------------------------------------------------------------ layout
    def _outline_points(self):
        pts = []
        for r in self.doc.recs:
            if r.type in ("POLY", "LINE", "ARC") and r.int("layerId") == L.OUTLINE:
                if r.type == "POLY":
                    for s in geom.parse_contours(r.d.get("path")):
                        pts += s.polygon(5)
                else:
                    pts += [(r.num("startX"), r.num("startY")), (r.num("endX"), r.num("endY"))]
        return pts

    def _compute_offset(self):
        pts = self._outline_points()
        if not pts:
            pts = [(r.num("x"), r.num("y")) for r in self.doc.of("COMPONENT")]
            pts += [(r.num("startX"), r.num("startY")) for r in self.doc.of("LINE")]
        if not pts:
            pts = [(0, 0)]
        x0, y0, x1, y1 = geom.bbox(pts)
        w, h = (x1 - x0) * MIL, (y1 - y0) * MIL
        page = PAGES[-1]
        for pg in PAGES:
            if w <= pg[1] - 60 and h <= pg[2] - 60:
                page = pg
                break
        self.page = page[0]
        # centre on the page, snapped to 1 mm (KiCad's own default grid friendly)
        cx, cy = page[1] / 2, (page[2] - 20) / 2
        ox = round(cx - (x0 + x1) / 2 * MIL)
        oy = round(cy + (y0 + y1) / 2 * MIL)
        self.offset = (ox, oy)

    def _header(self) -> list:
        tpl = loads(open(os.path.join(TEMPLATES, "board.kicad_pcb"), encoding="utf-8").read())
        setup = next(x for x in tpl if isinstance(x, list) and x and x[0] == "setup")
        setup = [x for x in setup if not (isinstance(x, list) and x and x[0] == "pad_to_mask_clearance")]
        extra = []
        stack, thickness = self._stackup()
        if stack:
            extra.append(stack)
        extra.append([Sym("pad_to_mask_clearance"), rnd(mm(self.rules.mask), 4)])
        if abs(self.rules.paste) > 1e-6:
            extra.append([Sym("pad_to_paste_clearance"), rnd(mm(self.rules.paste), 4)])
        setup = [setup[0]] + extra + setup[1:]
        if not self.rules.via_tented:
            for x in setup:
                if isinstance(x, list) and x and x[0] == "tenting":
                    x[1:] = [[Sym("front"), NO], [Sym("back"), NO]]
        title = self.doc.title or self.proj.title
        lt = L.board_layers_table(self.lm, {self.lm.inner[k]: v for k, v in self.inner_types.items()})
        return [
            [Sym("version"), 20260206],
            [Sym("generator"), "pcbnew"],
            [Sym("generator_version"), "10.0"],
            [Sym("general"), [Sym("thickness"), rnd(thickness, 4)], [Sym("legacy_teardrops"), NO]],
            [Sym("paper"), self.page],
            [Sym("title_block"), [Sym("title"), title], [Sym("date"), datetime.date.today().isoformat()],
             [Sym("comment"), 1, f"Converted from EasyEDA Pro ({os.path.basename(self.proj.source)})"]],
            lt,
            setup,
        ]

    def _stackup(self):
        phys = {}
        order = []
        for r in self.doc.of("LAYER_PHYS"):
            lid = r.int("layerId")
            phys[lid] = r.d
            order.append((r.num("zIndex", len(order)), lid))
        cu = self.lm.copper_layers()
        n_cu = len(cu)
        diel_ids = sorted(l for l in phys if l >= 361)
        if not phys or not diel_ids:
            return None, 1.6

        def thick(lid, default):
            v = phys.get(lid, {}).get("thickness")
            try:
                v = float(v)
            except (TypeError, ValueError):
                v = 0.0
            return v * MIL if v and v > 0 else default

        def mat(lid, default="FR4"):
            m = str(phys.get(lid, {}).get("material") or "")
            return m if m and m.isascii() else default

        def eps(lid, default=4.5):
            try:
                v = float(phys.get(lid, {}).get("permittivity") or 0)
            except (TypeError, ValueError):
                v = 0
            return v if v > 0 else default

        def loss(lid, default=0.02):
            try:
                v = float(phys.get(lid, {}).get("lossTangent") or 0)
            except (TypeError, ValueError):
                v = 0
            return v if v > 0 else default

        rows = [[Sym("layer"), "F.SilkS", [Sym("type"), "Top Silk Screen"]],
                [Sym("layer"), "F.Paste", [Sym("type"), "Top Solder Paste"]],
                [Sym("layer"), "F.Mask", [Sym("type"), "Top Solder Mask"], [Sym("thickness"), rnd(thick(5, 0.01), 4)]]]
        total = thick(5, 0.01) + thick(6, 0.01)
        cu_ids = [1] + sorted(self.lm.inner) + [2]
        for i, lid in enumerate(cu_ids):
            name = cu[i]
            t = thick(lid, 0.035)
            total += t
            rows.append([Sym("layer"), name, [Sym("type"), "copper"], [Sym("thickness"), rnd(t, 4)]])
            if i < n_cu - 1:
                did = diel_ids[i] if i < len(diel_ids) else None
                dt = thick(did, 1.51 / max(1, n_cu - 1)) if did else 1.51 / max(1, n_cu - 1)
                total += dt
                is_core = n_cu == 2 or (0 < i < n_cu - 2) or "copper" in str(phys.get(did, {}).get("material", "")).lower() \
                    or "core" in str(phys.get(did, {}).get("material", "")).lower()
                if n_cu > 2 and (i == 0 or i == n_cu - 2):
                    is_core = "core" in str(phys.get(did, {}).get("material", "")).lower()
                rows.append([Sym("layer"), f"dielectric {i + 1}", [Sym("type"), "core" if is_core else "prepreg"],
                             [Sym("thickness"), rnd(dt, 4)], [Sym("material"), mat(did) if did else "FR4"],
                             [Sym("epsilon_r"), rnd(eps(did) if did else 4.5, 3)],
                             [Sym("loss_tangent"), rnd(loss(did) if did else 0.02, 4)]])
        rows += [[Sym("layer"), "B.Mask", [Sym("type"), "Bottom Solder Mask"], [Sym("thickness"), rnd(thick(6, 0.01), 4)]],
                 [Sym("layer"), "B.Paste", [Sym("type"), "Bottom Solder Paste"]],
                 [Sym("layer"), "B.SilkS", [Sym("type"), "Bottom Silk Screen"]],
                 [Sym("copper_finish"), "None"],
                 [Sym("dielectric_constraints"), NO]]
        return [Sym("stackup")] + rows, total

    # ------------------------------------------------------------------ footprints
    def _lib_fp(self, fp_uuid: str) -> Footprint | None:
        doc = self.proj.footprints.get(fp_uuid)
        if doc is None:
            return None
        name = self.fp_names.get(fp_uuid) or sane_name(doc.title)
        fp = self.lib_fps.get(name)
        if fp is None:
            fp = convert_footprint(doc, self.lm, self.fp_opts, name=name)
            if fp_uuid in self.models:
                fp.models = [self.models[fp_uuid]]
            self.lib_fps[name] = fp
        return fp

    def _component(self, c: Rec, attrs: list[Rec], padnets: dict):
        a = {x.str("key"): x for x in attrs}
        fp_uuid = a["Footprint"].str("value") if "Footprint" in a else ""
        dev_uuid = a["Device"].str("value") if "Device" in a else ""
        des = a["Designator"].str("value") if "Designator" in a else ""
        if not fp_uuid and dev_uuid:
            fp_uuid = str(self.proj.devices.get(dev_uuid, {}).get("attributes", {}).get("Footprint") or "")
        lib = self._lib_fp(fp_uuid)
        if lib is None:
            self.warn.append(f"component {des or c.id}: footprint {fp_uuid} missing from project")
            return None
        dev = self.proj.devices.get(dev_uuid, {})
        dattrs = dev.get("attributes", {})
        comp_attrs = c.d.get("attrs") or {}
        value = resolve_value(dattrs, comp_attrs, dev.get("title", ""))
        back = c.int("layerId", 1) == L.BOTTOM
        angle = norm_deg(c.num("angle"))
        fp = lib.flipped() if back else _copy_fp(lib)
        kangle = norm_deg(angle + (self.flip_offset if back else 0))
        pos = self.P(c.num("x"), c.num("y"))
        link = self.sch_links.get(comp_attrs.get("Unique ID")) or self.sch_links.get(comp_attrs.get("Channel ID")) \
            or self.sch_links.get(des)
        if link and len(link) > 3 and link[3].get("Reference") and link[3]["Reference"] != des:
            self.warn.append(f"footprint {des} renamed to {link[3]['Reference']} (schematic designator)")
            des = link[3]["Reference"]
        # nets
        for p in fp.pads:
            nn = padnets.get(p.src_id)
            if nn is None:
                nn = padnets.get("#" + p.number)
            if p.kind == "np_thru_hole" and not p.number:
                nn = None
            if des and p.number:
                self.pad_nets[(des, p.number)] = nn or ""
            ov = self.pad_override.get((des, p.number)) if des and p.number else None
            if ov is not None:
                p.net = ov or None
                if ov:
                    self.nets.add(ov)
            else:
                p.net = self.net(nn) if nn else None
            pi = self.pin_info.get((des, p.number)) if des and p.number else None
            if pi:
                if pi[0]:
                    p.pinfunction = pi[0]
                p.pintype = pi[1] + ("+no_connect" if p.net and p.net.startswith("unconnected-") else "")
        # reference / value texts
        ref = fp.ref_text()
        if ref is not None:
            ref.text = des or "REF**"
            dattr = a.get("Designator")
            if dattr is not None:
                self._place_field(ref, dattr, pos, kangle, back)
        val = fp.value_text()
        if val is not None:
            val.text = value
            vattr = next((a[k] for k in ("Value", "Name") if k in a and a[k].d.get("valueVisible")), None)
            if vattr is not None:
                self._place_field(val, vattr, pos, kangle, back)
            else:  # EasyEDA shows no value on the board
                val.hidden = True
        for t in fp.texts:  # the library's Fab "${REFERENCE}" has no EasyEDA counterpart either
            if t.kind == "user" and t.text == "${REFERENCE}" and t.layer.endswith(".Fab"):
                t.hidden = True
        merged = dict(dattrs)
        merged.update({k: v for k, v in comp_attrs.items() if v})
        props = {}
        for k in ("Datasheet", "Description"):
            v = str(dattrs.get(k) or "")
            props[k] = "" if v.startswith("=") else v
        if not props["Description"]:
            props["Description"] = dev.get("description", "") or ""
        props.update(part_fields(merged))
        uid = self.uid("fp", c.id)
        path, sheetname, sheetfile = (link[:3] if link else (None, None, None))
        if link and len(link) > 3:
            # keep footprint fields identical to the schematic symbol (schematic parity)
            sf = link[3]
            value = sf.get("Value") or value
            if val is not None:
                val.text = value
            props = {k: v for k, v in props.items() if k in ("Datasheet", "Description")}
            props.update({k: v for k, v in sf.items() if k not in ("Value", "Reference")})
        attr = list(fp.attr)
        if link is None and self.sch_links:
            attr.append("board_only")
        if dattrs.get("Add into BOM") in ("no", "false", "0") or comp_attrs.get("Add into BOM") in ("no", "false"):
            attr.append("exclude_from_bom")
        if dattrs.get("Convert to PCB") in ("no", "false"):
            attr.append("board_only")
        lib_id = f"{self.lib_nick}:{lib.name}"
        # model: footprint models are attached in the library footprint already
        return footprint_board_sexp(fp, lib_id, pos, kangle, back, uid, path, sheetname, sheetfile, props, attr,
                                    locked=bool(c.d.get("locked")))

    def _place_field(self, t: Text, ar: Rec, pos, fp_angle: float, fp_back: bool):
        """Position a footprint field from an absolute EasyEDA ATTR record."""
        visible = bool(ar.d.get("valueVisible")) and ar.d.get("x") is not None
        lid = ar.int("layerId", L.TOP_SILK)
        layer = self.lm.to_kicad(lid) or ("B.SilkS" if fp_back else "F.SilkS")
        if layer in ("Edge.Cuts",) or L.is_copper(layer) and False:
            layer = "B.SilkS" if fp_back else "F.SilkS"
        if ar.d.get("x") is None:
            t.hidden = True
            t.layer = "B.Fab" if fp_back else "F.Fab"
            return
        shown = ar.str("value") or t.text
        if ar.d.get("keyVisible"):
            shown = f"{ar.str('key')}:{shown}"
        x, y, w, h = textfit.fit(shown, ar.num("x"), ar.num("y"), ar.num("angle"), ar.str("origin"),
                                 ar.num("fontSize", 45), ar.num("strokeWidth", 6), layer.startswith("B."))
        absp = self.P(x, y)
        rel = (absp[0] - pos[0], absp[1] - pos[1])
        local = krot(rel, -fp_angle)
        t.at = (rnd(local[0]), rnd(local[1]))
        is_back_text = layer.startswith("B.")
        a = norm_deg(ar.num("angle"))
        # stored text angle is relative to footprint in our model (footprint_board_sexp adds fp angle)
        t.angle = norm_deg((-a if is_back_text else a) - fp_angle)
        t.layer = layer
        t.mirror = is_back_text
        t.size = (rnd(w * MIL, 4), rnd(h * MIL, 4))
        t.thickness = rnd(max(0.05, mm(ar.num("strokeWidth", 6))), 4)
        t.justify = ("", "")  # centre: textfit placed the anchor in the middle of EasyEDA's ink
        t.hidden = not visible

    def _free_pad(self, r: Rec):
        p = _convert_free_pad(r, self.lm, self.fp_opts)
        if p is None:
            return None
        num = p.number or ""
        center = (r.num("centerX"), r.num("centerY"))
        pos = self.P(*center)
        p.at = (0.0, 0.0)
        p.net = self.net(r.str("netName")) or None
        fp = Footprint(f"Pad_{sane_name(num or r.id)}", attr=["smd" if p.kind == "smd" else "through_hole"])
        fp.pads = [p]
        fp.texts = [Text("reference", "", (0, -1.5), 0, "F.Fab", hidden=True),
                    Text("value", "", (0, 1.5), 0, "F.Fab", hidden=True)]
        uid = self.uid("freepad", r.id)
        angle = p.angle
        p.angle = 0.0
        self.stats["free_pads"] = self.stats.get("free_pads", 0) + 1
        return footprint_board_sexp(fp, f"{self.lib_nick}:{fp.name}", pos, angle, False, uid, None, None, None,
                                    {}, ["board_only", "exclude_from_pos_files", "exclude_from_bom"])

    # ------------------------------------------------------------------ primitives
    def _layer(self, lid):
        return self.lm.to_kicad(lid)

    def _line(self, r: Rec, gr, segs):
        lid = r.int("layerId")
        layer = self._layer(lid)
        if layer is None:
            return
        a, b = self.P(r.num("startX"), r.num("startY")), self.P(r.num("endX"), r.num("endY"))
        if a == b:
            return
        w = mm(r.num("width"))
        if L.is_copper(layer):
            n = [Sym("segment"), [Sym("start"), *a], [Sym("end"), *b], [Sym("width"), rnd(w, 4)], [Sym("layer"), layer]]
            if r.d.get("locked"):
                n.insert(1, Sym("locked"))
            n.append([Sym("net"), self.net(r.str("netName"))])
            n.append([Sym("uuid"), self.uid("seg", r.id)])
            segs.append(n)
        else:
            if layer == "Edge.Cuts" or lid == L.MULTI:
                layer, w = "Edge.Cuts", w or 0.05  # EasyEDA's own line width
            gr.append(graphic_sexp(Graphic("line", layer, w, [a, b]), self.uid("gl", r.id), prefix="gr_"))

    def _arc(self, r: Rec, gr, segs):
        lid = r.int("layerId")
        layer = self._layer(lid)
        if layer is None:
            return
        s, e = (r.num("startX"), r.num("startY")), (r.num("endX"), r.num("endY"))
        sweep = r.num("angle")
        if abs(sweep) < 1e-6 or geom.dist(s, e) < 1e-6:
            return
        m = geom.arc_mid(s, e, sweep)
        w = mm(r.num("width"))
        pts = [self.P(*s), self.P(*m), self.P(*e)]
        # nearly straight arcs: KiCad rebuilds the arc from start/mid/end and the huge radius makes the
        # endpoints drift off their pads -> emit a straight track/line instead (bulge < 1 um)
        sagitta = mm(geom.dist(s, e)) * abs(math.radians(sweep)) / 8
        if sagitta < 0.001:
            if L.is_copper(layer):
                segs.append([Sym("segment"), [Sym("start"), *pts[0]], [Sym("end"), *pts[2]], [Sym("width"), rnd(w, 4)],
                             [Sym("layer"), layer], [Sym("net"), self.net(r.str("netName"))],
                             [Sym("uuid"), self.uid("arc", r.id)]])
                return
            pts = [pts[0], pts[2]]
            if layer == "Edge.Cuts" or lid == L.MULTI:
                layer, w = "Edge.Cuts", w or 0.05  # EasyEDA's own line width
            gr.append(graphic_sexp(Graphic("line", layer, w, pts), self.uid("ga", r.id), prefix="gr_"))
            return
        if L.is_copper(layer):
            n = [Sym("arc"), [Sym("start"), *pts[0]], [Sym("mid"), *pts[1]], [Sym("end"), *pts[2]],
                 [Sym("width"), rnd(w, 4)], [Sym("layer"), layer], [Sym("net"), self.net(r.str("netName"))],
                 [Sym("uuid"), self.uid("arc", r.id)]]
            segs.append(n)
        else:
            if layer == "Edge.Cuts" or lid == L.MULTI:
                layer, w = "Edge.Cuts", w or 0.05  # EasyEDA's own line width
            gr.append(graphic_sexp(Graphic("arc", layer, w, pts), self.uid("ga", r.id), prefix="gr_"))

    def _via(self, r: Rec):
        n = [Sym("via"), [Sym("at"), *self.P(r.num("centerX"), r.num("centerY"))],
             [Sym("size"), rnd(mm(r.num("viaDiameter")), 4)], [Sym("drill"), rnd(mm(r.num("holeDiameter")), 4)],
             [Sym("layers"), "F.Cu", "B.Cu"]]
        if r.d.get("locked"):
            n.insert(1, Sym("locked"))
        exp = r.d.get("topSolderExpansion")
        if isinstance(exp, (int, float)) and exp > -999 and self.rules.via_tented:
            n.append([Sym("tenting"), [Sym("front"), NO], [Sym("back"), NO]])
        n.append([Sym("net"), self.net(r.str("netName"))])
        n.append([Sym("uuid"), self.uid("via", r.id)])
        return n

    def _shape_to_board(self, shape: geom.Shape, layer: str, width: float, fill=False) -> list[Graphic]:
        # convert in EasyEDA space (origin 0) then translate to board coordinates
        gs = _shape_graphics(shape, layer, width, (0.0, 0.0), fill)
        ox, oy = self.offset
        out = []
        for g in gs:
            g.pts = [(rnd(x + ox), rnd(y + oy)) for x, y in g.pts]
            out.append(g)
        return out

    def _poly(self, r: Rec, gr, segs):
        lid = r.int("layerId")
        layer = self._layer(lid)
        if layer is None:
            return
        w = mm(r.num("width"))
        net = r.str("netName")
        shapes = geom.parse_contours(r.d.get("path"))
        if lid in (L.OUTLINE, L.MULTI) or layer == "Edge.Cuts":
            layer, w = "Edge.Cuts", w or 0.05  # EasyEDA's own line width
        for k, s in enumerate(shapes):
            if L.is_copper(layer) and s.kind == "path":
                for i, sg in enumerate(s.segs):
                    a, b = self.P(*sg.a), self.P(*sg.b)
                    if a == b:
                        continue
                    if sg.kind == "arc":
                        m = self.P(*geom.arc_mid(sg.a, sg.b, sg.sweep))
                        segs.append([Sym("arc"), [Sym("start"), *a], [Sym("mid"), *m], [Sym("end"), *b],
                                     [Sym("width"), rnd(w, 4)], [Sym("layer"), layer], [Sym("net"), self.net(net)],
                                     [Sym("uuid"), self.uid("parc", r.id, k, i)]])
                    else:
                        segs.append([Sym("segment"), [Sym("start"), *a], [Sym("end"), *b], [Sym("width"), rnd(w, 4)],
                                     [Sym("layer"), layer], [Sym("net"), self.net(net)],
                                     [Sym("uuid"), self.uid("pseg", r.id, k, i)]])
                continue
            for i, g in enumerate(self._shape_to_board(s, layer, w)):
                n = graphic_sexp(g, self.uid("gp", r.id, k, i), prefix="gr_")
                if L.is_copper(layer) and net:
                    n.insert(-1, [Sym("net"), self.net(net)])
                gr.append(n)

    def _fill(self, r: Rec, gr):
        lid = r.int("layerId")
        shapes = geom.parse_contours(r.d.get("path"))
        if lid in (L.OUTLINE, L.MULTI, L.HOLE):
            for k, s in enumerate(shapes):
                for i, g in enumerate(self._shape_to_board(s, "Edge.Cuts", 0.05)):
                    gr.append(graphic_sexp(g, self.uid("cut", r.id, k, i), prefix="gr_"))
            return
        layer = self._layer(lid)
        if layer is None:
            return
        net = r.str("netName")
        if len(shapes) == 1 and shapes[0].kind == "circle":
            g = self._shape_to_board(shapes[0], layer, 0, fill=True)[0]
            n = graphic_sexp(g, self.uid("fc", r.id), prefix="gr_")
            if L.is_copper(layer) and net:
                n.insert(-1, [Sym("net"), self.net(net)])
            gr.append(n)
            return
        polys = [s.polygon(0.2) for s in shapes]
        for k, (outer, holes) in enumerate(geom.nest_contours(polys)):
            pts = geom.fracture(outer, holes) if holes else outer
            if len(pts) < 3:
                continue
            g = Graphic("poly", layer, 0, [self.P(*p) for p in pts], True)
            n = graphic_sexp(g, self.uid("fill", r.id, k), prefix="gr_")
            if L.is_copper(layer) and net:
                n.insert(-1, [Sym("net"), self.net(net)])
            gr.append(n)

    def _zone_polys(self, path, scale: float = 1.0) -> list[list]:
        shapes = geom.parse_contours(path)
        polys = [[(x * scale, y * scale) for x, y in s.polygon(0.5 / scale)] for s in shapes]
        out = []
        for outer, holes in geom.nest_contours([p for p in polys if len(p) >= 3]):
            pts = geom.fracture(outer, holes) if holes else outer
            pts = geom.simplify_collinear(pts)
            if len(pts) >= 3:
                out.append([self.P(*p) for p in pts])
        return out

    def _pour_settings(self, pour_id: str):
        R = self.rules
        sel = R.pour_rules.get(pour_id, {})
        cop_name = sel.get("COPPER") or R.defaults.get("COPPER")
        cop = R.lookup("COPPER", cop_name)
        # v2: the copper-region rule carries the pour clearance (verified against EasyEDA's own fills)
        if cop is not None and cop.clearance:
            return cop, cop.clearance
        # v3: each copper-region rule has a companion safe-spacing profile "Copper_<name>"
        safe_name = R.pour_safe.get(pour_id) or sel.get("SAFE")
        if not safe_name and cop_name and f"Copper_{cop_name}" in R.safe:
            safe_name = f"Copper_{cop_name}"
        safe = R.lookup("SAFE", safe_name)
        clr = None
        if safe is not None:
            # a copper pour is a "fill region" in EasyEDA's spacing table: its real copper keeps that distance
            # (the stored fill sits half the pour width further away and is stroked with it, see _easyeda_fill)
            vals = [safe.value("fill", k) for k in ("track", "smd", "th", "via")]
            vals = [v for v in vals if v]
            clr = max(vals) if vals else None
        if clr is None:
            clr = 10.0
        return cop, clr

    def _easyeda_fill(self, pour_id: str) -> tuple[list, list]:
        """EasyEDA's own computed fill of a pour (POURED records) as KiCad points: (fill outlines with their holes
        fractured, thermal spokes). EasyEDA's Gerber draws the outlines with a stroke of the pour width and the
        spokes as round-ended lines; KiCad's refill gets close but not identical."""
        if not hasattr(self, "_poured"):
            self._poured = {}
            for pr in self.doc.of("POURED"):
                ents = pr.d.get("pourFill")
                if not isinstance(ents, list):  # v2: one record per piece
                    ents = [{"fill": pr.d.get("fill"), "path": pr.d.get("path"), "strokeWidth": pr.d.get("strokeWidth")}]
                self._poured.setdefault(str(pr.d.get("pourId")), []).extend(ents)
        out, spokes = [], []
        for e in self._poured.get(pour_id, []):
            if not isinstance(e, dict) or not e.get("path"):
                continue
            if e.get("fill") in (True, 1, "1", "true"):
                out += self._zone_polys(e["path"], 10.0)  # fills are stored in units of 10 mil
            elif float(e.get("strokeWidth") or 0) > 0:  # thermal spokes: stroked lines with round ends
                w = float(e["strokeWidth"]) * 10
                for sh in geom.parse_contours(e["path"]):
                    pts = [(x * 10, y * 10) for x, y in sh.polygon(0.05)]
                    for a, b in zip(pts, pts[1:]):
                        spokes.append([self.P(*q) for q in _stadium(a, b, w / 2)])
        return out, spokes

    def _pour(self, r: Rec):
        lid = r.int("layerId")
        layer = self._layer(lid)
        if layer is None or not L.is_copper(layer):
            return None
        polys = self._zone_polys(r.d.get("path"))
        if not polys:
            return None
        cop, clr = self._pour_settings(r.id)
        orders = [p.int("order") for p in self.doc.of("POUR")]
        prio = max(orders + [0]) - r.int("order")
        n = [Sym("zone"), [Sym("net"), self.net(r.str("netName"))], [Sym("layer"), layer],
             [Sym("uuid"), self.uid("zone", r.id)]]
        if r.str("name"):
            n.append([Sym("name"), r.str("name")])
        if r.d.get("locked"):
            n.append([Sym("locked"), YES])
        n.append([Sym("hatch"), Sym("edge"), 0.5])
        if prio > 0:
            n.append([Sym("priority"), prio])
        connect = [Sym("connect_pads")]
        gap, spoke = 0.5, 0.5
        if cop is not None:
            smd, th = cop.smd.connect, cop.th.connect
            if smd == "direct" and th == "direct":
                connect.append(Sym("yes"))
            elif smd == "none" and th == "none":
                connect.append(Sym("no"))
            elif smd == "direct" and th == "thermal":
                connect.append(Sym("thru_hole_only"))
            ref = cop.th if smd == "direct" else cop.smd
            gap, spoke = mm(ref.spoke_gap), mm(ref.spoke_width)
        connect.append([Sym("clearance"), rnd(mm(clr), 4)])
        n.append(connect)
        # EasyEDA's pour "width" is its minimum fill width (mm; 0.2 by default): with it KiCad leaves the same thin
        # gaps unfilled (checked against EasyEDA's Gerber). KiCad drops thermal spokes thinner than that minimum.
        pw = r.num("width")
        min_w = pw if 0 < pw < 2 else 0.15
        if cop is not None:
            for t in (cop.smd, cop.th):
                if t.connect == "thermal" and t.spoke_width > 0:
                    min_w = min(min_w, mm(t.spoke_width) * 0.95)
        n.append([Sym("min_thickness"), rnd(max(min_w, 0.0508), 4)])
        pt = r.d.get("pourType") or {}
        ptype = str(pt.get("pourType") if isinstance(pt, dict) else pt or "SOLID").upper()
        filled, spokes = self._easyeda_fill(r.id)
        fill = [Sym("fill"), Sym("yes")] if filled else [Sym("fill")]
        if ptype in ("HATCH", "GRID", "MESH", "HATCHED"):
            fill += [[Sym("mode"), Sym("hatch")]]
        fill += [[Sym("thermal_gap"), rnd(gap, 4)], [Sym("thermal_bridge_width"), rnd(spoke, 4)],
                 [Sym("island_removal_mode"), 1 if r.d.get("keepIsland") in (1, True) else 0]]
        if ptype in ("HATCH", "GRID", "MESH", "HATCHED"):
            fine = float(pt.get("fineness") or 8) if isinstance(pt, dict) else 8
            fill += [[Sym("hatch_thickness"), rnd(mm(fine), 4)], [Sym("hatch_gap"), rnd(mm(fine * 3), 4)],
                     [Sym("hatch_orientation"), 45], [Sym("hatch_border_algorithm"), Sym("hatch_thickness")],
                     [Sym("hatch_min_hole_area"), 0.3]]
        n.append(fill)
        for poly in polys:
            n.append([Sym("polygon"), [Sym("pts")] + [[Sym("xy"), x, y] for x, y in poly]])
        for poly in filled:
            n.append([Sym("filled_polygon"), [Sym("layer"), layer], [Sym("pts")] + [[Sym("xy"), x, y] for x, y in poly]])
        if filled:
            self.stats["pours_filled_as_easyeda"] = self.stats.get("pours_filled_as_easyeda", 0) + 1
            stroke = round(pw / 0.0254) * 0.0254 if 0 < pw < 2 else 0.2032  # EasyEDA strokes 0.2 mm as 8 mil
            if not hasattr(self, "fill_fixes"):
                self.fill_fixes = {}
            self.fill_fixes[self.uid("zone", r.id)] = {"layer": layer, "grow": stroke / 2, "spokes": spokes}
        return n

    def _region(self, r: Rec):
        lid = r.int("layerId")
        flags = r.d.get("prohibitType") or []
        rtype = str(r.d.get("regionType") or "").upper()
        name = r.str("name")
        if lid == L.MULTI:
            layers = self.lm.copper_layers()
        else:
            layer = self._layer(lid)
            if layer is None:
                return None
            layers = [layer if L.is_copper(layer) else "F.Cu"]
        polys = self._zone_polys(r.d.get("path"))
        if not polys:
            return None
        z = _region_zone(r, layers[0], flags, (0, 0))
        if z is None:
            return None
        if rtype == "CONSTRAINT" or (not flags and name):
            z.no_tracks = z.no_vias = z.no_pads = z.no_pour = z.no_footprints = False
            if not name:
                name = f"REGION_{r.id}"
        if not name and r.id in self.rules.region_rules:
            name = f"REGION_{r.id}"
        z.name = name or ""
        z.layers = layers
        z.polygon = polys[0]
        self._regions_named = getattr(self, "_regions_named", {})
        if z.name:
            self._regions_named[r.id] = z.name
        return zone_sexp(z, self.uid("region", r.id), False)

    def _teardrop(self, r: Rec):
        layer = self._layer(r.int("layerId"))
        if layer is None or not L.is_copper(layer):
            return None
        polys = self._zone_polys(r.d.get("path"))
        if not polys:
            return None
        self.stats["teardrops"] = self.stats.get("teardrops", 0) + 1
        n = [Sym("zone"), [Sym("net"), self.net(r.str("netName"))], [Sym("layer"), layer],
             [Sym("uuid"), self.uid("td", r.id)], [Sym("hatch"), Sym("full"), 0.1], [Sym("priority"), 30000],
             [Sym("attr"), [Sym("teardrop"), [Sym("type"), Sym("padvia")]]],
             [Sym("connect_pads"), Sym("yes"), [Sym("clearance"), 0]], [Sym("min_thickness"), 0.0254],
             [Sym("filled_areas_thickness"), NO],
             [Sym("fill"), [Sym("thermal_gap"), 0.5], [Sym("thermal_bridge_width"), 0.5],
              [Sym("island_removal_mode"), 1]]]
        for poly in polys:
            n.append([Sym("polygon"), [Sym("pts")] + [[Sym("xy"), x, y] for x, y in poly]])
        return n

    def _string(self, r: Rec, gr):
        lid = r.int("layerId")
        layer = self._layer(lid)
        text = r.str("text")
        if layer is None or not text:
            return
        if layer == "Edge.Cuts":
            layer = "Cmts.User"
        back = layer.startswith("B.")
        a = norm_deg(r.num("angle"))
        x, y, w, h = textfit.fit(text, r.num("x"), r.num("y"), a, r.str("origin"), r.num("fontSize", 45),
                                 r.num("strokeWidth", 6), back)
        j = ("", "")  # centre: textfit placed the anchor in the middle of EasyEDA's ink
        eff = [Sym("effects"), [Sym("font"), [Sym("size"), rnd(h * MIL, 4), rnd(w * MIL, 4)],
                                [Sym("thickness"), rnd(max(0.05, mm(r.num("strokeWidth", 6))), 4)]]]
        if r.d.get("bold") in (1, True):
            eff[1].append([Sym("bold"), YES])
        if r.d.get("italic") in (1, True):
            eff[1].append([Sym("italic"), YES])
        just = [Sym("justify")] + [Sym(x) for x in j if x]
        if back:
            just.append(Sym("mirror"))
        if len(just) > 1:
            eff.append(just)
        at = [Sym("at"), *self.P(x, y)]
        ang = norm_deg(-a if back else a)
        if ang:
            at.append(ang)
        lay = [Sym("layer"), layer]
        if r.d.get("reverse") in (1, True):
            lay.append(Sym("knockout"))
        gr.append([Sym("gr_text"), text, at, lay, [Sym("uuid"), self.uid("txt", r.id)], eff])

    def _image(self, r: Rec, gr):
        lid = r.int("layerId")
        layer = self._layer(lid)
        if layer is None:
            return
        shapes = geom.parse_contours(r.d.get("path"))
        polys = [s.polygon(0.5) for s in shapes]
        polys = [p for p in polys if len(p) >= 3]
        if not polys:
            return
        x0, y0, x1, y1 = geom.bbox([q for p in polys for q in p])
        sx = r.num("width") / (x1 - x0) if r.num("width") and x1 > x0 else 1.0
        sy = r.num("height") / (y1 - y0) if r.num("height") and y1 > y0 else 1.0
        if abs(sx - 1) < 0.01:
            sx = 1.0
        if abs(sy - 1) < 0.01:
            sy = 1.0
        start = (r.num("startX"), r.num("startY"))
        ang = r.num("angle") % 360
        mirror = bool(r.d.get("mirror")) ^ layer.startswith("B.")

        def tf(p):
            # top-left (x0, y1) of the image goes to `start`
            q = ((p[0] - x0) * sx, (p[1] - y1) * sy)
            if mirror:
                q = ((x1 - x0) * sx - q[0], q[1])
            q = geom.rot(q, ang)
            return (start[0] + q[0], start[1] + q[1])

        n_out = 0
        for k, (outer, holes) in enumerate(geom.nest_contours(polys)):
            pts = geom.fracture(outer, holes) if holes else outer
            if len(pts) < 3:
                continue
            g = Graphic("poly", layer, 0, [self.P(*tf(p)) for p in pts], True)
            gr.append(graphic_sexp(g, self.uid("img", r.id, k), prefix="gr_"))
            n_out += 1
        self.stats["image_polys"] = self.stats.get("image_polys", 0) + n_out

    # ------------------------------------------------------------------ rules
    def _design_rules(self):
        R = self.rules
        safe = R.default("SAFE")
        track = R.default("TRACK") or (5, 10, 100)
        via = R.default("RADIUS") or (12, 24, 200, 6, 12, 120)
        diff = R.default("DIFFER_ENTAIL")
        base_clr = (safe.value("track", "track") if safe else None) or 6.0

        def mk(name, safe_r, track_r, via_r, diff_r=None):
            clr = (safe_r.value("track", "track") if safe_r else None) or base_clr
            nc = NetClass(name, rnd(mm(clr), 4), rnd(mm(track_r[1]), 4), rnd(mm(via_r[1]), 4), rnd(mm(via_r[4]), 4))
            if diff_r:
                nc.diff_pair_width, nc.diff_pair_gap = rnd(mm(diff_r[0]), 4), rnd(mm(diff_r[1]), 4)
            return nc

        classes = {"Default": mk("Default", safe, track, via, diff)}
        net_class_of = {}
        # v3 net classes (NET.netType) and NET_CLASS selectors
        for r in self.doc.of("NET"):
            nt = r.d.get("netType")
            nm = r.d.get("name") or r.id
            if nt:
                net_class_of[self.net_rename.get(nm, nm)] = nt
        for cname, rr in R.class_rules.items():
            classes[cname] = mk(cname, R.lookup("SAFE", rr.get("SAFE")), R.lookup("TRACK", rr.get("TRACK")) or track,
                                R.lookup("RADIUS", rr.get("RADIUS")) or via)
        for cname in set(net_class_of.values()):
            classes.setdefault(cname, mk(cname, safe, track, via, diff))
        # per-net selectors with non default rules -> class named after the rule combo
        for net, rr in R.net_rules.items():
            key = tuple(sorted((k, v) for k, v in rr.items() if k in ("SAFE", "TRACK", "RADIUS")))
            if not key:
                continue
            nn = self.net_rename.get(net, net)
            cur = net_class_of.get(nn)
            if cur and cur in R.class_rules:
                ck = tuple(sorted((k, v) for k, v in R.class_rules[cur].items() if k in ("SAFE", "TRACK", "RADIUS")))
                if ck == key:
                    continue
            cname = "_".join(v for _, v in key)
            classes.setdefault(cname, mk(cname, R.lookup("SAFE", rr.get("SAFE")), R.lookup("TRACK", rr.get("TRACK")) or track,
                                         R.lookup("RADIUS", rr.get("RADIUS")) or via))
            net_class_of[nn] = cname
        for n, c in net_class_of.items():
            if c in classes and n in self.nets:
                classes[c].nets.append(n)
        # global minimums
        all_safe = list(R.safe.values()) or ([safe] if safe else [])
        # KiCad never goes below the board minimum, even in custom rules: take the smallest copper pair
        kinds_ = ("track", "smd", "th", "via")
        min_clr = min([v for s_ in all_safe for a in kinds_ for b in kinds_ if (v := s_.value(a, b))] + [base_clr])
        min_track = min([t[0] for t in R.track.values()] + [track[0]])
        edge = (safe.value("outline", "track") if safe else None) or 10.0
        hole_vals = [v for k in ("track", "smd", "th", "via") if safe and (v := safe.value("hole", k))]
        hole_min = min(hole_vals) if hole_vals else None
        hole_clr = hole_min
        rules_cfg = {
            "min_clearance": rnd(mm(min_clr), 4),
            "min_track_width": rnd(mm(min_track), 4),
            "min_via_diameter": rnd(mm(min(v[0] for v in (list(R.via.values()) or [via]))), 4),
            "min_through_hole_diameter": rnd(mm(min(v[3] for v in (list(R.via.values()) or [via]))), 4),
            "min_via_annular_width": rnd(max(0.05, mm(min((v[0] - v[3]) / 2 for v in (list(R.via.values()) or [via])))), 4),
            "min_copper_edge_clearance": rnd(mm(edge), 4),
            "min_hole_clearance": rnd(mm(hole_clr), 4) if hole_clr else 0.25,
            "min_hole_to_hole": rnd(mm(R.hole_to_hole), 4) if R.hole_to_hole else 0.25,
        }
        # EasyEDA's safe-spacing table is per pair of object types (e.g. via to SMD pad 0.1 mm next to BGA
        # pads while via to track is 0.152 mm): one rule per pair that differs from the net class clearance.
        # KiCad applies the rules in file order and the last match wins; conditions are written both ways round.
        dru = ["(version 1)", ""]
        kinds = {"track": "({0}.Type == 'Track' || {0}.Type == 'Arc')", "smd": "{0}.Pad_Type == 'SMD'",
                 "th": "{0}.Pad_Type == 'Through-hole'", "via": "{0}.Type == 'Via'"}
        label = {"track": "track", "smd": "SMD pad", "th": "TH pad", "via": "via"}
        if safe:
            order = list(kinds)
            for i, a in enumerate(order):
                for b in order[i:]:
                    v = safe.value(a, b)
                    if not v or abs(v - base_clr) < 0.01:
                        continue
                    ca, cb = kinds[a], kinds[b]
                    cond = f"{ca.format('A')} && {cb.format('B')}"
                    if a != b:
                        cond = f"({cond}) || ({cb.format('A')} && {ca.format('B')})"
                    dru.append(f"(rule \"{label[a]} to {label[b]}\"\n\t(constraint clearance (min {rnd(mm(v), 4)}mm))\n"
                               f"\t(condition \"{cond}\"))")
                    dru.append("")
            # pours keep their own distance to the board outline (smaller than tracks' in JLC's templates)
            pe = [v for v in (safe.value("fill", "outline"), safe.value("zone", "outline")) if v]
            if pe and min(pe) < edge - 0.01:
                dru.append(f"(rule \"pour to board edge\"\n\t(constraint edge_clearance (min {rnd(mm(min(pe)), 4)}mm))\n"
                           f"\t(condition \"A.Type == 'Zone' || B.Type == 'Zone'\"))")
                dru.append("")
            # hole to copper: the board minimum is the smallest; larger ones per copper type, ascending so that
            # for a pair with holes on both sides the stricter value wins
            holes = sorted((v, k) for k in kinds if (v := safe.value("hole", k)))
            for v, k in holes:
                if hole_min is not None and v > hole_min + 0.01:
                    c = kinds[k]
                    dru.append(f"(rule \"hole to {label[k]}\"\n\t(constraint hole_clearance (min {rnd(mm(v), 4)}mm))\n"
                               f"\t(condition \"{c.format('A')} || {c.format('B')}\"))")
                    dru.append("")
        names = getattr(self, "_regions_named", {})
        for rid, rr in R.region_rules.items():
            if rid in names and rr.get("SAFE") in R.safe:
                v = R.safe[rr["SAFE"]].value("track", "track")
                if v:
                    dru.append(f"(rule \"{names[rid]} clearance\"\n\t(constraint clearance (min {rnd(mm(v), 4)}mm))\n"
                               f"\t(condition \"A.insideArea('{names[rid]}')\"))")
                    dru.append("")
            if rid in names and rr.get("TRACK") in R.track:
                t = R.track[rr["TRACK"]]
                dru.append(f"(rule \"{names[rid]} track width\"\n\t(constraint track_width (min {rnd(mm(t[0]), 4)}mm)"
                           f" (opt {rnd(mm(t[1]), 4)}mm) (max {rnd(mm(t[2]), 4)}mm))\n"
                           f"\t(condition \"A.insideArea('{names[rid]}')\"))")
                dru.append("")
        dru_text = "\n".join(dru) + "\n" if len(dru) > 2 else ""
        widths = sorted({rnd(mm(t[1]), 4) for t in R.track.values()} |
                        {rnd(mm(r.num("width")), 4) for r in self.doc.of("LINE") if L.is_copper(self._layer(r.int("layerId")) or "")})
        vias = sorted({(rnd(mm(r.num("viaDiameter")), 4), rnd(mm(r.num("holeDiameter")), 4)) for r in self.doc.of("VIA")})
        return list(classes.values()), rules_cfg, dru_text, widths[:20], vias[:10]


def _stadium(a, b, r, n=8):
    """Outline of a stroke from a to b with round ends of radius r."""
    ang = math.atan2(b[1] - a[1], b[0] - a[0]) if a != b else 0.0
    out = []
    for c, start in ((b, ang - math.pi / 2), (a, ang + math.pi / 2)):
        for k in range(n + 1):
            t = start + math.pi * k / n
            out.append((c[0] + r * math.cos(t), c[1] + r * math.sin(t)))
    return out


def _copy_fp(fp: Footprint) -> Footprint:
    f = fp.flipped().flipped()
    return f


def _convert_free_pad(r: Rec, lm, opts):
    from .footprint import convert_pad
    return convert_pad(r, lm, (r.num("centerX"), r.num("centerY")), opts)


_EXPR = re.compile(r"\{([^}]+)\}")


def resolve_attr(attrs: dict, key: str, depth: int = 0) -> str:
    v = attrs.get(key)
    if v is None:
        return ""
    v = str(v)
    if v.startswith("=") and depth < 5:
        return _EXPR.sub(lambda m: resolve_attr(attrs, m.group(1), depth + 1), v[1:])
    return v


# EasyEDA bookkeeping attributes that are not part data (or already map to a KiCad field)
_NOT_FIELDS = {"Symbol", "Footprint", "Device", "3D Model", "3D Model Title", "3D Model Transform", "Add into BOM",
               "Convert to PCB", "Name", "Designator", "Value", "Datasheet", "Description", "Supplier Part",
               "Manufacturer Part", "Manufacturer", "Supplier", "Unique ID", "Channel ID", "Reuse Block", "Group ID",
               "CBB Title", "Reference", "Symbol Type", "Sim Model", "Footprint Type"}


def part_fields(attrs: dict) -> dict:
    """EasyEDA part attributes as extra KiCad fields: LCSC, Manufacturer, MPN, then every parameter
    (Tolerance, Voltage Rating, Supplier Footprint, ...) so they stay visible in KiCad."""
    out = {}
    sup = str(attrs.get("Supplier Part") or "").strip()
    if re.match(r"^C\d+$", sup):
        out["LCSC"] = sup
    for src, dst in (("Manufacturer", "Manufacturer"), ("Manufacturer Part", "MPN")):
        v = str(attrs.get(src) or "").strip()
        if v and not v.startswith("="):
            out[dst] = v
    for k, v in attrs.items():
        v = "" if v is None else str(v).strip()
        if k in _NOT_FIELDS or k in out or not v or v.startswith("=") or k.startswith("@"):
            continue
        out[k] = v
    return out


def resolve_value(dattrs: dict, comp_attrs: dict, title: str) -> str:
    merged = dict(dattrs)
    merged.update({k: v for k, v in comp_attrs.items() if v not in (None, "")})
    for k in ("Value", "Name"):
        v = resolve_attr(merged, k).strip()
        if v:
            return v
    for k in ("Manufacturer Part",):
        v = resolve_attr(merged, k).strip()
        if v:
            return v
    return title or "~"


def assign_fp_names(proj: Project) -> dict[str, str]:
    names: dict[str, str] = {}
    used: set[str] = set()
    for uuid, doc in sorted(proj.footprints.items(), key=lambda kv: (kv[1].title, kv[0])):
        base = sane_name(doc.title)
        name = base
        k = 2
        while name.lower() in used:
            name = f"{base}_{k}"
            k += 1
        used.add(name.lower())
        names[uuid] = name
    return names


def pcb_footprint_map(proj: Project, doc: Doc) -> dict:
    """Unique ID / designator -> footprint uuid actually placed on the PCB."""
    out = {}
    attrs: dict[str, dict] = {}
    for a in doc.of("ATTR"):
        attrs.setdefault(str(a.d.get("parentId") or ""), {})[a.str("key")] = a.str("value")
    for c in doc.of("COMPONENT"):
        at = attrs.get(c.id, {})
        fp = at.get("Footprint") or ""
        if not fp and at.get("Device"):
            fp = str(proj.devices.get(at["Device"], {}).get("attributes", {}).get("Footprint") or "")
        if not fp:
            continue
        ca = c.d.get("attrs") or {}
        for k in (ca.get("Unique ID"), ca.get("Channel ID")):
            if k:
                out[k] = fp
    return out
