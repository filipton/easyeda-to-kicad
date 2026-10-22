"""EasyEDA Pro symbol -> KiCad library symbol."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field

from ..easyeda.project import Doc
from ..sexp import NO, YES, Sym, loads

U = 0.254  # EasyEDA schematic unit (10 mil) in mm
STOCK_DIRS = [os.environ.get("KICAD10_SYMBOL_DIR", ""), "/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols",
              "/usr/share/kicad/symbols", r"C:\Program Files\KiCad\10.0\share\kicad\symbols"]


def r4(v: float) -> float:
    v = round(v, 4)
    return 0.0 if v == 0 else v


def kp(x, y):
    return (r4(x * U), r4(y * U))


def font(size=1.27, bold=False, italic=False):
    f = [Sym("font"), [Sym("size"), size, size]]
    if bold:
        f.append([Sym("bold"), YES])
    if italic:
        f.append([Sym("italic"), YES])
    return f


def effects(size=1.27, justify=None, hide=False, bold=False, italic=False):
    e = [Sym("effects"), font(size, bold, italic)]
    if justify:
        e.append([Sym("justify")] + [Sym(j) for j in justify])
    if hide:
        e.append([Sym("hide"), YES])
    return e


def prop(name, value, at=(0, 0), angle=0, hide=False, size=1.27, justify=None):
    n = [Sym("property"), name, value, [Sym("at"), r4(at[0]), r4(at[1]), angle]]
    if hide:
        n.append([Sym("hide"), YES])
    n += [[Sym("show_name"), NO], [Sym("do_not_autoplace"), NO], effects(size, justify)]
    return n


PIN_TYPES = {"IN": "input", "INPUT": "input", "OUT": "output", "OUTPUT": "output", "BI": "bidirectional",
             "BIDIRECTIONAL": "bidirectional", "POWER": "power_in", "PWR": "power_in", "OPEN COLLECTOR": "open_collector",
             "OC": "open_collector", "OPEN EMITTER": "open_emitter", "TRISTATE": "tri_state", "PASSIVE": "passive",
             "UNDEFINED": "passive", "": "passive", "NC": "no_connect"}
PIN_SHAPES = {0: "line", "NONE": "line", 1: "inverted", "INVERTED": "inverted", "DOT": "inverted", 2: "clock",
              "CLOCK": "clock", 3: "inverted_clock", "INVERTED_CLOCK": "inverted_clock", "DOT_CLOCK": "inverted_clock"}


@dataclass
class SymPin:
    number: str
    name: str
    x: float  # EasyEDA units, Y-up, connection point
    y: float
    rot: float  # direction towards body
    length: float
    etype: str
    shape: str
    hidden: bool
    name_visible: bool
    number_visible: bool
    unit: int


@dataclass
class LibSymbol:
    name: str
    units: list = field(default_factory=list)  # part ids in order
    unit_names: list = field(default_factory=list)
    graphics: dict = field(default_factory=dict)  # unit -> [sexp]
    pins: list = field(default_factory=list)  # SymPin
    bbox: dict = field(default_factory=dict)  # unit -> (x0,y0,x1,y1) EasyEDA units
    ref_prefix: str = "U"
    kind: int = 2  # EasyEDA symbol type
    source_uuid: str = ""

    def unit_of(self, part_id: str) -> int:
        if part_id in self.units:
            return self.units.index(part_id) + 1
        # tolerate "NAME.2" style ids
        m = re.search(r"\.(\d+)$", part_id or "")
        if m and 1 <= int(m.group(1)) <= len(self.units):
            return int(m.group(1))
        return 1

    def pins_of(self, unit: int) -> list[SymPin]:
        return [p for p in self.pins if p.unit == unit or p.unit == 0]


def _attrs_by_parent(doc: Doc):
    out: dict[str, dict] = {}
    for a in doc.of("ATTR"):
        out.setdefault(str(a.d.get("parentId") or ""), {})[a.str("key")] = a
    return out


def _is_visible(a) -> bool:
    if a is None:
        return False
    v = a.d.get("valueVisible")
    return bool(v) if v is not None else True


def _stroke(width=0.0, kind="default"):
    return [Sym("stroke"), [Sym("width"), width], [Sym("type"), Sym(kind)]]


def _fill(kind="none"):
    return [Sym("fill"), [Sym("type"), Sym(kind)]]


def _has_fill(r) -> bool:
    fc = r.d.get("fillColor")
    return isinstance(fc, str) and fc.lower() not in ("", "none", "transparent")


def convert_symbol(doc: Doc, name: str) -> LibSymbol:
    sym = LibSymbol(name, kind=int(doc.meta.get("symbolType") or 2), source_uuid=doc.uuid)
    parts = [r for r in doc.of("PART")]
    sym.units = [p.id for p in parts] or [""]
    sym.unit_names = [p.d.get("title") or p.id for p in parts]
    attrs = _attrs_by_parent(doc)
    top = attrs.get("", {})
    des = top.get("Designator")
    if des is not None:
        pref = re.sub(r"[?\d]+$", "", des.str("value")).strip()
        sym.ref_prefix = pref or "U"

    def unit_for(r) -> int:
        pid = r.d.get("partId")
        if pid in sym.units:
            return sym.units.index(pid) + 1
        return 1

    pin_types = []
    for r in doc.of("PIN"):
        pa = attrs.get(r.id, {})
        t = pa.get("Pin Type")
        pin_types.append((t.str("value") if t else "").strip().upper())
    # EasyEDA library parts default every pin to "IN"; only trust IN when types are mixed
    trust_in = len(set(pin_types) - {"", "UNDEFINED"}) > 1
    for r in doc.of("PIN"):
        pa = attrs.get(r.id, {})
        nm = pa.get("NAME") or pa.get("Pin Name")
        no = pa.get("NUMBER") or pa.get("Pin Number")
        t = pa.get("Pin Type")
        tv = (t.str("value") if t else "").strip().upper()
        if not tv and r.d.get("electric") is not None:
            tv = {0: "", 1: "IN", 2: "OUT", 3: "BI", 4: "POWER"}.get(r.int("electric"), "")
        etype = PIN_TYPES.get(tv, "passive")
        if etype == "input" and not trust_in:
            etype = "passive"
        shape = PIN_SHAPES.get(r.d.get("pinShape"), "line")
        name_s = nm.str("value") if nm else ""
        num_s = no.str("value") if no else ""
        disp = r.d.get("display")
        sym.pins.append(SymPin(num_s, name_s, r.num("x"), r.num("y"), r.num("rotation"), r.num("length"), etype,
                               shape, disp in (False, 0), _is_visible(nm) and bool(name_s),
                               _is_visible(no), unit_for(r)))
    for r in doc.recs:
        g = _graphic(r)
        if g is not None:
            sym.graphics.setdefault(unit_for(r), []).append(g)
    # bounding boxes per unit (graphics + pins)
    for u in range(1, len(sym.units) + 1):
        pts = []
        for r in doc.recs:
            if unit_for(r) != u:
                continue
            pts += _record_points(r)
        for p in sym.pins:
            if p.unit == u:
                pts.append((p.x, p.y))
        if pts:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            sym.bbox[u] = (min(xs), min(ys), max(xs), max(ys))
        else:
            sym.bbox[u] = (-5, -5, 5, 5)
    _mark_body_rects(sym, doc, unit_for)
    return sym


def _record_points(r) -> list:
    d = r.d
    t = r.type
    if t == "RECT":
        return [(d.get("dotX1", 0), d.get("dotY1", 0)), (d.get("dotX2", 0), d.get("dotY2", 0))]
    if t in ("CIRCLE",):
        c = (r.num("centerX"), r.num("centerY"))
        rad = abs(r.num("radius"))
        return [(c[0] - rad, c[1] - rad), (c[0] + rad, c[1] + rad)]
    if t == "ELLIPSE":
        c = (r.num("centerX"), r.num("centerY"))
        rx, ry = abs(r.num("radiusX")), abs(r.num("radiusY"))
        return [(c[0] - rx, c[1] - ry), (c[0] + rx, c[1] + ry)]
    if t in ("POLY", "BEZIER"):
        return [tuple(p) for p in (d.get("points") or [])]
    if t == "ARC":
        return [(r.num("startX"), r.num("startY")), (r.num("referenceX"), r.num("referenceY")),
                (r.num("endX"), r.num("endY"))]
    if t == "LINE":
        return [(r.num("startX"), r.num("startY")), (r.num("endX"), r.num("endY"))]
    return []


def _graphic(r):
    t = r.type
    d = r.d
    fill = "outline" if _has_fill(r) else "none"
    if t == "RECT":
        a = kp(d.get("dotX1") or 0, d.get("dotY1") or 0)
        b = kp(d.get("dotX2") or 0, d.get("dotY2") or 0)
        if a == b:
            return None
        n = [Sym("rectangle"), [Sym("start"), *a], [Sym("end"), *b], _stroke(0.254), _fill(fill)]
        rad = abs(float(d.get("radiusX") or 0))
        if rad > 0:
            n.insert(3, [Sym("radius"), r4(rad * U)])
        return n
    if t == "CIRCLE":
        c = kp(r.num("centerX"), r.num("centerY"))
        return [Sym("circle"), [Sym("center"), *c], [Sym("radius"), r4(abs(r.num("radius")) * U)], _stroke(), _fill(fill)]
    if t == "ELLIPSE":
        rx, ry = abs(r.num("radiusX")), abs(r.num("radiusY"))
        c = (r.num("centerX"), r.num("centerY"))
        if abs(rx - ry) < 1e-6:
            if rx <= 2 and not _has_fill(r) and r.d.get("fillColor") is None:
                fill = "outline"  # small dots (pin 1 markers) are drawn filled in EasyEDA
            return [Sym("circle"), [Sym("center"), *kp(*c)], [Sym("radius"), r4(rx * U)], _stroke(), _fill(fill)]
        pts = []
        for k in range(37):
            a = math.radians(k * 10)
            pts.append(kp(c[0] + rx * math.cos(a), c[1] + ry * math.sin(a)))
        return [Sym("polyline"), [Sym("pts")] + [[Sym("xy"), *p] for p in pts], _stroke(), _fill(fill)]
    if t == "ARC":
        s = kp(r.num("startX"), r.num("startY"))
        m = kp(r.num("referenceX"), r.num("referenceY"))
        e = kp(r.num("endX"), r.num("endY"))
        if s == e or s == m or m == e:
            return None
        return [Sym("arc"), [Sym("start"), *s], [Sym("mid"), *m], [Sym("end"), *e], _stroke(), _fill("none")]
    if t == "POLY":
        pts = [kp(*p) for p in (d.get("points") or [])]
        if d.get("closed") in (True, 1) and pts and pts[0] != pts[-1]:
            pts.append(pts[0])
        if len(pts) < 2:
            return None
        closed = len(pts) > 2 and pts[0] == pts[-1]
        return [Sym("polyline"), [Sym("pts")] + [[Sym("xy"), *p] for p in pts], _stroke(),
                _fill(fill if closed else "none")]
    if t == "LINE":
        a, b = kp(r.num("startX"), r.num("startY")), kp(r.num("endX"), r.num("endY"))
        if a == b:
            return None
        return [Sym("polyline"), [Sym("pts"), [Sym("xy"), *a], [Sym("xy"), *b]], _stroke(), _fill("none")]
    if t == "BEZIER":
        pts = [kp(*p) for p in (d.get("points") or [])]
        if len(pts) < 4:
            return None
        return [Sym("bezier"), [Sym("pts")] + [[Sym("xy"), *p] for p in pts[:4]], _stroke(), _fill("none")]
    if t == "TEXT":
        txt = r.str("value")
        if not txt:
            return None
        size = text_size(r.num("fontSize", 7) or 7)
        return [Sym("text"), txt, [Sym("at"), *kp(r.num("x"), r.num("y")), r4(r.num("rotation") * 10) / 10],
                effects(size, justify=align_justify(r))]
    return None


def _mark_body_rects(sym: LibSymbol, doc: Doc, unit_for) -> None:
    """Fill rectangles that form the component body (pins end on their edges), like KiCad libs do."""
    for u, items in sym.graphics.items():
        for g in items:
            if g[0] != "rectangle":
                continue
            (x0, y0), (x1, y1) = g[1][1:], g[2][1:]
            xa, xb, ya, yb = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
            hits = 0
            for p in sym.pins_of(u):
                ex, ey = body_end(p)
                ex, ey = ex * U, ey * U
                on_v = (abs(ex - xa) < 0.01 or abs(ex - xb) < 0.01) and ya - 0.01 <= ey <= yb + 0.01
                on_h = (abs(ey - ya) < 0.01 or abs(ey - yb) < 0.01) and xa - 0.01 <= ex <= xb + 0.01
                hits += on_v or on_h
            if hits >= 2 or (hits >= 1 and len(sym.pins_of(u)) <= 2):
                g[-1] = _fill("background")


def body_end(p: SymPin):
    a = math.radians(p.rot)
    return (p.x + p.length * math.cos(a), p.y + p.length * math.sin(a))


def text_size(fs: float) -> float:
    """EasyEDA font size -> KiCad text size on a 0.127 mm step."""
    mmv = fs * U * 0.75
    v = max(1.0, round(mmv / 0.127) * 0.127)
    if abs(v - 1.27) < 0.2:
        v = 1.27
    return r4(v)


def align_justify(r) -> list | None:
    """EasyEDA text alignment -> KiCad justify keywords."""
    h = r.d.get("hAlign")
    v = r.d.get("vAlign")
    al = r.d.get("align")
    hj = vj = None
    if isinstance(al, str) and "_" in al:
        ha, va = al.upper().split("_", 1)
        hj = {"LEFT": "left", "RIGHT": "right"}.get(ha)
        vj = {"TOP": "top", "BOTTOM": "bottom"}.get(va)
    else:
        if h is not None:
            hj = {0: "left", 2: "right"}.get(int(h))
        if v is not None:
            vj = {0: "top", 2: "bottom"}.get(int(v))
    out = [x for x in (hj, vj) if x]
    return out or None


def lib_symbol_sexp(sym: LibSymbol, lib_name: str | None = None, value: str | None = None,
                    footprint: str = "", datasheet: str = "", description: str = "",
                    extra: dict | None = None) -> list:
    """Serialise as a symbol definition (used both in .kicad_sym and in lib_symbols)."""
    full = f"{lib_name}:{sym.name}" if lib_name else sym.name
    n = [Sym("symbol"), full]
    names_vis = any(p.name_visible for p in sym.pins)
    nums_vis = any(p.number_visible for p in sym.pins)
    if not nums_vis:
        n.append([Sym("pin_numbers"), [Sym("hide"), YES]])
    pn = [Sym("pin_names"), [Sym("offset"), 0.254]]
    if not names_vis:
        pn.append([Sym("hide"), YES])
    n.append(pn)
    n += [[Sym("exclude_from_sim"), NO], [Sym("in_bom"), YES], [Sym("on_board"), YES], [Sym("in_pos_files"), YES],
          [Sym("duplicate_pin_numbers_are_jumpers"), NO]]
    bb = sym.bbox.get(1, (-5, -5, 5, 5))
    top_y = bb[3] * U + 1.27
    bot_y = bb[1] * U - 1.27
    n.append(prop("Reference", sym.ref_prefix, (0, r4(top_y))))
    n.append(prop("Value", value or sym.name, (0, r4(bot_y))))
    n.append(prop("Footprint", footprint, hide=True))
    n.append(prop("Datasheet", datasheet, hide=True))
    n.append(prop("Description", description, hide=True))
    for k, v in (extra or {}).items():
        n.append(prop(k, v, hide=True))
    base = sym.name
    for u in range(1, max(1, len(sym.units)) + 1):
        body = [Sym("symbol"), f"{base}_{u}_1"]
        if len(sym.units) > 1 and sym.unit_names and sym.unit_names[u - 1] and not str(sym.unit_names[u - 1]).isdigit():
            uname = str(sym.unit_names[u - 1])
            if not re.match(r".*\.\d+$", uname):
                body.append([Sym("unit_name"), uname])
        body += sym.graphics.get(u, [])
        for p in sym.pins_of(u):
            body.append(pin_sexp(p))
        n.append(body)
    if len(sym.units) > 1:
        pass
    n.append([Sym("embedded_fonts"), NO])
    return n


def pin_sexp(p: SymPin) -> list:
    n = [Sym("pin"), Sym(p.etype), Sym(p.shape), [Sym("at"), *kp(p.x, p.y), int(round(p.rot)) % 360],
         [Sym("length"), r4(p.length * U)]]
    if p.hidden:
        n.append([Sym("hide"), YES])
    n.append([Sym("name"), p.name or "", effects()])
    n.append([Sym("number"), p.number or "", effects()])
    return n


# ---------------------------------------------------------------------------
# Power symbols
# ---------------------------------------------------------------------------

_stock_cache: dict[str, dict] = {}


def stock_symbols(lib: str) -> dict:
    """Symbols of an installed KiCad library (e.g. "power", "Device") by name; {} if KiCad's libraries aren't found."""
    if lib not in _stock_cache:
        syms = {}  # filled before publishing: the LCSC provider reads libraries from several threads
        paths = [os.path.join(d, f"{lib}.kicad_sym") for d in STOCK_DIRS if d]
        paths.append(os.path.join(os.path.dirname(__file__), "templates", f"{lib}.kicad_sym"))
        path = next((x for x in paths if os.path.exists(x)), None)
        if path:
            for x in loads(open(path, encoding="utf-8").read()):
                if isinstance(x, list) and x and x[0] == "symbol":
                    syms[x[1]] = x
        _stock_cache[lib] = syms
    return _stock_cache[lib]


def stock_power_symbols() -> dict:
    return stock_symbols("power")


def power_body_dir(sym_node: list) -> float:
    """Direction (deg, Y-up) from the pin towards the graphic body of a KiCad power symbol."""
    pts = []

    def walk(n):
        if isinstance(n, list) and n:
            if n[0] == "xy":
                pts.append((n[1], n[2]))
            elif n[0] in ("start", "end", "center", "mid"):
                pts.append((n[1], n[2]))
            for c in n[1:]:
                walk(c)

    for sub in sym_node:
        if isinstance(sub, list) and sub and sub[0] == "symbol":
            for g in sub[2:]:
                if isinstance(g, list) and g and g[0] != "pin":
                    walk(g)
    if not pts:
        return 270.0
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    if abs(cx) > abs(cy):
        return 0.0 if cx > 0 else 180.0
    return 90.0 if cy > 0 else 270.0


def make_power_symbol(net: str, ground_like: bool) -> tuple[str, list]:
    """Return (lib_id, symbol definition) for a power port named `net`."""
    stock = stock_power_symbols()
    if net in stock:
        node = _rename_symbol(stock[net], f"power:{net}")
        return f"power:{net}", node
    base = "GND" if ground_like else "VCC"
    if base in stock:
        node = _rename_symbol(stock[base], net, value=net)
    else:
        node = _fallback_power(net, ground_like)
    return net, node


def _rename_symbol(node: list, new_name: str, value: str | None = None) -> list:
    import copy

    n = copy.deepcopy(node)
    old = n[1]
    n[1] = new_name
    short = new_name.split(":")[-1]
    for sub in n:
        if isinstance(sub, list) and sub and sub[0] == "symbol" and isinstance(sub[1], str):
            sub[1] = sub[1].replace(old, short, 1)
        if value is not None and isinstance(sub, list) and sub and sub[0] == "property":
            if sub[1] == "Value":
                sub[2] = value
            elif sub[1] == "Description":
                sub[2] = f'Power symbol creates a global label with name "{value}"'
    return n


def _fallback_power(net: str, ground: bool) -> list:
    if ground:
        g = [Sym("polyline"), [Sym("pts"), [Sym("xy"), 0, 0], [Sym("xy"), 0, -1.27], [Sym("xy"), 1.27, -1.27],
                               [Sym("xy"), 0, -2.54], [Sym("xy"), -1.27, -1.27], [Sym("xy"), 0, -1.27]],
             _stroke(), _fill("none")]
        pin_dir, val_y = 270, -3.81
    else:
        g = [Sym("polyline"), [Sym("pts"), [Sym("xy"), -0.762, 1.27], [Sym("xy"), 0, 2.54], [Sym("xy"), 0.762, 1.27]],
             _stroke(), _fill("none")]
        pin_dir, val_y = 90, 3.556
    return [Sym("symbol"), net, [Sym("power"), Sym("global")], [Sym("pin_numbers"), [Sym("hide"), YES]],
            [Sym("pin_names"), [Sym("offset"), 0], [Sym("hide"), YES]],
            [Sym("exclude_from_sim"), NO], [Sym("in_bom"), YES], [Sym("on_board"), YES], [Sym("in_pos_files"), YES],
            [Sym("duplicate_pin_numbers_are_jumpers"), NO],
            prop("Reference", "#PWR", (0, -6.35 if ground else 6.35), hide=True), prop("Value", net, (0, val_y)),
            prop("Footprint", "", hide=True), prop("Datasheet", "", hide=True),
            prop("Description", f'Power symbol creates a global label with name "{net}"', hide=True),
            [Sym("symbol"), f"{net}_0_1", g],
            [Sym("symbol"), f"{net}_1_1", [Sym("pin"), Sym("power_in"), Sym("line"), [Sym("at"), 0, 0, pin_dir],
                                           [Sym("length"), 0], [Sym("name"), "", effects()],
                                           [Sym("number"), "1", effects()]]],
            [Sym("embedded_fonts"), NO]]
