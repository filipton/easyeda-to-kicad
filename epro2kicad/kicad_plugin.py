"""Toolbar actions of the KiCad plugin (launched by KiCad through the scripts the installer writes).

    python -m epro2kicad.kicad_plugin import   # pick an EasyEDA Pro project, translate it, open the result
    python -m epro2kicad.kicad_plugin lcsc     # make sure the LCSC provider for the Remote Symbols panel runs
"""

from __future__ import annotations

import math
import os
import platform
import subprocess
import sys
import threading
import time


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


def action_lcsc() -> int:
    wx, _ = _app()
    if start_provider():
        wx.MessageBox("LCSC search is running.\n\n"
                      "Open it in the schematic editor with the Remote Symbols button under Place Symbol "
                      "(or Shift+A).\n\n"
                      "Search, click Place, and the part is on your cursor. Symbol, footprint and 3D "
                      "model are saved into the project's RemoteLibrary folder.", "LCSC parts", wx.OK | wx.ICON_INFORMATION)
        return 0
    wx.MessageBox("Could not start the LCSC provider.\nSee ~/.epro2kicad-provider.log", "LCSC parts",
                  wx.OK | wx.ICON_ERROR)
    return 1


def action_import(path: str = "") -> int:
    wx, app = _app()
    from .convert import Options, convert_project, proj_name

    def default_out(src):
        return os.path.join(os.path.dirname(src), proj_name(os.path.splitext(os.path.basename(src))[0]) + "-kicad")

    # floats above KiCad like its Remote Symbols window
    frame = wx.Frame(None, title="Import EasyEDA Pro project", size=(760, 300),
                     style=wx.DEFAULT_FRAME_STYLE | wx.STAY_ON_TOP)
    pnl = wx.Panel(frame)
    src = wx.FilePickerCtrl(pnl, path=path, message="EasyEDA Pro project to import",
                            wildcard="EasyEDA Pro project (*.epro;*.epro2;*.zip)|*.epro;*.epro2;*.zip|All files|*",
                            style=wx.FLP_OPEN | wx.FLP_FILE_MUST_EXIST | wx.FLP_USE_TEXTCTRL)
    out = wx.DirPickerCtrl(pnl, path=default_out(path) if path else "", message="Where to create the KiCad project(s)",
                           style=wx.DIRP_USE_TEXTCTRL)
    want_3d = wx.CheckBox(pnl, label="Download 3D models for the footprints (internet, can take a minute)")
    std = wx.CheckBox(pnl, label="Use KiCad's standard symbols for standard parts (resistors, capacitors, diodes, "
                                 "transistors,\nconnectors, op-amps, ICs from KiCad's libraries); wiring is kept")
    want_3d.SetValue(True)
    std.SetValue(True)
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
    prog_row = wx.BoxSizer(wx.HORIZONTAL)
    prog_row.Add(step_lbl, 1, wx.ALIGN_CENTER_VERTICAL)
    prog_row.Add(eta_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 12)
    top = wx.BoxSizer(wx.VERTICAL)
    top.Add(form, 0, wx.EXPAND | wx.ALL, 12)
    top.Add(want_3d, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
    top.Add(std, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
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

    def on_src(_e):
        if src.GetPath():
            out.SetPath(default_out(src.GetPath()))
    src.Bind(wx.EVT_FILEPICKER_CHANGED, on_src)

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

    def done(path_in, out_dir, reps, err):
        timer.Stop()
        took = time.time() - t0[0]
        lines = []
        if err:
            log_box.AppendText(f"{stamp()} FAILED: {err}\n")
            step_lbl.SetLabel("Import failed")
            step_lbl.SetForegroundColour(wx.Colour(200, 60, 40))
            eta_lbl.SetLabel("")
            details.Expand()
        else:
            gauge.SetValue(1000)
            step_lbl.SetLabel(f"Done in {int(took // 60)} min {int(took % 60)} s" if took >= 60 else f"Done in {took:.0f} s")
            eta_lbl.SetLabel("100 %")
        for r in reps or []:
            con = r.checks.get("connectivity") or {}
            drc = r.checks.get("drc") or {}
            same = con and not (con["split"] or con["merged"] or con["missing"])
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
            for board, pro in reversed(pros):
                b = wx.Button(pnl, label=f"Open {board}")
                b.Bind(wx.EVT_BUTTON, lambda _e, p=pro: _open(p))
                bar.Insert(0, b, 0, wx.RIGHT, 6)
        else:  # many boards: one button with a menu
            b = wx.Button(pnl, label="Open board ▾")

            def menu(_e, btn=b):
                m = wx.Menu()
                for board, pro in pros:
                    it = m.Append(wx.ID_ANY, board)
                    frame.Bind(wx.EVT_MENU, lambda _e, p=pro: _open(p), it)
                btn.PopupMenu(m, (0, btn.GetSize().height))
            b.Bind(wx.EVT_BUTTON, menu)
            bar.Insert(0, b, 0, wx.RIGHT, 6)
        summary.SetLabel("\n".join(lines))
        summary.Show(bool(lines))
        b = wx.Button(pnl, label="Show folder")
        b.Bind(wx.EVT_BUTTON, lambda _e: _open(out_dir))
        bar.Insert(0, b, 0, wx.RIGHT, 6)
        close.Enable()
        relayout()

    def on_go(_e):
        path_in = src.GetPath()
        if not os.path.isfile(path_in):
            return wx.MessageBox("Pick the EasyEDA Pro project (.epro, .epro2 or .zip) first.", "Import", wx.OK, frame)
        out_dir = out.GetPath() or default_out(path_in)
        if os.path.abspath(out_dir) == os.path.abspath(os.path.dirname(path_in)):
            out_dir = default_out(path_in)
        for w in (src, out, want_3d, std, go, close):
            w.Disable()
        for w in (gauge, step_lbl, eta_lbl, details):
            w.Show()
        relayout()
        t0[0] = pr["t"] = time.time()
        timer.Start(150)
        opts = Options(download_3d=want_3d.GetValue(), kicad_symbols=std.GetValue(),
                       progress=lambda *a: wx.CallAfter(set_progress, *a))

        def work():
            reps, err = None, None
            try:
                reps = convert_project(path_in, out_dir, opts, log=log)
            except Exception as e:  # show the error in the window instead of losing it
                err = e
            wx.CallAfter(done, path_in, out_dir, reps, err)
        threading.Thread(target=work, daemon=True).start()

    go.Bind(wx.EVT_BUTTON, on_go)
    close.Bind(wx.EVT_BUTTON, lambda _e: frame.Close())
    frame.Centre()
    frame.Show()
    frame.Raise()
    app.MainLoop()
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "import":
        sys.exit(action_import(sys.argv[2] if len(sys.argv) > 2 else ""))
    sys.exit({"lcsc": action_lcsc}.get(cmd, lambda: print(__doc__) or 2)())
