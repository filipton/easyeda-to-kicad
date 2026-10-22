"""Normalised EasyEDA Pro design rules (v2 numeric + v3 named formats).

All lengths are returned in mil.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# v2 numeric rule types -> v3 names
V2_TYPES = {"1": "SAFE", "2": "OTHER", "3": "TRACK", "4": "BLIND", "5": "RADIUS", "6": "PLANE", "7": "COPPER",
            "8": "PASTE", "9": "SOLDER", "10": "DIFFER_ENTAIL", "11": "NET_LENGTH", "12": "AUTO_ROUTER"}

# Row/column indices of the safe-spacing matrix
TRACK, SMD_PAD, TH_PAD, VIA_V2, ZONE_V2 = 0, 1, 2, 5, 7


def _mil(v, unit: str) -> float:
    # EasyEDA stores rule values in mil regardless of the displayed unit.
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class Thermal:
    connect: str = "thermal"  # thermal | direct | none
    spoke_width: float = 10.0
    spoke_gap: float = 10.0
    angle: float = 90.0


@dataclass
class CopperRule:
    smd: Thermal = field(default_factory=Thermal)
    th: Thermal = field(default_factory=Thermal)
    clearance: float | None = None  # v2 only (unsure); v3 uses a SAFE profile


@dataclass
class SafeRule:
    matrix: list  # lower-triangular rows

    def get(self, a: int, b: int) -> float | None:
        if a < b:
            a, b = b, a
        try:
            return float(self.matrix[a][b])
        except (IndexError, TypeError, ValueError):
            return None

    @property
    def size(self) -> int:
        return len(self.matrix)

    # column indices differ between 10-row (v2) and 13-row (v3) tables
    def idx(self, what: str) -> int | None:
        n = self.size
        table = {
            "track": 0, "smd": 1, "th": 2, "via": 5, "fill": 6, "zone": 7,
            "outline": 8 if n <= 10 else 11,
            "hole": 9 if n <= 10 else 12,
        }
        i = table.get(what)
        return i if i is not None and i < n else None

    def value(self, a: str, b: str) -> float | None:
        ia, ib = self.idx(a), self.idx(b)
        if ia is None or ib is None:
            return None
        return self.get(ia, ib)

    def min_copper(self) -> float:
        vals = []
        for k in ("track", "smd", "th", "via", "fill", "zone"):
            for j in ("track", "smd", "th", "via", "fill", "zone"):
                v = self.value(k, j)
                if v:
                    vals.append(v)
        return min(vals) if vals else 6.0


@dataclass
class Rules:
    safe: dict = field(default_factory=dict)  # name -> SafeRule
    track: dict = field(default_factory=dict)  # name -> (min, def, max)
    via: dict = field(default_factory=dict)  # name -> (min_d, def_d, max_d, min_drill, def_drill, max_drill)
    copper: dict = field(default_factory=dict)  # name -> CopperRule
    plane: dict = field(default_factory=dict)  # name -> CopperRule
    diff: dict = field(default_factory=dict)  # name -> (width, gap)
    # name -> (min width, default width, max width, min gap, length tolerance or None)
    diff_full: dict = field(default_factory=dict)
    net_length: dict = field(default_factory=dict)  # name -> (min, max), 0 = no limit
    blind: dict = field(default_factory=dict)  # blind / buried via rule name -> (start layer id, end layer id)
    defaults: dict = field(default_factory=dict)  # type -> name
    hole_to_hole: float | None = None
    mask: float = 2.0
    paste: float = 0.0
    via_tented: bool = True
    # selectors
    net_rules: dict = field(default_factory=dict)  # net -> {type: name}
    class_rules: dict = field(default_factory=dict)  # netclass -> {type: name}
    pour_rules: dict = field(default_factory=dict)  # pour id -> {type: name}
    region_rules: dict = field(default_factory=dict)  # region id -> {type: name}
    pair_rules: dict = field(default_factory=dict)  # differential pair name -> {type: name}
    eqlen_rules: dict = field(default_factory=dict)  # equal-length group name -> {type: name}
    pour_safe: dict = field(default_factory=dict)  # pour id -> SAFE profile name used for the pour (v3)

    def default(self, typ: str):
        name = self.defaults.get(typ)
        table = {"SAFE": self.safe, "TRACK": self.track, "RADIUS": self.via, "COPPER": self.copper,
                 "PLANE": self.plane, "DIFFER_ENTAIL": self.diff}.get(typ, {})
        if name in table:
            return table[name]
        return next(iter(table.values()), None)

    def lookup(self, typ: str, name: str | None):
        table = {"SAFE": self.safe, "TRACK": self.track, "RADIUS": self.via, "COPPER": self.copper,
                 "PLANE": self.plane, "DIFFER_ENTAIL": self.diff}.get(typ, {})
        if name and name in table:
            return table[name]
        return self.default(typ)


def _conn(v) -> str:
    s = str(v).upper()
    if s in ("0", "DIVERGENCE", "THERMAL", "SPOKE"):
        return "thermal"
    if s in ("1", "DIRECT", "SOLID"):
        return "direct"
    if s in ("2", "NONE", "NO_CONNECT", "NOCONNECT"):
        return "none"
    return "thermal"


def _v3_first(ctx: dict, key: str) -> dict:
    blk = ctx.get(key) or {}
    content = blk.get("content") if isinstance(blk, dict) else None
    if isinstance(content, list) and content:
        return content[0] or {}
    return {}


def _thermal_v3(d: dict) -> Thermal:
    return Thermal(_conn(d.get("connType", "DIVERGENCE")), float(d.get("spoWidth") or 10),
                   float(d.get("spoSpac") or 10), float(d.get("spoAng") or 90))


def parse_rules(recs) -> Rules:
    R = Rules()
    for r in recs:
        if r.type != "RULE":
            continue
        typ = str(r.d.get("ruleType") or "")
        typ = V2_TYPES.get(typ, typ)
        name = r.d.get("name") or typ
        if r.d.get("isDefault") in (True, 1, "1"):
            R.defaults[typ] = name
        ctx = r.d.get("context")
        data = r.d.get("data")
        if isinstance(ctx, dict):
            unit = ctx.get("unit", "mil")
            if typ == "SAFE":
                ss = ctx.get("safeSpacing") or []
                if ss and isinstance(ss[0], dict) and ss[0].get("content"):
                    R.safe[name] = SafeRule(ss[0]["content"])
            elif typ == "TRACK":
                c = _v3_first(ctx, "track")
                if c:
                    R.track[name] = (_mil(c.get("stroMin"), unit), _mil(c.get("stroDef"), unit),
                                     _mil(c.get("stroMax"), unit))
            elif typ == "RADIUS":
                R.via[name] = tuple(_mil(ctx.get(k), unit) for k in
                                    ("minRadius", "defRadius", "maxRadius", "minInner", "defInner", "maxInner"))
            elif typ == "COPPER":
                R.copper[name] = CopperRule(_thermal_v3(_v3_first(ctx, "sglPad")), _thermal_v3(_v3_first(ctx, "mulPad")))
            elif typ == "PLANE":
                t = _thermal_v3(_v3_first(ctx, "mulPad"))
                R.plane[name] = CopperRule(t, t)
            elif typ == "DIFFER_ENTAIL":
                w = _v3_first(ctx, "stroWidth")
                s = _v3_first(ctx, "spacing")
                R.diff[name] = (_mil(w.get("stroDef"), unit), _mil(s.get("spacDef"), unit))
                tol = ctx.get("differPairLenTolerMax")
                R.diff_full[name] = (_mil(w.get("stroMin"), unit), _mil(w.get("stroDef"), unit),
                                     _mil(w.get("stroMax"), unit), _mil(s.get("spacMin"), unit),
                                     _mil(tol, unit) if tol else None)
            elif typ == "BLIND":
                for b in ((ctx.get("blinds") or {}).get("content") or []):
                    if isinstance(b, dict) and b.get("name") and b.get("staLayerId") is not None:
                        R.blind[str(b["name"])] = (int(b["staLayerId"]), int(b.get("endLayerId") or 0))
            elif typ == "NET_LENGTH":
                R.net_length[name] = (_mil(ctx.get("netLenMin") or 0, unit), _mil(ctx.get("netLenMax") or 0, unit))
            elif typ == "OTHER":
                if ctx.get("holeClearance"):
                    R.hole_to_hole = _mil(ctx.get("holeClearance"), unit)
            elif typ == "SOLDER" and r.d.get("isDefault"):
                R.mask = _mil(ctx.get("padTopExpan", 2), unit)
                R.via_tented = float(ctx.get("viaTopExpan", -1000) or 0) <= -999
            elif typ == "PASTE" and r.d.get("isDefault"):
                R.paste = _mil(ctx.get("padTopExpan", 0), unit)
        elif isinstance(data, list):
            unit = data[0] if data and isinstance(data[0], str) else "mil"
            vals = data[1:] if data and isinstance(data[0], str) else data
            if typ == "SAFE":
                if vals and isinstance(vals[0], list):
                    R.safe[name] = SafeRule(vals[0])
            elif typ == "TRACK":
                if vals and isinstance(vals[0], dict):  # per-layer form {"1": [min, def, max]}
                    v = next(iter(vals[0].values()))
                    R.track[name] = tuple(_mil(x, unit) for x in v[:3])
                elif len(vals) >= 3:
                    R.track[name] = tuple(_mil(x, unit) for x in vals[:3])
            elif typ == "RADIUS" and len(vals) >= 6:
                R.via[name] = tuple(_mil(x, unit) for x in vals[:6])
            elif typ in ("COPPER", "PLANE"):
                v = list(vals) + [None] * 12
                if typ == "COPPER":
                    smd = Thermal(_conn(v[2]), float(v[3] or 10), float(v[4] or 10), float(v[5] or 90))
                    th = Thermal(_conn(v[7]), float(v[8] or smd.spoke_width), float(v[9] or smd.spoke_gap),
                                 float(v[10] or 90)) if v[8] is not None else smd
                    R.copper[name] = CopperRule(smd, th, clearance=float(v[0]) if v[0] is not None else None)
                else:
                    th = Thermal(_conn(v[2]), float(v[3] or 10), float(v[4] or 10), float(v[5] or 90))
                    R.plane[name] = CopperRule(th, th, clearance=float(v[0]) if v[0] is not None else None)
            elif typ == "DIFFER_ENTAIL" and len(vals) >= 4:
                R.diff[name] = (_mil(vals[1], unit), _mil(vals[3], unit))
                # [width min, def, max, gap min, gap def, -, length tolerance]
                v = list(vals) + [None] * 7
                R.diff_full[name] = (_mil(v[0], unit), _mil(v[1], unit), _mil(v[2], unit), _mil(v[3], unit),
                                     _mil(v[6], unit) if v[6] else None)
            elif typ == "NET_LENGTH" and len(vals) >= 2:
                R.net_length[name] = (_mil(vals[0] or 0, unit), _mil(vals[1] or 0, unit))
            elif typ == "OTHER":
                if len(vals) >= 3 and vals[2]:
                    R.hole_to_hole = _mil(vals[2], unit)
            elif typ == "SOLDER" and r.d.get("isDefault") in (True, 1):
                if vals:
                    R.mask = _mil(vals[0], unit)
                if len(vals) >= 3:
                    R.via_tented = float(vals[2] if vals[2] is not None else -1000) <= -999
            elif typ == "PASTE" and r.d.get("isDefault") in (True, 1):
                if vals:
                    R.paste = _mil(vals[0], unit)
    for r in recs:
        if r.type != "RULE_SELECTOR":
            continue
        sel = r.d.get("selector") or []
        if len(sel) < 2:
            continue
        kind, key = str(sel[0]), str(sel[1])
        rules = {}
        if isinstance(r.d.get("rules"), dict):  # v2
            for k, v in r.d["rules"].items():
                rules[V2_TYPES.get(str(k), str(k))] = v
        for k in ("ruleKeyValue", "copperValue", "innerPlaneValue"):
            if isinstance(r.d.get(k), dict) and k == "ruleKeyValue":
                rules.update(r.d[k])
        if kind == "POUR" and isinstance(r.d.get("copperValue"), dict):
            cv = r.d["copperValue"].get("COPPER")
            if cv and cv in R.safe:
                R.pour_safe[key] = cv
        if not rules:
            continue
        if kind == "NET":
            R.net_rules[key] = rules
        elif kind == "NET_CLASS":
            R.class_rules[key] = rules
        elif kind == "POUR":
            R.pour_rules[key] = rules
        elif kind == "REGION":
            R.region_rules[key] = rules
        elif kind == "DIFF_PAIR":
            R.pair_rules[key] = rules
        elif kind == "EQ_LEN_GRP":
            R.eqlen_rules[key] = rules
    return R
