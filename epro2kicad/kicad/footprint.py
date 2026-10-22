"""KiCad footprint model + conversion from EasyEDA footprint documents.

Coordinates inside ``Footprint`` are KiCad library-local coordinates
(mm, Y axis down) exactly as they appear in a ``.kicad_mod`` file.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .. import geom
from ..easyeda.records import Rec
from ..sexp import NO, YES, Sym, stable_uuid
from . import layers as L
from . import textfit

MIL = 0.0254


def mm(v: float) -> float:
    return v * MIL


def rnd(v: float, nd: int = 6) -> float:
    r = round(v, nd)
    return 0.0 if r == 0 else r


def kpt(p, origin=(0.0, 0.0)):
    """EasyEDA point (mil, Y-up) -> KiCad mm (Y-down)."""
    return (rnd((p[0] - origin[0]) * MIL), rnd(-(p[1] - origin[1]) * MIL))


def krot(p, deg: float):
    """KiCad rotation (Y-down, positive = CCW on screen)."""
    if not deg:
        return p
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return (p[0] * c + p[1] * s, -p[0] * s + p[1] * c)


def norm_deg(a: float) -> float:
    a = math.fmod(a, 360.0)
    if a < 0:
        a += 360.0
    if abs(a) < 1e-6 or abs(a - 360) < 1e-6:
        return 0.0
    return rnd(a, 4)


def sane_name(s: str, maxlen: int = 120) -> str:
    s = (s or "").strip()
    s = re.sub(r'[\\/:"*?<>|\x00-\x1f]', "_", s)
    s = s.replace(" ", "_")
    return s[:maxlen] or "unnamed"


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass
class Graphic:
    kind: str  # line arc circle rect poly curve
    layer: str
    width: float
    pts: list = field(default_factory=list)  # line: [a,b]; arc: [start, mid, end]; circle: [center, end]; rect: [start,end]; poly: [...]; curve: 4 pts
    fill: bool = False

    def flipped(self) -> "Graphic":
        return Graphic(self.kind, L.flip_layer(self.layer), self.width, [(x, -y) for x, y in self.pts], self.fill)

    def bbox_pts(self):
        if self.kind == "circle":
            c, e = self.pts
            r = math.hypot(e[0] - c[0], e[1] - c[1])
            return [(c[0] - r, c[1] - r), (c[0] + r, c[1] + r)]
        return self.pts


@dataclass
class Text:
    kind: str  # reference | value | user
    text: str
    at: tuple
    angle: float
    layer: str
    size: tuple = (1.0, 1.0)
    thickness: float = 0.15
    justify: tuple = ("", "")  # (h: left/right/"" , v: top/bottom/"")
    mirror: bool = False
    hidden: bool = False
    knockout: bool = False
    face: str | None = None
    name: str | None = None  # property name for user properties

    def flipped(self) -> "Text":
        t = Text(**self.__dict__)
        t.at = (self.at[0], -self.at[1])
        t.angle = 180 - self.angle
        t.layer = L.flip_layer(self.layer)
        t.mirror = not self.mirror
        return t


@dataclass
class Pad:
    number: str
    kind: str  # smd thru_hole np_thru_hole connect
    shape: str  # rect roundrect circle oval custom
    at: tuple
    angle: float
    size: tuple
    layers: list
    drill: tuple | None = None  # (oval?, w, h)
    drill_offset: tuple = (0.0, 0.0)
    rratio: float = 0.0
    primitives: list = field(default_factory=list)  # list of polygons (pad-local)
    mask_margin: float | None = None
    paste_margin: float | None = None
    net: str | None = None
    zone_connect: int | None = None
    thermal_width: float | None = None
    thermal_gap: float | None = None
    pinfunction: str | None = None
    pintype: str | None = None
    src_id: str = ""

    def flipped(self) -> "Pad":
        p = Pad(**{k: (list(v) if isinstance(v, list) else v) for k, v in self.__dict__.items()})
        p.at = (self.at[0], -self.at[1])
        p.angle = -self.angle
        p.layers = [L.flip_layer(x) for x in self.layers]
        p.drill_offset = (self.drill_offset[0], -self.drill_offset[1])
        p.primitives = [[(x, -y) for x, y in poly] for poly in self.primitives]
        return p

    def extent_pts(self):
        """Approximate absolute (library frame) extents for bbox computations."""
        w, h = self.size
        if self.shape == "custom" and self.primitives:
            pts = [krot(q, -self.angle) for poly in self.primitives for q in poly]
        else:
            pts = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
            pts = [krot(q, self.angle) for q in pts]
        return [(self.at[0] + q[0], self.at[1] + q[1]) for q in pts]


@dataclass
class Zone:
    """Keepout/rule area inside a footprint or on the board."""

    layers: list
    polygon: list
    name: str = ""
    no_tracks: bool = False
    no_vias: bool = False
    no_pads: bool = False
    no_pour: bool = False
    no_footprints: bool = False


@dataclass
class Model3D:
    path: str
    offset: tuple = (0.0, 0.0, 0.0)
    rotate: tuple = (0.0, 0.0, 0.0)
    scale: tuple = (1.0, 1.0, 1.0)


@dataclass
class Footprint:
    name: str
    descr: str = ""
    tags: str = ""
    attr: list = field(default_factory=list)  # e.g. ["smd"], ["through_hole"]
    texts: list = field(default_factory=list)
    graphics: list = field(default_factory=list)
    pads: list = field(default_factory=list)
    zones: list = field(default_factory=list)
    models: list = field(default_factory=list)
    properties: dict = field(default_factory=dict)  # extra user properties (hidden)
    source_uuid: str = ""
    net_tie: list = field(default_factory=list)  # KiCad net_tie_pad_groups, e.g. ["1,2"] for a PCB antenna

    def ref_text(self) -> Text | None:
        return next((t for t in self.texts if t.kind == "reference"), None)

    def value_text(self) -> Text | None:
        return next((t for t in self.texts if t.kind == "value"), None)

    def flipped(self) -> "Footprint":
        f = Footprint(self.name, self.descr, self.tags, list(self.attr))
        f.texts = [t.flipped() for t in self.texts]
        f.graphics = [g.flipped() for g in self.graphics]
        f.pads = [p.flipped() for p in self.pads]
        f.zones = [Zone([L.flip_layer(x) for x in z.layers], [(x, -y) for x, y in z.polygon], z.name, z.no_tracks,
                        z.no_vias, z.no_pads, z.no_pour, z.no_footprints) for z in self.zones]
        f.models = list(self.models)
        f.properties = dict(self.properties)
        f.source_uuid = self.source_uuid
        f.net_tie = list(self.net_tie)
        return f


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _font(t: Text) -> list:
    f = [Sym("font")]
    if t.face:
        f.append([Sym("face"), t.face])
    f.append([Sym("size"), rnd(t.size[1], 4), rnd(t.size[0], 4)])
    f.append([Sym("thickness"), rnd(t.thickness, 4)])
    return f


def _effects(t: Text) -> list:
    e = [Sym("effects"), _font(t)]
    j = [Sym("justify")]
    if t.justify[0]:
        j.append(Sym(t.justify[0]))
    if t.justify[1]:
        j.append(Sym(t.justify[1]))
    if t.mirror:
        j.append(Sym("mirror"))
    if len(j) > 1:
        e.append(j)
    return e


def _at(p, angle=None) -> list:
    a = [Sym("at"), rnd(p[0]), rnd(p[1])]
    if angle is not None and norm_deg(angle) != 0:
        a.append(norm_deg(angle))
    return a


def text_sexp(t: Text, angle_abs: float, uid: str) -> list:
    if t.kind in ("reference", "value") or t.name:
        pname = {"reference": "Reference", "value": "Value"}.get(t.kind, t.name)
        # EasyEDA prints text at its angle (180 deg = upside down); KiCad's default "keep upright" would turn it
        # around its anchor and move it to the other side
        n = [Sym("property"), pname, t.text, _at(t.at, angle_abs), [Sym("unlocked"), YES]]
        n.append([Sym("layer"), t.layer])
        if t.hidden:
            n.append([Sym("hide"), YES])
        n.append([Sym("uuid"), uid])
        n.append(_effects(t))
        return n
    n = [Sym("fp_text"), Sym("user"), t.text, _at(t.at, angle_abs), [Sym("unlocked"), YES], [Sym("layer"), t.layer]]
    if t.knockout:
        n[-1].append(Sym("knockout"))
    if t.hidden:
        n.append([Sym("hide"), YES])
    n.append([Sym("uuid"), uid])
    n.append(_effects(t))
    return n


def _stroke(w: float) -> list:
    return [Sym("stroke"), [Sym("width"), rnd(w, 4)], [Sym("type"), Sym("solid")]]


def graphic_sexp(g: Graphic, uid: str | None, prefix: str = "fp_") -> list:
    P = lambda p: [rnd(p[0]), rnd(p[1])]  # noqa: E731
    if g.kind == "line":
        n = [Sym(prefix + "line"), [Sym("start"), *P(g.pts[0])], [Sym("end"), *P(g.pts[1])], _stroke(g.width)]
    elif g.kind == "arc":
        n = [Sym(prefix + "arc"), [Sym("start"), *P(g.pts[0])], [Sym("mid"), *P(g.pts[1])], [Sym("end"), *P(g.pts[2])],
             _stroke(g.width)]
    elif g.kind == "circle":
        n = [Sym(prefix + "circle"), [Sym("center"), *P(g.pts[0])], [Sym("end"), *P(g.pts[1])], _stroke(g.width),
             [Sym("fill"), YES if g.fill else NO]]
    elif g.kind == "rect":
        n = [Sym(prefix + "rect"), [Sym("start"), *P(g.pts[0])], [Sym("end"), *P(g.pts[1])], _stroke(g.width),
             [Sym("fill"), YES if g.fill else NO]]
    elif g.kind == "curve":
        n = [Sym(prefix + "curve"), [Sym("pts")] + [[Sym("xy"), *P(p)] for p in g.pts], _stroke(g.width)]
    else:
        n = [Sym(prefix + "poly"), [Sym("pts")] + [[Sym("xy"), *P(p)] for p in g.pts], _stroke(g.width),
             [Sym("fill"), YES if g.fill else NO]]
    n.append([Sym("layer"), g.layer])
    if uid:
        n.append([Sym("uuid"), uid])
    return n


def pad_sexp(p: Pad, angle_abs: float, uid: str | None, with_net: bool) -> list:
    n = [Sym("pad"), p.number, Sym(p.kind), Sym(p.shape), _at(p.at, angle_abs),
         [Sym("size"), rnd(max(p.size[0], 0.001)), rnd(max(p.size[1], 0.001))]]
    if p.drill:
        oval, w, h = p.drill
        d = [Sym("drill")]
        if oval and abs(w - h) > 1e-6:
            d += [Sym("oval"), rnd(w), rnd(h)]
        else:
            d.append(rnd(w))
        if abs(p.drill_offset[0]) > 1e-6 or abs(p.drill_offset[1]) > 1e-6:
            d.append([Sym("offset"), rnd(p.drill_offset[0]), rnd(p.drill_offset[1])])
        n.append(d)
    n.append([Sym("layers")] + list(p.layers))
    if p.kind == "thru_hole":
        n.append([Sym("remove_unused_layers"), NO])
    if p.shape == "roundrect":
        n.append([Sym("roundrect_rratio"), rnd(p.rratio, 4)])
    if with_net and p.net:
        n.append([Sym("net"), p.net])
    if p.pinfunction:
        n.append([Sym("pinfunction"), p.pinfunction])
    if p.pintype:
        n.append([Sym("pintype"), p.pintype])
    if p.mask_margin is not None:
        n.append([Sym("solder_mask_margin"), rnd(p.mask_margin, 4)])
    if p.paste_margin is not None:
        n.append([Sym("solder_paste_margin"), rnd(p.paste_margin, 4)])
    if p.zone_connect is not None:
        n.append([Sym("zone_connect"), p.zone_connect])
    if p.thermal_width is not None:
        n.append([Sym("thermal_bridge_width"), rnd(p.thermal_width, 4)])
    if p.thermal_gap is not None:
        n.append([Sym("thermal_gap"), rnd(p.thermal_gap, 4)])
    if p.shape in ("circle", "custom"):
        # KiCad's default spokes for round pads are diagonal; EasyEDA's are straight (between BGA balls only a
        # straight spoke fits, without it the pour there becomes an island and is removed)
        # shortcut: always 90 deg, the rule's own angle (COPPER spoAng) is 90 in every project seen so far
        n.append([Sym("thermal_bridge_angle"), 90])
    if p.shape == "custom":
        n.append([Sym("options"), [Sym("clearance"), Sym("outline")], [Sym("anchor"), Sym("circle")]])
        prims = [Sym("primitives")]
        for poly in p.primitives:
            prims.append([Sym("gr_poly"), [Sym("pts")] + [[Sym("xy"), rnd(x), rnd(y)] for x, y in poly],
                          [Sym("width"), 0], [Sym("fill"), YES]])
        n.append(prims)
    if uid:
        n.append([Sym("uuid"), uid])
    return n


def zone_sexp(z: Zone, uid: str, in_footprint: bool) -> list:
    n = [Sym("zone")]
    if len(z.layers) == 1:
        n.append([Sym("layer"), z.layers[0]])
    else:
        n.append([Sym("layers")] + z.layers)
    n.append([Sym("uuid"), uid])
    if z.name:
        n.append([Sym("name"), z.name])
    n.append([Sym("hatch"), Sym("edge"), 0.5])
    n.append([Sym("connect_pads"), [Sym("clearance"), 0]])
    n.append([Sym("min_thickness"), 0.25])
    n.append([Sym("keepout"),
              [Sym("tracks"), Sym("not_allowed" if z.no_tracks else "allowed")],
              [Sym("vias"), Sym("not_allowed" if z.no_vias else "allowed")],
              [Sym("pads"), Sym("not_allowed" if z.no_pads else "allowed")],
              [Sym("copperpour"), Sym("not_allowed" if z.no_pour else "allowed")],
              [Sym("footprints"), Sym("not_allowed" if z.no_footprints else "allowed")]])
    n.append([Sym("placement"), [Sym("enabled"), NO], [Sym("sheetname"), ""]])
    n.append([Sym("fill"), [Sym("thermal_gap"), 0.5], [Sym("thermal_bridge_width"), 0.5],
              [Sym("island_removal_mode"), 0]])
    n.append([Sym("polygon"), [Sym("pts")] + [[Sym("xy"), rnd(x), rnd(y)] for x, y in z.polygon]])
    return n


def model_sexp(m: Model3D) -> list:
    return [Sym("model"), m.path,
            [Sym("offset"), [Sym("xyz"), rnd(m.offset[0], 4), rnd(m.offset[1], 4), rnd(m.offset[2], 4)]],
            [Sym("scale"), [Sym("xyz"), rnd(m.scale[0], 4), rnd(m.scale[1], 4), rnd(m.scale[2], 4)]],
            [Sym("rotate"), [Sym("xyz"), rnd(m.rotate[0], 4), rnd(m.rotate[1], 4), rnd(m.rotate[2], 4)]]]


def _prop(name: str, value: str, layer: str, uid: str | None, hidden=True, at=(0, 0), angle=0.0) -> list:
    n = [Sym("property"), name, value, _at(at, angle), [Sym("layer"), layer]]
    if hidden:
        n.append([Sym("hide"), YES])
    if uid:
        n.append([Sym("uuid"), uid])
    n.append([Sym("effects"), [Sym("font"), [Sym("size"), 1, 1], [Sym("thickness"), 0.15]]])
    return n


def footprint_lib_sexp(fp: Footprint) -> list:
    n = [Sym("footprint"), fp.name,
         [Sym("version"), 20260206],
         [Sym("generator"), "epro2kicad"],
         [Sym("generator_version"), "1.0"],
         [Sym("layer"), "F.Cu"]]
    if fp.descr:
        n.append([Sym("descr"), fp.descr])
    if fp.tags:
        n.append([Sym("tags"), fp.tags])
    for t in fp.texts:
        if t.kind in ("reference", "value"):
            n.append(text_sexp(t, t.angle, stable_uuid(fp.name, "lib", t.kind)))
    for k, v in fp.properties.items():
        n.append(_prop(k, v, "F.Fab", None))
    if fp.attr:
        n.append([Sym("attr")] + [Sym(a) for a in fp.attr])
    if fp.net_tie:
        n.append([Sym("net_tie_pad_groups")] + fp.net_tie)
    n.append([Sym("duplicate_pad_numbers_are_jumpers"), NO])
    for i, g in enumerate(fp.graphics):
        n.append(graphic_sexp(g, stable_uuid(fp.name, "lib-g", i)))
    for i, t in enumerate(fp.texts):
        if t.kind == "user":
            n.append(text_sexp(t, t.angle, stable_uuid(fp.name, "lib-t", i)))
    for i, p in enumerate(fp.pads):
        n.append(pad_sexp(p, p.angle, stable_uuid(fp.name, "lib-p", i), with_net=False))
    for i, z in enumerate(fp.zones):
        n.append(zone_sexp(z, stable_uuid(fp.name, "lib-z", i), True))
    n.append([Sym("embedded_fonts"), NO])
    for m in fp.models:
        n.append(model_sexp(m))
    return n


def footprint_board_sexp(fp: Footprint, lib_id: str, pos: tuple, angle: float, back: bool, uid: str,
                         path: str | None, sheetname: str | None, sheetfile: str | None,
                         extra_props: dict, attr_override: list | None = None, locked: bool = False) -> list:
    """Footprint instance inside a .kicad_pcb.  `fp` must already be flipped when on the back."""
    n = [Sym("footprint"), lib_id]
    if locked:
        n.append(Sym("locked"))
    n += [[Sym("layer"), "B.Cu" if back else "F.Cu"], [Sym("uuid"), uid], _at(pos, angle)]
    if fp.descr:
        n.append([Sym("descr"), fp.descr])
    if fp.tags:
        n.append([Sym("tags"), fp.tags])
    for t in fp.texts:
        if t.kind in ("reference", "value"):
            n.append(text_sexp(t, angle + t.angle, stable_uuid(uid, t.kind)))
    fab = "B.Fab" if back else "F.Fab"
    for k, v in list(fp.properties.items()) + list(extra_props.items()):
        if k in ("Reference", "Value"):
            continue
        n.append(_prop(k, v, fab, stable_uuid(uid, "prop", k), at=(0, 0), angle=angle))
    if path:
        n.append([Sym("path"), path])
    if sheetname is not None:
        n.append([Sym("sheetname"), sheetname])
    if sheetfile is not None:
        n.append([Sym("sheetfile"), sheetfile])
    attr = attr_override if attr_override is not None else fp.attr
    if attr:
        n.append([Sym("attr")] + [Sym(a) for a in attr])
    if fp.net_tie:
        n.append([Sym("net_tie_pad_groups")] + fp.net_tie)
    n.append([Sym("duplicate_pad_numbers_are_jumpers"), NO])
    for i, g in enumerate(fp.graphics):
        n.append(graphic_sexp(g, stable_uuid(uid, "g", i)))
    for i, t in enumerate(fp.texts):
        if t.kind == "user":
            n.append(text_sexp(t, angle + t.angle, stable_uuid(uid, "t", i)))
    for i, p in enumerate(fp.pads):
        n.append(pad_sexp(p, angle + p.angle, stable_uuid(uid, "p", i), with_net=True))
    for i, z in enumerate(fp.zones):
        n.append(zone_sexp(z, stable_uuid(uid, "z", i), True))
    n.append([Sym("embedded_fonts"), NO])
    for m in fp.models:
        n.append(model_sexp(m))
    return n


# ---------------------------------------------------------------------------
# EasyEDA -> KiCad conversion
# ---------------------------------------------------------------------------

_ALIGN = {
    "LEFT": "left", "RIGHT": "right", "CENTER": "",
    "TOP": "top", "BOTTOM": "bottom", "MIDDLE": "",
}


def origin_justify(origin: str | None) -> tuple:
    o = (origin or "LEFT_BOTTOM").upper()
    if "_" in o:
        h, v = o.split("_", 1)
    else:
        h, v = "LEFT", "BOTTOM"
    return (_ALIGN.get(h, ""), _ALIGN.get(v, ""))


@dataclass
class FpOptions:
    default_mask_mil: float | None = 2.0
    default_paste_mil: float = 0.0
    courtyard: bool = True


def _shape_graphics(shape: geom.Shape, layer: str, width: float, origin, fill=False) -> list[Graphic]:
    out: list[Graphic] = []
    if shape.kind == "circle":
        c = kpt(shape.center, origin)
        out.append(Graphic("circle", layer, width, [c, (rnd(c[0] + shape.r * MIL), c[1])], fill))
        return out
    if shape.kind == "rect" and abs(shape.angle) % 360 < 1e-9 and shape.radius <= 1e-9:
        a = kpt((shape.x, shape.y), origin)
        b = kpt((shape.x + shape.w, shape.y - shape.h), origin)
        out.append(Graphic("rect", layer, width, [a, b], fill))
        return out
    if fill:
        poly = [kpt(p, origin) for p in shape.polygon(max_err=0.2)]
        if len(poly) >= 3:
            out.append(Graphic("poly", layer, width, poly, True))
        return out
    for s in shape.to_segments():
        if s.kind == "line":
            if geom.dist(s.a, s.b) > 1e-6:
                out.append(Graphic("line", layer, width, [kpt(s.a, origin), kpt(s.b, origin)]))
        elif s.kind == "arc":
            if abs(s.sweep) >= 359.999:
                c = geom.arc_center(s.a, s.b, s.sweep)
                kc = kpt(c, origin)
                out.append(Graphic("circle", layer, width, [kc, kpt(s.a, origin)]))
            elif abs(s.sweep) > 1e-6 and geom.dist(s.a, s.b) > 1e-6:
                m = geom.arc_mid(s.a, s.b, s.sweep)
                out.append(Graphic("arc", layer, width, [kpt(s.a, origin), kpt(m, origin), kpt(s.b, origin)]))
        else:
            out.append(Graphic("curve", layer, width, [kpt(s.a, origin), kpt(s.c1, origin), kpt(s.c2, origin),
                                                      kpt(s.b, origin)]))
    return out


def convert_pad(r: Rec, lm: L.LayerMap, origin, opts: FpOptions) -> Pad | None:
    lid = r.int("layerId", 1)
    num = r.str("num")
    c = (r.num("centerX"), r.num("centerY"))
    angle = norm_deg(r.num("padAngle"))
    hole = r.d.get("hole")
    shp = r.d.get("defaultPad") or {}
    has_hole = False
    drill = None
    if isinstance(hole, dict):
        ht = (hole.get("holeType") or "ROUND").upper()
        hw = float(hole.get("width") or 0)
        hh = float(hole.get("height") or hw)
        if hw > 0 or hh > 0:
            has_hole = True
            rel = norm_deg(r.num("relativeAngle"))
            if ht in ("SLOT", "RECT", "OVAL"):
                if 45 <= (rel % 180) < 135:
                    hw, hh = hh, hw
                drill = (True, mm(hw), mm(hh))
            else:
                drill = (False, mm(hw), mm(hw))
    plated = r.d.get("plated")
    plated = True if plated is None else bool(plated)
    if has_hole:
        kind = "thru_hole" if plated else "np_thru_hole"
        layers = ["*.Cu", "*.Mask"]
    else:
        side = lm.copper(lid) or "F.Cu"
        if side not in ("F.Cu", "B.Cu"):
            side = "F.Cu"
        kind = "smd"
        pre = "F" if side == "F.Cu" else "B"
        layers = [side, f"{pre}.Mask", f"{pre}.Paste"]
    pt = (shp.get("padType") or "RECT").upper()
    w = mm(float(shp.get("width") or 0))
    h = mm(float(shp.get("height") or 0))
    pad = Pad(num, kind, "rect", kpt(c, origin), angle, (w, h), layers, drill)
    if pt == "RECT":
        rad = float(shp.get("radius") or 0)
        if rad > 0:
            pad.shape = "roundrect"
            pad.rratio = min(0.5, rad / 100.0)
        else:
            pad.shape = "rect"
    elif pt == "ELLIPSE":
        pad.shape = "circle" if abs(w - h) < 1e-6 else "oval"
    elif pt == "OVAL":
        pad.shape = "oval" if abs(w - h) > 1e-6 else "circle"
    else:
        path = shp.get("path")
        shapes = geom.parse_contours(path) if path else []
        polys = []
        for s in shapes:
            poly = s.polygon(max_err=0.1)
            if len(poly) >= 3:
                # absolute footprint coords -> pad-local, unrotated
                local = [geom.rot(geom.sub(p, c), -angle) for p in poly]
                polys.append([(rnd(x * MIL), rnd(-y * MIL)) for x, y in local])
        if not polys:
            return None
        pad.shape = "custom"
        bx = geom.bbox([q for poly in polys for q in poly])
        anchor = max(0.01, min(bx[2] - bx[0], bx[3] - bx[1]) * 0.5)
        anchor = min(anchor, 0.2)
        pad.size = (anchor, anchor)
        pad.primitives = polys
    if pad.shape == "circle":
        d = max(w, h)
        pad.size = (d, d)
    # drill offset (EasyEDA stores the pad shape offset relative to the hole)
    ox, oy = r.num("padOffsetX"), r.num("padOffsetY")
    if has_hole and (abs(ox) > 0.05 or abs(oy) > 0.05):
        pad.drill_offset = (rnd(ox * MIL), rnd(-oy * MIL))
    # mask / paste
    top_mask = r.d.get("topSolderExpansion")
    if isinstance(top_mask, (int, float)) and top_mask > -1000:
        if top_mask <= -999:
            pass
        elif opts.default_mask_mil is None or abs(top_mask - opts.default_mask_mil) > 0.06:
            pad.mask_margin = mm(top_mask)
    elif isinstance(top_mask, (int, float)) and top_mask <= -1000 and kind == "smd":
        pad.layers = [x for x in pad.layers if not x.endswith(".Mask")]  # EasyEDA: -1000 and below = no opening
    top_paste = r.d.get("topPasteExpansion")
    if kind == "smd" and isinstance(top_paste, (int, float)):
        if top_paste <= -1000:
            # no paste from the pad itself (EasyEDA writes -3937 mil); footprints then draw their own paste
            # shapes, e.g. a smaller window on a QFN's exposed pad
            pad.layers = [x for x in pad.layers if not x.endswith(".Paste")]
        elif abs(top_paste - opts.default_paste_mil) > 0.06:
            pad.paste_margin = mm(top_paste)
    if kind == "np_thru_hole":
        # NPTH: a pad bigger than the hole gets copper on both sides
        if drill and max(pad.size) <= max(drill[1], drill[2]) + 1e-6:
            pad.layers = ["*.Cu", "*.Mask"]
            pad.size = (drill[1], drill[2])
            pad.shape = "oval" if drill[0] and abs(drill[1] - drill[2]) > 1e-6 else "circle"
    # per-pad thermal (v3)
    cm = r.d.get("connectMode")
    if isinstance(cm, str):
        pad.zone_connect = {"DIRECT": 2, "DIVERGENCE": 1, "NONE": 0, "NOCONNECT": 0}.get(cm.upper())
        if r.d.get("spokeWidth"):
            pad.thermal_width = mm(float(r.d["spokeWidth"]))
        if r.d.get("spokeSpace"):
            pad.thermal_gap = mm(float(r.d["spokeSpace"]))
    return pad


def _npth_from_fill(shape: geom.Shape, origin) -> Pad | None:
    """FILL on the multi/hole layer = non plated hole (or cutout)."""
    if shape.kind == "circle":
        d = mm(shape.r * 2)
        return Pad("", "np_thru_hole", "circle", kpt(shape.center, origin), 0, (d, d), ["*.Cu"],
                   (False, d, d))  # EasyEDA's Gerber has no mask opening on these holes
    if shape.kind == "rect":
        w, h = abs(shape.w), abs(shape.h)
        rad = shape.radius
        if rad >= min(w, h) / 2 - 0.5:  # rounded slot
            c = shape.rect_center()
            if w >= h:
                ang = shape.angle
                size = (mm(w), mm(h))
            else:
                ang = shape.angle
                size = (mm(w), mm(h))
            return Pad("", "np_thru_hole", "oval", kpt(c, origin), norm_deg(ang), size, ["*.Cu"],
                       (True, size[0], size[1]))
    return None


def convert_footprint(doc, lm: L.LayerMap | None = None, opts: FpOptions | None = None,
                      name: str | None = None) -> Footprint:
    lm = lm or L.LayerMap()
    opts = opts or FpOptions()
    canvas = next((r for r in doc.recs if r.type == "CANVAS"), None)
    origin = (canvas.num("originX"), canvas.num("originY")) if canvas else (0.0, 0.0)
    fp = Footprint(sane_name(name or doc.title), source_uuid=doc.uuid)
    pads_by_id = {}
    for r in doc.recs:
        if r.type == "PAD":
            p = convert_pad(r, lm, origin, opts)
            if p:
                p.src_id = r.id
                fp.pads.append(p)
                pads_by_id[r.id] = p
    for r in doc.recs:
        if r.type == "POLY":
            layer = lm.to_kicad(r.int("layerId"))
            if not layer or layer == "Edge.Cuts" and r.int("layerId") == L.MULTI:
                if r.int("layerId") != L.MULTI:
                    continue
                layer = "Edge.Cuts"
            w = mm(r.num("width"))
            for shape in geom.parse_contours(r.d.get("path")):
                fp.graphics += _shape_graphics(shape, layer, w if layer != "Edge.Cuts" else 0.05, origin)
        elif r.type == "FILL":
            lid = r.int("layerId")
            shapes = geom.parse_contours(r.d.get("path"))
            if lid in (L.MULTI, L.HOLE):
                for s in shapes:
                    p = _npth_from_fill(s, origin)
                    if p:
                        fp.pads.append(p)
                    else:
                        fp.graphics += _shape_graphics(s, "Edge.Cuts", 0.05, origin)
                continue
            if lid == L.OUTLINE:
                for s in shapes:
                    fp.graphics += _shape_graphics(s, "Edge.Cuts", 0.05, origin)
                continue
            layer = lm.to_kicad(lid)
            if not layer:
                continue
            polys = [s.polygon(0.2) for s in shapes]
            for outline, holes in geom.nest_contours(polys):
                pts = geom.fracture(outline, holes) if holes else outline
                if len(pts) >= 3:
                    fp.graphics.append(Graphic("poly", layer, 0, [kpt(p, origin) for p in pts], True))
        elif r.type == "STRING":
            layer = lm.to_kicad(r.int("layerId"))
            if not layer or not r.str("text"):
                continue
            x, y, w, h = textfit.fit(r.str("text"), r.num("x"), r.num("y"), r.num("angle"), r.str("origin"),
                                     r.num("fontSize", 45), r.num("strokeWidth", 6), layer.startswith("B."))
            t = Text("user", r.str("text"), kpt((x, y), origin), norm_deg(r.num("angle")), layer,
                     (rnd(w * MIL, 4), rnd(h * MIL, 4)), max(0.05, mm(r.num("strokeWidth", 6))), ("", ""),
                     mirror=layer.startswith("B."), knockout=r.bool("reverse"))
            ff = r.str("fontFamily", "default")
            if ff and ff != "default":
                t.face = ff
            fp.texts.append(t)
        elif r.type == "REGION":
            layer = lm.to_kicad(r.int("layerId"))
            flags = r.d.get("prohibitType") or []
            z = _region_zone(r, layer, flags, origin)
            if z:
                fp.zones.append(z)
    _finish_footprint(fp, opts)
    fp.net_tie = net_tie_groups(fp)
    return fp


def _seg_dist(a, b, c, d) -> float:
    """Distance between segments ab and cd."""
    if geom._seg_intersect(a, b, c, d):
        return 0.0

    def pt_seg(p, s0, s1):
        dx, dy = s1[0] - s0[0], s1[1] - s0[1]
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - s0[0]) * dx + (p[1] - s0[1]) * dy) / L2))
        return math.hypot(p[0] - s0[0] - t * dx, p[1] - s0[1] - t * dy)
    return min(pt_seg(a, c, d), pt_seg(b, c, d), pt_seg(c, a, b), pt_seg(d, a, b))


def _outline(g: Graphic) -> tuple[list, bool]:
    """Polygon/polyline of a graphic and whether it is a filled area."""
    if g.kind == "rect":
        (x0, y0), (x1, y1) = g.pts
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], g.fill
    if g.kind == "circle":
        c, e = g.pts
        pts = geom.circle_points(c, math.hypot(e[0] - c[0], e[1] - c[1]), 0.02)
        return pts + pts[:1], g.fill
    if g.kind == "poly":
        return list(g.pts) + list(g.pts[:1]), g.fill
    return list(g.pts), False  # line, arc (start, mid, end), curve: as a polyline


def net_tie_groups(fp: Footprint) -> list:
    """Pads that the footprint's own copper drawing connects (a PCB antenna joins its feed to GND on purpose).
    EasyEDA treats that copper as part of the footprint; in KiCad it has no net and would short them unless the
    footprint declares those pads a net tie."""
    cu = [g for g in fp.graphics if L.is_copper(g.layer)]
    if not cu or len(cu) > 400 or len(fp.pads) < 2:
        return []
    outl = [_outline(g) for g in cu]

    def touches(i, poly, filled, tol):
        segs = list(zip(outl[i][0], outl[i][0][1:]))
        if any(_seg_dist(a, b, c, d) <= tol + cu[i].width / 2 for a, b in segs for c, d in zip(poly, poly[1:])):
            return True
        if outl[i][1] and any(geom.point_in_poly(q, outl[i][0]) for q in poly):
            return True
        return filled and any(geom.point_in_poly(q, poly) for q in outl[i][0])

    def on(pad, layer):
        return layer in pad.layers or "*.Cu" in pad.layers
    parent = list(range(len(cu)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(len(cu)):  # drawings that touch each other are one piece of copper
        for j in range(i + 1, len(cu)):
            if cu[i].layer == cu[j].layer and touches(i, outl[j][0], outl[j][1], cu[j].width / 2 + 0.001):
                parent[find(i)] = find(j)
    groups: dict = {}
    for pad in fp.pads:
        if not pad.number or pad.kind == "np_thru_hole":
            continue
        box = pad.extent_pts()
        box = box + box[:1]
        for i, g in enumerate(cu):
            if on(pad, g.layer) and touches(i, box, True, 0.001):
                groups.setdefault(find(i), set()).add(pad.number)
    out = sorted({",".join(sorted(nums, key=lambda x: (len(x), x))) for nums in groups.values() if len(nums) > 1})
    return out


def _region_zone(r: Rec, layer, flags, origin) -> Zone | None:
    if layer is None:
        layer = "F.Cu"
    if not L.is_copper(layer):
        layer = "F.Cu"
    names = set()
    for f in flags:
        if isinstance(f, (int, float)):
            # v2 numeric flags.  7 is a copper-pour keepout: real boards route hundreds of tracks/vias
            # through regions flagged [7] (KiCad's builtin importer wrongly treats it as "keep out all")
            names.add({2: "COMPONENT", 5: "TRACK", 6: "FILL", 7: "POUR", 8: "PLANE"}.get(int(f), str(f)))
        else:
            names.add(str(f).upper())
    shapes = geom.parse_contours(r.d.get("path"))
    if not shapes:
        return None
    poly = [kpt(p, origin) for p in shapes[0].polygon(0.5)]
    if len(poly) < 3:
        return None
    allc = "ALL" in names
    return Zone([layer], poly, name=r.str("name"),
                no_tracks=allc or "TRACK" in names,
                no_vias=allc or "VIA" in names,
                no_pads=allc or "PAD" in names,
                no_pour=allc or "FILL" in names or "PLANE" in names or "COPPER" in names or "POUR" in names,
                no_footprints="COMPONENT" in names)


def _snap_out(v: float, g: float = 0.01, up: bool = True) -> float:
    return rnd((math.ceil(v / g - 1e-9) if up else math.floor(v / g + 1e-9)) * g, 4)


def _finish_footprint(fp: Footprint, opts: FpOptions) -> None:
    # attributes
    kinds = {p.kind for p in fp.pads if p.number != ""}
    if "thru_hole" in kinds:
        fp.attr = ["through_hole"]
    elif "smd" in kinds:
        fp.attr = ["smd"]
    else:
        fp.attr = []
    # bbox of pads + fab body
    pts = []
    for p in fp.pads:
        pts += p.extent_pts()
    body = [q for g in fp.graphics if g.layer == "F.Fab" for q in g.bbox_pts()]
    silk = [q for g in fp.graphics if g.layer == "F.SilkS" for q in g.bbox_pts()]
    ext = pts + (body if body else silk)
    if not ext:
        ext = [(-0.5, -0.5), (0.5, 0.5)]
    x0, y0, x1, y1 = geom.bbox(ext)
    if opts.courtyard and pts:
        cx0, cy0 = _snap_out(x0 - 0.25, up=False), _snap_out(y0 - 0.25, up=False)
        cx1, cy1 = _snap_out(x1 + 0.25), _snap_out(y1 + 0.25)
        fp.graphics.append(Graphic("rect", "F.CrtYd", 0.05, [(cx0, cy0), (cx1, cy1)]))
    # fields, like KiCad's own libraries: Reference on silk above, Value on fab below
    fp.texts.insert(0, Text("reference", "REF**", (rnd((x0 + x1) / 2, 3), rnd(y0 - 1.0, 3)), 0, "F.SilkS"))
    fp.texts.insert(1, Text("value", fp.name, (rnd((x0 + x1) / 2, 3), rnd(y1 + 1.0, 3)), 0, "F.Fab"))
    size = max(0.5, min(1.0, (min(x1 - x0, y1 - y0)) * 0.3))
    size = round(size, 2)
    fp.texts.append(Text("user", "${REFERENCE}", (rnd((x0 + x1) / 2, 3), rnd((y0 + y1) / 2, 3)), 0, "F.Fab",
                         (size, size), round(size * 0.15, 3)))
