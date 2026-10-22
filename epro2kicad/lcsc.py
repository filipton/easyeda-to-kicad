"""Quick LCSC part import: fetch a part by its LCSC number (C2040, ...) from the EasyEDA Pro library,
translate symbol / footprint / 3D model with the same converters as the project translator, add them to the
KiCad project's own libraries and put a ready-to-paste symbol on the clipboard.

    python -m epro2kicad.lcsc C2040 C25804 [--project DIR]

In Eeschema just press Ctrl+V (Cmd+V) afterwards: the part hangs on the cursor like in EasyEDA.  It is also
in the project library "LCSC", so "Add Symbol" (A) and typing the C-number finds it too.
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import math
import os
import platform
import re
import subprocess
import sys
import urllib.parse
import urllib.request

from .easyeda.project import parse_v2_doc
from .kicad import layers as L
from .kicad.board import part_fields, resolve_attr, resolve_value
from .kicad.footprint import FpOptions, Model3D, convert_footprint, footprint_lib_sexp, sane_name
from .kicad.symbol import U, convert_symbol, effects, lib_symbol_sexp, r4
from .stock import choose, stock_symbol
from .sexp import NO, YES, Sym, dumps, loads, stable_uuid

API = "https://pro.easyeda.com/api"
MODELS = "https://modules.easyeda.com/qAxj6KHrDKw4blvCG8QJPs7Y/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/129.0 Safari/537.36")
LIB = "LCSC"
# components and STEP models are addressed by uuid and never change, so they're kept across imports
CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "epro2kicad")


class LcscError(Exception):
    pass


# --------------------------------------------------------------------------- fetching


_SSL = None


def _ssl_context():
    """Python builds without a CA bundle (KiCad's bundled one on macOS) fall back to the system file."""
    global _SSL
    if _SSL is None:
        import ssl
        paths = ssl.get_default_verify_paths()
        have = any(x and os.path.exists(x) for x in (paths.cafile, paths.openssl_cafile))
        cafile = None
        if not have:
            try:
                import certifi
                cafile = certifi.where()
            except ImportError:
                cafile = next((f for f in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt")
                               if os.path.exists(f)), None)
        _SSL = ssl.create_default_context(cafile=cafile)
    return _SSL


def _get(url: str, data: dict | None = None, raw: bool = False, timeout: int = 30):
    body = urllib.parse.urlencode(data, doseq=True).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": UA, "Accept": "application/json, */*",
                                                          "Accept-Encoding": "gzip"})  # search answers shrink ~5x
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as r:
            payload = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                payload = gzip.decompress(payload)
    except OSError as e:
        raise LcscError(f"request failed: {url}: {e}") from e
    if raw:
        return payload
    try:
        j = json.loads(payload)
    except ValueError as e:
        raise LcscError(f"unexpected answer from {url}") from e
    if not j.get("success", True):
        raise LcscError(f"EasyEDA API error for {url}: {j.get('message') or j.get('code')}")
    return j.get("result")


def fetch_device(code: str) -> dict:
    code = code.strip().upper()
    if not re.fullmatch(r"C\d+", code):
        raise LcscError(f"'{code}' is not an LCSC part number (C followed by digits)")
    res = _get(f"{API}/devices/searchByCodes", {"codes[]": code})
    if not res:
        raise LcscError(f"{code}: not found in the EasyEDA library")
    return res[0]


def _cached(kind: str, key: str, fetch) -> bytes:
    path = os.path.join(CACHE, kind, re.sub(r"[^\w.-]", "_", key))
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        pass
    data = fetch()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)  # atomic: parallel imports never see half a file
    except OSError:
        pass  # no cache is fine
    return data


def fetch_component(uuid: str) -> dict:
    def get():
        res = _get(f"{API}/components/{uuid}")
        if not res:
            raise LcscError(f"component {uuid} not found")
        return json.dumps(res).encode()
    return json.loads(_cached("components", uuid, get))


def find_model(title: str) -> str:
    """uuid of the LCSC library's 3D model with exactly this title (models get new uuids over time, titles stay)."""
    res = _get(f"{API}/components/search", {"wd": title, "docType": 16}) or {}
    for c in (res.get("lists") or {}).get("lcsc") or []:
        if c.get("docType") == 16 and (c.get("display_title") or "").lower() == title.lower():
            return c["uuid"]
    raise LcscError(f"3D model '{title}' not in the library")


def fetch_model(model_id: str) -> bytes:
    def get():
        step = _get(MODELS + model_id, raw=True, timeout=60)
        if not step.startswith(b"ISO-10303-21"):
            raise LcscError("STEP not available")
        return step
    return _cached("models", f"{model_id}.step", get)


def _doc(comp: dict, kind: str):
    ds = comp.get("dataStr")
    if not ds:
        raise LcscError(f"{comp.get('display_title') or comp.get('uuid')}: library data not available")
    return parse_v2_doc(ds, comp["uuid"], comp.get("display_title") or comp.get("title") or comp["uuid"], kind)


# --------------------------------------------------------------------------- project libraries


def find_project(start: str) -> str:
    """Folder that holds the .kicad_pro (start itself or a parent)."""
    d = os.path.abspath(start)
    while True:
        if glob.glob(os.path.join(d, "*.kicad_pro")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            raise LcscError(f"no KiCad project (.kicad_pro) in {start} or its parents; use --project")
        d = parent


def _ensure_lib_table(proj_dir: str, kind: str, nick: str, uri: str, descr: str) -> None:
    path = os.path.join(proj_dir, "sym-lib-table" if kind == "sym" else "fp-lib-table")
    root = "sym_lib_table" if kind == "sym" else "fp_lib_table"
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            tree = loads(f.read())
        for e in tree[1:]:
            if isinstance(e, list) and e and str(e[0]) == "lib":
                name = next((x[1] for x in e if isinstance(x, list) and str(x[0]) == "name"), None)
                if name == nick:
                    return
    else:
        tree = [Sym(root), [Sym("version"), 7]]
    tree.append([Sym("lib"), [Sym("name"), nick], [Sym("type"), "KiCad"], [Sym("uri"), uri],
                 [Sym("options"), ""], [Sym("descr"), descr]])
    with open(path, "w", encoding="utf-8") as f:
        f.write(dumps(tree) + "\n")


def _update_symbol_lib(path: str, node: list) -> None:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            tree = loads(f.read())
    else:
        tree = [Sym("kicad_symbol_lib"), [Sym("version"), 20251024], [Sym("generator"), "kicad_symbol_editor"],
                [Sym("generator_version"), "10.0"]]
    name = node[1]
    tree = [x for x in tree if not (isinstance(x, list) and x and str(x[0]) == "symbol" and x[1] == name)]
    tree.append(node)
    with open(path, "w", encoding="utf-8") as f:
        f.write(dumps(tree) + "\n")


# --------------------------------------------------------------------------- import


def fetch_step(attrs: dict) -> bytes:
    comp = fetch_component(attrs["3D Model"])
    try:
        info = json.loads(comp.get("dataStr") or "{}")
    except ValueError:
        info = {}
    model_id = info.get("model") or comp.get("3d_model_uuid")
    if not model_id:
        raise LcscError("no model id")
    return fetch_model(model_id)


def build_part(code: str, fp_lib: str, model_path, with_3d: bool = True, dev: dict | None = None) -> dict:
    """Fetch and convert one LCSC part, nothing written to disk.
    model_path(fp_name) -> path string stored in the footprint for its STEP model."""
    dev = dev or fetch_device(code)
    attrs = {k: ("" if v is None else str(v)) for k, v in (dev.get("attributes") or {}).items()}
    sym_uuid, fp_uuid = attrs.get("Symbol"), attrs.get("Footprint")
    if not sym_uuid or not fp_uuid:
        raise LcscError(f"{code}: device has no symbol/footprint")
    mpn = attrs.get("Manufacturer Part") or code
    sym_name = sane_name(f"{mpn}") or code
    sym_doc = _doc(fetch_component(sym_uuid), "sym")
    fp_comp = fetch_component(fp_uuid)
    fp_doc = _doc(fp_comp, "fp")
    fp_name = sane_name(fp_comp.get("display_title") or fp_doc.title) or code

    fp = convert_footprint(fp_doc, L.LayerMap(), FpOptions(), name=fp_name)
    fp.descr = f"{fp_name} (LCSC {code})"
    step, model_note = None, ""
    if with_3d and attrs.get("3D Model"):
        try:
            step = fetch_step(attrs)
            fp.models = [model_from_transform(model_path(fp_name), attrs.get("3D Model Transform", ""),
                                              step_extent(step))]
        except LcscError as e:
            model_note = f"3D model skipped ({e})"

    ls = convert_symbol(sym_doc, sym_name)
    ls.ref_prefix = re.sub(r"[?\d]+$", "", attrs.get("Designator") or "") or ls.ref_prefix
    value = resolve_value(attrs, {}, mpn) or mpn
    desc = attrs.get("Description") or ""
    if not desc or desc.startswith("="):
        desc = " ".join(attrs.get(k) for k in ("LCSC Part Name", "Supplier Footprint") if attrs.get(k)) or mpn
    ds = attrs.get("Datasheet", "")
    extra = dict(part_fields(attrs), LCSC=code)
    extra["ki_keywords"] = " ".join(x for x in (code, mpn, attrs.get("Supplier Footprint", "")) if x)
    node = lib_symbol_sexp(ls, None, value, f"{fp_lib}:{fp_name}", ds if not ds.startswith("=") else "", desc, extra)
    choice = choose(attrs, ls, sym_doc)
    std = stock_symbol(attrs, ls, node, sym_doc, choice) if choice else None
    return {"code": code, "mpn": mpn, "ls": ls, "node": std or node, "node_easyeda": node, "value": value,
            "stock": f"{choice[0]}:{choice[1]}" if std else "", "sym_name": sym_name,
            "fp": fp, "fp_name": fp_name, "step": step, "model_note": model_note}


def import_part(code: str, proj_dir: str, lib: str = LIB, with_3d: bool = True, log=print) -> dict:
    p = build_part(code, lib, lambda n: f"${{KIPRJMOD}}/{lib}.3dshapes/{n}.step", with_3d)
    fp_name, sym_name = p["fp_name"], p["sym_name"]
    os.makedirs(os.path.join(proj_dir, f"{lib}.pretty"), exist_ok=True)
    with open(os.path.join(proj_dir, f"{lib}.pretty", f"{fp_name}.kicad_mod"), "w", encoding="utf-8") as f:
        f.write(dumps(footprint_lib_sexp(p["fp"])) + "\n")
    if p["step"]:
        os.makedirs(os.path.join(proj_dir, f"{lib}.3dshapes"), exist_ok=True)
        with open(os.path.join(proj_dir, f"{lib}.3dshapes", f"{fp_name}.step"), "wb") as f:
            f.write(p["step"])
    _update_symbol_lib(os.path.join(proj_dir, f"{lib}.kicad_sym"), p["node"])
    _ensure_lib_table(proj_dir, "sym", lib, f"${{KIPRJMOD}}/{lib}.kicad_sym", "Parts imported from LCSC/EasyEDA")
    _ensure_lib_table(proj_dir, "fp", lib, f"${{KIPRJMOD}}/{lib}.pretty", "Parts imported from LCSC/EasyEDA")
    clip = clipboard_snippet(p["ls"], p["node"], lib, p["value"], code)
    note = p["model_note"] or ("3D model added" if p["step"] else "")
    log(f"{code}: {p['mpn']} -> {lib}:{sym_name} / {lib}:{fp_name}" + (f"  [{note}]" if note else ""))
    return {"code": code, "symbol": f"{lib}:{sym_name}", "footprint": f"{lib}:{fp_name}", "clipboard": clip}


_PT = re.compile(rb"#(\d+)\s*=\s*CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*([-\d.Ee+]+)\s*,\s*([-\d.Ee+]+)\s*,\s*([-\d.Ee+]+)\s*\)")
_VX = re.compile(rb"VERTEX_POINT\s*\(\s*'[^']*'\s*,\s*#(\d+)\s*\)")


def step_extent(step: bytes):
    """(centre x, centre y, bottom z) of a STEP model in its own frame (mm), from its B-rep vertices; None if unknown.
    The vertex box matches the size EasyEDA stores in "3D Model Transform" (checked on headers and a USB-C)."""
    pts = {m.group(1): m.groups()[1:] for m in _PT.finditer(step)}
    v = [tuple(map(float, pts[i])) for i in _VX.findall(step) if i in pts]
    if not v:
        return None
    xs, ys, zs = zip(*v)
    return ((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2, min(zs))


def model_from_transform(path: str, transform: str, extent=None, rz_sign: float = -1.0) -> Model3D:
    """EasyEDA "3D Model Transform" = sizeX, sizeY, -, rotation, -, -, offX, offY, offZ (mil, degrees).
    EasyEDA centres the model's box on the footprint and stands its bottom on the board, then applies the offsets;
    KiCad uses the STEP's own origin, so `extent` (step_extent of the file) moves it there. Rotation has the
    opposite sense in KiCad (checked on SOT-23)."""
    t = [float(x) for x in re.split(r"[,\s]+", (transform or "").strip()) if x] + [0.0] * 9
    rz = rz_sign * t[3]
    ox, oy, oz = t[6] * 0.0254, t[7] * 0.0254, t[8] * 0.0254
    if extent:
        cx, cy, z0 = extent
        a = math.radians(rz)  # KiCad rotates the model about its origin before offsetting it
        ox -= cx * math.cos(a) - cy * math.sin(a)
        oy -= cx * math.sin(a) + cy * math.cos(a)
        oz -= z0
    return Model3D(path, offset=(r4(ox), r4(oy), r4(oz)), rotate=(0.0, 0.0, r4(rz)))


# --------------------------------------------------------------------------- clipboard


def clipboard_snippet(ls, lib_node: list, lib: str, value: str, code: str) -> str:
    """Eeschema clipboard content (same layout eeschema itself copies): lib_symbols + one symbol."""
    full = f"{lib}:{ls.name}"
    libdef = [Sym("symbol"), full] + lib_node[2:]
    uid = stable_uuid("lcsc-paste", code)

    def prop(name, val, y, hide=False):
        n = [Sym("property"), name, val, [Sym("at"), 0, r4(y), 0]]
        if hide:
            n.append([Sym("hide"), YES])
        return n + [[Sym("show_name"), NO], [Sym("do_not_autoplace"), NO], effects()]

    bb = ls.bbox.get(1, (-5, -5, 5, 5))
    props = {}
    for x in lib_node:
        if isinstance(x, list) and x and str(x[0]) == "property":
            props[x[1]] = x[2]
    inst = [Sym("symbol"), [Sym("lib_id"), full], [Sym("at"), 0, 0, 0], [Sym("unit"), 1], [Sym("body_style"), 1],
            [Sym("exclude_from_sim"), NO], [Sym("in_bom"), YES], [Sym("on_board"), YES], [Sym("in_pos_files"), YES],
            [Sym("dnp"), NO], [Sym("uuid"), uid],
            prop("Reference", f"{ls.ref_prefix}?", -bb[3] * U - 1.27),
            prop("Value", value, -bb[1] * U + 1.27)]
    for k, v in props.items():
        if k not in ("Reference", "Value") and not k.startswith("ki_"):
            inst.append(prop(k, v, 0, hide=True))
    for p in sorted({p.number for p in ls.pins_of(1) if p.number}):
        inst.append([Sym("pin"), p, [Sym("uuid"), stable_uuid(uid, p)]])
    return dumps([Sym("lib_symbols"), libdef]) + "\n" + dumps(inst) + "\n"


def copy_to_clipboard(text: str) -> bool:
    system = platform.system()
    cmds = {"Darwin": [["pbcopy"]], "Windows": [["clip"]]}.get(
        system, [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]])
    for c in cmds:
        try:
            subprocess.run(c, input=text.encode("utf-8"), check=True, capture_output=True)
            return True
        except (OSError, subprocess.CalledProcessError):
            continue
    return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="epro2kicad.lcsc", description=__doc__.split("\n\n")[0])
    ap.add_argument("codes", nargs="+", help="LCSC part numbers, e.g. C2040")
    ap.add_argument("-p", "--project", default=".", help="KiCad project folder (default: current / parents)")
    ap.add_argument("--lib", default=LIB, help=f"library nickname (default {LIB})")
    ap.add_argument("--no-3d", action="store_true", help="skip the STEP model")
    ap.add_argument("--no-clipboard", action="store_true")
    a = ap.parse_args(argv)
    try:
        proj_dir = find_project(a.project)
    except LcscError as e:
        print(e, file=sys.stderr)
        return 2
    clips, rc = [], 0
    for code in a.codes:
        try:
            r = import_part(code, proj_dir, a.lib, with_3d=not a.no_3d)
            clips.append(r["clipboard"])
        except LcscError as e:
            print(f"{code}: {e}", file=sys.stderr)
            rc = 1
    if clips and not a.no_clipboard:
        # several parts: paste them as one group (offset so they do not overlap)
        text = clips[0] if len(clips) == 1 else _merge_clips(clips)
        if copy_to_clipboard(text):
            print("Symbol copied - press Ctrl+V / Cmd+V in the schematic editor to place it.")
        else:
            print("(clipboard tool not found; the parts are in the project library - press A and type the C-number)")
    return rc


def _merge_clips(clips: list[str]) -> str:
    libs, insts = [], []
    for i, c in enumerate(clips):
        trees = loads("(x " + c + ")")[1:]
        libs += trees[0][1:]
        inst = trees[1]
        for x in inst:
            if isinstance(x, list) and x and str(x[0]) == "at":
                x[1] = r4(i * 15.24)
        insts.append(inst)
    return dumps([Sym("lib_symbols")] + libs) + "\n" + "\n".join(dumps(x) for x in insts) + "\n"


if __name__ == "__main__":
    sys.exit(main())
