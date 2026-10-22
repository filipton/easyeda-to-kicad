"""EasyEDA <-> KiCad layer mapping and native KiCad layer tables."""

from __future__ import annotations

# EasyEDA layer ids
TOP, BOTTOM = 1, 2
TOP_SILK, BOT_SILK = 3, 4
TOP_MASK, BOT_MASK = 5, 6
TOP_PASTE, BOT_PASTE = 7, 8
TOP_ASSY, BOT_ASSY = 9, 10
OUTLINE, MULTI = 11, 12
DOCUMENT, MECHANICAL = 13, 14
HOLE = 47
COMP_SHAPE, COMP_MARKING, PIN_SOLDERING, PIN_FLOATING = 48, 49, 50, 51
INNER_FIRST, INNER_LAST = 15, 44

# Non copper layers (front variant).  None = dropped.
_FIXED = {
    TOP_SILK: "F.SilkS",
    BOT_SILK: "B.SilkS",
    TOP_MASK: "F.Mask",
    BOT_MASK: "B.Mask",
    TOP_PASTE: "F.Paste",
    BOT_PASTE: "B.Paste",
    TOP_ASSY: "F.Fab",
    BOT_ASSY: "B.Fab",
    OUTLINE: "Edge.Cuts",
    DOCUMENT: "Dwgs.User",
    MECHANICAL: "Eco1.User",
    COMP_SHAPE: "F.Fab",
    COMP_MARKING: "F.Fab",
    PIN_SOLDERING: None,
    PIN_FLOATING: None,
    52: None,  # ratline
    53: "User.2",  # 3D shell outline
    54: "User.3",  # 3D shell top
    55: "User.4",  # 3D shell bottom
    56: None,  # drill drawing
    57: "Eco2.User",  # stiffener / others
}

FLIP = {
    "F.Cu": "B.Cu", "B.Cu": "F.Cu",
    "F.SilkS": "B.SilkS", "B.SilkS": "F.SilkS",
    "F.Mask": "B.Mask", "B.Mask": "F.Mask",
    "F.Paste": "B.Paste", "B.Paste": "F.Paste",
    "F.Fab": "B.Fab", "B.Fab": "F.Fab",
    "F.CrtYd": "B.CrtYd", "B.CrtYd": "F.CrtYd",
    "F.Adhes": "B.Adhes", "B.Adhes": "F.Adhes",
}

# KiCad 10 internal layer ordinals (from a freshly created board)
_ORD = {
    "F.Cu": 0, "B.Cu": 2, "F.Mask": 1, "B.Mask": 3, "F.SilkS": 5, "B.SilkS": 7, "F.Adhes": 9, "B.Adhes": 11,
    "F.Paste": 13, "B.Paste": 15, "Dwgs.User": 17, "Cmts.User": 19, "Eco1.User": 21, "Eco2.User": 23,
    "Edge.Cuts": 25, "Margin": 27, "B.CrtYd": 29, "F.CrtYd": 31, "B.Fab": 33, "F.Fab": 35,
}
for _i in range(1, 46):
    _ORD[f"User.{_i}"] = 37 + 2 * _i
for _i in range(1, 31):
    _ORD[f"In{_i}.Cu"] = 2 + 2 * _i


class LayerMap:
    """Maps EasyEDA layer ids of one board onto a native KiCad layer stack."""

    def __init__(self, inner_ids: list[int] | None = None):
        self.inner = {}
        for k, lid in enumerate(sorted(inner_ids or [])):
            self.inner[lid] = f"In{k + 1}.Cu"

    @property
    def copper_count(self) -> int:
        return 2 + len(self.inner)

    def copper(self, lid: int) -> str | None:
        if lid == TOP:
            return "F.Cu"
        if lid == BOTTOM:
            return "B.Cu"
        return self.inner.get(lid)

    def to_kicad(self, lid: int) -> str | None:
        c = self.copper(lid)
        if c:
            return c
        if INNER_FIRST <= lid <= INNER_LAST:
            return None  # inner layer not enabled on this board
        return _FIXED.get(lid, "Cmts.User")

    def copper_layers(self) -> list[str]:
        return ["F.Cu"] + [self.inner[k] for k in sorted(self.inner)] + ["B.Cu"]


def is_copper(name: str) -> bool:
    return name.endswith(".Cu")


def flip_layer(name: str) -> str:
    return FLIP.get(name, name)


def board_layers_table(lm: LayerMap, inner_types: dict[str, str] | None = None) -> list:
    """The (layers ...) list exactly as a fresh KiCad 10 board writes it."""
    from ..sexp import Sym

    inner_types = inner_types or {}
    rows = [[0, "F.Cu", Sym("signal")]]
    for k in range(1, lm.copper_count - 1):
        name = f"In{k}.Cu"
        rows.append([_ORD[name], name, Sym(inner_types.get(name, "signal"))])
    rows.append([2, "B.Cu", Sym("signal")])
    rows += [
        [9, "F.Adhes", Sym("user"), "F.Adhesive"],
        [11, "B.Adhes", Sym("user"), "B.Adhesive"],
        [13, "F.Paste", Sym("user")],
        [15, "B.Paste", Sym("user")],
        [5, "F.SilkS", Sym("user"), "F.Silkscreen"],
        [7, "B.SilkS", Sym("user"), "B.Silkscreen"],
        [1, "F.Mask", Sym("user")],
        [3, "B.Mask", Sym("user")],
        [17, "Dwgs.User", Sym("user"), "User.Drawings"],
        [19, "Cmts.User", Sym("user"), "User.Comments"],
        [21, "Eco1.User", Sym("user"), "User.Eco1"],
        [23, "Eco2.User", Sym("user"), "User.Eco2"],
        [25, "Edge.Cuts", Sym("user")],
        [27, "Margin", Sym("user")],
        [31, "F.CrtYd", Sym("user"), "F.Courtyard"],
        [29, "B.CrtYd", Sym("user"), "B.Courtyard"],
        [35, "F.Fab", Sym("user")],
        [33, "B.Fab", Sym("user")],
        [39, "User.1", Sym("user")],
        [41, "User.2", Sym("user")],
        [43, "User.3", Sym("user")],
        [45, "User.4", Sym("user")],
    ]
    return [Sym("layers")] + rows
