"""Minimal S-expression builder/serializer that writes KiCad-style files.

Lists are plain Python lists whose first element is a ``Sym`` (unquoted token).
Strings are always quoted, numbers are written with KiCad's precision rules.
"""

from __future__ import annotations

import math
import re
import uuid as _uuid


class Sym(str):
    """An unquoted token such as ``yes``, ``smd`` or a list head like ``at``."""

    __slots__ = ()


def sym(s: str) -> Sym:
    return Sym(s)


YES = Sym("yes")
NO = Sym("no")


def yesno(v: bool) -> Sym:
    return YES if v else NO


def fmt_num(v: float, decimals: int = 6) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return str(v)
    if math.isnan(v) or math.isinf(v):
        raise ValueError(f"bad number {v}")
    r = round(v, decimals)
    if r == 0:
        return "0"
    s = f"{r:.{decimals}f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


_ESC = re.compile(r'["\\\n\r\t]')


def quote(s: str) -> str:
    def rep(m: re.Match) -> str:
        c = m.group(0)
        return {"\n": "\\n", "\r": "\\r", "\t": "\\t"}.get(c, "\\" + c)

    return '"' + _ESC.sub(rep, s) + '"'


def atom(v) -> str:
    if isinstance(v, Sym):
        return str(v)
    if isinstance(v, str):
        return quote(v)
    if isinstance(v, (int, float)):
        return fmt_num(v)
    raise TypeError(f"unsupported atom {v!r}")


# Lists that KiCad prints on a single line even when they contain sub-lists.
_INLINE_HEADS = {
    "at", "xy", "size", "start", "end", "mid", "center", "offset", "xyz", "scale",
    "rotate", "drill", "rect_delta", "layers", "color", "pts_inline", "justify",
    "font_inline", "margins", "uuid", "net", "width", "type", "layer", "length",
    "thickness", "property_inline",
}


def _is_atom(x) -> bool:
    return not isinstance(x, list)


def dumps(node, indent: int = 0) -> str:
    out: list[str] = []
    _dump(node, indent, out)
    return "".join(out)


def _dump(node, indent: int, out: list[str]) -> None:
    if _is_atom(node):
        out.append(atom(node))
        return
    head = node[0] if node else None
    if all(_is_atom(x) for x in node) or (isinstance(head, Sym) and head in _INLINE_HEADS):
        out.append("(" + " ".join(_inline(x) for x in node) + ")")
        return
    # Leading atoms stay on the same line, nested lists go on their own lines.
    i = 0
    parts = []
    while i < len(node) and _is_atom(node[i]):
        parts.append(atom(node[i]))
        i += 1
    out.append("(" + " ".join(parts))
    for child in node[i:]:
        out.append("\n" + "\t" * (indent + 1))
        if _is_atom(child):
            out.append(atom(child))
        elif child and child[0] == "pts":
            _dump_pts(child, indent + 1, out)
        else:
            _dump(child, indent + 1, out)
    out.append("\n" + "\t" * indent + ")")


def _dump_pts(node, indent: int, out: list[str]) -> None:
    out.append("(pts")
    items = node[1:]
    for k in range(0, len(items), 4):
        out.append("\n" + "\t" * (indent + 1))
        out.append(" ".join(_inline(x) for x in items[k:k + 4]))
    out.append("\n" + "\t" * indent + ")")


def _inline(node) -> str:
    if _is_atom(node):
        return atom(node)
    return "(" + " ".join(_inline(x) for x in node) + ")"


def new_uuid() -> str:
    return str(_uuid.uuid4())


_NS = _uuid.UUID("5d6c9b52-3d1e-4b8e-9c1a-6f0f2b7e9a10")


def stable_uuid(*parts) -> str:
    """Deterministic UUID so re-running the converter yields identical files."""
    return str(_uuid.uuid5(_NS, "\x1f".join(str(p) for p in parts)))


# ---------------------------------------------------------------------------
# Parser (used for reading KiCad files such as stock power symbols/netlists)
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.S)


def loads(text: str):
    pos = 0
    stack: list[list] = [[]]
    n = len(text)
    while pos < n:
        m = _TOKEN.match(text, pos)
        if not m:
            if text[pos:].strip() == "":
                break
            raise ValueError(f"parse error at {pos}")
        pos = m.end()
        if m.group(1):
            stack.append([])
        elif m.group(2):
            done = stack.pop()
            stack[-1].append(done)
        elif m.group(3) is not None:
            stack[-1].append(_unescape(m.group(3)))
        elif m.group(4) is not None:
            tok = m.group(4)
            stack[-1].append(_num_or_sym(tok))
    root = stack[0]
    return root[0] if len(root) == 1 else root


def _unescape(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "r": "\r", "t": "\t"}.get(m.group(1), m.group(1)), s)


def _num_or_sym(tok: str):
    try:
        if re.fullmatch(r"-?\d+", tok):
            return int(tok)
        if re.fullmatch(r"-?\d*\.\d*(e-?\d+)?", tok):
            return float(tok)
    except ValueError:
        pass
    return Sym(tok)


def find(node, head: str):
    for c in node:
        if isinstance(c, list) and c and c[0] == head:
            return c
    return None


def find_all(node, head: str):
    return [c for c in node if isinstance(c, list) and c and c[0] == head]
