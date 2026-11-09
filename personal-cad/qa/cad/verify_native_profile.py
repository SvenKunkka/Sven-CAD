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
from PySide import QtCore

def verify_profile():
    marker = Path(os.environ["PERSONALCAD_PROFILE_EVIDENCE"])
    result = {"status": "failed", "pid": os.getpid()}
    try:
        result["identity"] = {key: App.ConfigGet(key) for key in
            ("ExeName", "ExeVendor", "DesktopFileName", "UserAppData",
             "UserConfigPath", "UserCachePath", "AppTempPath")}
        result["qt_locale"] = QtCore.QLocale().name()
        result["translated_unnamed"] = QtCore.QCoreApplication.translate("StdCmdNew", "Unnamed")
        result["length"] = App.Units.Quantity("1 mm").UserString
        result["legacy_addon_path"] = any(
            "Library/Application Support/FreeCAD/Mod" in path for path in sys.path
        )
        path = Path(os.environ["PERSONALCAD_PROFILE_SAMPLE"])
        doc = App.openDocument(str(path))
        result["opened_objects"] = len(doc.Objects)
        assert doc.Housing.Shape.isValid()
        App.closeDocument(doc.Name)
        assert result["identity"]["ExeName"] == "PersonalCAD"
        assert result["identity"]["ExeVendor"] == "PersonalCAD"
        assert result["identity"]["DesktopFileName"] == "local.personalcad.PersonalCAD"
        for key in ("UserAppData", "UserConfigPath", "UserCachePath", "AppTempPath"):
            assert "PersonalCAD" in Path(result["identity"][key]).parts, (key, result["identity"][key])
        assert result["qt_locale"].startswith("zh"), result["qt_locale"]
        assert result["translated_unnamed"] != "Unnamed"
        assert result["length"].endswith("mm"), result["length"]
        assert not result["legacy_addon_path"]
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
    args = parser.parse_args()
    app = args.app.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True)
    home = output / "profile"
    temp = home / "temp"
    temp.mkdir(parents=True)
    source = output / "file_handler.m"
    source.write_text(HANDLER)
    probe = output / "file-handler"
    subprocess.run(["/usr/bin/xcrun", "--sdk", "macosx", "clang", "-arch", "arm64",
                    "-fobjc-arc", "-framework", "AppKit", str(source), "-o", str(probe)], check=True)

    plist = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    assert plist["CFBundleIdentifier"] == "local.personalcad.PersonalCAD"
    assert plist["CFBundleDocumentTypes"][0]["LSHandlerRank"] == "Alternate"
    sample = output / "housing-copy.FCStd"
    shutil.copy2(args.sample.resolve(), sample)
    sample_hash = hashlib.sha256(sample.read_bytes()).hexdigest()
    handler_before = subprocess.check_output([probe, sample], text=True).strip()
    macro = output / "verify_profile.FCMacro"
    macro.write_text(MACRO)
    marker = output / "profile-result.json"
    for name in ("open.stdout", "open.stderr"):
        (output / name).touch()
    subprocess.run([
        "/usr/bin/open", "-n", "-W", "-g", "--stdout", str(output / "open.stdout"),
        "--stderr", str(output / "open.stderr"), "--env", f"HOME={home}",
        "--env", f"TMPDIR={temp}", "--env", f"PERSONALCAD_PROFILE_EVIDENCE={marker}",
        "--env", f"PERSONALCAD_PROFILE_SAMPLE={sample}", str(app), "--args", str(macro),
    ], timeout=180, check=True)
    result = json.loads(marker.read_text())
    handler_after = subprocess.check_output([probe, sample], text=True).strip()
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    result.update({
        "bundle_identifier": plist["CFBundleIdentifier"], "stock_handler_before": handler_before,
        "stock_handler_after": handler_after,
        "fixture_sha256": sample_hash, "strict_signature_after_launch": True,
    })
    assert hashlib.sha256(sample.read_bytes()).hexdigest() == sample_hash
    if handler_before:
        assert handler_before == handler_after, (handler_before, handler_after)
    assert result["status"] == "passed", result.get("error")
    marker.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(marker)


if __name__ == "__main__":
    main()
