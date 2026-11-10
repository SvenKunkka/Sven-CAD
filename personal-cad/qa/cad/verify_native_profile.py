"""Launch the signed PersonalCAD bundle through LaunchServices and verify isolation."""

import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import subprocess

MACRO = r'''
import json
import os
from pathlib import Path
import sys
import traceback
import FreeCAD as App
import FreeCADGui as Gui
from PySide import QtCore, QtWidgets

def verify_start_branding(expected, output):
    window = Gui.getMainWindow()
    start = next(widget for widget in window.findChildren(QtWidgets.QWidget)
                 if widget.metaObject().className().endswith("StartView") and widget.isVisible())
    pages = start.findChild(QtWidgets.QStackedWidget)
    pages.setCurrentIndex(0)
    themes = start.findChild(QtWidgets.QWidget, "ThemeSelectorWidget")
    theme_names = [button.text() for button in themes.findChildren(QtWidgets.QToolButton)
                   if button.isVisible()]
    assert theme_names and all(expected in name for name in theme_names), theme_names
    pages.setCurrentIndex(1)
    labels = [label.text() for label in start.findChildren(QtWidgets.QLabel) if label.isVisible()]
    description, = [text for text in labels if expected in text and "文件" in text]
    assert not any("FreeCAD" in text for text in labels), labels
    assert window.grab().save(str(output / "start-screen.png"))
    observed = {}

    def inspect_about():
        dialog = QtWidgets.QApplication.activeModalWidget()
        try:
            assert dialog is not None
            author = dialog.findChild(QtWidgets.QLabel, "labelAuthor")
            artwork = dialog.findChild(QtWidgets.QLabel, "labelSplashPicture")
            assert expected in author.text(), author.text()
            assert not artwork.pixmap().isNull()
            assert artwork.pixmap().save(str(output / "about-wordmark.png"))
            assert dialog.grab().save(str(output / "about-screen.png"))
            observed["title"] = dialog.windowTitle()
        except Exception:
            observed["error"] = traceback.format_exc()
        finally:
            if dialog is not None:
                dialog.reject()

    QtCore.QTimer.singleShot(100, inspect_about)
    Gui.runCommand("Std_About")
    assert "title" in observed, observed
    return {"start_description": description, "theme_names": theme_names,
            "about_title": observed["title"]}

def verify_profile():
    marker = Path(os.environ["PERSONALCAD_PROFILE_EVIDENCE"])
    result = {"status": "failed", "pid": os.getpid()}
    try:
        result["identity"] = {key: App.ConfigGet(key) for key in
            ("Application", "ExeName", "ExeVendor", "DesktopFileName", "UserAppData",
             "UserConfigPath", "UserCachePath", "AppTempPath", "UserParameter", "SystemParameter")}
        result["resource_directory"] = str(Path(App.getResourceDir()).resolve())
        result["version"] = list(App.Version())
        result["runtime_source_revision"] = App.ConfigGet("BuildRevisionHash")
        result["qt_locale"] = QtCore.QLocale().name()
        result["translated_unnamed"] = QtCore.QCoreApplication.translate("StdCmdNew", "Unnamed")
        result["length"] = App.Units.Quantity("1 mm").UserString
        result["legacy_addon_path"] = any(
            "Library/Application Support/FreeCAD/Mod" in path for path in sys.path
        )
        assert result["identity"]["ExeName"] == "PersonalCAD"
        assert result["identity"]["ExeVendor"] == "PersonalCAD"
        assert result["identity"]["DesktopFileName"] == "local.personalcad.PersonalCAD"
        home = Path(os.environ["FREECAD_USER_HOME"]).resolve()
        for key in ("UserAppData", "UserConfigPath", "UserCachePath", "AppTempPath",
                    "UserParameter", "SystemParameter"):
            path = Path(result["identity"][key]).resolve()
            assert path.is_relative_to(home), (key, path)
        assert result["qt_locale"].startswith("zh"), result["qt_locale"]
        assert result["translated_unnamed"] != "Unnamed"
        assert result["length"].endswith("mm"), result["length"]
        assert not result["legacy_addon_path"]
        if expected := os.environ.get("SVEN_EXPECT_START_BRANDING"):
            result["startup_branding"] = verify_start_branding(expected, marker.parent)
        path = Path(os.environ["PERSONALCAD_PROFILE_SAMPLE"])
        doc = App.openDocument(str(path))
        result["opened_objects"] = len(doc.Objects)
        assert doc.Housing.Shape.isValid()
        Gui.updateGui()
        result["window_title"] = Gui.getMainWindow().windowTitle()
        App.closeDocument(doc.Name)
        result["status"] = "passed"
    except Exception:
        result["error"] = traceback.format_exc()
    finally:
        marker.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        Gui.runCommand("Std_Quit", 0)

QtCore.QTimer.singleShot(300, verify_profile)
'''

HANDLER = r'''
#import <AppKit/AppKit.h>
#include <stdio.h>
int main(int argc, char** argv) {
    @autoreleasepool {
        if (argc != 2) return 64;
        NSURL *file = [NSURL fileURLWithPath:[NSString stringWithUTF8String:argv[1]]];
        NSURL *handler = [NSWorkspace.sharedWorkspace URLForApplicationToOpenURL:file];
        if (handler) {
            printf("%s\n", [NSBundle bundleWithURL:handler].bundleIdentifier.UTF8String ?: "");
        }
        return 0;
    }
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-display-name")
    parser.add_argument("--check-start-branding", action="store_true")
    args = parser.parse_args()
    if args.check_start_branding and not args.expected_display_name:
        parser.error("--check-start-branding requires --expected-display-name")
    app = args.app.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True)
    home = output / "profile"
    temp = home / "temp"
    data = home / "data"
    temp.mkdir(parents=True)
    data.mkdir()
    source = output / "file_handler.m"
    source.write_text(HANDLER)
    probe = output / "file-handler"
    subprocess.run(["/usr/bin/xcrun", "--sdk", "macosx", "clang", "-arch", "arm64",
                    "-fobjc-arc", "-framework", "AppKit", str(source), "-o", str(probe)], check=True)

    plist = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    assert plist["CFBundleIdentifier"] == "local.personalcad.PersonalCAD"
    assert plist["CFBundleDocumentTypes"][0]["LSHandlerRank"] == "Alternate"
    if args.expected_display_name:
        for key in ("CFBundleName", "CFBundleDisplayName"):
            assert plist.get(key) == args.expected_display_name, (key, plist.get(key))
    sample = output / "housing-copy.FCStd"
    shutil.copy2(args.sample.resolve(), sample)
    sample_hash = hashlib.sha256(sample.read_bytes()).hexdigest()
    handler_before = subprocess.check_output([probe, sample], text=True).strip()
    macro = output / "verify_profile.FCMacro"
    macro.write_text(MACRO)
    marker = output / "profile-result.json"
    for name in ("open.stdout", "open.stderr"):
        (output / name).touch()
    env = {
        "HOME": home,
        "TMPDIR": temp,
        "FREECAD_USER_HOME": home,
        "FREECAD_USER_DATA": data,
        "FREECAD_USER_TEMP": temp,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PERSONALCAD_PROFILE_EVIDENCE": marker,
        "PERSONALCAD_PROFILE_SAMPLE": sample,
    }
    if args.check_start_branding:
        env["SVEN_EXPECT_START_BRANDING"] = args.expected_display_name
    environment_options = [arg for key, value in env.items() for arg in ("--env", f"{key}={value}")]
    subprocess.run([
        "/usr/bin/open", "-n", "-W", "-g", "--stdout", str(output / "open.stdout"),
        "--stderr", str(output / "open.stderr"), *environment_options, str(app), "--args",
        "--user-cfg", str(home / "user.cfg"), "--system-cfg", str(home / "system.cfg"), str(macro),
    ], timeout=180, check=True)
    result = json.loads(marker.read_text())
    assert result["status"] == "passed", result.get("error")
    assert Path(result["resource_directory"]).is_relative_to(app / "Contents/Resources"), result
    if args.expected_display_name:
        assert result["identity"]["Application"] == args.expected_display_name
        assert args.expected_display_name in result["window_title"], result["window_title"]
    bundle_revision = plist.get("SvenCADSourceRevision")
    if bundle_revision:
        assert result["runtime_source_revision"] == bundle_revision, result
        assert ".".join(result["version"][:3]) == plist["CFBundleShortVersionString"], result
    handler_after = subprocess.check_output([probe, sample], text=True).strip()
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    result.update({
        "bundle_identifier": plist["CFBundleIdentifier"], "stock_handler_before": handler_before,
        "stock_handler_after": handler_after,
        "bundle_short_version": plist["CFBundleShortVersionString"],
        "bundle_version": plist["CFBundleVersion"],
        "bundle_source_revision": bundle_revision,
        "fixture_sha256": sample_hash, "strict_signature_after_launch": True,
    })
    assert hashlib.sha256(sample.read_bytes()).hexdigest() == sample_hash
    if handler_before:
        assert handler_before == handler_after, (handler_before, handler_after)
    marker.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(marker)


if __name__ == "__main__":
    main()
