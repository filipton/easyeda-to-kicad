"""Normalise EasyEDA Pro v2 (positional JSON arrays) and v3 (keyed objects)
records into one representation: ``Rec(type, id, d)`` where ``d`` uses the v3
field names.

v3 field order is (almost) the v2 positional order with names attached, so for
v2 each record type gets a list of field names indexed by array position.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class Rec:
    type: str
    id: str
    d: dict = field(default_factory=dict)

    def get(self, k, default=None):
        v = self.d.get(k, default)
        return default if v is None else v

    def num(self, k, default=0.0) -> float:
        v = self.d.get(k)
        if v is None or v == "":
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def int(self, k, default=0) -> int:
        v = self.d.get(k)
        if v is None or v == "":
            return default
        if isinstance(v, bool):
            return int(v)
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default

    def bool(self, k, default=False) -> bool:
        v = self.d.get(k)
        if v is None:
            return default
        if isinstance(v, str):
            return v.lower() in ("1", "true", "yes")
        return bool(v)

    def str(self, k, default="") -> str:
        v = self.d.get(k)
        if v is None:
            return default
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v)


# --------------------------------------------------------------------------
# v2 positional layouts.  Index 0 is the record type, index 1 usually the id.
# A name of None means "ignore".
# --------------------------------------------------------------------------

PCB_V2 = {
    "LINE": ["id", "groupId", "netName", "layerId", "startX", "startY", "endX", "endY", "width", "locked"],
    "ARC": ["id", "groupId", "netName", "layerId", "startX", "startY", "endX", "endY", "angle", "width", "locked"],
    "VIA": ["id", "groupId", "netName", "ruleName", "centerX", "centerY", "holeDiameter", "viaDiameter",
            "viaType", "topSolderExpansion", "bottomSolderExpansion", "locked", "unusedInnerLayers"],
    "POLY": ["id", "groupId", "netName", "layerId", "width", "path", "locked"],
    "FILL": ["id", "groupId", "netName", "layerId", "width", "fillStyle", "path", "locked"],
    "POUR": ["id", "groupId", "netName", "layerId", "width", "name", "order", "path", "pourType", "keepIsland",
             "locked"],
    "REGION": ["id", "groupId", "layerId", "width", "prohibitType", "path", "locked", "name"],
    "TEARDROP": ["id", "netName", "layerId", "path", "locked"],
    "STRING": ["id", "groupId", "layerId", "x", "y", "text", "fontFamily", "fontSize", "strokeWidth", "bold",
               "italic", "origin", "angle", "reverse", "expansion", "mirror", "locked"],
    "ATTR": ["id", "groupId", "parentId", "layerId", "x", "y", "key", "value", "keyVisible", "valueVisible",
             "fontFamily", "fontSize", "strokeWidth", "bold", "italic", "origin", "angle", "reverse", "expansion",
             "mirror", "locked"],
    "COMPONENT": ["id", "groupId", "layerId", "x", "y", "angle", "attrs", "locked"],
    "PAD": ["id", "groupId", "netName", "layerId", "num", "centerX", "centerY", "padAngle", "hole", "defaultPad",
            "specialPad", "padOffsetX", "padOffsetY", "relativeAngle", "plated", "padType",
            "topSolderExpansion", "bottomSolderExpansion", "topPasteExpansion", "bottomPasteExpansion", "locked"],
    "IMAGE": ["id", "groupId", "layerId", "startX", "startY", "width", "height", "angle", "mirror", "path",
              "locked"],
    "OBJ": ["id", "groupId", "layerId", "unknown", "startX", "startY", "width", "height", "angle", "mirror",
            "path", "locked"],
    "DIMENSION": ["id", "dimensionType", "layerId", "unit", "precision", "textFollow", "lineWidth", "coordinates",
                  "locked"],
    "LAYER": ["layerId", "layerType", "layerName", "use", "activeColor", "activateTransparency",
              "inactiveColor", "inactiveTransparency"],
    "LAYER_PHYS": ["layerId", "material", "thickness", "permittivity", "lossTangent", "isKeepIsland"],
    "NET": ["id", "netType", "specialColor", "retLine", "differentialName", "isPositiveNet",
            "equalLengthGroupName"],
    "PAD_NET": ["parentId", "num", "padNet", "padId"],
    "POURED": ["id", "pourId", "strokeWidth", "fill", "path"],
    "CANVAS": ["originX", "originY", "unit", "gridXSize", "gridYSize", "snapXSize", "snapYSize"],
    "GROUP": ["id", "groupName"],
}

SCH_V2 = {
    "COMPONENT": ["id", "partId", "x", "y", "rotation", "isMirror", "attrs", "locked"],
    "ATTR": ["id", "parentId", "key", "value", "keyVisible", "valueVisible", "x", "y", "rotation", "style",
             "locked"],
    "WIRE": ["id", "geometry", "style", "locked"],
    "TEXT": ["id", "x", "y", "rotation", "value", "style", "locked"],
    "RECT": ["id", "dotX1", "dotY1", "dotX2", "dotY2", "radiusX", "radiusY", "rotation", "style", "locked"],
    "CIRCLE": ["id", "centerX", "centerY", "radius", "style", "locked"],
    "ELLIPSE": ["id", "centerX", "centerY", "radiusX", "radiusY", "rotation", "style", "locked"],
    "ARC": ["id", "startX", "startY", "referenceX", "referenceY", "endX", "endY", "style", "locked"],
    "BEZIER": ["id", "points", "style", "locked"],
    "POLY": ["id", "points", "closed", "style", "locked"],
    "PIN": ["id", "display", "electric", "x", "y", "length", "rotation", "color", "pinShape", "locked"],
    "PART": ["id", "info"],
    "OBJ": ["id", "unknown", "x", "y", "width", "height", "rotation", "isMirror", "path", "locked"],
    "FONTSTYLE": ["id", "color", "fillColor", "fontFamily", "fontSize", "italic", "bold", "underline",
                  "strikeout", "vAlign", "hAlign"],
    "LINESTYLE": ["id", "strokeColor", "strokeStyle", "fillColor", "strokeWidth", "fillStyle"],
}


def _v2_fields(layout: dict, arr: list) -> Rec | None:
    if not arr or not isinstance(arr[0], str):
        return None
    t = arr[0]
    names = layout.get(t)
    d: dict = {}
    if names is None:
        rid = arr[1] if len(arr) > 1 and isinstance(arr[1], str) else ""
        return Rec(t, rid, {"_raw": arr})
    for i, name in enumerate(names):
        if name is None or i + 1 >= len(arr):
            continue
        d[name] = arr[i + 1]
    d["_raw"] = arr
    rid = d.pop("id", None)
    if rid is None:
        rid = ""
    return Rec(t, str(rid), d)


_V2_ORIGIN = {
    1: "LEFT_TOP", 2: "LEFT_MIDDLE", 3: "LEFT_BOTTOM",
    4: "CENTER_TOP", 5: "CENTER_MIDDLE", 6: "CENTER_BOTTOM",
    7: "RIGHT_TOP", 8: "RIGHT_MIDDLE", 9: "RIGHT_BOTTOM",
}


def _norm_pad_shape(v):
    """v2 ["RECT", w, h, r] -> v3 dict."""
    if isinstance(v, dict) or v is None:
        return v
    if not isinstance(v, list) or not v:
        return None
    t = v[0]
    if t in ("POLY", "POLYGON"):
        return {"padType": "POLYLINE_COMPLEX_POLYGON", "path": v[1] if len(v) > 1 else []}
    d = {"padType": t}
    if len(v) > 1:
        d["width"] = v[1]
    if len(v) > 2:
        d["height"] = v[2]
    if len(v) > 3:
        d["radius"] = v[3]
    return d


def _norm_hole(v):
    if isinstance(v, dict) or v is None:
        return v
    if not isinstance(v, list) or not v:
        return None
    return {"holeType": v[0], "width": v[1] if len(v) > 1 else 0, "height": v[2] if len(v) > 2 else 0}


def norm_v2_pcb(arr: list) -> Rec | None:
    r = _v2_fields(PCB_V2, arr)
    if r is None:
        return None
    d = r.d
    t = r.type
    if t in ("STRING", "ATTR"):
        o = d.get("origin")
        if isinstance(o, (int, float)):
            d["origin"] = _V2_ORIGIN.get(int(o), "LEFT_BOTTOM")
    elif t == "PAD":
        d["hole"] = _norm_hole(d.get("hole"))
        d["defaultPad"] = _norm_pad_shape(d.get("defaultPad"))
        sp = d.get("specialPad") or []
        d["specialPad"] = [[g[0], g[1], _norm_pad_shape(g[2])] if isinstance(g, list) and len(g) > 2 else g
                           for g in sp] if isinstance(sp, list) else []
    elif t == "LAYER":
        r.id = str(d.get("layerId"))
        u = d.get("use")
        d["use"] = bool(u) if not isinstance(u, bool) else u
    elif t == "LAYER_PHYS":
        r.id = str(d.get("layerId"))
    elif t == "NET":
        d["name"] = r.id
    elif t == "POUR":
        pt = d.get("pourType")
        if isinstance(pt, list):
            d["pourType"] = {"pourType": pt[0] if pt else "SOLID", "fineness": pt[1] if len(pt) > 1 else 8,
                             "params": pt[1:]}
    elif t == "RULE":
        # ["RULE", typeNum, name, isDefault, data]
        d.clear()
        d.update({"ruleType": str(arr[1]), "name": arr[2], "isDefault": bool(arr[3]),
                  "data": arr[4] if len(arr) > 4 else None, "_raw": arr})
        r.id = f"{arr[1]}:{arr[2]}"
    elif t == "RULE_SELECTOR":
        d.clear()
        sel = arr[1] if len(arr) > 1 else []
        d.update({"selector": sel, "order": arr[2] if len(arr) > 2 else 0,
                  "rules": arr[3] if len(arr) > 3 else {}, "_raw": arr})
        r.id = json.dumps(sel)
    elif t == "COMPONENT":
        if not isinstance(d.get("attrs"), dict):
            d["attrs"] = {}
    return r


def norm_v2_sch(arr: list) -> Rec | None:
    r = _v2_fields(SCH_V2, arr)
    if r is None:
        return None
    if r.type == "WIRE":
        segs = []
        for poly in r.d.get("geometry") or []:
            pts = [(poly[i], poly[i + 1]) for i in range(0, len(poly) - 1, 2)]
            for a, b in zip(pts, pts[1:]):
                segs.append((a[0], a[1], b[0], b[1]))
        r.d["segments"] = segs
    elif r.type == "PART":
        info = r.d.get("info") or {}
        r.d["bbox"] = info.get("BBOX") if isinstance(info, dict) else None
    elif r.type == "POLY":
        pts = r.d.get("points") or []
        r.d["points"] = [(pts[i], pts[i + 1]) for i in range(0, len(pts) - 1, 2)]
    elif r.type == "BEZIER":
        pts = r.d.get("points") or []
        r.d["points"] = [(pts[i], pts[i + 1]) for i in range(0, len(pts) - 1, 2)]
    elif r.type == "OBJ":
        # two variants: [OBJ,id,"",mime,x,y,w,h,rot,data] or [OBJ,id,"",x,y,w,h,rot,mirror,url]
        if len(arr) > 3 and isinstance(arr[3], str):
            mime = arr[3]
            r.d = {"x": arr[4], "y": arr[5], "width": arr[6], "height": arr[7], "rotation": arr[8],
                   "isMirror": False, "path": f"data:{mime}," + str(arr[9]) if len(arr) > 9 else ""}
    return r


def resolve_v2_styles(recs: list[Rec]) -> None:
    """Inline FONTSTYLE / LINESTYLE references (v3 stores them inline)."""
    fonts = {r.id: r.d for r in recs if r.type == "FONTSTYLE"}
    lines = {r.id: r.d for r in recs if r.type == "LINESTYLE"}
    for r in recs:
        st = r.d.get("style")
        if not isinstance(st, str):
            continue
        if r.type in ("ATTR", "TEXT"):
            f = fonts.get(st)
            if f:
                for k in ("color", "fontFamily", "fontSize", "italic", "bold", "vAlign", "hAlign"):
                    if f.get(k) is not None and r.d.get(k) is None:
                        r.d[k] = f.get(k)
        else:
            ls = lines.get(st)
            if ls:
                for k in ("strokeColor", "strokeStyle", "fillColor", "strokeWidth", "fillStyle"):
                    if ls.get(k) is not None and r.d.get(k) is None:
                        r.d[k] = ls.get(k)


def assign_v2_parts(recs: list[Rec]) -> None:
    """v2 symbol primitives follow their PART record; v3 carries partId."""
    cur = ""
    for r in recs:
        if r.type == "PART":
            cur = r.id
        elif r.type in ("PIN", "RECT", "CIRCLE", "ELLIPSE", "ARC", "BEZIER", "POLY", "TEXT", "OBJ", "ATTR"):
            r.d.setdefault("partId", cur)


# --------------------------------------------------------------------------
# v3
# --------------------------------------------------------------------------

# v3 numeric text origins (prodocs format/pcb/attr): row by row from the top left
_V3_ORIGIN = {0: "LEFT_TOP", 1: "CENTER_TOP", 2: "RIGHT_TOP", 3: "LEFT_MIDDLE", 4: "CENTER_MIDDLE",
              5: "RIGHT_MIDDLE", 6: "LEFT_BOTTOM", 7: "CENTER_BOTTOM", 8: "RIGHT_BOTTOM"}
_V3_ALIGN_FIX = {"LEFT_CENTER": "LEFT_MIDDLE", "RIGHT_CENTER": "RIGHT_MIDDLE", "CENTER_CENTER": "CENTER_MIDDLE"}


def norm_v3(outer: dict, inner: dict, doc_type: str) -> Rec | None:
    t = outer.get("type")
    rid = outer.get("id") or ""
    d = dict(inner) if isinstance(inner, dict) else {}
    r = Rec(t, str(rid), d)
    idarr = None
    if isinstance(rid, str) and rid.startswith("["):
        try:
            idarr = json.loads(rid)
        except ValueError:
            idarr = None
    if t == "LAYER" and idarr:
        d.setdefault("layerId", idarr[1])
        r.id = str(idarr[1])
    elif t == "LAYER_PHYS" and idarr:
        d["layerId"] = idarr[1]
        r.id = str(idarr[1])
    elif t == "NET" and idarr:
        d["name"] = idarr[1]
        r.id = idarr[1]
    elif t == "PAD_NET" and idarr:
        d["parentId"] = idarr[1]
        d["num"] = idarr[2]
        d["padId"] = idarr[3] if len(idarr) > 3 else ""
    elif t == "POURED" and idarr:
        d["pourId"] = idarr[1]
    elif t == "RULE" and idarr:
        d = {"ruleType": idarr[1], "name": idarr[2], "isDefault": inner.get("ruleState") == "DEFAULT",
             "context": inner.get("ruleContext") or {}}
        r.d = d
        r.id = f"{idarr[1]}:{idarr[2]}"
    elif t == "RULE_SELECTOR" and idarr:
        d["selector"] = idarr[1]
        r.id = json.dumps(idarr[1])
    if "origin" in d and isinstance(d["origin"], str):
        d["origin"] = _V3_ALIGN_FIX.get(d["origin"], d["origin"])
    elif isinstance(d.get("origin"), (int, float)) and not isinstance(d.get("origin"), bool):
        d["origin"] = _V3_ORIGIN.get(int(d["origin"]), "LEFT_BOTTOM")
    if "positionX" in d and "x" not in d:  # documented v3 names of text positions
        d["x"], d["y"] = d.get("positionX"), d.get("positionY")
    if "align" in d and isinstance(d["align"], str):
        d["align"] = _V3_ALIGN_FIX.get(d["align"], d["align"])
    return r


def flip_v3_sch_y(recs: list[Rec]) -> None:
    """v3 schematic/symbol coordinates are Y-down; v2 (and our model) is Y-up."""
    for r in recs:
        d = r.d
        for k in ("y", "dotY1", "dotY2", "centerY", "startY", "endY", "referenceY", "originY"):
            if isinstance(d.get(k), (int, float)):
                d[k] = -d[k]
        if r.type in ("POLY", "BEZIER", "POLYLINE"):
            pts = d.get("points") or []
            if pts and isinstance(pts[0], dict):
                d["points"] = [(p.get("x", 0), -p.get("y", 0)) for p in pts]
            elif pts and isinstance(pts[0], (int, float)):
                d["points"] = [(pts[i], -pts[i + 1]) for i in range(0, len(pts) - 1, 2)]
        if r.type == "PART" and isinstance(d.get("BBOX"), list):
            b = d["BBOX"]
            d["bbox"] = [b[0], -b[1], b[2], -b[3]] if len(b) >= 4 else None


def assemble_v3_wires(recs: list[Rec]) -> list[Rec]:
    """v3 stores a wire as a WIRE row plus LINE rows pointing at it via lineGroup."""
    wires = {r.id: r for r in recs if r.type == "WIRE"}
    out = []
    for r in recs:
        if r.type == "LINE" and r.d.get("lineGroup") in wires:
            w = wires[r.d["lineGroup"]]
            w.d.setdefault("segments", []).append((r.num("startX"), r.num("startY"), r.num("endX"), r.num("endY")))
            continue
        out.append(r)
    for w in wires.values():
        w.d.setdefault("segments", [])
    return out
