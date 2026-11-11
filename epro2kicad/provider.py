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
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import lcsc
from .kicad.footprint import footprint_lib_sexp
from .sexp import Sym, dumps

HOST = "127.0.0.1"
# 61840 is the address the installer registers in KiCad; EPRO2KICAD_PORT runs a second copy beside it (testing)
PORT = int(os.environ.get("EPRO2KICAD_PORT") or 61840)
BASE = f"http://{HOST}:{PORT}"
VERSION = "1.2"
LIBRARY = "lcsc"  # KiCad turns this into the nickname "<prefix>_lcsc"
PAGE_SIZE = 30  # LCSC answers smaller pages faster; the panel loads more on scroll
CTYPES = {"symbol": "application/x-kicad-symbol", "footprint": "application/x-kicad-footprint",
          "3dmodel": "model/step"}


def metadata() -> dict:
    return {
        "provider_name": "LCSC / EasyEDA (epro2kicad)",
        "provider_version": VERSION,
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
_tree_lock = threading.Lock()
TREE_FILE = os.path.join(lcsc.CACHE, "category-tree.json")  # not categories.json: that is the converter's
WARM_EVERY = 7 * 86400  # what the provider keeps on disk (categories, pre-loaded searches) is renewed weekly


def _fetch_tree() -> list:
    res = lcsc._get(f"{lcsc.API}/eda/catalog/tree?path=lcsc") or []
    tree = [{"id": c["catalogId"], "name": c.get("catalogNameEn") or c.get("catalogName") or str(c["catalogId"]),
             "children": sorted(({"id": s["catalogId"],
                                  "name": s.get("catalogNameEn") or s.get("catalogName") or str(s["catalogId"])}
                                 for s in c.get("sonList") or []), key=lambda x: x["name"].lower())}
            for c in res]
    if not tree:  # not remembered: an empty tree would stay until the provider restarts
        raise lcsc.LcscError("unexpected answer from LCSC: no categories")
    tree.sort(key=lambda x: x["name"].lower())
    try:
        os.makedirs(os.path.dirname(TREE_FILE), exist_ok=True)
        with open(TREE_FILE + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"time": time.time(), "tree": tree}, f)
        os.replace(TREE_FILE + ".tmp", TREE_FILE)
    except OSError:
        pass  # no disk cache is fine
    return tree


def _renew_tree() -> None:
    global _tree
    try:
        _tree = _fetch_tree()
    except Exception:
        pass  # the saved tree stays


def categories() -> list:
    """LCSC's category tree. Kept on disk as well: there at once after a restart and without a network; a copy
    older than a week is used and renewed in the background."""
    global _tree
    with _tree_lock:  # two first requests at once: one asks LCSC, the other waits for it
        if _tree is None:
            age = 0.0
            try:
                with open(TREE_FILE, encoding="utf-8") as f:
                    saved = json.load(f)
                _tree, age = saved["tree"] or None, time.time() - saved["time"]
            except (OSError, ValueError, KeyError, TypeError):
                pass
            if _tree is None:
                _tree = _fetch_tree()
            elif age > WARM_EVERY:
                threading.Thread(target=_renew_tree, daemon=True).start()
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


SORTS = {"stock": ("stock", "desc"), "price": ("price", "asc"), "price-desc": ("price", "desc")}


SEARCH_DIR = os.path.join(lcsc.CACHE, "search")
FRESH = 600  # answers younger than this are used as they are; older ones are shown and refreshed by the panel
KEEP = WARM_EVERY  # answers older than this are asked again (and still used when LCSC can't be reached)
MEM_SEARCHES = 400

# LCSC takes 0.3-2 s per search, so every answer is kept (repeats, back, undoing a filter are instant): the newest
# few hundred in memory, all of them on disk
_searches: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()
_cache_lock = threading.Lock()  # guards _searches only: the disk is read and written outside it
_searching: dict[str, threading.Event] = {}  # searches in flight
_searching_lock = threading.Lock()


def _key(q: str, catalog: str, filters: dict, page, sort: str) -> str:
    return json.dumps([q, catalog, filters, page, sort], sort_keys=True)


def _cache_path(key: str) -> str:
    return os.path.join(SEARCH_DIR, hashlib.sha1(key.encode()).hexdigest() + ".json.gz")


def _cache_keep(key: str, hit: tuple) -> None:
    with _cache_lock:
        _searches[key] = hit
        _searches.move_to_end(key)
        while len(_searches) > MEM_SEARCHES:  # the oldest go; they are still on disk
            _searches.popitem(last=False)


def _cache_get(key: str):
    """(time, answer) from memory or the disk cache; None if this was never asked."""
    with _cache_lock:
        hit = _searches.get(key)
        if hit:
            _searches.move_to_end(key)
            return hit
    try:
        with gzip.open(_cache_path(key)) as f:
            t, res = json.load(f)
        hit = (float(t), res)
        _remember(res)
    except Exception:  # not there, or a damaged file: same as never asked
        return None
    _cache_keep(key, hit)
    return hit


def _cache_put(key: str, res: dict) -> None:
    hit = (time.time(), res)
    _cache_keep(key, hit)
    try:
        os.makedirs(SEARCH_DIR, exist_ok=True)
        path = _cache_path(key)
        tmp = f"{path}.{threading.get_ident()}.tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(hit, f)
        os.replace(tmp, path)
    except OSError:
        pass  # no disk cache is fine


def _remember(res: dict) -> None:
    """Links and categories that a live search notes for each part (a cached answer skips that)."""
    for p in res.get("parts") or []:
        _category.setdefault(p["code"], p.get("category") or "")
        _pages.setdefault(p["code"], p.get("url") or "")
        if p.get("datasheet"):
            _datasheets.setdefault(p["code"], p["datasheet"])


def _cached(key: str, fetch, fresh: bool = False):
    """(time, answer, stale) for one search. From the cache while young enough; otherwise fetch() asks LCSC - once,
    however many requests want the same thing at that moment (the panel prefetches the hovered category or filter
    value and the next page: the click that follows waits for that request instead of starting its own).
    When LCSC can't be reached an old answer is better than none: it comes back with stale=True."""
    old = _cache_get(key)
    if old and not fresh and time.time() - old[0] < KEEP:
        return old[0], old[1], False
    with _searching_lock:
        ev = _searching.get(key)
        owner = ev is None
        if owner:
            ev = _searching[key] = threading.Event()
    try:
        if not owner:
            ev.wait(DEADLINE + 1)
            hit = _cache_get(key)
            if hit and hit[0] > (old[0] if old else 0):  # the other request's answer
                return hit[0], hit[1], False
            # it failed: ask ourselves
        try:
            res = _in_time(key, fetch)
        except lcsc.LcscError:
            if old:
                return old[0], old[1], True
            raise
        return time.time(), res, False
    finally:
        if owner:
            with _searching_lock:
                _searching.pop(key, None)
            ev.set()


DEADLINE = 20  # seconds a search may take; LCSC answers in 0.3-3


class Workers:
    """The threads that talk to LCSC. lcsc keeps one connection per thread open, so work goes to the thread that
    was busy last - its connection is the one most likely still open - instead of to a new thread, which would
    start with a TLS handshake every time. When all are busy (requests that hang) the work gets a thread of its
    own rather than waiting behind them."""

    def __init__(self, most: int = 8):
        self.most, self.count = most, 0
        self.idle: list = []  # job queues of the waiting threads, the last used on top
        self.lock = threading.Lock()

    def run(self, fn) -> None:
        with self.lock:
            q = self.idle.pop() if self.idle else None
            if q is None and self.count < self.most:
                self.count += 1
                q = queue.SimpleQueue()
                threading.Thread(target=self._loop, args=(q,), daemon=True).start()
        if q is None:
            threading.Thread(target=fn, daemon=True).start()
        else:
            q.put(fn)

    def _loop(self, q) -> None:
        while True:
            fn = q.get()
            try:
                fn()
            except Exception:
                pass  # every job reports its own errors
            with self.lock:
                self.idle.append(q)


_workers = Workers()


def _in_time(key: str, fetch) -> dict:
    """fetch() and keep its answer, but don't wait for it longer than DEADLINE. Now and then a request to LCSC
    just hangs (over a minute has been seen) while a second one is answered at once: the panel gets "taking too
    long" and a Retry instead of an endless progress bar, and the late answer is still kept if it ever arrives."""
    box, done = {}, threading.Event()

    def run():
        try:
            box["res"] = fetch()
            _cache_put(key, box["res"])
        except Exception as e:
            box["err"] = e
        finally:
            done.set()
    _workers.run(run)
    if not done.wait(DEADLINE):
        raise lcsc.LcscError(f"request failed: LCSC search timed out after {DEADLINE} s")
    if "err" in box:
        raise box["err"]
    return box["res"]


def _normalise(q, catalog, filters, sort):
    """One spelling per search: equal searches share a cache entry and LCSC isn't asked twice for the same thing."""
    q = " ".join(str(q or "").split())
    catalog = str(catalog or "")
    fl = {}
    for name, values in (filters or {}).items() if isinstance(filters, dict) else ():
        values = sorted({str(v) for v in (values if isinstance(values, list) else [values]) if v not in (None, "")})
        if values:
            fl[str(name)] = values
    cats = fl.pop("Category", None)
    if cats and not catalog:  # picking a category facet switches to that category (full parametric facets)
        catalog = "$".join(str(i) for i in (category_id(c) for c in cats) if i)
    if not catalog or sort not in SORTS:  # LCSC sorts within a category only (see _query)
        sort = ""
    return q, catalog, fl, sort


def search(q: str = "", catalog: str = "", filters: dict | None = None, page: int = 1, sort: str = "",
           fresh: bool = False) -> dict:
    """One page of parts and the filters LCSC offers for them. 'age' says how old the answer is (the panel brings
    old ones up to date with fresh=True: stock, prices), 'stale' that it is old because LCSC couldn't be asked."""
    q, catalog, filters, sort = _normalise(q, catalog, filters, sort)
    page = max(1, int(page))
    if not q and not catalog:
        return {"total": 0, "parts": [], "facets": [], "catalog": "", "page": 1, "page_size": PAGE_SIZE, "age": 0}
    t, res, stale = _cached(_key(q, catalog, filters, page, sort), lambda: _search(q, catalog, filters, page, sort),
                            fresh)
    res = dict(res, age=max(0.0, time.time() - t))
    if stale:
        res["stale"] = True
    return res


_spec = threading.BoundedSemaphore(3)


def speculate(fn, *args) -> bool:
    """Run a search nobody waits for yet (hovered category or filter value, next page) in the background, at most
    three at a time. More than that are skipped - the click asks itself - so guesses neither queue up in front of
    what the user actually asked for nor turn into a burst of requests to LCSC."""
    if not _spec.acquire(blocking=False):
        return False

    def run():
        try:
            fn(*args)
        except Exception:
            pass
        finally:
            _spec.release()
    threading.Thread(target=run, daemon=True).start()
    return True


# Package lists are alphabetical (3000+ values for capacitors), so a click on 0603 can't be guessed from the list:
# warm the packages people actually pick.  Each one is a full LCSC search (~2 s) returning the narrowed lists of the
# other filters (Capacitance for 0603, ...), so after a few seconds those clicks come from the cache.
COMMON_PACKAGES = ["0603", "0402", "0805", "1206", "0201", "1210", "SOT-23", "SOT-23-3", "SOT-23-5", "SOT-23-6",
                   "SOD-123", "SOD-323", "SOT-223", "SOIC-8", "SOP-8", "TSSOP-8"]
_warm_gen = [0]
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
    try:
        return search("", catalog, filters or {}, 1, "")
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
    if os.environ.get("EPRO2KICAD_NO_WARM"):
        return
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


KEYWORD_BLOCK = 5  # pages of a keyword search that are loaded together (see _search)
KEYWORD_PAGES = 20  # ... and how many pages of it are offered: past 600 parts, narrowing the search is the way


def _search(q: str, catalog: str, filters: dict, page: int, sort: str) -> dict:
    """Ask LCSC for one page (arguments as _normalise leaves them).
    A category lists page by page. A keyword search ignores currPage - LCSC answers from the first part whatever
    is asked - so its later pages are cut out of one bigger answer per KEYWORD_BLOCK pages (pages 2-5 out of the
    first 150 parts, 6-10 out of the first 300, ...), kept like every other answer: one request per block instead
    of an ever larger one per page."""
    if catalog or page == 1:
        found, parts, facets = _query(q, catalog, filters, page, PAGE_SIZE, sort)
        total = found
        if not catalog:
            total = min(found, PAGE_SIZE * KEYWORD_PAGES) if len(parts) >= PAGE_SIZE else len(parts)
    else:
        last = min(KEYWORD_PAGES, -(-page // KEYWORD_BLOCK) * KEYWORD_BLOCK)
        size = PAGE_SIZE * last

        def block():
            n, rows, _ = _query(q, "", filters, 1, size, "", aggs=False)
            return {"total": n, "parts": rows}
        _, res, _ = _cached(_key(q, "", filters, f"1-{last}", ""), block)
        found, facets = res["total"], []
        parts = res["parts"][PAGE_SIZE * (page - 1):PAGE_SIZE * page]
        total = min(found, PAGE_SIZE * KEYWORD_PAGES) if len(res["parts"]) >= size else len(res["parts"])
    out = {"total": total, "parts": parts, "facets": facets, "catalog": catalog, "page": page, "page_size": PAGE_SIZE}
    if found > total:
        out["found"] = found  # LCSC has more than a keyword search lists: the panel says so
    return out


def _trim(d: dict, most: int = 20000) -> None:
    """Keep a code -> something map from growing for as long as the provider runs (weeks, as a login item)."""
    if len(d) > most:
        try:
            for k in list(d)[:most // 4]:
                d.pop(k, None)
        except RuntimeError:  # another search added to it meanwhile: next time
            pass


def _query(q: str, catalog: str, filters: dict, page: int, size: int, sort: str, aggs: bool = True):
    """One request to LCSC: (parts found, [part], [facet]).  filters: {facet name: [values]}.
    Keyword and/or category search with EasyEDA's facets.  sort (SORTS) only within a category: a keyword search
    matches loosely (any word), and sorting it puts parts that merely share "0603" first, so the panel sorts those
    results itself."""
    params = {"currPage": page, "pageSize": size, "needAggs": "true" if aggs else "false"}
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
    for p in res.get("productList") or []:
        code = p.get("number")
        if not code:
            continue
        imgs = p.get("image") or []
        img = imgs[0] if imgs and isinstance(imgs[0], dict) else {}
        attrs = (p.get("device_info") or {}).get("attributes") or {}
        _category[code] = (p.get("url") or "").rsplit("/", 1)[-1].split("_")[0].replace("-", " ")
        _pages[code] = "https://www.lcsc.com" + (p.get("url") or "")
        if re.match(r"https?://", attrs.get("Datasheet") or ""):
            _datasheets[code] = attrs["Datasheet"]
        prices = [{"qty": x[0], "price": x[1]} for x in (p.get("price") or []) if isinstance(x, list) and len(x) > 1]
        parts.append({
            "code": code, "mpn": p.get("mpn") or code, "package": p.get("package") or "",
            "manufacturer": p.get("manufacturer") or "", "stock": p.get("stock") or 0, "prices": prices,
            "cls": p.get("JLCPCB Part Class") or "", "image": img.get("224x224") or img.get("96x96") or "",
            "image_big": img.get("900x900") or "", "placeable": bool(attrs.get("Symbol") and attrs.get("Footprint")),
            "summary": attrs.get("LCSC Part Name") or "", "specs": _specs(attrs), "datasheet": _datasheets.get(code, ""),
            "category": " ".join(_category[code].split()), "url": "https://www.lcsc.com" + (p.get("url") or ""),
        })
    for d in (_category, _pages, _datasheets):
        _trim(d)
    facets = []
    for f in res.get("paramList") or []:
        vals = [v for v in f.get("parameterValueList") or [] if v not in ("-", "")]
        if vals:
            facets.append({"name": f.get("parameterName"), "values": vals})
    return res.get("total") or 0, parts, facets


# --------------------------------------------------------------------------- prepared parts


CACHE = os.path.join(tempfile.gettempdir(), f"epro2kicad-provider{'' if PORT == 61840 else '-%d' % PORT}")
_parts: "OrderedDict[str, Prepared]" = OrderedDict()  # least recently asked for first
_parts_lock = threading.Lock()
MAX_PARTS = 60  # converted parts kept in memory (each opened one holds its STEP model, often megabytes)


_category: dict[str, str] = {}  # LCSC code -> category seen in search results
_pages: dict[str, str] = {}  # LCSC code -> product page
_datasheets: dict[str, str] = {}  # LCSC code -> datasheet, from the search results
_hovered = [""]  # the part the panel asked to prepare last (hover, keyboard selection)


class Prepared:
    """One part converted for KiCad, in the steps the panel needs them:
    - symbol + footprint as soon as it is asked for (`ready`): three small downloads and the conversion;
    - the previews (kicad-cli) when the part is looked at, or right away for the one the pointer rests on;
    - the STEP model, megabytes from LCSC, only when the part is opened or placed.
    need() starts the later two, each once. Hovering over a list of parts so costs neither a burst of kicad-cli
    runs nor a pile of model downloads."""

    def __init__(self, code: str):
        self.code = code
        self.dir = os.path.join(CACHE, code)
        self.ready = threading.Event()  # symbol + footprint done
        self.previews_done = threading.Event()
        self.step_done = threading.Event()
        self.error = ""
        self.problem: dict = {}  # explain() of what went wrong, when something did
        self.files: dict[str, tuple[bytes, str]] = {}  # asset kind -> (payload, content type)
        self.blobs: dict[str, tuple[bytes, str]] = {}  # sha256 -> exactly the bytes a manifest promised KiCad
        self.info: dict = {}
        self._lock = threading.Lock()
        self._want: set[str] = set()  # "previews" / "step" asked for before the conversion was done
        self._started: set[str] = set()
        self._fp = None
        self._attrs: dict = {}
        _workers.run(self._run)

    def need(self, *stages: str) -> None:
        """Start the previews and/or the STEP download unless under way or done (after the conversion)."""
        with self._lock:
            todo = [s for s in stages if s not in self._started]
            if not self.ready.is_set():
                self._want.update(todo)  # _run starts them
                return
            self._started.update(todo)
        self._start(todo)

    def _start(self, stages) -> None:
        for s in stages:
            if s == "previews":  # kicad-cli, no network
                threading.Thread(target=self._previews, daemon=True).start()
            else:
                _workers.run(self._step)

    def _fp_file(self) -> tuple[bytes, str]:
        return (dumps(footprint_lib_sexp(self._fp)) + "\n").encode("utf-8"), CTYPES["footprint"]

    def _run(self):
        try:
            dev = lcsc.fetch_device(self.code)
            if _category.get(self.code):  # from the search results; also tells op-amps from comparators
                dev.setdefault("attributes", {}).setdefault("LCSC Category", _category[self.code])
            p = lcsc.build_part(self.code, LIBRARY, model_path, with_3d=False, dev=dev)
            attrs = {k: ("" if v is None else str(v)) for k, v in (dev.get("attributes") or {}).items()}
            sym, fp = kicad_sanitize(p["sym_name"]), kicad_sanitize(p["fp_name"])
            p["fp"].name = fp
            self._fp, self._attrs = p["fp"], attrs
            self.files["footprint_bare"] = self._fp_file()  # no 3D model: placed without one, or none to be had
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
            self.files["footprint"] = self._fp_file()
            skip = {"Symbol", "Footprint", "3D Model", "3D Model Transform", "Add into BOM", "Convert to PCB",
                    "Name", "Supplier", "Designator", "3D Model Title"}
            self.info = {"code": self.code, "symbol": sym, "footprint": fp, "mpn": p["mpn"], "value": p["value"],
                         "units": units(sym_lib), "pins": len(p["ls"].pins), "stock": p["stock"],
                         "symbol_easyeda": alt, "units_easyeda": units(alt_lib) if alt else 0,
                         "has_3d": bool(attrs.get("3D Model")),
                         "datasheet": attrs.get("Datasheet", ""),
                         "specs": [[k, v] for k, v in attrs.items() if k not in skip and v and not v.startswith("=")]}
        except Exception as e:  # shown in the panel
            self.problem = explain(e)
            self.error = self.problem["error"]
            self.previews_done.set()
            self.step_done.set()
        finally:
            self.ready.set()
        if self.error:
            return
        with self._lock:
            if _hovered[0] == self.code:  # still the part under the pointer: its detail view opens complete
                self._want.add("previews")
            todo = [s for s in self._want if s not in self._started]
            self._started.update(todo)
        self._start(todo)

    def _step(self):
        try:
            if self.info["has_3d"]:
                step = lcsc.fetch_step(self._attrs)
                # centre the model the way EasyEDA does (needs the STEP itself)
                self._fp.models = [lcsc.model_from_transform(model_path(self.info["footprint"]),
                                                             self._attrs.get("3D Model Transform", ""),
                                                             lcsc.step_extent(step))]
                self.files["footprint"] = self._fp_file()
                self.files["3dmodel"] = (step, CTYPES["3dmodel"])
        except Exception as e:  # part still placeable without 3D
            self.info["has_3d"] = False
            self.info["note"] = f"3D model unavailable ({explain(e)['error']})"
        finally:
            self.step_done.set()

    def _previews(self):
        try:
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
                f.write(self.files["footprint_bare"][0])
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
        except OSError:
            pass  # no previews: the panel says so, the part can still be placed
        finally:
            self.previews_done.set()

    def preview(self, kind: str, unit: int = 1, easyeda: bool = False) -> bytes | None:
        sym = self.info["symbol_easyeda"] if easyeda and self.info.get("symbol_easyeda") else self.info["symbol"]
        name = f"{sym}_unit{unit}.svg" if kind == "symbol" else f"{self.info['footprint']}.svg"
        try:
            with open(os.path.join(self.dir, name), "rb") as f:
                return f.read()
        except OSError:
            return None

    def manifest(self, with_3d: bool, easyeda: bool = False) -> dict:
        """What KiCad is to download for a Place. easyeda: EasyEDA's own symbol drawing instead of KiCad's standard
        one. The footprint names a 3D model exactly when the model comes with it; placed without one (the user's
        choice, or the download failed) it is the plain footprint, not one pointing at a file that isn't there."""
        if with_3d and self.info.get("has_3d"):
            self.need("step")
            self.step_done.wait(120)
        files = dict(self.files)  # one consistent set: the STEP may arrive (and change the footprint) any moment
        with_3d = with_3d and "3dmodel" in files
        sym_file = "symbol_easyeda" if easyeda and "symbol_easyeda" in files else "symbol"
        sym_name = self.info["symbol_easyeda"] if sym_file == "symbol_easyeda" else self.info["symbol"]
        assets = []
        for kind, file in (("footprint", "footprint" if with_3d else "footprint_bare"), ("3dmodel", "3dmodel"),
                           ("symbol", sym_file)):
            if file not in files or (kind == "3dmodel" and not with_3d):
                continue
            data, ctype = files[file]
            sha = hashlib.sha256(data).hexdigest()
            self.blobs[sha] = (data, ctype)  # KiCad checks size and digest: it gets these very bytes
            name = {"footprint": self.info["footprint"], "symbol": sym_name,
                    "3dmodel": self.info["footprint"] + ".step"}[kind]
            assets.append({"asset_type": kind, "name": name, "target_library": LIBRARY, "target_name": name,
                           "content_type": ctype, "size_bytes": len(data), "sha256": sha,
                           "download_url": f"{BASE}/asset/{self.code}/{file}?v={sha}", "required": kind != "3dmodel"})
        return {"part_id": self.code, "display_name": f"{self.info['mpn']} ({self.code})",
                "summary": self.info.get("value", ""), "symbol_name": sym_name,
                "library_name": LIBRARY, "assets": assets}


def prepared(code: str) -> Prepared:
    with _parts_lock:
        p = _parts.get(code)
        if p is None or (p.ready.is_set() and p.error):
            p = _parts[code] = Prepared(code)
        _parts.move_to_end(code)
        while len(_parts) > MAX_PARTS:  # keep memory bounded (STEP files are big): the longest unused go
            _parts.popitem(last=False)
        return p


def explain(e: Exception) -> dict:
    """An error in the user's words: {"error": what the panel shows, "kind": offline | timeout | lcsc | internal,
    "detail": the technical message}."""
    detail = str(e) or type(e).__name__
    low = detail.lower()
    if not isinstance(e, lcsc.LcscError):
        return {"error": f"{type(e).__name__}: {e}", "kind": "internal", "detail": detail}
    if low.startswith("request failed"):
        if "timed out" in low or "timeout" in low:
            return {"error": "LCSC is taking too long to answer.", "kind": "timeout", "detail": detail}
        m = re.search(r"http error (\d+)", low)
        if m:
            return {"error": f"LCSC answered with an error (HTTP {m.group(1)}).", "kind": "lcsc", "detail": detail}
        return {"error": "Can't reach LCSC. Check the internet connection.", "kind": "offline", "detail": detail}
    if low.startswith("unexpected answer"):
        return {"error": "LCSC sent an answer that can't be read.", "kind": "lcsc", "detail": detail}
    m = re.match(r"easyeda api error for \S+ (.*)", detail, re.I | re.S)
    if m:
        return {"error": f"LCSC reported an error: {m.group(1).strip() or 'unknown'}", "kind": "lcsc", "detail": detail}
    return {"error": detail, "kind": "lcsc", "detail": detail}  # "C123: not found in the EasyEDA library", ...


# --------------------------------------------------------------------------- HTTP


PANEL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.html")


_GONE = (ConnectionError, socket.timeout)  # the other end closed the connection or stopped listening


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def handle_error(self, request, client_address):
        # The panel drops connections all the time (window closed, a request superseded by a newer one, idle
        # keep-alives): nothing went wrong, so nothing goes to the log. Real errors still do.
        if isinstance(sys.exc_info()[1], _GONE):
            return
        super().handle_error(request, client_address)


def _int(s, default: int = 1) -> int:
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


class Handler(BaseHTTPRequestHandler):
    server_version = f"epro2kicad-provider/{VERSION}"
    protocol_version = "HTTP/1.1"
    timeout = 300  # idle keep-alive connections end here instead of holding a thread for good

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _send(self, code: int, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, (bytes, str)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)  # exact: KiCad compares it with the manifest
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except _GONE:  # nobody left to tell
            self.close_connection = True

    def _fail(self, e: Exception):
        return self._send(500 if not isinstance(e, lcsc.LcscError) else 502, explain(e))

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
                return self._send(200, {"ok": True, "version": VERSION})
            if u.path == "/api/categories":
                return self._send(200, categories())
            if u.path == "/api/search":
                filters = json.loads(q.get("f") or "{}")
                if not isinstance(filters, dict):
                    filters = {}
                args = (q.get("q", "").strip(), q.get("catalog", ""), filters, max(1, _int(q.get("page"))),
                        q.get("sort", ""))
                if q.get("pre") == "1":  # the panel's guess at what is asked next: nobody waits for the answer
                    speculate(search, *args)
                    return self._send(202, {"ok": True})
                res = search(*args, q.get("fresh") == "1")
                if args[3] == 1 and q.get("fresh") != "1" and res.get("parts") and not res.get("stale"):
                    threading.Thread(target=warm_packages, args=(*args[:3], args[4], res.get("facets") or []),
                                     daemon=True).start()
                return self._send(200, res)
            m = re.fullmatch(r"/api/prepare/(C\d+)", u.path, re.I)
            if m:  # the pointer rests on a part: start converting it so the detail view opens at once
                _hovered[0] = code = m.group(1).upper()
                p = prepared(code)
                if p.ready.is_set() and not p.error:  # converted earlier; otherwise _run sees it is the hovered one
                    p.need("previews")
                return self._send(202, {"ok": True})
            m = re.fullmatch(r"/api/(detail|manifest)/(C\d+)", u.path, re.I)
            if m:
                p = prepared(m.group(2).upper())
                with_3d = q.get("3d", "1") != "0"
                if m.group(1).lower() == "detail":  # the part is looked at: get the rest going
                    p.need("previews", *(["step"] if with_3d else []))
                p.ready.wait(90)
                if p.error or not p.ready.is_set():
                    return self._send(502, p.problem or {"error": "Converting the part took too long.",
                                                         "kind": "timeout"})
                if m.group(1).lower() == "detail":
                    return self._send(200, dict(p.info, step_ready=p.step_done.is_set()))
                return self._send(200, p.manifest(with_3d, q.get("sym") == "easyeda"))
            m = re.fullmatch(r"/preview/(C\d+)/(symbol|footprint)\.svg", u.path, re.I)
            if m:
                p = prepared(m.group(1).upper())
                p.need("previews")
                p.previews_done.wait(90)
                svg = None if p.error else p.preview(m.group(2), max(1, _int(q.get("unit"))), q.get("v") == "easyeda")
                return self._send(200, svg, "image/svg+xml") if svg else self._send(404, {"error": "no preview"})
            m = re.fullmatch(r"/asset/(C\d+)/(symbol|symbol_easyeda|footprint|footprint_bare|3dmodel)", u.path, re.I)
            if m:
                p = _parts.get(m.group(1).upper())
                # v: the digest from the manifest - the bytes it was made from, whatever happened to the part since
                hit = p and (p.blobs.get(q["v"]) if q.get("v") else p.files.get(m.group(2).lower()))
                if not hit:
                    return self._send(404, {"error": "unknown asset"})
                return self._send(200, hit[0], hit[1])
            return self._send(404, {"error": "not found"})
        except _GONE:
            self.close_connection = True
        except Exception as e:  # report instead of dropping the connection
            return self._fail(e)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            n = _int(self.headers.get("Content-Length"), 0)
            if n > 0:  # nothing here takes a body; read it so it isn't taken for the next request
                self.rfile.read(min(n, 1 << 20))
            # These act on the user's desktop (close KiCad's panel, open the browser), so only our own page may ask:
            # a browser names the page a request comes from, and some other website's is turned away.
            origin = self.headers.get("Origin")
            if origin and urllib.parse.urlparse(origin).netloc not in (f"{HOST}:{PORT}", f"localhost:{PORT}"):
                return self._send(403, {"error": "not allowed from this page"})
            m = re.fullmatch(r"/api/placed/(C\d+)", path, re.I)
            if m:
                p = _parts.get(m.group(1).upper())
                if p and p.info.get("footprint"):
                    threading.Thread(target=fix_project_tables, args=(p.info["footprint"],), daemon=True).start()
                return self._send(200, {"ok": True})
            if path in ("/api/close-panel", "/api/open-panel"):
                err = close_panel() if path.endswith("close-panel") else open_panel()
                return self._send(500 if err else 200, {"error": err} if err else {"ok": True})
            # KiCad's panel opens links inside itself; this opens a part's datasheet / LCSC page in the default
            # browser. Only URLs the provider already has for that part, so other pages can't make it open
            # arbitrary links.
            m = re.fullmatch(r"/api/open/(C\d+)/(datasheet|lcsc)", path, re.I)
            if not m:
                return self._send(404, {"error": "not found"})
            code = m.group(1).upper()
            p = _parts.get(code)
            url = ((p.info.get("datasheet", "") if p else "") or _datasheets.get(code, "")) \
                if m.group(2).lower() == "datasheet" else _pages.get(code, "")
            if not re.match(r"https?://", url or ""):
                return self._send(404, {"error": "no link"})
            from .kicad_plugin import _open
            _open(url)
            return self._send(200, {"ok": True})
        except _GONE:
            self.close_connection = True
        except Exception as e:
            return self._fail(e)


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
# the plugin's "LCSC parts" toolbar button: show the panel (the same menu item, when it isn't ticked yet)
_OPEN_PANEL = _PANEL_ITEM + '''
set found to false
tell application "System Events"
    repeat with n in {"kicad", "eeschema"}
        if exists process n then
            try
                set mi to my panelItem(process n)
                if not (my isShown(mi)) then click mi
                set found to true
            end try
        end if
    end repeat
end tell
if not found then error "no schematic editor in front: Remote Symbols is in its View > Panels menu"
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


def open_panel() -> str:
    return panel_action(_OPEN_PANEL)


def running() -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen(BASE + "/health", timeout=1) as r:
            return r.status == 200
    except OSError:
        return False


def warm_start() -> None:
    """What the first part of a session would otherwise wait 1-3 s for: KiCad's standard symbol libraries (the
    index of all of them and the big Device library, read once per run) and the category tree."""
    try:
        from . import stock
        from .kicad.symbol import stock_symbols
        stock._mpn_index()
        stock_symbols("Device")
    except Exception:
        pass  # the first part reads them itself
    try:
        categories()
    except Exception:
        pass


def serve():
    httpd = Server((HOST, PORT), Handler)
    print(f"LCSC provider for KiCad running on {BASE}", flush=True)
    threading.Thread(target=prune_cache, daemon=True).start()
    threading.Thread(target=warm_start, daemon=True).start()
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
