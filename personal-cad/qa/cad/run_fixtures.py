"""Run the personal-edition CAD fixtures against a macOS FreeCAD bundle."""

import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile


SCENARIO_MACROS = {
    "fixtures": "run_fixtures.FCMacro",
    "ax-table": "ax_fixture.FCMacro",
    "ax-model-tree": "ax_fixture.FCMacro",
    "ax-model-tree-root": "ax_fixture.FCMacro",
    "shutdown": "shutdown_fixture.FCMacro",
    "shutdown-direct-qt": "shutdown_fixture.FCMacro",
    "viewport": "viewport_fixture.FCMacro",
    "soak": "soak_fixture.FCMacro",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path, help="Path to the application bundle")
    parser.add_argument("--output", required=True, type=Path, help="New directory for evidence")
    parser.add_argument("--mode", choices=("baseline", "personal"), default="baseline")
    parser.add_argument("--scenario", choices=SCENARIO_MACROS, default="fixtures")
    parser.add_argument("--legacy-source", type=Path, help="Verified baseline fixture directory")
    parser.add_argument("--cocoa-plugin-path", type=Path, help="Isolated directory containing libqcocoa.dylib")
    parser.add_argument("--widgets-library-path", type=Path, help="Isolated directory containing libQt6Widgets.6.dylib")
    parser.add_argument("--duration", type=int, default=120, help="AX fixture lifespan in seconds")
    parser.add_argument("--timeout", default=240, type=int)
    args = parser.parse_args()
    if min(args.timeout, args.duration) <= 0:
        parser.error("--timeout and --duration must be positive")
    legacy_source = args.legacy_source.resolve() if args.legacy_source else None
    if args.scenario == "fixtures" and args.mode == "personal":
        baseline_files = ("report.json", "legacy-mixed.FCStd", "legacy-link-source.FCStd",
                          "keyboard-step-roundtrip.FCStd", "keyboard-assembly.FCStd")
        if legacy_source is None or not all((legacy_source / name).is_file() for name in baseline_files):
            parser.error("--mode personal requires --legacy-source with the verified baseline fixtures")

    app = args.app.resolve()
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    executable = app / "Contents/MacOS" / info["CFBundleExecutable"]
    if not os.access(executable, os.X_OK):
        parser.error(f"Cannot execute {executable}")
    plugin_path = args.cocoa_plugin_path.resolve() if args.cocoa_plugin_path else None
    if plugin_path and not (plugin_path / "libqcocoa.dylib").is_file():
        parser.error(f"No libqcocoa.dylib in {plugin_path}")
    widgets_path = args.widgets_library_path.resolve() if args.widgets_library_path else None
    if widgets_path and not (widgets_path / "libQt6Widgets.6.dylib").is_file():
        parser.error(f"No libQt6Widgets.6.dylib in {widgets_path}")

    code = Path(__file__).resolve().parent
    macro = code / SCENARIO_MACROS[args.scenario]
    if not macro.is_file():
        parser.error(f"Fixture macro is missing: {macro}")
    output = args.output.resolve()
    output.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="freecad-cad-qa-") as path:
        profile = Path(path)
        (profile / "data").mkdir()
        (profile / "temp").mkdir()
        # The official bundle launcher prints its environment. Pass only what the
        # test and the GUI need so unrelated credentials do not enter its log.
        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(profile),
            "TMPDIR": str(profile / "temp"),
            "LANG": "en_US.UTF-8",
            "FREECAD_USER_HOME": str(profile),
            "FREECAD_USER_DATA": str(profile / "data"),
            "FREECAD_USER_TEMP": str(profile / "temp"),
            "FREECAD_PERSONAL_QA_CODE": str(code),
            "FREECAD_PERSONAL_QA_OUTPUT": str(output),
            "FREECAD_PERSONAL_QA_MODE": args.mode,
            "FREECAD_PERSONAL_QA_SCENARIO": args.scenario,
            "FREECAD_PERSONAL_QA_DURATION": str(args.duration),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if plugin_path:
            env["QT_PLUGIN_PATH"] = str(plugin_path.parent)
            env["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(plugin_path)
            env["QT_DEBUG_PLUGINS"] = "1"
        if widgets_path:
            env["DYLD_LIBRARY_PATH"] = str(widgets_path)
        if legacy_source:
            env["FREECAD_PERSONAL_QA_LEGACY_SOURCE"] = str(legacy_source)
        command = [
            str(executable),
            "--user-cfg", str(profile / "user.cfg"),
            "--system-cfg", str(profile / "system.cfg"),
            str(macro),
        ]
        with (output / "freecad.log").open("wb") as log:
            try:
                completed = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT,
                                           timeout=args.timeout, check=False)
            except subprocess.TimeoutExpired:
                print(f"FreeCAD timed out after {args.timeout}s. See {log.name}.")
                return 1

    report_path = output / "report.json"
    result_path = output / "run-result.json"
    result = {
        "scenario": args.scenario,
        "application": str(app),
        "freecad_exit": completed.returncode,
        "status": "failed",
    }
    if not report_path.is_file():
        result["error"] = "FreeCAD exited without a fixture report"
        result_path.write_text(json.dumps(result, indent=2) + "\n")
        print(f"FreeCAD exited {completed.returncode} without a fixture report. See {output / 'freecad.log'}.")
        return 1
    report = json.loads(report_path.read_text())
    result["fixture_status"] = report["status"]
    result["cocoa_plugin"] = report.get("cocoa_plugin")
    result["qt_widgets"] = report.get("qt_widgets")
    result["freecad_app"] = report.get("freecad_app")
    if plugin_path:
        expected = str((plugin_path / "libqcocoa.dylib").resolve())
        actual = (report.get("cocoa_plugin") or {}).get("path")
        result["expected_cocoa_plugin"] = expected
        if expected != actual:
            result["error"] = f"Expected Cocoa plugin {expected}, loaded {actual}"
    if widgets_path:
        expected = str((widgets_path / "libQt6Widgets.6.dylib").resolve())
        actual = (report.get("qt_widgets") or {}).get("path")
        result["expected_qt_widgets"] = expected
        if expected != actual:
            result.setdefault("error", f"Expected Qt Widgets {expected}, loaded {actual}")
    if completed.returncode != 0:
        result.setdefault("error", f"FreeCAD exited {completed.returncode}")
    if report["status"] != "passed":
        result.setdefault("error", report.get("error", "Fixture assertions failed"))
    if "error" not in result:
        result["status"] = "passed"
    result_path.write_text(json.dumps(result, indent=2) + "\n")
    print(f"{result['status']}: {result_path}")
    if "error" in result:
        print(result["error"])
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
