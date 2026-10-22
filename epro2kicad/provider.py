"""LCSC parts inside KiCad's "Remote Symbols" panel (KiCad 10).

A small local provider on http://127.0.0.1:61840.  KiCad shows our page (panel.html) in the schematic editor's
Remote Symbols panel: category tree, keyword search, parametric filters (like EasyEDA's LCSC library), a detail
view with KiCad-rendered symbol/footprint previews, and "Place".  Placing sends KiCad a manifest; KiCad then
downloads symbol, footprint and STEP from here itself (size + sha256 checked), so big parts are no problem.

    python -m epro2kicad.provider            # run it (the plugin / installer start it for you)
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import lcsc
from .kicad.footprint import footprint_lib_sexp
from .sexp import Sym, dumps

HOST, PORT = "127.0.0.1", 61840
BASE = f"http://{HOST}:{PORT}"
LIBRARY = "lcsc"  # KiCad turns this into the nickname "<prefix>_lcsc"
PAGE_SIZE = 30  # LCSC answers smaller pages faster; the panel loads more on scroll
CTYPES = {"symbol": "application/x-kicad-symbol", "footprint": "application/x-kicad-footprint",
          "3dmodel": "model/step"}


def metadata() -> dict:
    return {
        "provider_name": "LCSC / EasyEDA (epro2kicad)",
        "provider_version": "1.1",
        "api_base_url": BASE,
        "panel_url": BASE + "/panel",
        "allow_insecure_localhost": True,
        "auth": {"type": "none"},
        "capabilities": {"web_ui_v1": True, "direct_downloads_v1": True, "inline_payloads_v1": True},
        "max_download_bytes": 256 * 1024 * 1024,
        "supported_asset_types": ["symbol", "footprint", "3dmodel"],
    }


# --------------------------------------------------------------------------- KiCad settings


def kicad_config_dir() -> str:
    s = platform.system()
    if s == "Darwin":
        return os.path.expanduser("~/Library/Preferences/kicad/10.0")
    if s == "Windows":
        return os.path.join(os.environ.get("APPDATA", ""), "kicad", "10.0")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "kicad", "10.0")


def remote_settings() -> dict:
    try:
        with open(os.path.join(kicad_config_dir(), "eeschema.json"), encoding="utf-8") as f:
            return json.load(f).get("remote_symbols") or {}
    except (OSError, ValueError):
        return {}


def kicad_sanitize(s: str, lower: bool = False) -> str:
    """Same rule KiCad applies to remote file/library names."""
    s = re.sub(r"[^0-9A-Za-z_.\-]", "_", s.strip())
    return s.lower() if lower else s


def model_path(fp_name: str) -> str:
    """Where KiCad stores the STEP we hand it (destination_dir/<prefix>_3d/<name>)."""
    rs = remote_settings()
    dest = (rs.get("destination_dir") or "${KIPRJMOD}/RemoteLibrary").rstrip("/\\")
    prefix = kicad_sanitize(rs.get("library_prefix") or "remote", lower=True) or "remote"
    return f"{dest}/{prefix}_3d/{kicad_sanitize(fp_name)}.step"


def _recent_projects() -> list:
    """Project folders KiCad has open or opened lately (from its settings)."""
    dirs = []
    for name in ("kicad.json", "eeschema.json"):
        try:
            with open(os.path.join(kicad_config_dir(), name), encoding="utf-8") as f:
                sysd = json.load(f).get("system") or {}
        except (OSError, ValueError):
            continue
        dirs += [os.path.dirname(p) for p in (sysd.get("open_projects") or []) + (sysd.get("file_history") or [])[:10]]
    return list(dict.fromkeys(d for d in dirs if d))


def _just_written(fp_name: str) -> list:
    """Project folders where KiCad has just saved this footprint (Spotlight; finds projects not in the history)."""
    if platform.system() != "Darwin":
        return []
    q = f'kMDItemFSName == "{kicad_sanitize(fp_name)}.kicad_mod" && kMDItemFSContentChangeDate >= $time.now(-300)'
    try:
        out = subprocess.run(["mdfind", "-onlyin", os.path.expanduser("~"), q], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    # <project>/<dest>/footprints/<lib>.pretty/<fp>.kicad_mod: the project is the folder holding the tables
    found = []
    for f in out.splitlines():
        d = os.path.dirname(f)
        while d != os.path.dirname(d) and not os.path.exists(os.path.join(d, "sym-lib-table")):
            d = os.path.dirname(d)
        if os.path.exists(os.path.join(d, "sym-lib-table")):
            found.append(d)
    return found


def relative_lib_tables(proj: str) -> bool:
    """KiCad writes the remote library into the project's tables with an absolute path (and again on every Place),
    so the project breaks when moved or opened elsewhere.  Point the entries under the project at ${KIPRJMOD}."""
    changed = False
    root = os.path.realpath(proj).rstrip("/")
    dest = (remote_settings().get("destination_dir") or "${KIPRJMOD}/RemoteLibrary").rstrip("/\\")
    if not dest.startswith("${KIPRJMOD}/"):
        return False  # libraries kept outside the project: absolute is right
    dest = dest[len("${KIPRJMOD}/"):]
    for name in ("sym-lib-table", "fp-lib-table"):
        path = os.path.join(proj, name)
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        def fix(m):
            real = os.path.realpath(m.group(2))
            rel = os.path.relpath(real, root) if (real + "/").startswith(root + "/") else ""
            if not rel.startswith(dest + "/"):
                return m.group(0)  # not the remote library inside this project: leave it
            return f'{m.group(1)}${{KIPRJMOD}}/{rel}{"/" if m.group(2).endswith("/") else ""}")'
        new = re.sub(r'(\(uri ")(/[^"]+)"\)', fix, text)
        if new != text:
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                f.write(new)
            os.replace(path + ".tmp", path)
            changed = True
    return changed


def fix_project_tables(fp_name: str) -> None:
    """After a Place: make the libraries KiCad registered portable (runs in the background, KiCad saves first)."""
    for wait in (1, 4, 15):
        time.sleep(wait)
        for proj in dict.fromkeys(_just_written(fp_name) + _recent_projects()):
            try:
                if relative_lib_tables(proj):
                    print(f"library paths made relative in {proj}", flush=True)
            except OSError:
                pass


# --------------------------------------------------------------------------- catalogue


_tree = None


def categories() -> list:
    global _tree
    if _tree is None:
        res = lcsc._get(f"{lcsc.API}/eda/catalog/tree?path=lcsc") or []
        _tree = [{"id": c["catalogId"], "name": c.get("catalogNameEn") or c.get("catalogName"),
                  "children": sorted(({"id": s["catalogId"], "name": s.get("catalogNameEn") or s.get("catalogName")}
                                      for s in c.get("sonList") or []), key=lambda x: x["name"].lower())}
                 for c in res]
        _tree.sort(key=lambda x: x["name"].lower())
    return _tree


def category_id(name: str):
    for c in categories():
        if c["name"] == name:
            return c["id"]
        for s in c["children"]:
            if s["name"] == name:
                return s["id"]
    return None


# device attributes that aren't electrical specs (ids, supplier bookkeeping, things the list shows elsewhere)
_NOT_SPECS = {"LCSC Part Name", "Supplier Part", "Manufacturer", "Manufacturer Part", "Supplier Footprint",
              "JLCPCB Part Class", "Datasheet", "Supplier", "Add into BOM", "Convert to PCB", "Symbol", "Designator",
              "Footprint", "3D Model", "3D Model Title", "3D Model Transform", "Name", "Description"}


def _specs(attrs: dict) -> list:
    """[[name, value]] for the list, the part's value first (10uF, then ±10%, 10V, X5R)."""
    out = [[k, str(v)] for k, v in attrs.items()
           if k not in _NOT_SPECS and v not in (None, "", "-") and not str(v).startswith("=")]
    return sorted(out, key=lambda kv: kv[0] != "Value")


_searches: dict[str, tuple[float, dict]] = {}  # LCSC takes ~2 s per search: repeats (back, undo a filter) are instant
_searching: dict[str, threading.Event] = {}  # searches in flight
_searching_lock = threading.Lock()


SORTS = {"stock": ("stock", "desc"), "price": ("price", "asc"), "price-desc": ("price", "desc")}


SEARCH_DIR = os.path.join(lcsc.CACHE, "search")
FRESH = 600  # answers younger than this are used as they are; older ones are shown and refreshed by the panel


def _cache_get(key: str):
    hit = _searches.get(key)
    if hit:
        return hit
    try:
        with gzip.open(os.path.join(SEARCH_DIR, hashlib.sha1(key.encode()).hexdigest() + ".json.gz")) as f:
            hit = tuple(json.load(f))
    except (OSError, ValueError):
        return None
    _remember(hit[1])
    _searches[key] = hit
    return hit


def _cache_put(key: str, res: dict) -> None:
    hit = (time.time(), res)
    if len(_searches) > 300:
        _searches.clear()
    _searches[key] = hit
    try:
        os.makedirs(SEARCH_DIR, exist_ok=True)
        path = os.path.join(SEARCH_DIR, hashlib.sha1(key.encode()).hexdigest() + ".json.gz")
        with gzip.open(path + ".tmp", "wt", encoding="utf-8") as f:
            json.dump(hit, f)
        os.replace(path + ".tmp", path)
    except OSError:
        pass  # no disk cache is fine


def _remember(res: dict) -> None:
    """Links and categories that a live search notes for each part (a cached answer skips that)."""
    for p in res.get("parts") or []:
        _category.setdefault(p["code"], p.get("category") or "")
        _pages.setdefault(p["code"], p.get("url") or "")
        if p.get("datasheet"):
            _datasheets.setdefault(p["code"], p["datasheet"])


def search(q: str = "", catalog: str = "", filters: dict | None = None, page: int = 1, sort: str = "",
           fresh: bool = False) -> dict:
    """Answers come from memory or the disk cache when there; 'age' says how old (the panel refreshes old ones
    with fresh=True for current stock and prices)."""
    key = json.dumps([q, catalog, filters, page, sort], sort_keys=True)
    with _searching_lock:
        hit = None if fresh else _cache_get(key)
        if hit:
            return dict(hit[1], age=time.time() - hit[0])
        # the panel prefetches (hovered category or filter value, next page): a click on the same thing waits for
        # that request instead of starting its own ~2 s one
        ev, owner = _searching.get(key), False
        if ev is None:
            ev, owner = _searching.setdefault(key, threading.Event()), True
    if not owner:
        ev.wait(60)
        hit = _searches.get(key)
        return dict(hit[1], age=time.time() - hit[0]) if hit else _search(q, catalog, filters, page, sort)
    try:
        res = _search(q, catalog, filters, page, sort)
        with _searching_lock:
            _cache_put(key, res)
        return dict(res, age=0)
    finally:
        with _searching_lock:
            _searching.pop(key, None)
        ev.set()


# Package lists are alphabetical (3000+ values for capacitors), so a click on 0603 can't be guessed from the list:
# warm the packages people actually pick.  Each one is a full LCSC search (~2 s) returning the narrowed lists of the
# other filters (Capacitance for 0603, ...), so after a few seconds those clicks come from the cache.
COMMON_PACKAGES = ["0603", "0402", "0805", "1206", "0201", "1210", "SOT-23", "SOT-23-3", "SOT-23-5", "SOT-23-6",
                   "SOD-123", "SOD-323", "SOT-223", "SOIC-8", "SOP-8", "TSSOP-8"]
_warm_gen = [0]
WARM_EVERY = 7 * 86400
# the passive categories (top and the usual SMD subcategories) -> their value filter and the values people use
_CAPS = ["10pF", "22pF", "100pF", "1nF", "10nF", "100nF", "1uF", "2.2uF", "4.7uF", "10uF", "22uF", "47uF", "100uF"]
_RES = ["0Ω", "10Ω", "22Ω", "33Ω", "47Ω", "100Ω", "220Ω", "330Ω", "470Ω", "1kΩ", "2.2kΩ", "4.7kΩ", "5.1kΩ", "10kΩ",
        "47kΩ", "100kΩ", "1MΩ"]
_INDS = ["1uH", "2.2uH", "4.7uH", "10uH", "22uH", "47uH", "100uH"]
PASSIVES = {"312": ("Capacitance", _CAPS), "313": ("Capacitance", _CAPS), "308": ("Resistance", _RES),
            "439": ("Resistance", _RES), "316": ("Inductance", _INDS), "337": ("Inductance", _INDS),
            "544": ("Inductance", _INDS)}
PASSIVE_PACKAGES = ["0402", "0603", "0805", "1206"]


def warm_packages(q: str, catalog: str, filters: dict, sort: str, facets: list) -> None:
    """After a search the user made, load the common Package values on top of it (2 at a time, newest search
    only: a newer search stops the older one's warming)."""
    if (filters or {}).get("Package"):
        return
    offered = next((set(f["values"]) for f in facets if f["name"] == "Package"), set())
    todo = [v for v in COMMON_PACKAGES if v in offered][:12]
    _warm_gen[0] += 1
    gen = _warm_gen[0]

    def one(v):
        if _warm_gen[0] == gen:
            try:
                search(q, catalog, dict(filters or {}, Package=[v]), 1, sort)
            except Exception:
                pass
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as ex:
        list(ex.map(one, todo))


def _warm(catalog: str, filters: dict | None = None) -> dict:
    """One background search, reloaded once it's a week old (the panel refreshes stock and prices itself)."""
    filters = filters or {}
    hit = _cache_get(json.dumps(["", catalog, filters, 1, ""], sort_keys=True))
    if hit and time.time() - hit[0] < WARM_EVERY:
        return hit[1]
    try:
        return search("", catalog, filters, 1, "", fresh=True)
    except Exception:
        return {}


def _offered(res: dict, name: str, wanted: list) -> list:
    have = next((set(f["values"]) for f in res.get("facets") or [] if f["name"] == name), set())
    return [v for v in wanted if v in have]


def warm_categories() -> None:
    """Pre-cache only the most used searches, so the panel answers from disk instead of LCSC (~2 s each): the
    passives' common values within their common packages (0603 → 100nF) and the first page of each top category.
    About 250 searches on the first start, then only what's older than a week; everything else is cached as it's
    used (warm_packages, the panel's hover and next-page prefetches)."""
    time.sleep(10)
    try:
        cats = categories()
    except lcsc.LcscError:
        return
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(3) as ex:  # gently: LCSC is a shared service
        def run(fn, items):
            return list(ex.map(fn, items))
        # passives first: that's where most picking happens
        for cat, (name, values) in PASSIVES.items():
            res = _warm(cat)
            for pkg in _offered(res, "Package", PASSIVE_PACKAGES):
                narrowed = _warm(cat, {"Package": [pkg]})
                run(lambda v, pkg=pkg: _warm(cat, {"Package": [pkg], name: [v]}), _offered(narrowed, name, values))
        run(lambda c: _warm(str(c["id"])), cats)


def prune_cache(max_files: int = 20000) -> None:
    try:
        files = sorted((os.path.join(SEARCH_DIR, f) for f in os.listdir(SEARCH_DIR)), key=os.path.getmtime)
    except OSError:
        return
    for f in files[:-max_files]:
        try:
            os.remove(f)
        except OSError:
            pass


def _search(q: str = "", catalog: str = "", filters: dict | None = None, page: int = 1, sort: str = "") -> dict:
    """Keyword and/or category search with EasyEDA's facets.  filters: {facet name: [values]}.
    sort (SORTS) only within a category: a keyword search matches loosely (any word), and sorting it puts parts
    that merely share "0603" first, so the panel sorts those results itself."""
    filters = {k: v for k, v in (filters or {}).items() if v}
    cats = filters.pop("Category", None)
    if cats and not catalog:  # picking a category facet switches to that category (full parametric facets)
        ids = [str(category_id(c)) for c in cats if category_id(c)]
        catalog = "$".join(ids)
    # keyword search ignores currPage (always the first page), so later pages ask for a bigger first page
    # and keep the new tail; category listings page normally
    grow = not catalog
    params = {"currPage": 1 if grow else page, "pageSize": PAGE_SIZE * page if grow else PAGE_SIZE,
              "needAggs": "true"}
    if q:
        params["keyword"] = q
    if catalog:
        params["catalog"] = catalog
        if sort in SORTS:
            params["sortField"], params["sortType"] = SORTS[sort]
    other = []
    for name, values in filters.items():
        if name == "Package":
            params["standard"] = "$".join(values)
        elif name == "Manufacturer":
            params["brand"] = "$".join(values)
        else:
            other += [f"{v}@{name}" for v in values]
    if other:
        params["param"] = "$".join(other)
    endpoint = "v2/eda/product/list" if catalog else "v2/eda/product/search"
    res = lcsc._get(f"{lcsc.API}/{endpoint}?" + urllib.parse.urlencode(params)) or {}
    parts = []
    for p in (res.get("productList") or [])[PAGE_SIZE * (page - 1) if grow else 0:]:
        imgs = p.get("image") or []
        img = imgs[0] if imgs and isinstance(imgs[0], dict) else {}
        attrs = (p.get("device_info") or {}).get("attributes") or {}
        _category[p.get("number")] = (p.get("url") or "").rsplit("/", 1)[-1].split("_")[0].replace("-", " ")
        _pages[p.get("number")] = "https://www.lcsc.com" + (p.get("url") or "")
        if re.match(r"https?://", attrs.get("Datasheet") or ""):
            _datasheets[p.get("number")] = attrs["Datasheet"]
        prices = [{"qty": x[0], "price": x[1]} for x in (p.get("price") or []) if isinstance(x, list) and len(x) > 1]
        parts.append({
            "code": p.get("number"), "mpn": p.get("mpn"), "package": p.get("package"),
            "manufacturer": p.get("manufacturer"), "stock": p.get("stock") or 0, "prices": prices,
            "cls": p.get("JLCPCB Part Class") or "", "image": img.get("224x224") or img.get("96x96") or "",
            "image_big": img.get("900x900") or "", "placeable": bool(attrs.get("Symbol") and attrs.get("Footprint")),
            "summary": attrs.get("LCSC Part Name") or "", "specs": _specs(attrs), "datasheet": _datasheets.get(p.get("number"), ""),
            "category": " ".join(_category[p.get("number")].split()), "url": "https://www.lcsc.com" + (p.get("url") or ""),
        })
    facets = []
    for f in res.get("paramList") or []:
        vals = [v for v in f.get("parameterValueList") or [] if v not in ("-", "")]
        if vals:
            facets.append({"name": f.get("parameterName"), "values": vals})
    total = res.get("total") or 0
    if grow and len(parts) < PAGE_SIZE:
        total = PAGE_SIZE * (page - 1) + len(parts)
    return {"total": total, "parts": parts, "facets": facets, "catalog": catalog,
            "page": page, "page_size": PAGE_SIZE}


# --------------------------------------------------------------------------- prepared parts


CACHE = os.path.join(tempfile.gettempdir(), "epro2kicad-provider")
_parts: dict[str, "Prepared"] = {}
_parts_lock = threading.Lock()


_category: dict[str, str] = {}  # LCSC code -> category seen in search results
_pages: dict[str, str] = {}  # LCSC code -> product page
_datasheets: dict[str, str] = {}  # LCSC code -> datasheet, from the search results


class Prepared:
    """One part converted for KiCad: symbol + footprint at once, STEP fetched in the background."""

    def __init__(self, code: str):
        self.code = code
        self.dir = os.path.join(CACHE, code)
        self.ready = threading.Event()  # symbol + footprint done
        self.step_done = threading.Event()
        self.error = ""
        self.files: dict[str, tuple[bytes, str]] = {}  # asset kind -> (payload, content type)
        self.info: dict = {}
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            dev = lcsc.fetch_device(self.code)
            if _category.get(self.code):  # from the search results; also tells op-amps from comparators
                dev.setdefault("attributes", {}).setdefault("LCSC Category", _category[self.code])
            p = lcsc.build_part(self.code, LIBRARY, model_path, with_3d=False, dev=dev)
            attrs = {k: ("" if v is None else str(v)) for k, v in (dev.get("attributes") or {}).items()}
            sym, fp = kicad_sanitize(p["sym_name"]), kicad_sanitize(p["fp_name"])
            p["fp"].name = fp
            if attrs.get("3D Model"):
                p["fp"].models = [lcsc.model_from_transform(model_path(fp), attrs.get("3D Model Transform", ""))]

            def sym_file(node, name):  # renamed symbol: its unit sub-symbols carry the name as prefix
                old = node[1]
                node = [node[0], name] + [[c[0], name + c[1][len(old):]] + c[2:] if isinstance(c, list) and c
                                           and c[0] == "symbol" and c[1].startswith(old) else c for c in node[2:]]
                return dumps([Sym("kicad_symbol_lib"), [Sym("version"), 20251024], [Sym("generator"),
                              "kicad_symbol_editor"], [Sym("generator_version"), "10.0"], node]) + "\n"

            def units(text):
                return max([1] + [int(u) for u in re.findall(r'\(symbol "[^"]*_(\d+)_\d+"', text)])
            sym_lib = sym_file(p["node"], sym)
            self.files["symbol"] = (sym_lib.encode("utf-8"), CTYPES["symbol"])
            alt = ""
            if p["stock"]:  # KiCad's standard drawing; EasyEDA's own stays available under its own name
                alt = kicad_sanitize(p["sym_name"] + "_EasyEDA")
                alt_lib = sym_file(p["node_easyeda"], alt)
                self.files["symbol_easyeda"] = (alt_lib.encode("utf-8"), CTYPES["symbol"])
            self.files["footprint"] = ((dumps(footprint_lib_sexp(p["fp"])) + "\n").encode("utf-8"), CTYPES["footprint"])
            skip = {"Symbol", "Footprint", "3D Model", "3D Model Transform", "Add into BOM", "Convert to PCB",
                    "Name", "Supplier", "Designator", "3D Model Title"}
            self.info = {"code": self.code, "symbol": sym, "footprint": fp, "mpn": p["mpn"], "value": p["value"],
                         "units": units(sym_lib), "pins": len(p["ls"].pins), "stock": p["stock"],
                         "symbol_easyeda": alt, "units_easyeda": units(alt_lib) if alt else 0,
                         "has_3d": bool(attrs.get("3D Model")),
                         "datasheet": attrs.get("Datasheet", ""),
                         "specs": [[k, v] for k, v in attrs.items() if k not in skip and v and not v.startswith("=")]}
            self._previews()
        except Exception as e:  # shown in the panel
            self.error = str(e) or type(e).__name__
            self.step_done.set()
        finally:
            self.ready.set()
        if not self.error:
            try:
                if self.info["has_3d"]:
                    step = lcsc.fetch_step(attrs)
                    self.files["3dmodel"] = (step, CTYPES["3dmodel"])
                    # centre the model the way EasyEDA does (needs the STEP itself)
                    p["fp"].models = [lcsc.model_from_transform(model_path(fp), attrs.get("3D Model Transform", ""),
                                                                lcsc.step_extent(step))]
                    self.files["footprint"] = ((dumps(footprint_lib_sexp(p["fp"])) + "\n").encode("utf-8"),
                                               CTYPES["footprint"])
            except Exception as e:  # part still placeable without 3D
                self.info["has_3d"] = False
                self.info["note"] = f"3D model unavailable ({e})"
            finally:
                self.step_done.set()

    def _previews(self):
        from .convert import find_kicad_cli
        cli = find_kicad_cli()
        if not cli:
            return
        shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(os.path.join(self.dir, "fp.pretty"))
        with open(os.path.join(self.dir, "s.kicad_sym"), "wb") as f:
            f.write(self.files["symbol"][0])
        if "symbol_easyeda" in self.files:
            with open(os.path.join(self.dir, "e.kicad_sym"), "wb") as f:
                f.write(self.files["symbol_easyeda"][0])
        with open(os.path.join(self.dir, "fp.pretty", self.info["footprint"] + ".kicad_mod"), "wb") as f:
            f.write(self.files["footprint"][0])
        cmds = [[cli, "sym", "export", "svg", "-o", self.dir, os.path.join(self.dir, "s.kicad_sym")],
                [cli, "fp", "export", "svg", "--layers", "F.Cu,B.Cu,F.SilkS,F.CrtYd,Edge.Cuts", "-o", self.dir,
                 os.path.join(self.dir, "fp.pretty")]]
        if "symbol_easyeda" in self.files:
            cmds.append([cli, "sym", "export", "svg", "-o", self.dir, os.path.join(self.dir, "e.kicad_sym")])
        procs = [subprocess.Popen(c, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for c in cmds]  # in parallel
        for pr in procs:
            try:
                pr.wait(60)
            except subprocess.TimeoutExpired:
                pr.kill()

    def preview(self, kind: str, unit: int = 1, easyeda: bool = False) -> bytes | None:
        sym = self.info["symbol_easyeda"] if easyeda and self.info.get("symbol_easyeda") else self.info["symbol"]
        name = f"{sym}_unit{unit}.svg" if kind == "symbol" else f"{self.info['footprint']}.svg"
        try:
            with open(os.path.join(self.dir, name), "rb") as f:
                return f.read()
        except OSError:
            return None

    def manifest(self, with_3d: bool, easyeda: bool = False) -> dict:
        """easyeda: place EasyEDA's own symbol drawing instead of KiCad's standard one."""
        self.step_done.wait(120)
        sym_file = "symbol_easyeda" if easyeda and "symbol_easyeda" in self.files else "symbol"
        sym_name = self.info["symbol_easyeda"] if sym_file == "symbol_easyeda" else self.info["symbol"]
        assets = []
        for kind, file in (("footprint", "footprint"), ("3dmodel", "3dmodel"), ("symbol", sym_file)):
            if file not in self.files or (kind == "3dmodel" and not with_3d):
                continue
            data, ctype = self.files[file]
            name = {"footprint": self.info["footprint"], "symbol": sym_name,
                    "3dmodel": self.info["footprint"] + ".step"}[kind]
            assets.append({"asset_type": kind, "name": name, "target_library": LIBRARY, "target_name": name,
                           "content_type": ctype, "size_bytes": len(data),
                           "sha256": hashlib.sha256(data).hexdigest(),
                           "download_url": f"{BASE}/asset/{self.code}/{file}", "required": kind != "3dmodel"})
        return {"part_id": self.code, "display_name": f"{self.info['mpn']} ({self.code})",
                "summary": self.info.get("value", ""), "symbol_name": sym_name,
                "library_name": LIBRARY, "assets": assets}


def prepared(code: str) -> Prepared:
    with _parts_lock:
        p = _parts.get(code)
        if p is None or (p.ready.is_set() and p.error):
            p = _parts[code] = Prepared(code)
            if len(_parts) > 200:  # keep memory bounded (STEP files are big)
                for k in list(_parts)[:50]:
                    _parts.pop(k, None)
        return p


# --------------------------------------------------------------------------- HTTP


PANEL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.html")


class Handler(BaseHTTPRequestHandler):
    server_version = "epro2kicad-provider/1.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _send(self, code: int, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, (bytes, str)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)  # exact: KiCad compares it with the manifest
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path == "/.well-known/kicad-remote-provider":
                return self._send(200, metadata())
            if u.path in ("/", "/panel"):
                with open(PANEL_FILE, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/health":
                return self._send(200, {"ok": True})
            if u.path == "/api/categories":
                return self._send(200, categories())
            if u.path == "/api/search":
                filters = json.loads(q.get("f") or "{}")
                if not (q.get("q") or q.get("catalog") or filters.get("Category")):
                    return self._send(200, {"total": 0, "parts": [], "facets": []})
                args = (q.get("q", "").strip(), q.get("catalog", ""), filters, int(q.get("page") or 1), q.get("sort", ""))
                res = search(*args, q.get("fresh") == "1")
                if args[3] == 1 and q.get("pre") != "1" and q.get("fresh") != "1":  # pre: the panel's own prefetches
                    threading.Thread(target=warm_packages, args=(*args[:3], args[4], res.get("facets") or []),
                                     daemon=True).start()
                return self._send(200, res)
            m = re.fullmatch(r"/api/prepare/(C\d+)", u.path, re.I)
            if m:  # the panel hovers a part: start converting it so the detail view opens at once
                prepared(m.group(1).upper())
                return self._send(202, {"ok": True})
            m = re.fullmatch(r"/api/(detail|manifest)/(C\d+)", u.path, re.I)
            if m:
                p = prepared(m.group(2).upper())
                p.ready.wait(90)
                if p.error or not p.ready.is_set():
                    return self._send(502, {"error": p.error or "timed out converting the part"})
                if m.group(1) == "detail":
                    return self._send(200, dict(p.info, step_ready=p.step_done.is_set()))
                return self._send(200, p.manifest(q.get("3d", "1") != "0", q.get("sym") == "easyeda"))
            m = re.fullmatch(r"/preview/(C\d+)/(symbol|footprint)\.svg", u.path, re.I)
            if m:
                p = prepared(m.group(1).upper())
                p.ready.wait(90)
                svg = None if p.error else p.preview(m.group(2), int(q.get("unit") or 1), q.get("v") == "easyeda")
                return self._send(200, svg, "image/svg+xml") if svg else self._send(404, {"error": "no preview"})
            m = re.fullmatch(r"/asset/(C\d+)/(symbol|symbol_easyeda|footprint|3dmodel)", u.path, re.I)
            if m:
                p = _parts.get(m.group(1).upper())
                if not p or m.group(2) not in p.files:
                    return self._send(404, {"error": "unknown asset"})
                data, ctype = p.files[m.group(2)]
                return self._send(200, data, ctype)
            return self._send(404, {"error": "not found"})
        except lcsc.LcscError as e:
            return self._send(502, {"error": str(e)})
        except Exception as e:  # report instead of dropping the connection
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        m = re.fullmatch(r"/api/placed/(C\d+)", urllib.parse.urlparse(self.path).path, re.I)
        if m:
            p = _parts.get(m.group(1).upper())
            if p and p.info.get("footprint"):
                threading.Thread(target=fix_project_tables, args=(p.info["footprint"],), daemon=True).start()
            return self._send(200, {"ok": True})
        if urllib.parse.urlparse(self.path).path == "/api/close-panel":
            err = close_panel()
            return self._send(500 if err else 200, {"error": err} if err else {"ok": True})
        # KiCad's panel opens links inside itself; this opens a part's datasheet / LCSC page in the default browser.
        # Only URLs the provider already has for that part, so other pages can't make it open arbitrary links.
        m = re.fullmatch(r"/api/open/(C\d+)/(datasheet|lcsc)", urllib.parse.urlparse(self.path).path, re.I)
        if not m:
            return self._send(404, {"error": "not found"})
        code = m.group(1).upper()
        p = _parts.get(code)
        url = ((p.info.get("datasheet", "") if p else "") or _datasheets.get(code, "")) if m.group(2) == "datasheet" \
            else _pages.get(code, "")
        if not re.match(r"https?://", url or ""):
            return self._send(404, {"error": "no link"})
        from .kicad_plugin import _open
        _open(url)
        return self._send(200, {"ok": True})


# KiCad gives the Remote Symbols panel no way to hide itself (no RPC command, no IPC RunAction handler in
# 10.0), so this works KiCad's View > Panels > Remote Symbols menu item (docked or floating) through macOS UI
# scripting, from a small applet the user grants Accessibility.  The applet is a fixed stub that runs PANEL_ACTION,
# so changing these scripts never changes the applet (macOS would ask for the permission again).
_PANEL_ITEM = '''
on panelItem(p)
    tell application "System Events" to tell p
        return menu item "Remote Symbols" of menu 1 of menu item "Panels" of menu 1 of menu bar item "View" of menu bar 1
    end tell
end panelItem
on isShown(mi)
    tell application "System Events" to return (value of attribute "AXMenuItemMarkChar" of mi) is not missing value
end isShown
'''
_CLOSE_PANEL = _PANEL_ITEM + '''
tell application "System Events"
    repeat with n in {"kicad", "eeschema"}
        if exists process n then
            set p to process n
            set done to false
            try
                set mi to my panelItem(p)
                if my isShown(mi) then
                    click mi
                    set done to true
                end if
            end try
            if not done then
                repeat with w in (every window of p whose name is "Remote Symbols")
                    click (first button of w whose subrole is "AXCloseButton")
                end repeat
            end if
        end if
    end repeat
end tell
'''
_APPLET = '''set d to (POSIX path of (path to home folder)) & ".cache/epro2kicad/"
try
    run script (read (POSIX file (d & "panel-action.applescript")) as «class utf8»)
on error m
    do shell script "printf %s " & quoted form of m & " > " & quoted form of (d & "panel-action.err")
end try'''
PANEL_ACTION = os.path.join(lcsc.CACHE, "panel-action.applescript")
PANEL_ERROR = os.path.join(lcsc.CACHE, "panel-action.err")

CLOSER = os.path.expanduser("~/Applications/Close Remote Symbols.app")  # built by install.install_closer
_last_prompt = 0.0


_panel_lock = threading.Lock()


def panel_action(script: str) -> str:
    """Run a panel script through the applet; '' when done, else what went wrong."""
    if platform.system() != "Darwin" or not os.path.exists(CLOSER):
        return "only on macOS, with the closer app installed"
    with _panel_lock:
        os.makedirs(os.path.dirname(PANEL_ACTION), exist_ok=True)
        with open(PANEL_ACTION, "w", encoding="utf-8") as f:
            f.write(script)
        if os.path.exists(PANEL_ERROR):
            os.remove(PANEL_ERROR)
        try:
            subprocess.run(["open", "-g", "-j", "-W", CLOSER], stdout=subprocess.DEVNULL, timeout=15)
        except subprocess.TimeoutExpired:
            return "the closer app did not finish"
        try:
            with open(PANEL_ERROR, encoding="utf-8") as f:
                err = f.read().strip()
        except OSError:
            return ""
    if "assistive access" in err or "-25211" in err or "-1719" in err:
        global _last_prompt
        if time.time() - _last_prompt > 300:  # show the user where to allow it, not on every Place
            _last_prompt = time.time()
            subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"])
        return ("macOS doesn't let \"Close Remote Symbols\" control KiCad: allow it in System Settings → "
                "Privacy & Security → Accessibility (remove and re-add it if it's already listed)")
    return err


def close_panel() -> str:
    return panel_action(_CLOSE_PANEL)


def running() -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen(BASE + "/health", timeout=1) as r:
            return r.status == 200
    except OSError:
        return False


def serve():
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True
    print(f"LCSC provider for KiCad running on {BASE}")
    prune_cache()
    threading.Thread(target=warm_categories, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    if running():
        print(f"already running on {BASE}")
        sys.exit(0)
    for _ in range(20):  # the port may still be held by a previous instance shutting down
        try:
            serve()
            break
        except OSError:
            time.sleep(0.5)
