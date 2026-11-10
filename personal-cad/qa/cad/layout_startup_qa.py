"""Validate personal layout restoration across isolated launches of the macOS app."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import traceback
from xml.etree import ElementTree


CASES = ("valid", "corrupt-digest", "unknown-version", "legacy", "removed-dock", "invalid-geometry")
ENV_PREFIX = "FREECAD_LAYOUT_QA_"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def write_report(path, report):
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def run_macro():
    import FreeCAD as App
    import FreeCADGui as Gui
    from PySide import QtCore, QtWidgets

    case = os.environ[ENV_PREFIX + "CASE"]
    output = Path(os.environ[ENV_PREFIX + "REPORT"])
    window = Gui.getMainWindow()
    settings = App.ParamGet("User parameter:BaseApp/Preferences/MainWindow")
    report = {"status": "failed", "case": case}

    def capture():
        try:
            dock = window.findChild(QtWidgets.QDockWidget, "Model")
            require(dock is not None, "Startup Model dock is missing")
            report["dock_area"] = window.dockWidgetArea(dock).value
            report["restored_flag"] = settings.GetBool("WindowStateRestored", False)
            geometry = window.frameGeometry()
            report["frame"] = [geometry.x(), geometry.y(), geometry.width(), geometry.height()]
            report["frame_reachable"] = any(
                screen.availableGeometry().adjusted(-10, -32, 10, 10).contains(geometry.topLeft())
                for screen in QtWidgets.QApplication.screens()
            )
            require(report["frame_reachable"], "Main window opened outside the usable displays")
            report["status"] = "passed"
        except Exception:
            report["error"] = traceback.format_exc()
        write_report(output, report)
        if report["status"] != "passed":
            os._exit(1)
        Gui.runCommand("Std_Quit", 0)

    def start():
        try:
            if case != "seed":
                capture()
                return
            dock = window.findChild(QtWidgets.QDockWidget, "Model")
            require(dock is not None, "Startup Model dock is missing")
            before = window.dockWidgetArea(dock)
            report["default_dock_area"] = before.value
            right = QtCore.Qt.DockWidgetArea.RightDockWidgetArea
            left = QtCore.Qt.DockWidgetArea.LeftDockWidgetArea
            require(before != QtCore.Qt.DockWidgetArea.NoDockWidgetArea, "Model dock is undocked")
            window.addDockWidget(right if before != right else left, dock)
            dock.show()
            window.resize(850, 620)
            screen = QtWidgets.QApplication.primaryScreen()
            window.move(screen.availableGeometry().topLeft() + QtCore.QPoint(30, 35))
            QtCore.QTimer.singleShot(300, capture)
        except Exception:
            report["error"] = traceback.format_exc()
            write_report(output, report)
            os._exit(1)

    QtCore.QTimer.singleShot(250, start)


def config_group(tree):
    group = tree.getroot().find(
        ".//FCParamGroup[@Name='BaseApp']/FCParamGroup[@Name='Preferences']"
        "/FCParamGroup[@Name='MainWindow']"
    )
    require(group is not None, "Missing MainWindow settings")
    return group


def config_item(group, key):
    item = group.find(f"*[@Name='{key}']")
    require(item is not None, f"Missing layout key {key}")
    return item


def layout_saved(path):
    group = config_group(ElementTree.parse(path))
    version = int(config_item(group, "LayoutVersion").get("Value"))
    state = base64.b64decode(config_item(group, "MainWindowState").text, validate=True)
    checksum = config_item(group, "LayoutDigest").text
    require(version > 0 and len(state) > 8, "Missing saved Qt layout data")
    require(hashlib.sha256(state).hexdigest() == checksum, "Saved layout checksum is invalid")
    return {"layout_version": version, "layout_digest": checksum, "state_bytes": len(state)}


def write_scenario(seed, target, case):
    tree = ElementTree.parse(seed)
    group = config_group(tree)
    if case == "corrupt-digest":
        config_item(group, "LayoutDigest").text = "0" * 64
    elif case == "unknown-version":
        config_item(group, "LayoutVersion").set("Value", "99999")
    elif case == "legacy":
        group.remove(config_item(group, "LayoutVersion"))
    elif case == "removed-dock":
        item = config_item(group, "LayoutDocks")
        docks = base64.b64decode(item.text, validate=True)
        item.text = base64.b64encode(docks + b"\nRetiredDockFixture").decode("ascii")
    elif case == "invalid-geometry":
        config_item(group, "Geometry").text = "2147483647 0 900 700"
    tree.write(target, encoding="utf-8", xml_declaration=True)


def launch(executable, profile, output, case, timeout):
    profile.mkdir()
    (profile / "data").mkdir()
    (profile / "temp").mkdir()
    macro = profile / "LayoutQA.FCMacro"
    code = str(Path(__file__).resolve().parent)
    macro.write_text(
        f"import sys\nsys.path.insert(0, {code!r})\n"
        "from layout_startup_qa import run_macro\nrun_macro()\n",
        encoding="utf-8",
    )
    user_cfg = profile / "user.cfg"
    if case != "seed":
        write_scenario(profile.parent / "seed/user.cfg", user_cfg, case)
    report_path = output / f"{case}.json"
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(profile),
        "TMPDIR": str(profile / "temp"),
        "LANG": "en_US.UTF-8",
        "FREECAD_USER_HOME": str(profile),
        "FREECAD_USER_DATA": str(profile / "data"),
        "FREECAD_USER_TEMP": str(profile / "temp"),
        "PYTHONDONTWRITEBYTECODE": "1",
        ENV_PREFIX + "CASE": case,
        ENV_PREFIX + "REPORT": str(report_path),
    }
    command = [
        str(executable), "--user-cfg", str(user_cfg),
        "--system-cfg", str(profile / "system.cfg"), str(macro),
    ]
    with (output / f"{case}.log").open("wb") as log:
        completed = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   timeout=timeout, check=False)
    require(completed.returncode == 0 and report_path.is_file(), f"Layout launch failed for {case}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    require(report["status"] == "passed", f"Layout probe failed for {case}")
    report["saved"] = layout_saved(user_cfg)
    write_report(report_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path, help="Path to the PersonalCAD app bundle")
    parser.add_argument("--output", required=True, type=Path, help="A new evidence directory")
    parser.add_argument("--timeout", type=int, default=60, help="Seconds per launch")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    app = args.app.resolve()
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    executable = app / "Contents/MacOS" / info["CFBundleExecutable"]
    if not os.access(executable, os.X_OK):
        parser.error(f"Cannot execute {executable}")
    output = args.output.resolve()
    output.mkdir(parents=True)
    result = {"status": "failed", "application": str(app), "cases": {}}
    try:
        with tempfile.TemporaryDirectory(prefix="freecad-layout-startup-qa-") as folder:
            profile = Path(folder)
            seed = launch(executable, profile / "seed", output, "seed", args.timeout)
            result["cases"]["seed"] = seed
            require(seed["dock_area"] != seed["default_dock_area"], "Seed dock relocation failed")
            for case in CASES:
                observed = launch(executable, profile / case, output, case, args.timeout)
                result["cases"][case] = observed
                expected_area = seed["dock_area"] if case == "valid" else seed["default_dock_area"]
                expected_flag = not seed["restored_flag"] if case == "valid" else seed["restored_flag"]
                require(observed["dock_area"] == expected_area, f"Unexpected Model dock area for {case}")
                require(observed["restored_flag"] == expected_flag, f"Unexpected restore event for {case}")
                require(observed["saved"]["layout_version"] == seed["saved"]["layout_version"],
                        f"Layout did not save back the current version for {case}")
        result["status"] = "passed"
    except Exception:
        result["error"] = traceback.format_exc()
    write_report(output / "run-result.json", result)
    print(f"{result['status']} {output / 'run-result.json'}")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
