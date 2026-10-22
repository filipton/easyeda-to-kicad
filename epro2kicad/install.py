"""Install / remove the KiCad 10 plugin.

    python3 -m epro2kicad.install              # install (close KiCad first so the LCSC provider can be registered)
    python3 -m epro2kicad.install --uninstall
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys

from .provider import BASE, kicad_config_dir

PLUGIN_ID = "com.github.epro2kicad"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = os.path.expanduser("~/Library/LaunchAgents/com.github.epro2kicad.provider.plist")
APP = os.path.expanduser("~/Applications/EasyEDA to KiCad.app")
SERVICES = os.path.expanduser("~/Library/Services")
SERVICE_NAMES = ("Import EasyEDA Pro Project.workflow", "Convert EasyEDA Project to KiCad.workflow")
EPRO_UTI = "com.github.epro2kicad.project"
CLOSER = os.path.expanduser("~/Applications/Close Remote Symbols.app")  # same path as provider.CLOSER
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/"
              "lsregister")


def plugins_dir() -> str:
    if platform.system() == "Linux":
        return os.path.expanduser("~/.local/share/kicad/10.0/plugins")
    return os.path.join(os.path.expanduser("~/Documents"), "KiCad", "10.0", "plugins")


def kicad_python() -> str:
    """The interpreter KiCad is configured to use for plugins (has tkinter), else this one."""
    try:
        with open(os.path.join(kicad_config_dir(), "kicad_common.json"), encoding="utf-8") as f:
            p = (json.load(f).get("api") or {}).get("interpreter_path") or ""
        if p and os.path.exists(p):
            return p
    except (OSError, ValueError):
        pass
    return sys.executable


def kicad_running() -> bool:
    if platform.system() == "Windows":
        out = subprocess.run(["tasklist"], capture_output=True, text=True).stdout.lower()
        return "kicad.exe" in out or "eeschema.exe" in out
    r = subprocess.run(["pgrep", "-ix", "kicad|eeschema|pcbnew"], capture_output=True)
    return r.returncode == 0


def provider_registered() -> bool:
    try:
        with open(os.path.join(kicad_config_dir(), "eeschema.json"), encoding="utf-8") as f:
            provs = (json.load(f).get("remote_symbols") or {}).get("providers") or []
    except (OSError, ValueError):
        return False
    return any(str(p.get("metadata_url", "")).startswith(BASE) for p in provs)


def register_provider(remove: bool = False) -> bool:
    path = os.path.join(kicad_config_dir(), "eeschema.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return False
    rs = cfg.setdefault("remote_symbols", {})
    provs = [p for p in rs.get("providers") or [] if not str(p.get("metadata_url", "")).startswith(BASE)]
    if not remove:
        # KiCad fills in provider_id itself when it is empty
        provs.append({"provider_id": "", "metadata_url": BASE, "display_name_override": "LCSC",
                      "last_account_label": "", "last_auth_status": "signed_out"})
    rs["providers"] = provs
    shutil.copy(path, path + ".bak")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return True


PANEL_ACTION = "eeschema.RemoteSymbols.showPanel"
PANEL_HOTKEY = "Shift+A"
# KiCad 10's default right toolbar of the schematic editor (toolbars_sch_editor.cpp), with the panel after Place Symbol
_RIGHT_TOOLBAR = [
    ("TB_GROUP", "Selection modes", ["common.Interactive.selectSetRect", "common.Interactive.selectSetLasso"]),
    "eeschema.EditorControl.highlightNetTool", None,
    "eeschema.InteractiveDrawing.placeSymbol", PANEL_ACTION, "eeschema.InteractiveDrawing.placePowerSymbol",
    "eeschema.InteractiveDrawingLineWireBus.drawWires", "eeschema.InteractiveDrawingLineWireBus.drawBuses",
    "eeschema.InteractiveDrawing.placeBusWireEntry", "eeschema.InteractiveDrawing.placeNoConnect",
    "eeschema.InteractiveDrawing.placeJunction",
    ("TB_GROUP", "Labels", ["eeschema.InteractiveDrawing.placeLabel", "eeschema.InteractiveDrawing.placeClassLabel",
                            "eeschema.InteractiveDrawing.placeGlobalLabel",
                            "eeschema.InteractiveDrawing.placeHierarchicalLabel"]),
    "eeschema.InteractiveDrawing.drawRuleArea", "eeschema.InteractiveDrawing.drawSheet",
    "eeschema.InteractiveDrawing.placeSheetPin", "eeschema.InteractiveDrawing.syncAllSheetsPins", None,
    "eeschema.InteractiveDrawing.placeSchematicText", "eeschema.InteractiveDrawing.drawTextBox",
    "eeschema.InteractiveDrawing.drawTable", "eeschema.InteractiveDrawing.drawRectangle",
    "eeschema.InteractiveDrawing.drawCircle", "eeschema.InteractiveDrawing.drawArc",
    "eeschema.InteractiveDrawing.drawBezier", "eeschema.InteractiveDrawingLineWireBus.drawLines",
    "eeschema.InteractiveDrawing.placeImage", "common.Interactive.deleteTool",
]


def _tb_item(x):
    if x is None:
        return {"type": "SEPARATOR"}
    if isinstance(x, tuple):
        return {"type": "TB_GROUP", "group_name": x[1], "group_items": [_tb_item(i) for i in x[2]]}
    return {"type": "TOOL", "name": x}


def _rw_json(path: str, fn) -> None:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    fn(cfg)
    shutil.copy(path, path + ".bak")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def setup_schematic_ui() -> None:
    """Remote Symbols button under Place Symbol, Shift+A, and the panel as a floating window (closed until asked)."""
    cdir = kicad_config_dir()
    tb_path = os.path.join(cdir, "toolbars", "eeschema-toolbars.json")
    if not os.path.exists(tb_path):
        os.makedirs(os.path.dirname(tb_path), exist_ok=True)
        with open(tb_path, "w", encoding="utf-8") as f:
            json.dump({"meta": {"version": 1}, "toolbars": []}, f)

    def toolbar(cfg):
        tbs = cfg.setdefault("toolbars", [])
        right = next((t for t in tbs if str(t.get("name", "")).upper() == "RIGHT"), None)
        if right is None:
            tbs.append({"name": "RIGHT", "contents": [_tb_item(x) for x in _RIGHT_TOOLBAR]})
            return
        items = right.setdefault("contents", [])
        if any(i.get("name") == PANEL_ACTION for i in items):
            return
        at = next((k + 1 for k, i in enumerate(items) if i.get("name") == "eeschema.InteractiveDrawing.placeSymbol"),
                  len(items))
        items.insert(at, _tb_item(PANEL_ACTION))
    _rw_json(tb_path, toolbar)

    def eeschema(cfg):
        cfg.setdefault("appearance", {})["custom_toolbars"] = True
        aui = cfg.setdefault("aui", {})
        aui["remote_symbol_show"] = False
        if aui.get("remote_symbol_panel_float_width", -1) < 400:
            aui["remote_symbol_panel_float_width"], aui["remote_symbol_panel_float_height"] = 1100, 800
        win = cfg.setdefault("window", {})
        parts = (win.get("perspective") or "").split("|")
        for k, part in enumerate(parts):
            if part.startswith("name=RemoteSymbol;"):
                kv = dict(x.split("=", 1) for x in part.split(";") if "=" in x)
                kv["state"] = str(int(kv.get("state", "0")) | 1)  # wxAuiPaneInfo::optionFloating
                if int(kv.get("floatw", -1)) < 400:
                    kv.update(floatw="1100", floath="800", floatx="120", floaty="120")
                parts[k] = ";".join(f"{a}={b}" for a, b in kv.items())
        win["perspective"] = "|".join(parts)
    _rw_json(os.path.join(cdir, "eeschema.json"), eeschema)

    hk = os.path.join(cdir, "user.hotkeys")
    lines = open(hk, encoding="utf-8").read().splitlines() if os.path.exists(hk) else []
    used = {ln.split("\t")[1] for ln in lines if ln.count("\t") >= 1 and ln.startswith(("eeschema.", "common."))}
    if PANEL_HOTKEY not in used:
        lines = [ln for ln in lines if not ln.startswith(PANEL_ACTION + "\t")] + [f"{PANEL_ACTION}\t{PANEL_HOTKEY}\t"]
        with open(hk, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(lines)) + "\n")


def install_agent(py: str, pkg_parent: str) -> None:
    os.makedirs(os.path.dirname(AGENT), exist_ok=True)
    log = os.path.expanduser("~/.epro2kicad-provider.log")
    with open(AGENT, "w", encoding="utf-8") as f:
        f.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
 <key>Label</key><string>com.github.epro2kicad.provider</string>
 <key>ProgramArguments</key><array><string>{py}</string><string>-m</string><string>epro2kicad.provider</string></array>
 <key>WorkingDirectory</key><string>{os.path.expanduser("~")}</string>
 <key>EnvironmentVariables</key><dict><key>PYTHONPATH</key><string>{pkg_parent}</string></dict>
 <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
 <key>StandardOutPath</key><string>{log}</string><key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
""")
    subprocess.run(["launchctl", "unload", AGENT], capture_output=True)
    subprocess.run(["launchctl", "load", AGENT], capture_output=True)


def install_app(import_script: str) -> None:
    """macOS app: double-click / drop an .epro(.epro2) on it to translate it (KiCad can't add this to its menus)."""
    if os.path.exists(APP):
        shutil.rmtree(APP)
    os.makedirs(os.path.dirname(APP), exist_ok=True)
    q = import_script.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
on run
    do shell script quoted form of "{q}" & " > /dev/null 2>&1 &"
end run
on open theFiles
    repeat with f in theFiles
        do shell script quoted form of "{q}" & " " & quoted form of (POSIX path of f) & " > /dev/null 2>&1 &"
    end repeat
end open
'''
    subprocess.run(["osacompile", "-o", APP, "-e", script], check=True, capture_output=True)
    plist = os.path.join(APP, "Contents", "Info.plist")
    with open(plist, "rb") as f:
        import plistlib
        info = plistlib.load(f)
    info.pop("CFBundleIconName", None)  # the template's asset-catalog icon would win over ours
    info.update({
        "CFBundleName": "EasyEDA to KiCad", "CFBundleIdentifier": "com.github.epro2kicad.import",
        "CFBundleIconFile": "app",
        "UTExportedTypeDeclarations": [
            {"UTTypeIdentifier": EPRO_UTI, "UTTypeDescription": "EasyEDA Pro project",
             "UTTypeConformsTo": ["public.data"], "UTTypeIconFile": "app",
             "UTTypeTagSpecification": {"public.filename-extension": ["epro", "epro2"]}},
        ],
        "CFBundleDocumentTypes": [
            {"CFBundleTypeName": "EasyEDA Pro project", "CFBundleTypeRole": "Viewer", "LSHandlerRank": "Owner",
             "LSItemContentTypes": [EPRO_UTI], "CFBundleTypeIconFile": "app"},
            {"CFBundleTypeName": "EasyEDA Pro project (zip)", "CFBundleTypeRole": "Viewer",
             "LSHandlerRank": "Alternate", "CFBundleTypeExtensions": ["zip"]},
        ],
    })
    with open(plist, "wb") as f:
        plistlib.dump(info, f)
    shutil.copy(os.path.join(REPO, "kicad_plugin", "icons", "app.icns"), os.path.join(APP, "Contents", "Resources", "app.icns"))
    for old in ("applet.icns", "droplet.icns", "Assets.car"):
        p = os.path.join(APP, "Contents", "Resources", old)
        if os.path.exists(p):
            os.remove(p)
    subprocess.run(["codesign", "--force", "--deep", "-s", "-", APP], capture_output=True)  # re-sign after edits
    if os.path.exists(LSREGISTER):
        subprocess.run([LSREGISTER, "-f", APP], capture_output=True)


def _workflow(path: str, name: str, command: str, service: dict, meta: dict) -> None:
    """An Automator Quick Action (Services menu / Finder right-click) that runs one shell command."""
    import plistlib
    import uuid
    if os.path.exists(path):
        shutil.rmtree(path)
    os.makedirs(os.path.join(path, "Contents"))
    with open(os.path.join(path, "Contents", "Info.plist"), "wb") as f:
        plistlib.dump({"CFBundleDevelopmentRegion": "English", "CFBundleIdentifier": "com.github.epro2kicad." +
                       re.sub(r"\W", "", name).lower(), "CFBundleName": name, "CFBundlePackageType": "BNDL",
                       "CFBundleShortVersionString": "1.0", "CFBundleInfoDictionaryVersion": "6.0",
                       "NSServices": [{"NSMenuItem": {"default": name}, "NSMessage": "runWorkflowAsService",
                                       **service}]}, f)
    action = {
        "AMAccepts": {"Container": "List", "Optional": True, "Types": ["com.apple.cocoa.path"]},
        "AMActionVersion": "2.0.3", "AMApplication": ["Automator"],
        "AMParameterProperties": {k: {} for k in ("COMMAND_STRING", "CheckedForUserDefaultShell", "inputMethod",
                                                  "shell", "source")},
        "AMProvides": {"Container": "List", "Types": ["com.apple.cocoa.string"]},
        "ActionBundlePath": "/System/Library/Automator/Run Shell Script.action", "ActionName": "Run Shell Script",
        "ActionParameters": {"COMMAND_STRING": command, "CheckedForUserDefaultShell": True, "inputMethod": 1,
                             "shell": "/bin/sh", "source": ""},
        "BundleIdentifier": "com.apple.RunShellScript", "CFBundleVersion": "2.0.3",
        "CanShowSelectedItemsWhenRun": False, "CanShowWhenRun": True, "Category": ["AMCategoryUtilities"],
        "Class Name": "RunShellScriptAction", "InputUUID": str(uuid.uuid4()).upper(),
        "OutputUUID": str(uuid.uuid4()).upper(), "UUID": str(uuid.uuid4()).upper(),
        "UnlocalizedApplications": ["Automator"],
    }
    with open(os.path.join(path, "Contents", "document.wflow"), "wb") as f:
        plistlib.dump({"AMApplicationBuild": "521", "AMApplicationVersion": "2.10", "AMDocumentVersion": "2",
                       "actions": [{"action": action, "isViewVisible": True}], "connectors": {},
                       "workflowMetaData": {"serviceOutputTypeIdentifier": "com.apple.Automator.nothing",
                                            "serviceProcessesInput": False,
                                            "workflowTypeIdentifier": "com.apple.Automator.servicesMenu", **meta}}, f)


def install_services(import_script: str) -> None:
    """KiCad's project manager takes no plugin buttons, so: KiCad → Services → Import EasyEDA Pro Project…
    (the macOS menu bar, assignable to a shortcut) and Finder right-click → Quick Actions on .epro/.epro2 files."""
    q = "'" + import_script.replace("'", "'\\''") + "'"
    _workflow(os.path.join(SERVICES, SERVICE_NAMES[0]), "Import EasyEDA Pro Project…",
              f"{q} > /dev/null 2>&1 &",
              {"NSRequiredContext": {"NSApplicationIdentifier": "org.kicad.kicad"}},
              {"serviceApplicationBundleID": "org.kicad.kicad", "serviceApplicationPath": "/Applications/KiCad/KiCad.app",
               "serviceInputTypeIdentifier": "com.apple.Automator.nothing"})
    _workflow(os.path.join(SERVICES, SERVICE_NAMES[1]), "Convert to KiCad",
              f'for f in "$@"; do {q} "$f" > /dev/null 2>&1 & done',
              {"NSRequiredContext": {"NSApplicationIdentifier": "com.apple.finder"}, "NSSendFileTypes": [EPRO_UTI]},
              {"serviceApplicationBundleID": "com.apple.finder",
               "serviceApplicationPath": "/System/Library/CoreServices/Finder.app",
               "serviceInputTypeIdentifier": "com.apple.Automator.fileSystemObject"})
    subprocess.run(["/System/Library/CoreServices/pbs", "-update"], capture_output=True)


def install_closer() -> None:
    """Background applet that closes the Remote Symbols window after Place (UI scripting). Its own app so the user
    grants Accessibility to just this, not to KiCad's python. Kept as is when unchanged: macOS ties the permission
    to the signature, and rebuilding would make the user grant it again."""
    from .provider import _APPLET
    stamp = os.path.join(CLOSER, "Contents", "Resources", "close.applescript")
    try:
        with open(stamp, encoding="utf-8") as f:
            if f.read() == _APPLET:
                return
    except OSError:
        pass
    for p in [CLOSER] + [os.path.join(SERVICES, n) for n in SERVICE_NAMES]:
        if os.path.exists(p):
            shutil.rmtree(p)
    subprocess.run(["osacompile", "-o", CLOSER, "-e", _APPLET], check=True, capture_output=True)
    import plistlib
    plist = os.path.join(CLOSER, "Contents", "Info.plist")
    with open(plist, "rb") as f:
        info = plistlib.load(f)
    info.update({"CFBundleIdentifier": "com.github.epro2kicad.closepanel", "LSUIElement": True,
                 "NSAppleEventsUsageDescription": "Closes or reloads KiCad's Remote Symbols panel (after Place, "
                                                  "or when it lost its connection to KiCad)."})
    with open(plist, "wb") as f:
        plistlib.dump(info, f)
    with open(stamp, "w", encoding="utf-8") as f:
        f.write(_APPLET)
    subprocess.run(["codesign", "--force", "--deep", "-s", "-", CLOSER], capture_output=True)


def install(autostart: bool) -> int:
    dest = os.path.join(plugins_dir(), PLUGIN_ID)
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(os.path.join(REPO, "kicad_plugin"), dest)
    shutil.copytree(os.path.join(REPO, "epro2kicad"), os.path.join(dest, "epro2kicad"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    py = kicad_python()
    for name, action in (("import_easyeda.sh", "import"), ("lcsc_parts.sh", "lcsc")):
        p = os.path.join(dest, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(f'#!/bin/sh\n'
                    # not cd'ed into the plugin folder: a reinstall replaces it, and kicad-cli fails in a deleted cwd
                    f'PYTHONPATH="$(cd "$(dirname "$0")" && pwd)"; export PYTHONPATH; cd "$HOME"\n'
                    f'exec "{py}" -m epro2kicad.kicad_plugin {action} "$@"\n')
        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"plugin installed: {dest}  (python: {py})")

    if platform.system() == "Darwin":
        install_app(os.path.join(dest, "import_easyeda.sh"))
        install_closer()
        install_services(os.path.join(dest, "import_easyeda.sh"))
        print("menu bar: KiCad → Services → Import EasyEDA Pro Project…; Finder: right-click an .epro → Quick Actions")
        print(f"app: {APP}  (double-click an .epro/.epro2 in Finder, or drop it on the app)")

    if autostart and platform.system() == "Darwin":
        install_agent(py, dest)
        print("LCSC provider starts automatically at login (LaunchAgent)")
    else:
        print("LCSC provider starts when you press the 'LCSC parts' button")

    if kicad_running():
        print("\nKiCad is running: close it and run this again to register the LCSC provider and add the"
              f" Remote Symbols button + {PANEL_HOTKEY} (KiCad overwrites its settings when it quits).")
    else:
        if provider_registered():
            print(f"LCSC provider already registered in KiCad ({BASE})")
        elif register_provider():
            print(f"LCSC provider registered in KiCad ({BASE})")
        setup_schematic_ui()
        print(f"schematic editor: Remote Symbols button under Place Symbol (right toolbar), hotkey {PANEL_HOTKEY},"
              " opens as a separate window")
    print("\nImport buttons: right end of the top toolbar in the schematic and PCB editors; PCB editor also has"
          " Tools → External Plugins.")
    return 0


def uninstall() -> int:
    dest = os.path.join(plugins_dir(), PLUGIN_ID)
    if os.path.exists(dest):
        shutil.rmtree(dest)
    if os.path.exists(AGENT):
        subprocess.run(["launchctl", "unload", AGENT], capture_output=True)
        os.remove(AGENT)
    for p in [CLOSER] + [os.path.join(SERVICES, n) for n in SERVICE_NAMES]:
        if os.path.exists(p):
            shutil.rmtree(p)
    if os.path.exists(APP):
        if os.path.exists(LSREGISTER):
            subprocess.run([LSREGISTER, "-u", APP], capture_output=True)
        shutil.rmtree(APP)
    if not kicad_running():
        register_provider(remove=True)
    print("removed")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="epro2kicad.install")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--no-autostart", action="store_true", help="do not start the LCSC provider at login (macOS)")
    a = ap.parse_args()
    sys.exit(uninstall() if a.uninstall else install(not a.no_autostart))
