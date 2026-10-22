"""KiCad's own symbols for LCSC parts that have a standard equivalent.

A placed LCSC part normally gets the symbol converted from EasyEDA.  When the part is something KiCad draws with a
standard symbol (resistor, capacitor, diode, transistor, crystal, switch, connector, op-amp, ... or an IC that is in
KiCad's libraries under its part number) we use KiCad's drawing instead and renumber its pins to the footprint pads.

The pin mapping is by function (gate/drain/source, anode/cathode, +/-, ...), taken from the EasyEDA pin names; the
LCSC category decides what the part is.  Whenever a pin can't be identified with certainty the part keeps its EasyEDA
symbol: a wrong pin mapping is far worse than a non-standard drawing.
"""

from __future__ import annotations

import functools
import os
import re

from .kicad.symbol import STOCK_DIRS, stock_symbols
from .sexp import Sym

# ----------------------------------------------------------------------------------------------- helpers


def _norm(s: str) -> str:
    return re.sub(r"[\s_]", "", str(s or "").upper()).replace("–", "-").replace("−", "-").replace("＋", "+")


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _natural(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(s))]


def stock_pins(node) -> list:
    """(unit, electrical type, name, number) of a stock symbol's pins; unit 0 (common) counts as 1."""
    out = []
    for sub in node:
        if isinstance(sub, list) and sub and sub[0] == "symbol":
            unit = max(1, int(sub[1].rsplit("_", 2)[-2]))
            for c in sub:
                if isinstance(c, list) and c and c[0] == "pin":
                    nm = next(x[1] for x in c if isinstance(x, list) and x and x[0] == "name")
                    nr = next(x[1] for x in c if isinstance(x, list) and x and x[0] == "number")
                    out.append((unit, str(c[1]), str(nm), str(nr)))
    return out


def _resolved(lib: str, name: str):
    """Stock symbol with `extends` flattened: the parent's drawing and pins, the child's own fields."""
    syms = stock_symbols(lib)
    node = syms.get(name)
    if node is None:
        return None
    ext = next((c for c in node if isinstance(c, list) and c and c[0] == "extends"), None)
    if ext is None:
        return node
    base = _resolved(lib, ext[1])
    if base is None:
        return None
    own = {c[1]: c for c in node if isinstance(c, list) and c and c[0] == "property"}
    out = [Sym("symbol"), name]
    for c in base[2:]:
        if isinstance(c, list) and c and c[0] == "property" and c[1] in own:
            out.append(own.pop(c[1]))
        elif isinstance(c, list) and c and c[0] == "symbol":
            out.append([c[0], name + c[1][len(ext[1]):]] + c[2:])
        else:
            out.append(c)
    return out


def _assign(ours: list[tuple[str, str]], stock: list[tuple[str, str]]) -> dict | None:
    """Pin roles -> {stock pin number: our pad number(s)}.  Equal counts pair up in pad order; one stock pin for
    several pads of the same role becomes a stacked pin "[a,b]".  Any role without a partner -> None."""
    mine: dict[str, list[str]] = {}
    for num, role in ours:
        mine.setdefault(role, []).append(num)
    theirs: dict[str, list[str]] = {}
    for num, role in stock:
        theirs.setdefault(role, []).append(num)
    if set(mine) != set(theirs):
        return None
    remap = {}
    for role, s in theirs.items():
        o = sorted(mine[role], key=_natural)
        s = sorted(s, key=_natural)
        if len(o) == len(s):
            remap.update(zip(s, o))
        elif len(s) == 1 and all(re.fullmatch(r"[A-Za-z0-9.+]+", x) for x in o):
            remap[s[0]] = "[" + ",".join(o) + "]"
        else:
            return None
    return remap


class Part:
    """What we know about the LCSC part: attributes, category, EasyEDA pins (number, upper-cased name)."""

    def __init__(self, attrs: dict, ls, doc=None):
        self.attrs = attrs
        self.cat = _key(attrs.get("LCSC Category", ""))
        self.prefix = (ls.ref_prefix or "").upper()
        self.pins = [(p.number, _norm(p.name)) for p in ls.pins if p.number]
        self.doc = doc

    def a(self, *keys) -> str:
        return " ".join(str(self.attrs.get(k) or "") for k in keys).lower()

    def roles(self, fn) -> list | None:
        """[(pad, role)] with fn(name) -> role, "NC" (dropped) or None (unknown: give up)."""
        out = []
        for num, name in self.pins:
            r = fn(name)
            if r is None:
                return None
            if r != "NC":
                out.append((num, r))
        return out

    def ordered(self) -> list | None:
        """Pads in number order as roles "0", "1", ... (for parts whose pins are interchangeable or numbered as KiCad
        numbers them); NC pins dropped."""
        pads = [n for n, name in self.pins if name not in _NC]
        return [(n, str(i)) for i, n in enumerate(sorted(pads, key=_natural))]


_NC = {"NC", "N.C.", "N.C", "DNC", "NC.", "N/C"}


def _nc(fn):
    return lambda n: "NC" if n in _NC else fn(n)


# ----------------------------------------------------------------------------------------------- pin roles

def _role_ak(n):  # diode anode / cathode
    if n in ("A", "ANODE", "AN", "+", "P"):
        return "A"
    if n in ("K", "C", "CATHODE", "CAT", "CA", "-", "N"):
        return "K"
    return None


def _role_pm(n):  # polarity of two-terminal parts
    if n in ("+", "POS", "+VE", "P", "A", "ANODE", "VCC", "V+", "RED"):
        return "+"
    if n in ("-", "NEG", "-VE", "N", "K", "CATHODE", "GND", "V-", "BLACK"):
        return "-"
    return None


def _role_mos(n):
    return {"G": "G", "GATE": "G", "D": "D", "DRAIN": "D", "S": "S", "SOURCE": "S"}.get(n)


def _role_bjt(n):
    return {"B": "B", "BASE": "B", "C": "C", "COLLECTOR": "C", "COL": "C", "E": "E", "EMITTER": "E",
            "EM": "E"}.get(n)


def _role_igbt(n):
    return {"G": "G", "GATE": "G", "C": "C", "COLLECTOR": "C", "E": "E", "EMITTER": "E"}.get(n)


def _role_scr(n):
    return {"A": "A", "ANODE": "A", "K": "K", "CATHODE": "K", "G": "G", "GATE": "G"}.get(n)


def _role_triac(n):
    return {"T1": "A1", "MT1": "A1", "A1": "A1", "T2": "A2", "MT2": "A2", "A2": "A2", "G": "G", "GATE": "G"}.get(n)


def _role_bridge(n):
    if n in ("+", "DC+", "V+", "POS"):
        return "+"
    if n in ("-", "DC-", "V-", "NEG"):
        return "-"
    if re.fullmatch(r"(AC|~)\d?|C[12]|AC[12]|~[12]|AC~", n):
        return "~"
    return None


def _role_gnd(n):  # crystals / resonators: ground pins vs the two crystal ends
    return "G" if n in ("GND", "G", "CASE", "VSS", "GROUND") else "X"


def _role_ws(n):
    if n in ("VDD", "VCC", "V+", "5V", "VIN"):
        return "VDD"
    if n in ("GND", "VSS", "G"):
        return "VSS"
    if n in ("DI", "DIN", "SDI", "IN"):
        return "DIN"
    if n in ("DO", "DOUT", "DOU", "SDO", "OUT"):
        return "DOUT"
    return None


def _role_sw3(n):  # limit / micro switches: COM, NO, NC contacts -> KiCad SW_SPDT A/B/C (B is the common)
    return {"COM": "B", "C": "B", "COMMON": "B", "NO": "A", "ON": "A", "NC": "C", "OFF": "C"}.get(n)


# ----------------------------------------------------------------------------------------------- polarity

def _plus_pin(doc) -> str | None:
    """Pad number of the pin nearest the "+" drawn in the EasyEDA symbol (two short crossing strokes)."""
    if doc is None:
        return None
    segs, pins, numbers = [], {}, {}
    for r in doc.recs:
        raw = r.d.get("_raw") or []
        if not raw:
            continue
        if raw[0] == "POLY" and isinstance(raw[2], list) and len(raw[2]) == 4:
            segs.append(tuple(float(v) for v in raw[2]))
        elif raw[0] == "RECT" and len(raw) > 5:
            x1, y1, x2, y2 = (float(v) for v in raw[2:6])
            if abs(x2 - x1) < 0.5:
                segs.append(((x1 + x2) / 2, y1, (x1 + x2) / 2, y2))
            elif abs(y2 - y1) < 0.5:
                segs.append((x1, (y1 + y2) / 2, x2, (y1 + y2) / 2))
        elif raw[0] == "PIN":
            pins[raw[1]] = (float(raw[4]), float(raw[5]))
        elif raw[0] == "ATTR" and len(raw) > 4 and raw[3] == "NUMBER":
            numbers[raw[2]] = str(raw[4])
    hs = [s for s in segs if abs(s[1] - s[3]) < 1e-6 and 1.5 <= abs(s[2] - s[0]) <= 8]
    vs = [s for s in segs if abs(s[0] - s[2]) < 1e-6 and 1.5 <= abs(s[3] - s[1]) <= 8]
    plus = None
    for h in hs:
        for v in vs:
            cx, cy = (h[0] + h[2]) / 2, (v[1] + v[3]) / 2
            if abs(cx - v[0]) < 0.6 and abs(cy - h[1]) < 0.6 and abs(abs(h[2] - h[0]) - abs(v[3] - v[1])) < 0.6:
                if plus is not None:
                    return None  # two "+" signs: don't guess
                plus = (cx, cy)
    if plus is None or len(pins) != 2:
        return None
    d = sorted((((x - plus[0]) ** 2 + (y - plus[1]) ** 2), pid) for pid, (x, y) in pins.items())
    if d[1][0] < d[0][0] * 1.5:  # not clearly nearer to one pin
        return None
    return numbers.get(d[0][1])


def _polar(part: Part) -> list | None:
    """[(pad, "+"), (pad, "-")] for a two-terminal polarised part, from pin names or the drawn "+"."""
    pads = part.ordered()
    if pads is None or len(pads) != 2:
        return None
    named = part.roles(_nc(_role_pm))
    if named and sorted(r for _, r in named) == ["+", "-"]:
        return named
    names = dict(part.pins)
    one = [n for n, _ in pads if _role_pm(names[n]) == "+"]
    if len(one) == 1:  # "+" and a numbered pin
        return [(n, "+" if n == one[0] else "-") for n, _ in pads]
    p = _plus_pin(part.doc)
    if p in [n for n, _ in pads]:
        return [(n, "+" if n == p else "-") for n, _ in pads]
    return None


# ----------------------------------------------------------------------------------------------- op-amps

_VPOS = {"V+", "VCC", "VDD", "+VS", "VS+", "+V", "VCC/VDD", "VCC+"}
_VNEG = {"V-", "VEE", "VSS", "GND", "-VS", "VS-", "-V", "VEE/GND", "VSS/GND", "GND/VEE", "GND/VSS", "VCC-"}
_AMPS = {(False, 1): ("Amplifier_Operational", "LM321"), (False, 2): ("Amplifier_Operational", "Opamp_Dual"),
         (False, 4): ("Amplifier_Operational", "Opamp_Quad"), (True, 1): ("Comparator", "LMV331"),
         (True, 2): ("Comparator", "LM2903"), (True, 4): ("Comparator", "LM339")}


def _amp_role(n: str):
    """Op-amp / comparator pin -> "<channel>:<role>"; role OUT / IN+ / IN- / V+ / V-."""
    if n in _VPOS:
        return "P:V+"
    if n in _VNEG:
        return "P:V-"
    n = n.replace("/", "")
    n = re.sub(r"^(NEG|INVERTING)", "IN-", n)
    n = re.sub(r"^(POS|NONINVERTING)", "IN+", n)
    if "OUT" in n:
        role, rest = "OUT", n.replace("OUT", "", 1)
    elif "IN" in n:
        rest = n.replace("IN", "", 1)
        sign = [c for c in rest if c in "+-"]
        if len(sign) != 1:
            return None
        role, rest = "IN" + sign[0], rest.replace(sign[0], "")
    else:
        return None
    return f"{rest}:{role}" if re.fullmatch(r"[1-4A-D]?", rest) else None


def _amp(part: Part):
    cat = part.cat
    if part.attrs.get("Gain Bandwidth Product") or "operationalamplifier" in cat or "precisionopamp" in cat \
            or "fetinputamplifier" in cat:
        comp = False
    elif part.attrs.get("Propagation Delay Time") or part.attrs.get("Response Time (tr)") or "comparator" in cat:
        comp = True
    else:
        return None
    ours = part.roles(_nc(_amp_role))
    if not ours:
        return None
    chans = sorted({r.split(":")[0] for _, r in ours if not r.startswith("P:")})
    if (comp, len(chans)) not in _AMPS:
        return None
    alias = {c: str(i + 1) for i, c in enumerate(chans)}
    ours = [(n, r if r.startswith("P:") else alias[r.split(":")[0]] + ":" + r.split(":")[1]) for n, r in ours]
    lib, name = _AMPS[(comp, len(chans))]
    node = _resolved(lib, name)
    if node is None:
        return None
    stock = []
    for unit, _, pname, num in stock_pins(node):
        role = {"": "OUT", "+": "IN+", "-": "IN-"}.get(pname, pname)
        stock.append((num, ("P:" if role in ("V+", "V-") else f"{unit}:") + role))
    return lib, name, stock, ours


# ----------------------------------------------------------------------------------------------- classification

def _stock_roles(lib, name, by="name", override=None):
    node = _resolved(lib, name)
    if node is None:
        return None
    pins = stock_pins(node)
    if by == "order":
        nums = sorted((p[3] for p in pins), key=_natural)
        return [(n, (override or {}).get(n, str(i))) for i, n in enumerate(nums)]
    return [(p[3], (override or {}).get(p[3], p[2])) for p in pins]


def _two(part: Part, lib: str, name: str):
    ours = part.ordered()
    if ours is None or len(ours) != 2:
        return None
    return lib, name, _stock_roles(lib, name, "order"), ours


def _polar2(part: Part, lib: str, name: str, plus: str = "1"):
    ours = _polar(part)
    if ours is None:
        return None
    stock = _stock_roles(lib, name, "order")
    if stock is None:
        return None
    return lib, name, [(n, "+" if n == plus else "-") for n, _ in stock], ours


def _named(part: Part, lib: str, name: str, fn, override=None):
    ours = part.roles(_nc(fn))
    if not ours:
        return None
    return lib, name, _stock_roles(lib, name, "name", override), ours


def _diode(part: Part, name: str):
    return _named(part, "Device", name, _role_ak)


def _numbered(part: Part, lib: str, name: str):
    """Parts whose pads are numbered exactly as KiCad numbers the stock symbol (1..N)."""
    ours = part.ordered()
    stock = _stock_roles(lib, name, "order")
    if not ours or stock is None or len(ours) != len(stock):
        return None
    if [n for n, _ in ours] != [n for n, _ in stock]:
        return None
    return lib, name, stock, ours


def _count(text: str) -> int:
    m = re.search(r"(\d+)", text or "")
    return int(m.group(1)) if m else 0


def _connector(part: Part):
    """Headers, terminal blocks, wire-to-board, FPC ...: Connector_Generic, plus mounting pads if any."""
    names = dict(part.pins)
    nums = [n for n, _ in part.pins]
    signal = [n for n in nums if n.isdigit() and names[n] in (n, "")]
    rows = _count(part.attrs.get("Number of Rows", "")) or (2 if re.search(r"2\s*[x*]\s*\d", part.a(
        "Pin Structure", "Number of Positions or Pins", "Number of Pins")) else 1)
    positions = _count(part.attrs.get("Number of Contacts", "") or part.attrs.get("Number of Pins", "")
                       or part.attrs.get("Number of Positions", ""))
    if "2x" in part.a("Number of Positions or Pins").replace(" ", "") and part.attrs.get("Number of PINs Per Row"):
        positions = 2 * _count(part.attrs["Number of PINs Per Row"])
    signal = sorted(signal, key=int)
    if positions and len(signal) > positions:
        signal = [n for n in signal if int(n) <= positions]
    n = len(signal)
    if n == 0 or [int(x) for x in signal] != list(range(1, n + 1)) or (positions and positions != n):
        return None
    mount = [x for x in nums if x not in signal and names[x] not in _NC]
    if rows == 2:
        if n % 2 or n // 2 > 40:
            return None
        name = f"Conn_02x{n // 2:02d}_Odd_Even"
    elif n == 1 and not mount:
        return "Connector", "TestPoint", [("1", "0")], [(signal[0], "0")]
    else:
        if n > 60:
            return None
        name = f"Conn_01x{n:02d}"
    lib = "Connector_Generic"
    ours = [(x, x) for x in signal]
    stock = [(str(i), str(i)) for i in range(1, n + 1)]
    if mount:
        lib, name = "Connector_Generic_MountingPin", name + "_MountingPin"
        ours += [(x, "MP") for x in mount]
        stock.append(("MP", "MP"))
    return lib, name, stock, ours


def _rgb(part: Part):
    common, colours = None, {}
    for num, n in part.pins:
        if n in _NC:
            continue
        m = re.fullmatch(r"(R|G|B|RED|GREEN|BLUE)[-_]?(K|A|\+|-)?", n)
        if m:
            colours[m.group(1)[0]] = num
            continue
        if n in ("+", "COM+", "A", "COMA", "VCC", "COMMONANODE", "ANODE"):
            common = ("A", num)
        elif n in ("-", "COM-", "K", "COMK", "GND", "COMMONCATHODE", "CATHODE"):
            common = ("K", num)
        else:
            return None
    if common is None or sorted(colours) != ["B", "G", "R"]:
        return None
    if common[0] == "A":
        name, sfx = "LED_RGBA", "K"
    else:
        name, sfx = "LED_RGBK", "A"
    ours = [(num, c + sfx) for c, num in colours.items()] + [(common[1], common[0])]
    return "Device", name, _stock_roles("Device", name), ours


def _transistor(part: Part):
    t = part.a("Type", "type", "Number", "FET Type", "Transistor Type")
    cat = part.cat
    n = "N" if re.search(r"\bnpn\b|n-?channel", t) else "P" if re.search(r"\bpnp\b|p-?channel", t) else None
    bjt = {"N": "NPN", "P": "PNP"}.get(n)
    if "mosfet" in cat or "sicmosfet" in cat or "siliconcarbidefieldeffect" in cat or "gantransistor" in cat:
        if re.search(r"\b2\b|dual", part.a("Number")) or n is None and "gan" not in cat and "silicon" not in cat:
            return None
        return _named(part, "Device", "Q_PMOS" if n == "P" else "Q_NMOS", _role_mos)
    if "jfet" in cat:
        return _named(part, "Device", f"Q_{n or 'N'}JFET_DGS", _role_mos) if n else None
    if "igbt" in cat:
        return _named(part, "Device", "Q_NIGBT_GCE", _role_igbt)
    if "darlingtontransistorarray" in cat:
        return None
    if "darlington" in cat and n:
        return _named(part, "Device", f"Q_{bjt}_Darlington", _role_bjt)
    if "digitaltransistor" in cat and n and not re.search(r"\b2\b|dual", part.a("Number")):
        return _named(part, "Device", f"Q_{bjt}_BRT", _role_bjt)
    if "bipolar" in cat and n and not re.search(r"\b2\b|dual", part.a("Number")):
        return _named(part, "Device", f"Q_{bjt}", _role_bjt)
    if "thyristor" in cat:
        if part.roles(_nc(_role_triac)) and any(r in ("A1", "A2") for _, r in part.roles(_nc(_role_triac))):
            return _named(part, "Device", "Q_Triac", _role_triac)
        return _named(part, "Device", "Q_SCR_AGK", _role_scr)
    return None


def _tvs(part: Part):
    if "bidirectional" in part.a("Polarity"):
        return _two(part, "Device", "D_TVS")
    if not part.roles(_nc(_role_ak)):
        return None if "unidirectional" in part.a("Polarity") else _two(part, "Device", "D_TVS")
    return _named(part, "Diode", "SM6T6V8A", _role_ak)


def _crystal(part: Part):
    ours = part.roles(_nc(_role_gnd))
    if not ours:
        return None
    g = sum(1 for _, r in ours if r == "G")
    x = [n for n, r in ours if r == "X"]
    if len(x) != 2:
        return None
    name = {0: "Crystal", 1: "Crystal_GND2", 2: "Crystal_GND24"}.get(g)
    if name is None:
        return None
    ours = [(n, ("X" + str(sorted(x, key=_natural).index(n))) if r == "X" else "G") for n, r in ours]
    stock = _stock_roles("Device", name)
    if name == "Crystal_GND2":
        stock = [(n, "G" if n == "2" else n) for n, _ in stock]
    xs = sorted((n for n, r in stock if r != "G"), key=_natural)
    stock = [(n, "G" if r == "G" else "X" + str(xs.index(n))) for n, r in stock]
    return "Device", name, stock, ours


def _resonator(part: Part):
    ours = part.roles(_nc(_role_gnd))
    if not ours or sum(1 for _, r in ours if r == "G") != 1 or len(ours) != 3:
        return None
    x = sorted((n for n, r in ours if r == "X"), key=_natural)
    ours = [(n, "G" if r == "G" else "X" + str(x.index(n))) for n, r in ours]
    return "Device", "Resonator", [("1", "X0"), ("2", "G"), ("3", "X1")], ours


def _limit(part: Part):
    ours = part.roles(_role_sw3)  # here "NC" is the normally-closed contact, not "no connect"
    if not ours or sorted(r for _, r in ours) != ["A", "B", "C"]:
        return None
    return "Switch", "SW_SPDT", [("1", "A"), ("2", "B"), ("3", "C")], ours


def _dip(part: Part):
    ours = part.ordered()
    if not ours or len(ours) % 2 or not 2 <= len(ours) <= 24:
        return None
    return _numbered(part, "Switch", f"SW_DIP_x{len(ours) // 2:02d}")


def _rpack(part: Part):
    ours = part.ordered()
    if not ours or len(ours) % 2 or not 4 <= len(ours) <= 22 or "bussed" in part.a("Circuit", "Circuit Type"):
        return None
    return _numbered(part, "Device", f"R_Pack{len(ours) // 2:02d}")


# (category key fragments, handler).  First match wins; category keys are lower-case alphanumerics.
_RULES = [
    (("chipresistor", "throughholeresistor", "aluminumcaseporcelain"), lambda p: _two(p, "Device", "R")),
    (("currentsenseresistor",), lambda p: _two(p, "Device", "R")),
    (("potentiometer",), lambda p: _numbered(p, "Device", "R_Potentiometer")),
    (("resistornetwork",), _rpack),
    (("varistor",), lambda p: _two(p, "Device", "Varistor")),
    (("ntcthermistor",), lambda p: _two(p, "Device", "Thermistor_NTC")),
    (("ptcthermistor",), lambda p: _two(p, "Device", "Thermistor_PTC")),
    (("photoresistor",), lambda p: _two(p, "Device", "R_Photo")),
    (("resettablefuse",), lambda p: _two(p, "Device", "Polyfuse")),
    (("disposablefuse", "automotivefuse", "thermalfuse"), lambda p: _two(p, "Device", "Fuse")),
    (("multilayerceramiccapacitor", "throughholeceramiccapacitor", "filmcapacitor", "polypropylenefilm",
      "safetycapacitor", "micaandptfe", "paperdielectric", "siliconcapacitor", "disccapacitor"),
     lambda p: _two(p, "Device", "C")),
    (("trimmersvariablecapacitor",), lambda p: _two(p, "Device", "C_Trim")),
    (("aluminumelectrolytic", "tantalumcapacitor", "polymeraluminum", "hybridaluminum", "niobiumoxide",
      "horntypeelectrolytic", "lithiumioncapacitor", "supercapacitor", "electricdoublelayer"),
     lambda p: _polar2(p, "Device", "C_Polarized")),
    (("ferritebead",), lambda p: _two(p, "Device", "FerriteBead")),
    (("adjustableinductor",), lambda p: _two(p, "Device", "L_Trim")),
    (("inductorssmd", "moldingpowerinductor", "powerinductor", "colorringinductor", "throughholeinductor"),
     lambda p: _two(p, "Device", "L")),
    (("schottky", "sicdiode"), lambda p: _diode(p, "D_Schottky")),
    (("zenerdiode",), lambda p: _diode(p, "D_Zener")),
    (("variablecapacitance",), lambda p: _diode(p, "D_Capacitance")),
    (("bridgerectifier",), lambda p: _named(p, "Device", "D_Bridge_+-AA", _role_bridge, {"3": "~", "4": "~"})),
    (("tvsesd", "esdandsurge"), _tvs),
    (("diodesgeneralpurpose", "fastrecovery", "switchingdiode", "avalanchediode", "superbarrierrectifier",
      "pindiode"), lambda p: _diode(p, "D")),
    (("photodiode",), lambda p: _diode(p, "D_Photo")),
    (("phototransistor",), lambda p: _named(p, "Device", "Q_Photo_NPN", _role_bjt)),
    (("rgbledsbuiltinic",), lambda p: _named(p, "LED", "WS2812B", _role_ws)),
    (("rgbled",), _rgb),
    (("ledindication", "infraredledemitter", "ultravioletled", "chiponboard"), lambda p: _diode(p, "LED")),
    (("mosfet", "jfet", "bipolarbjt", "digitaltransistor", "darlington", "igbt", "thyristor", "gantransistor",
      "siliconcarbidefieldeffect"), _transistor),
    (("ceramicresonator",), _resonator),
    (("crystals",), _crystal),
    (("comparator", "operationalamplifier", "precisionopamp", "fetinputamplifier"), _amp),
    (("tactileswitch", "pushbuttonswitch", "mechanicalkeyboard"), lambda p: _two(p, "Switch", "SW_Push")),
    (("rockerswitch",), lambda p: _two(p, "Switch", "SW_SPST")),
    (("reedswitch",), lambda p: _two(p, "Switch", "SW_Reed")),
    (("dipswitch",), _dip),
    (("slideswitch", "toggleswitch"), lambda p: _numbered(p, "Switch", "SW_SPDT")),
    (("limitswitch",), _limit),
    (("buzzer",), lambda p: _polar2(p, "Device", "Buzzer")),
    (("speaker",), lambda p: _polar2(p, "Device", "Speaker")),
    (("microphones",), lambda p: _polar2(p, "Device", "Microphone", plus="2")),
    (("vibrationmotor",), lambda p: _polar2(p, "Motor", "Motor_DC")),
    (("buttonandstripbattery", "bladeshrapnelcontactbattery"), lambda p: _polar2(p, "Device", "Battery_Cell")),
    (("pinheader", "femaleheader", "wiretoboard", "screwterminal", "barrierterminal", "pluggablesystemterminal",
      "springclampsystemterminal", "ffcfpc", "idcconnector", "micromatch", "testpoint", "pinjack"), _connector),
]


def _by_designator(part: Part):
    """No category (part not from a search): only the unambiguous cases."""
    p = part.prefix
    if p == "R" and (part.attrs.get("Power(Watts)") or "resistor" in part.a("Type")):
        return _two(part, "Device", "R")
    if p == "C" and part.attrs.get("Temperature Coefficient"):  # MLCC
        return _two(part, "Device", "C")
    if p in ("L", "FB"):
        return _two(part, "Device", "FerriteBead" if part.attrs.get("Impedance @ Frequency") else "L")
    if p == "LED" or (p == "D" and part.attrs.get("Illumination Color")):
        return _diode(part, "LED")
    if p in ("U", "IC") and (part.attrs.get("Gain Bandwidth Product") or part.attrs.get("Propagation Delay Time")):
        return _amp(part)
    return None


# ----------------------------------------------------------------------------------------------- ICs by MPN

_GENERIC_LIBS = {"Device", "Connector_Generic", "Connector_Generic_MountingPin", "Connector_Generic_Shielded",
                 "power", "Switch", "Mechanical", "Graphic", "Simulation_SPICE", "pspice", "Jumper"}


@functools.cache
def _mpn_index() -> dict:
    """Upper-case stock symbol name -> [(library, name)] over all installed KiCad libraries (names only, fast)."""
    d = next((x for x in STOCK_DIRS if x and os.path.isdir(x)), None)
    idx: dict[str, list] = {}
    if not d:
        return idx
    for f in os.listdir(d):
        lib = f[:-len(".kicad_sym")]
        if not f.endswith(".kicad_sym") or lib in _GENERIC_LIBS:
            continue
        with open(os.path.join(d, f), encoding="utf-8") as fh:
            for m in re.finditer(r'^\t\(symbol "([^"]+)"', fh.read(), re.M):
                idx.setdefault(m.group(1).upper(), []).append((lib, m.group(1)))
    return idx


_PIN_ALIAS = [({"GND", "VSS", "AGND", "DGND", "PGND", "GROUND", "ADJ/GND", "GND/ADJ", "ADJ(GND)", "VEE"}, "GND"),
              ({"VIN", "IN", "INPUT", "VI"}, "VIN"), ({"VOUT", "OUT", "OUTPUT", "VO", "VOUT(TAB)"}, "VOUT"),
              ({"VCC", "VDD", "V+", "VS"}, "VCC")]


def _pin_key(n: str) -> str:
    n = _norm(n).replace("#", "").replace("~", "").replace("/", "").replace("{", "").replace("}", "")
    n = n.replace("OVERBAR", "")
    if n in _NC:
        return "NC"
    for group, k in _PIN_ALIAS:
        if n in {g.replace("/", "") for g in group}:
            return k
    return n


def _tokens(n: str) -> set:
    n = _norm(n).replace("~", "").replace("{", "").replace("}", "").replace("#", "").replace("′", "'")
    return {t for t in re.split(r"[/()\[\],|]+", n) if t} | {_pin_key(n)}


def _same_pin(a: str, b: str) -> bool:
    """Pin names that mean the same pin: equal after aliases, or sharing a function token (PD3 vs (INT1)PD3)."""
    return _pin_key(a) == _pin_key(b) or bool({_pin_key(t) for t in _tokens(a)} & {_pin_key(t) for t in _tokens(b)})


def _mpn_candidates(mpn: str) -> list:
    """[(stock lib, name, exact?)] whose name matches the part number ("x" in KiCad names is a wildcard).  Longer
    matches first; exact = the whole part number, or all but a short package / reel suffix."""
    mpn = re.sub(r"_C\d+$", "", (mpn or "").upper().strip())
    if len(mpn) < 3:
        return []
    idx = _mpn_index()
    out = []
    for m in [mpn] + ([mpn[2:]] if mpn.startswith("SN74") else []):  # TI's "SN" prefix: KiCad names 74LVC1G04
        for k in range(len(m), 2, -1):  # drop suffixes (package / reel codes) step by step
            whole = k >= len(m) - 4 or m[k] in "-/ (#,_."  # the rest is a package / reel / grade suffix
            out += [(lib, name, whole) for lib, name in idx.get(m[:k], [])]
    if not out:
        for name, entries in idx.items():
            if "X" in name and len(name) >= 4:
                pat = "".join("." if c == "X" else re.escape(c) for c in name)
                if re.match(pat + r"$|" + pat + r"[-/#]?[A-Z0-9]{0,6}$", mpn):
                    out += [(lib, n, True) for lib, n in entries]
    return out


def _by_mpn(part: Part):
    ours = {n: name for n, name in part.pins}
    for lib, name, exact in _mpn_candidates(part.attrs.get("Manufacturer Part", "")):
        node = _resolved(lib, name)
        if node is None:
            continue
        pins = stock_pins(node)
        stock = {}
        for _, _, pname, num in pins:
            stock.setdefault(num, pname)
        if len(pins) != len(stock):  # pins drawn several times (stacked power pins): leave those alone
            continue
        # extra pads of ours: a tab / exposed pad joins the pin marked "(TAB)", a repeated pin its namesake
        extra = {n: v for n, v in ours.items() if n not in stock}
        join = {}
        for n, v in extra.items():
            key = _pin_key(v)
            tab = [m for m, w in ours.items() if m in stock and "(TAB)" in _norm(w)]
            same = [m for m in stock if m in ours and _pin_key(ours[m]) == key]
            if key in ("TAB", "EP", "EPAD", "PAD") and len(tab) == 1:
                join[n] = tab[0]
            elif len(same) == 1:
                join[n] = same[0]
            else:
                break
        if len(join) != len(extra) or set(stock) != set(ours) - set(extra):
            continue
        # pins both sides name (KiCad leaves gate inputs/outputs unnamed; EasyEDA often just numbers them)
        named = [n for n in stock if not re.fullmatch(r"\d*", ours[n] or "") and stock[n]]
        agree = sum(1 for n in named if _same_pin(ours[n], stock[n]))
        if named and agree < (0.6 if exact else 0.8) * len(named) or not named and not exact:
            continue
        return lib, name, [(n, n) for n in stock], [(n, join.get(n, n)) for n in ours]
    return None


# ----------------------------------------------------------------------------------------------- entry point

def choose(attrs: dict, ls, doc=None):
    """(library, symbol, {stock pin number: our pad number(s)}) or None."""
    part = Part(attrs, ls, doc)
    if not part.pins:
        return None
    hit = None
    if part.cat:
        for keys, fn in _RULES:
            if any(k in part.cat for k in keys):
                hit = fn(part)
                break
    else:
        hit = _by_designator(part)
    if hit is None:
        hit = _by_mpn(part)
    if hit is None or hit[2] is None:
        return None
    lib, name, stock, ours = hit
    remap = _assign(ours, stock)
    if remap is None:
        return None
    covered = {x for v in remap.values() for x in v.strip("[]").split(",")}
    if covered != {n for n, _ in ours}:
        return None
    return lib, name, remap


def stock_symbol(attrs: dict, ls, node: list, doc=None, choice=None) -> list | None:
    """Our symbol node redrawn with KiCad's stock symbol (fields kept), or None to keep EasyEDA's drawing."""
    choice = choice or choose(attrs, ls, doc)
    if choice is None:
        return None
    lib, name, remap = choice
    stock = _resolved(lib, name)
    push_pull = "push" in str(attrs.get("Output Type", "")).lower()
    sym_name = node[1]
    ours = {c[1]: c for c in node if isinstance(c, list) and c and c[0] == "property"}
    out = [Sym("symbol"), sym_name]
    for c in stock[2:]:
        head = c[0] if isinstance(c, list) and c else None
        if head == "property":
            key = c[1]
            if key in ("Reference", "Value"):
                out.append([c[0], key, ours[key][2] if key == "Value" else c[2]] + c[3:])
            elif key == "Sim.Pins":
                out.append([c[0], key, re.sub(r"\b(\w+)=", lambda m: remap.get(m.group(1), m.group(1)) + "=", c[2])]
                           + c[3:])
            elif key in ours:
                out.append(ours.pop(key))
            elif isinstance(key, str) and (key.startswith("Sim.") or key == "ki_locked"):
                out.append(c)
            # the stock part's keywords / footprint filters / description don't describe this part
        elif head == "symbol":
            sub = _renumber([c[0], sym_name + c[1][len(name):]] + c[2:], remap)
            if push_pull:
                sub = [[x[0], Sym("output")] + x[2:] if isinstance(x, list) and x and x[0] == "pin"
                       and x[1] == "open_collector" else x for x in sub]
            out.append(sub)
        elif head not in ("extends",):
            out.append(c)
    extra = [v for k, v in ours.items() if k not in ("Reference", "Value")]
    i = max(i for i, x in enumerate(out) if isinstance(x, list) and x and x[0] == "property") + 1
    return out[:i] + extra + out[i:]


def _renumber(n, remap: dict):
    if isinstance(n, list):
        if n and n[0] == "number" and isinstance(n[1], str) and n[1] in remap:
            return [n[0], remap[n[1]]] + n[2:]
        return [_renumber(x, remap) for x in n]
    return n
