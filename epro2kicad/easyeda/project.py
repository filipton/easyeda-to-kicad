"""Load EasyEDA Pro projects (v2 ``.epro``/``.zip`` and v3 ``.epro2``) and
libraries into a format-independent ``Project`` object."""

from __future__ import annotations

import base64
import io
import json
import os
import zipfile
from collections import Counter
from dataclasses import dataclass, field

from .records import (Rec, assemble_v3_wires, assign_v2_parts, flip_v3_sch_y, norm_v2_pcb, norm_v2_sch, norm_v3,
                      resolve_v2_styles)


class FormatError(Exception):
    pass


@dataclass
class Doc:
    uuid: str
    title: str
    recs: list[Rec] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def of(self, *types: str) -> list[Rec]:
        return [r for r in self.recs if r.type in types]


@dataclass
class Page:
    uuid: str
    name: str
    index: int
    doc: Doc


@dataclass
class Schematic:
    uuid: str
    name: str
    pages: list[Page] = field(default_factory=list)


@dataclass
class Board:
    name: str
    schematic: str | None  # schematic uuid
    pcb: str | None  # pcb uuid


@dataclass
class Project:
    title: str
    fmt: int  # 2 or 3
    boards: list[Board] = field(default_factory=list)
    schematics: dict[str, Schematic] = field(default_factory=dict)
    pcbs: dict[str, Doc] = field(default_factory=dict)
    symbols: dict[str, Doc] = field(default_factory=dict)
    footprints: dict[str, Doc] = field(default_factory=dict)
    devices: dict[str, dict] = field(default_factory=dict)  # uuid -> {title, attributes, ...}
    blobs: dict[str, str] = field(default_factory=dict)  # id -> data url
    source: str = ""

    def symbol_type(self, uuid: str) -> int:
        doc = self.symbols.get(uuid)
        if doc is None:
            return 2
        return int(doc.meta.get("symbolType") or 2)


# ---------------------------------------------------------------------------


def _read_lines(data: bytes):
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def load(path: str) -> Project:
    with open(path, "rb") as f:
        raw = f.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as e:
        raise FormatError(f"{path} is not a zip archive (EasyEDA Pro .epro/.epro2/.zip expected)") from e
    names = zf.namelist()
    base = {n.rsplit("/", 1)[-1].lower(): n for n in names}
    if "project2.json" in base or any(n.lower().endswith(".epru") for n in names):
        p = _load_v3(zf, names)
    elif "project.json" in base:
        p = _load_v2(zf, names, base["project.json"])
    else:
        raise FormatError(f"{path}: no project.json / project2.json found")
    p.source = path
    return p


# ---------------------------------------------------------------------------
# v2
# ---------------------------------------------------------------------------


def _v2_doc(zf: zipfile.ZipFile, name: str, uuid: str, title: str, kind: str) -> Doc:
    return parse_v2_doc(zf.read(name), uuid, title, kind)


def parse_v2_doc(data: bytes | str, uuid: str, title: str, kind: str) -> Doc:
    """One EasyEDA Pro v2 document (line-per-record JSON).  kind: sch | sym | pcb | fp"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    doc = Doc(uuid, title)
    for arr in _read_lines(data):
        if not isinstance(arr, list):
            continue
        r = norm_v2_sch(arr) if kind in ("sch", "sym") else norm_v2_pcb(arr)
        if r is None:
            continue
        if r.type == "HEAD" and isinstance(arr[1:2] and arr[1], dict):
            doc.meta.update(arr[1])
        doc.recs.append(r)
    if kind in ("sch", "sym"):
        resolve_v2_styles(doc.recs)
    if kind == "sym":
        assign_v2_parts(doc.recs)
    return doc


def _load_v2(zf: zipfile.ZipFile, names: list[str], pj_name: str) -> Project:
    pj = json.loads(zf.read(pj_name).decode("utf-8"))
    prefix = pj_name[: -len("project.json")]
    cfg = pj.get("config") or {}
    proj = Project(title=cfg.get("title") or "project", fmt=2)

    def path(*parts):
        return prefix + "/".join(parts)

    nameset = set(names)
    for uuid, info in (pj.get("symbols") or {}).items():
        fn = path("SYMBOL", uuid + ".esym")
        if fn in nameset:
            d = _v2_doc(zf, fn, uuid, info.get("display_title") or info.get("title") or uuid, "sym")
            d.meta.setdefault("symbolType", info.get("type", d.meta.get("symbolType", 2)))
            d.meta["info"] = info
            proj.symbols[uuid] = d
    for uuid, info in (pj.get("footprints") or {}).items():
        fn = path("FOOTPRINT", uuid + ".efoo")
        if fn in nameset:
            d = _v2_doc(zf, fn, uuid, info.get("display_title") or info.get("title") or uuid, "fp")
            d.meta["info"] = info
            proj.footprints[uuid] = d
    for uuid, info in (pj.get("devices") or {}).items():
        attrs = info.get("attributes") or {}
        proj.devices[uuid] = {
            "title": info.get("display_title") or info.get("title") or uuid,
            "attributes": {k: ("" if v is None else str(v) if not isinstance(v, str) else v) for k, v in attrs.items()},
            "description": info.get("description") or info.get("desc") or "",
        }
    for uuid, info in (pj.get("schematics") or {}).items():
        sch = Schematic(uuid, info.get("name") or uuid)
        for k, sh in enumerate(info.get("sheets") or []):
            fn = path("SHEET", uuid, f"{sh.get('id')}.esch")
            if fn in nameset:
                d = _v2_doc(zf, fn, sh.get("uuid") or f"{uuid}-{sh.get('id')}", sh.get("name") or f"P{k + 1}", "sch")
                sch.pages.append(Page(sh.get("uuid") or d.uuid, sh.get("name") or f"P{k + 1}", int(sh.get("id") or k + 1), d))
        sch.pages.sort(key=lambda p: p.index)
        proj.schematics[uuid] = sch
    for uuid, title in (pj.get("pcbs") or {}).items():
        fn = path("PCB", uuid + ".epcb")
        if fn in nameset:
            if isinstance(title, dict):
                title = title.get("title") or title.get("name") or uuid
            proj.pcbs[uuid] = _v2_doc(zf, fn, uuid, str(title), "pcb")
    for name, b in (pj.get("boards") or {}).items():
        proj.boards.append(Board(name, b.get("schematic") or None, b.get("pcb") or None))
    for n in names:
        if n.startswith(prefix + "BLOB/") and n.endswith(".eblob"):
            for arr in _read_lines(zf.read(n)):
                if isinstance(arr, list) and arr and arr[0] == "BLOB" and len(arr) > 3:
                    proj.blobs[str(arr[1])] = str(arr[3])
        elif n.startswith(prefix + "FONT/") and n.endswith(".efon"):
            try:
                pcb = base64.b64decode(n[len(prefix) + 5:-5]).decode().rsplit("_", 1)[0]
            except ValueError:
                continue
            if pcb in proj.pcbs:
                proj.pcbs[pcb].meta.setdefault("ttf", {}).update(_ttf_boxes(_read_lines(zf.read(n))))
    _add_orphan_boards(proj)
    return proj


def ttf_key(text: str, family: str, font_size: float) -> str:
    return f"{text}\0{family}\0{round(font_size / 10)}"


def _ttf_boxes(lines) -> dict:
    """EasyEDA's rendering of TrueType texts (FONT/<pcb>_0.efon): key -> (x0, x1, baseline, top, box w, box h), mil.

    Version 1.4 records are keyed by the text's record id, 1.6 ones by text, font and size (see ttf_key).  The
    outlines are in mil/10, Y up, from the bottom-left corner of the text box (box height = 1.2 x fontSize)."""
    out = {}
    for arr in lines:
        if not (isinstance(arr, list) and len(arr) >= 5 and arr[0] == "FONT" and isinstance(arr[-1], list)):
            continue
        if len(arr) >= 12:  # 1.6: text, family, size/10, ..., box w, box h, outlines
            key, w, h = f"{arr[1]}\0{arr[2]}\0{arr[3]}", arr[-3], arr[-2]
        else:  # 1.4: record id, box w, box h, outlines
            key, w, h = str(arr[1]), arr[2], arr[3]
        boxes = []
        for c in arr[-1]:
            nums = [v for v in c if isinstance(v, (int, float))]
            if len(nums) >= 4:
                boxes.append((min(nums[0::2]) * 10, max(nums[0::2]) * 10, min(nums[1::2]) * 10, max(nums[1::2]) * 10))
        if not boxes:
            continue
        # baseline: where most outlines end (descenders and inner contours don't)
        base = Counter(round(b[2]) for b in boxes).most_common(1)[0][0]
        out[key] = (min(b[0] for b in boxes), max(b[1] for b in boxes), float(base), max(b[3] for b in boxes),
                    float(w), float(h))
    return out


# ---------------------------------------------------------------------------
# v3
# ---------------------------------------------------------------------------


def parse_epru(data: bytes) -> list[tuple[dict, list[Rec]]]:
    """Return [(dochead, records)] from a v3 .epru/.elibu stream."""
    docs: list[tuple[dict, dict[str, tuple[int, dict, dict]], list[str]]] = []
    cur = None
    line_no = 0
    for raw_line in data.decode("utf-8", errors="replace").splitlines():
        line_no += 1
        line = raw_line.rstrip("\r\n")
        if not line:
            continue
        if line.endswith("|"):
            line = line[:-1]
        sep = line.find("||")
        if sep < 0:
            continue
        try:
            outer = json.loads(line[:sep])
            inner_s = line[sep + 2:]
            inner = json.loads(inner_s) if inner_s else None
        except ValueError:
            continue
        if outer.get("type") == "DOCHEAD":
            cur = (inner or {}, {}, [])
            docs.append(cur)
            continue
        if cur is None:
            continue
        rid = outer.get("id") or f"__line_{line_no}"
        ticket = int(outer.get("ticket") or 0)
        prev = cur[1].get(rid)
        if prev is None:
            cur[2].append(rid)
            cur[1][rid] = (ticket, outer, inner)
        elif ticket >= prev[0]:
            cur[1][rid] = (ticket, outer, inner)
    out = []
    for head, rows, order in docs:
        dt = head.get("docType", "")
        recs = []
        for rid in order:
            _, outer, inner = rows[rid]
            if inner is None:
                continue  # deletion tombstone
            r = norm_v3(outer, inner, dt)
            if r is not None:
                recs.append(r)
        out.append((head, recs))
    return out


def _meta(recs: list[Rec]) -> dict:
    for r in recs:
        if r.type == "META":
            return r.d
    return {}


def _load_v3(zf: zipfile.ZipFile, names: list[str]) -> Project:
    pj = {}
    for n in names:
        if n.rsplit("/", 1)[-1].lower() == "project2.json":
            pj = json.loads(zf.read(n).decode("utf-8"))
    proj = Project(title=pj.get("title") or "project", fmt=3)
    raw_docs: list[tuple[dict, list[Rec]]] = []
    for n in names:
        if n.lower().endswith((".epru", ".elibu")):
            raw_docs.extend(parse_epru(zf.read(n)))
    sch_docs = {}
    pages = []
    boards = {}
    for head, recs in raw_docs:
        dt = head.get("docType")
        uuid = head.get("uuid") or ""
        meta = _meta(recs)
        title = meta.get("title") or uuid
        if dt == "SYMBOL":
            flip_v3_sch_y(recs)
            d = Doc(uuid, title, recs, dict(meta))
            d.meta["symbolType"] = meta.get("docType") or 2
            proj.symbols[uuid] = d
        elif dt == "FOOTPRINT":
            proj.footprints[uuid] = Doc(uuid, title, recs, dict(meta))
        elif dt == "DEVICE":
            attrs = meta.get("attributes") or {}
            proj.devices[uuid] = {
                "title": title,
                "attributes": {k: ("" if v is None else v if isinstance(v, str) else json.dumps(v) if isinstance(v, (list, dict)) else str(v)) for k, v in attrs.items()},
                "description": meta.get("description") or "",
            }
        elif dt == "SCH":
            sch_docs[uuid] = (title, meta)
        elif dt == "SCH_PAGE":
            flip_v3_sch_y(recs)
            recs = assemble_v3_wires(recs)
            pages.append((uuid, title, meta, recs))
        elif dt == "PCB":
            proj.pcbs[uuid] = Doc(uuid, title, recs, dict(meta))
        elif dt == "BOARD":
            boards[uuid] = title
        elif dt == "BLOB":
            for r in recs:
                if r.type == "BLOB":
                    url = r.d.get("url") or r.d.get("data") or r.d.get("content") or ""
                    proj.blobs[r.id] = url
    for uuid, (title, meta) in sch_docs.items():
        proj.schematics[uuid] = Schematic(uuid, title)
    for k, (uuid, title, meta, recs) in enumerate(pages):
        sid = meta.get("schematic")
        sch = proj.schematics.get(sid)
        if sch is None:
            sch = proj.schematics.setdefault(sid or "default", Schematic(sid or "default", "Schematic"))
        zi = meta.get("zIndex")
        idx = int(zi) if isinstance(zi, (int, float)) else len(sch.pages) + 1
        sch.pages.append(Page(uuid, title, idx, Doc(uuid, title, recs, dict(meta))))
    for sch in proj.schematics.values():
        sch.pages.sort(key=lambda p: p.index)
    # boards: link schematic + pcb via meta.board
    for buuid, btitle in boards.items():
        s = next((u for u, (t, m) in sch_docs.items() if m.get("board") == buuid), None)
        p = next((u for u, d in proj.pcbs.items() if d.meta.get("board") == buuid), None)
        proj.boards.append(Board(btitle, s, p))
    _add_orphan_boards(proj)
    return proj


def _add_orphan_boards(proj: Project) -> None:
    used_s = {b.schematic for b in proj.boards}
    used_p = {b.pcb for b in proj.boards}
    for u, p in proj.pcbs.items():
        if u not in used_p:
            proj.boards.append(Board(p.title, None, u))
    for u, s in proj.schematics.items():
        if u not in used_s and s.pages:
            proj.boards.append(Board(s.name, u, None))


# ---------------------------------------------------------------------------
# Libraries (.elibz / .elibz2) – used by the LCSC tool too
# ---------------------------------------------------------------------------


def load_library(path: str) -> Project:
    with open(path, "rb") as f:
        raw = f.read()
    zf = zipfile.ZipFile(io.BytesIO(raw))
    names = zf.namelist()
    if any(n.lower().endswith(".elibu") for n in names):
        p = _load_v3(zf, names)
        p.source = path
        return p
    # v2 library: symbol.json/footprint.json/device.json + SYMBOL/FOOTPRINT dirs
    pj = {}
    for key in ("symbols", "footprints", "devices"):
        for n in names:
            if n.rsplit("/", 1)[-1].lower() in (key[:-1] + ".json", key + ".json"):
                try:
                    pj.setdefault(key, {}).update(json.loads(zf.read(n).decode("utf-8")).get(key, {}))
                except ValueError:
                    pass
    proj = Project(title=os.path.splitext(os.path.basename(path))[0], fmt=2)
    nameset = set(names)
    for n in names:
        if n.endswith(".esym"):
            uuid = n.rsplit("/", 1)[-1][:-5]
            info = (pj.get("symbols") or {}).get(uuid, {})
            d = _v2_doc(zf, n, uuid, info.get("title") or uuid, "sym")
            d.meta.setdefault("symbolType", info.get("type", d.meta.get("symbolType", 2)))
            proj.symbols[uuid] = d
        elif n.endswith(".efoo"):
            uuid = n.rsplit("/", 1)[-1][:-5]
            info = (pj.get("footprints") or {}).get(uuid, {})
            proj.footprints[uuid] = _v2_doc(zf, n, uuid, info.get("title") or uuid, "fp")
    for uuid, info in (pj.get("devices") or {}).items():
        proj.devices[uuid] = {"title": info.get("title") or uuid, "attributes": info.get("attributes") or {},
                              "description": info.get("description") or ""}
    del nameset
    proj.source = path
    return proj
