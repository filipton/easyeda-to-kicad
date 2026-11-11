"""Toolbar actions of the KiCad plugin (launched by KiCad through the scripts the installer writes).

    python -m epro2kicad.kicad_plugin import   # pick an EasyEDA Pro project, translate it, open the result
    python -m epro2kicad.kicad_plugin lcsc     # make sure the LCSC provider for the Remote Symbols panel runs
"""

from __future__ import annotations

import json
import math
import os
import platform
import subprocess
import sys
import threading
import time
import traceback

PROJECT_EXTS = (".epro", ".epro2", ".zip")  # what EasyEDA Pro exports
PREFS = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "epro2kicad",
                     "import.json")


def load_prefs() -> dict:
    """What the import window remembers between runs: last folder, the options, a chosen output parent."""
    try:
        with open(PREFS, encoding="utf-8") as f:
            p = json.load(f)
        return p if isinstance(p, dict) else {}
    except (OSError, ValueError):
        return {}


def save_prefs(**changes) -> None:
    p = dict(load_prefs(), **changes)
    try:
        os.makedirs(os.path.dirname(PREFS), exist_ok=True)
        with open(PREFS + ".tmp", "w", encoding="utf-8") as f:
            json.dump(p, f, indent=1)
        os.replace(PREFS + ".tmp", PREFS)
    except OSError:
        pass  # remembering is a convenience


def default_out(src: str, parent: str = "") -> str:
    """<project name>-kicad, next to the EasyEDA file unless another parent folder is given (and still there)."""
    from .convert import proj_name
    name = proj_name(os.path.splitext(os.path.basename(src))[0]) + "-kicad"
    return os.path.join(parent if parent and os.path.isdir(parent) else os.path.dirname(os.path.abspath(src)), name)


def is_project(path: str) -> bool:
    return bool(path) and path.lower().endswith(PROJECT_EXTS) and os.path.isfile(path)


def _open(path: str) -> None:
    s = platform.system()
    if s == "Darwin":
        subprocess.Popen(["open", path])
    elif s == "Windows":
        os.startfile(path)  # noqa: S606 - opening the user's own file
    else:
        subprocess.Popen(["xdg-open", path])


def start_provider() -> bool:
    """Start the provider in the background (detached) unless it already runs."""
    from . import provider
    if provider.running():
        return True
    log = os.path.join(os.path.expanduser("~"), ".epro2kicad-provider.log")
    kw = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x00000008}  # DETACHED_PROCESS
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(log, "ab") as out:
        subprocess.Popen([sys.executable, "-m", "epro2kicad.provider"], cwd=os.path.expanduser("~"),
                         env=dict(os.environ, PYTHONPATH=pkg_parent), stdout=out, stderr=out,
                         stdin=subprocess.DEVNULL, **kw)
    import time
    for _ in range(30):
        time.sleep(0.2)
        if provider.running():
            return True
    return False


def _app():
    import wx
    app = wx.App(False)
    app.SetAppName("EasyEDA to KiCad")
    if platform.system() == "Darwin":  # started by KiCad in the background: come to the front like a KiCad window
        import ctypes
        import ctypes.util
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        objc.objc_getClass.restype = objc.sel_registerName.restype = ctypes.c_void_p
        objc.objc_msgSend.restype, objc.objc_msgSend.argtypes = ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_void_p]
        ns_app = objc.objc_msgSend(objc.objc_getClass(b"NSApplication"), objc.sel_registerName(b"sharedApplication"))
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
        objc.objc_msgSend(ns_app, objc.sel_registerName(b"activateIgnoringOtherApps:"), True)
    return wx, app


def _show_panel() -> str:
    """Ask the provider to show KiCad's Remote Symbols panel (macOS, through the small applet that also closes it
    after a Place). '' when the panel is up; otherwise why not. The provider does it, not this short-lived
    process: it knows when it last sent the user to System Settings for the permission."""
    from . import provider
    import urllib.error
    import urllib.request
    try:
        req = urllib.request.Request(provider.BASE + "/api/open-panel", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=25) as r:
            return "" if r.status == 200 else f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read()).get("error") or f"HTTP {e.code}"
        except ValueError:
            return f"HTTP {e.code}"
    except OSError as e:
        return str(e)


def action_lcsc() -> int:
    """The "LCSC parts" toolbar button: make sure the provider runs, then open the parts browser. Where the panel
    can't be opened from here, say where it is instead."""
    if not start_provider():
        wx, _ = _app()
        wx.MessageBox("Could not start the LCSC provider.\nSee ~/.epro2kicad-provider.log", "LCSC parts",
                      wx.OK | wx.ICON_ERROR)
        return 1
    why = _show_panel()
    if not why:
        return 0  # the parts browser is open: nothing to say
    wx, _ = _app()
    tip = ""
    if "System Settings" in why:  # macOS has not allowed the applet yet
        tip = ("\n\nThis button can open it for you once macOS allows it: System Settings → Privacy & Security → "
               "Accessibility → \"Close Remote Symbols\".")
    wx.MessageBox("LCSC search is running.\n\n"
                  "Open it in the schematic editor with the Remote Symbols button under Place Symbol "
                  "(or Shift+A).\n\n"
                  "Search, click Place, and the part is on your cursor. Symbol, footprint and 3D "
                  "model are saved into the project's RemoteLibrary folder." + tip, "LCSC parts",
                  wx.OK | wx.ICON_INFORMATION)
    return 0


def action_import(path: str = "") -> int:
    wx, app = _app()
    from .convert import Options, convert_project

    prefs = load_prefs()  # last folder, the two options, an output parent the user chose instead of "next to it"
    state = {"running": False, "done": False}
    title = "Import EasyEDA Pro project"
    wildcard = "EasyEDA Pro project (*.epro;*.epro2;*.zip)|*.epro;*.epro2;*.zip|All files|*"
    gray = wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT)
    last_dir = prefs.get("last_dir") if os.path.isdir(prefs.get("last_dir") or "") else ""

    def out_for(src_path):
        return default_out(src_path, prefs.get("out_parent") or "")

    # floats above KiCad like its Remote Symbols window
    frame = wx.Frame(None, title=title, size=(760, 380), style=wx.DEFAULT_FRAME_STYLE | wx.STAY_ON_TOP)
    pnl = wx.Panel(frame)
    hint = wx.StaticText(pnl, label="Drop an EasyEDA Pro project (.epro, .epro2 or .zip) on this window, or choose it:")
    hint.SetForegroundColour(gray)
    src = wx.FilePickerCtrl(pnl, path=path, message="EasyEDA Pro project to import", wildcard=wildcard,
                            style=wx.FLP_OPEN | wx.FLP_FILE_MUST_EXIST | wx.FLP_USE_TEXTCTRL)
    out = wx.DirPickerCtrl(pnl, path=out_for(path) if path else "", message="Where to create the KiCad project(s)",
                           style=wx.DIRP_USE_TEXTCTRL)
    if not path and last_dir:
        src.SetInitialDirectory(last_dir)
    note = wx.StaticText(pnl, label="")  # what is wrong with the choice, or what happens to an existing folder
    note.SetForegroundColour(gray)
    want_3d = wx.CheckBox(pnl, label="Download 3D models for the footprints (internet, can take a minute)")
    std = wx.CheckBox(pnl, label="Use KiCad's standard symbols for standard parts (resistors, capacitors, diodes, "
                                 "transistors,\nconnectors, op-amps, ICs from KiCad's libraries); wiring is kept")
    tol = wx.CheckBox(pnl, label="Let rule misses of up to 3 µm pass (rounding between EasyEDA and KiCad): the rules "
                                 "of a\nboard that has them are written that little lower")
    want_3d.SetValue(bool(prefs.get("download_3d", True)))
    std.SetValue(bool(prefs.get("kicad_symbols", True)))
    tol.SetValue(bool(prefs.get("tolerance", True)))
    # progress: bar, current step, ETA; the detailed log is folded away under "Details"
    gauge = wx.Gauge(pnl, range=1000, style=wx.GA_HORIZONTAL | wx.GA_SMOOTH)
    step_lbl = wx.StaticText(pnl, label="", style=wx.ST_ELLIPSIZE_END)
    eta_lbl = wx.StaticText(pnl, label="")
    eta_lbl.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
    summary = wx.StaticText(pnl, label="")
    details = wx.CollapsiblePane(pnl, label="Details", style=wx.CP_DEFAULT_STYLE | wx.CP_NO_TLW_RESIZE)
    log_box = wx.TextCtrl(details.GetPane(), style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
    log_box.SetFont(wx.Font(wx.FontInfo(11).Family(wx.FONTFAMILY_TELETYPE)))
    dsz = wx.BoxSizer(wx.VERTICAL)
    dsz.Add(log_box, 1, wx.EXPAND | wx.TOP, 4)
    details.GetPane().SetSizer(dsz)
    log_box.SetMinSize((-1, 220))
    bar = wx.BoxSizer(wx.HORIZONTAL)
    go = wx.Button(pnl, wx.ID_OK, "Import")
    close = wx.Button(pnl, wx.ID_CANCEL, "Close")
    go.SetDefault()
    bar.AddStretchSpacer()
    bar.Add(close, 0, wx.RIGHT, 6)
    bar.Add(go)

    form = wx.FlexGridSizer(2, 8, 8)
    form.AddGrowableCol(1)
    form.Add(wx.StaticText(pnl, label="EasyEDA project:"), 0, wx.ALIGN_CENTER_VERTICAL)
    form.Add(src, 1, wx.EXPAND)
    form.Add(wx.StaticText(pnl, label="KiCad output folder:"), 0, wx.ALIGN_CENTER_VERTICAL)
    form.Add(out, 1, wx.EXPAND)
    form.AddSpacer(0)
    form.Add(note, 1, wx.EXPAND)
    prog_row = wx.BoxSizer(wx.HORIZONTAL)
    prog_row.Add(step_lbl, 1, wx.ALIGN_CENTER_VERTICAL)
    prog_row.Add(eta_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 12)
    top = wx.BoxSizer(wx.VERTICAL)
    top.Add(hint, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 12)
    top.Add(form, 0, wx.EXPAND | wx.ALL, 12)
    top.Add(want_3d, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
    top.Add(std, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
    top.Add(tol, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
    top.Add(gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 12)
    top.Add(prog_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 12)
    top.Add(summary, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 12)
    top.Add(details, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
    top.Add(bar, 0, wx.EXPAND | wx.ALL, 12)
    pnl.SetSizer(top)
    for w in (gauge, step_lbl, eta_lbl, summary, details):
        w.Hide()

    def relayout(_e=None):
        pnl.Layout()
        h = top.GetMinSize().height + (0 if details.IsCollapsed() else 60)
        frame.SetClientSize((frame.GetClientSize().width, max(h, 200)))
    details.Bind(wx.EVT_COLLAPSIBLEPANE_CHANGED, relayout)
    wx.CallAfter(relayout)

    def refresh(_e=None):
        """Import is offered once there is a project to import; the note says what else the user should know."""
        p, o = src.GetPath(), out.GetPath()
        ok = is_project(p)
        if not state["running"] and not state["done"]:
            go.Enable(ok)
        text = ""
        if p and not ok:
            text = "That is not an EasyEDA Pro project file (.epro, .epro2 or .zip)."
        else:
            try:
                if o and os.path.isdir(o) and os.listdir(o):
                    text = "This folder exists already: boards converted into it before are replaced."
            except OSError:
                pass
        if text != note.GetLabel():
            note.SetLabel(text)

    def set_source(p):
        src.SetPath(p)
        out.SetPath(out_for(p))
        refresh()

    def on_src(_e):
        if is_project(src.GetPath()):
            out.SetPath(out_for(src.GetPath()))
        refresh()
    src.Bind(wx.EVT_FILEPICKER_CHANGED, on_src)
    out.Bind(wx.EVT_DIRPICKER_CHANGED, refresh)

    class Drop(wx.FileDropTarget):  # a project file dragged from Finder onto the window
        def OnDropFiles(self, _x, _y, files):
            f = next((f for f in files if is_project(f)), None)
            if not f or state["running"] or state["done"]:
                return False
            set_source(f)
            return True
    pnl.SetDropTarget(Drop())

    def choose():
        """Started without a file (toolbar button, Services menu): ask for it right away, one click less."""
        if state["running"] or state["done"] or src.GetPath():
            return
        with wx.FileDialog(frame, "EasyEDA Pro project to import", defaultDir=last_dir, wildcard=wildcard,
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                set_source(dlg.GetPath())

    t0 = [0.0]

    def stamp():
        s_ = time.time() - t0[0]
        return f"[{int(s_ // 60):02d}:{s_ % 60:04.1f}]"

    def log(msg):
        wx.CallAfter(log_box.AppendText, f"{stamp()} {msg}\n")

    # step from the converter: [a, b) of the job, est seconds, sub = fraction within the step (None: interpolate)
    pr = {"a": 0.0, "b": 0.0, "est": 1.0, "label": "", "sub": None, "t": 0.0, "f": 0.0, "eta": None}

    def set_progress(a, b, est, label, sub):
        now = time.time()
        if a != pr["a"]:
            pr["t"] = now
        if sub is None or a != pr["a"]:
            log_box.AppendText(f"{stamp()} {label}\n")
        pr.update(a=a, b=b, est=est, label=label, sub=sub)

    def tick(_e):
        now = time.time()
        el = now - t0[0]
        a, b, est = pr["a"], pr["b"], pr["est"]
        total = est / (b - a) if b > a else 1.0  # whole job in estimated seconds
        done_est = a * total
        ratio = min(5.0, max(0.3, (pr["t"] - t0[0]) / done_est)) if done_est > 1 else 1.0  # real vs estimated speed
        in_step = now - pr["t"]
        span = max(0.1, est * ratio)  # this step's expected length at the speed seen so far
        if pr["sub"] is not None:
            frac = pr["sub"]
            step_left = span * (1 - frac)
        else:
            x = in_step / span
            # past its estimate the step keeps creeping on (never stalls) and is expected to need another third
            # of the time it has taken so far
            frac = x * 0.85 if x < 1 else 0.85 + 0.14 * (1 - math.exp(-(x - 1)))
            step_left = span - in_step if x < 1 else in_step / 3
        pr["f"] = max(pr["f"], min(0.99, a + (b - a) * frac))
        gauge.SetValue(int(pr["f"] * 1000))
        step_lbl.SetLabel(f"{pr['label']}…")
        if el > 2 and pr["f"] > 0.03:
            left = step_left + (1 - b) * total * ratio
            pr["eta"] = left if pr["eta"] is None else 0.7 * pr["eta"] + 0.3 * left
            e = pr["eta"]
            txt = "almost done" if e < 3 else f"about {int(round(e / 5) * 5) or 5} s left" if e < 60 else \
                f"about {int(e // 60)} min {int(round(e % 60 / 10) * 10):02d} s left"
            eta_lbl.SetLabel(f"{int(pr['f'] * 100)} %  ·  {txt}")
        else:
            eta_lbl.SetLabel(f"{int(pr['f'] * 100)} %")
        prog_row.Layout()

    timer = wx.Timer(frame)
    frame.Bind(wx.EVT_TIMER, tick, timer)

    red = wx.Colour(200, 60, 40)

    def done(path_in, out_dir, reps, err, trace=""):
        timer.Stop()
        state["running"] = False
        took = time.time() - t0[0]
        if err or not reps:
            # nothing converted: say why where it is seen (not only in the log), and let the user fix it and retry
            why = (str(err) or type(err).__name__) if err else "The project has no board with a schematic or a PCB."
            log_box.AppendText(f"{stamp()} FAILED: {why}\n{trace}")
            step_lbl.SetLabel("Import failed")
            step_lbl.SetForegroundColour(red)
            eta_lbl.SetLabel("")
            summary.SetForegroundColour(red)
            summary.SetLabel(why)
            summary.Wrap(max(200, pnl.GetClientSize().width - 24))
            summary.Show()
            if trace:
                details.Expand()
            for w in (src, out, want_3d, std, tol, close):
                w.Enable()
            go.SetLabel("Try again")
            refresh()
            return relayout()
        state["done"] = True
        lines = [f"Converted {len(reps)} board{'s' if len(reps) > 1 else ''} into {out_dir}"]
        gauge.SetValue(1000)
        step_lbl.SetLabel(f"Done in {int(took // 60)} min {int(took % 60)} s" if took >= 60 else f"Done in {took:.0f} s")
        eta_lbl.SetLabel("100 %")
        for r in reps or []:
            con = r.checks.get("connectivity") or {}
            drc = r.checks.get("drc") or {}
            same = con and con.get("identical", not (con["split"] or con["merged"] or con["missing"]))
            parts = [r.board]
            if con:
                parts.append("schematic and PCB nets identical" if same else "schematic/PCB nets DIFFER (see conversion-report.md)")
            if drc:
                parts.append(f"{len(drc.get('unconnected_items', []))} unconnected")
            if r.warnings:
                n = len(set(r.warnings))
                parts.append(f"{n} note{'s' if n > 1 else ''} in conversion-report.md")
            lines.append("  ·  ".join(parts))
            log_box.AppendText(f"{stamp()} {r.board}: {r.out_dir}\n")
            for w in sorted(set(r.warnings)):
                log_box.AppendText(f"           note: {w}\n")
        pros = [(r.board, os.path.join(r.out_dir, os.path.basename(r.out_dir) + ".kicad_pro")) for r in reps or []]
        if len(pros) <= 2:
            first = None
            for board, pro in reversed(pros):
                first = b = wx.Button(pnl, label=f"Open {board} in KiCad")
                b.Bind(wx.EVT_BUTTON, lambda _e, p=pro: _open(p))
                bar.Insert(0, b, 0, wx.RIGHT, 6)
        else:  # many boards: one button with a menu
            first = b = wx.Button(pnl, label="Open board in KiCad ▾")

            def menu(_e, btn=b):
                m = wx.Menu()
                for board, pro in pros:
                    it = m.Append(wx.ID_ANY, board)
                    frame.Bind(wx.EVT_MENU, lambda _e, p=pro: _open(p), it)
                btn.PopupMenu(m, (0, btn.GetSize().height))
            b.Bind(wx.EVT_BUTTON, menu)
            bar.Insert(0, b, 0, wx.RIGHT, 6)
        summary.SetForegroundColour(wx.NullColour)
        summary.SetLabel("\n".join(lines))
        summary.Show()
        b = wx.Button(pnl, label="Show folder")
        b.Bind(wx.EVT_BUTTON, lambda _e: _open(out_dir))
        bar.Insert(0, b, 0, wx.RIGHT, 6)
        go.Hide()  # this import is finished: what is left to do is open the result
        close.Enable()
        if first:
            first.SetDefault()  # Enter opens the (first) board
            first.SetFocus()
        relayout()

    def on_go(_e):
        path_in = src.GetPath()
        if not is_project(path_in):
            return wx.MessageBox("Pick the EasyEDA Pro project (.epro, .epro2 or .zip) first.", title, wx.OK, frame)
        out_dir = out.GetPath() or out_for(path_in)
        src_dir = os.path.dirname(os.path.abspath(path_in))
        if os.path.abspath(out_dir) == src_dir:
            out_dir = out_for(path_in)
        # next time: start in this folder, with these options, and under the same parent if the user picked one
        parent = os.path.dirname(os.path.abspath(out_dir))
        save_prefs(last_dir=src_dir, download_3d=want_3d.GetValue(), kicad_symbols=std.GetValue(),
                   tolerance=tol.GetValue(),
                   out_parent="" if parent == src_dir else parent)
        state["running"] = True
        for w in (src, out, want_3d, std, tol, go):  # Close stays: it asks before stopping a running import
            w.Disable()
        # a second try after a failure starts from a clean slate
        pr.update(a=0.0, b=0.0, est=1.0, label="", sub=None, f=0.0, eta=None)
        gauge.SetValue(0)
        step_lbl.SetForegroundColour(wx.NullColour)
        step_lbl.SetLabel("Starting…")
        eta_lbl.SetLabel("")
        summary.Hide()
        for w in (gauge, step_lbl, eta_lbl, details):
            w.Show()
        relayout()
        t0[0] = pr["t"] = time.time()
        timer.Start(150)
        opts = Options(download_3d=want_3d.GetValue(), kicad_symbols=std.GetValue(),
                       tolerance=0.003 if tol.GetValue() else 0.0,
                       progress=lambda *a: wx.CallAfter(set_progress, *a))

        def work():
            reps, err, trace = None, None, ""
            try:
                reps = convert_project(path_in, out_dir, opts, log=log)
            except Exception as e:  # show the error in the window instead of losing it
                err, trace = e, traceback.format_exc()
            wx.CallAfter(done, path_in, out_dir, reps, err, trace)
        threading.Thread(target=work, daemon=True).start()

    def on_close(e):
        # closing ends the process and with it a running conversion: not by accident
        if state["running"] and e.CanVeto():
            if wx.MessageBox("The import is still running.\n\nStop it and close this window? What has been written so "
                             "far stays in the output folder.", title, wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
                             frame) != wx.YES:
                return e.Veto()
        timer.Stop()
        frame.Destroy()

    go.Bind(wx.EVT_BUTTON, on_go)
    close.Bind(wx.EVT_BUTTON, lambda _e: frame.Close())
    frame.Bind(wx.EVT_CLOSE, on_close)
    refresh()
    frame.Centre()
    frame.Show()
    frame.Raise()
    if not path:
        wx.CallAfter(choose)
    app.MainLoop()
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "import":
        sys.exit(action_import(sys.argv[2] if len(sys.argv) > 2 else ""))
    sys.exit({"lcsc": action_lcsc}.get(cmd, lambda: print(__doc__) or 2)())
