"""Exercise compressed recovery in an isolated profile using a synthetic housing."""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import resource
import shutil
import signal
import subprocess
import tempfile
import time
import traceback
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


ABNORMAL_EXIT = 73
FILE_SIZE_LIMIT = 4096
ENV_PREFIX = "FREECAD_RECOVERY_QA_"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def zip_metrics(data):
    result = {"bytes": len(data), "sha256": digest(data), "valid": False}
    try:
        with ZipFile(io.BytesIO(data)) as archive:
            result["bad_entry"] = archive.testzip()
            result["valid"] = (
                result["bad_entry"] is None
                and ElementTree.fromstring(archive.read("Document.xml")).tag == "Document"
            )
    except (BadZipFile, KeyError, ElementTree.ParseError, OSError) as error:
        result["error"] = str(error)
    return result


def document_metrics(document):
    shape = document.Housing.Shape
    bounds = shape.BoundBox
    return {
        "size_mm": [round(length, 6) for length in (bounds.XLength, bounds.YLength, bounds.ZLength)],
        "volume_mm3": round(shape.Volume, 6),
        "solids": len(shape.Solids),
        "outline_length_mm": round(document.Outline.getDatum("Length").Value, 6),
        "constraints": {
            obj.Name: {"count": obj.ConstraintCount, "fully_constrained": obj.FullyConstrained}
            for obj in document.Objects
            if obj.TypeId == "Sketcher::SketchObject"
        },
    }


def write_report(path, report):
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def forced_write_failure(app, document):
    limits = resource.getrlimit(resource.RLIMIT_FSIZE)
    require(limits[0] == resource.RLIM_INFINITY or limits[0] > FILE_SIZE_LIMIT,
            "Parent already restricts the test process file size")
    previous = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (FILE_SIZE_LIMIT, limits[1]))
        try:
            return {"returned": app.writeRecoverySnapshotToTransientDir(document)}
        except Exception as error:
            return {"exception": type(error).__name__, "message": str(error)}
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, limits)
        signal.signal(signal.SIGXFSZ, previous)


def write_snapshots(app, original, output):
    report = {"status": "failed", "file_size_limit_bytes": FILE_SIZE_LIMIT}
    try:
        report["original_sha256"] = digest(original.read_bytes())
        document = app.openDocument(str(original))
        document.recompute()
        report["original_model"] = document_metrics(document)
        require(report["original_model"]["outline_length_mm"] == 124, "Unexpected housing fixture")
        require(all(item["fully_constrained"]
                    for item in report["original_model"]["constraints"].values()),
                "The original model must have fully constrained sketches")
        transient = Path(document.TransientDir).resolve()
        require(transient.is_relative_to(original.parent.resolve()),
                "FreeCAD recovery cache is outside the isolated profile")
        archive = transient / "fc_recovery_file.fcstd"
        metadata = transient / "fc_recovery_file.xml"

        document.Outline.setDatum("Length", app.Units.Quantity("127 mm"))
        document.recompute()
        report["first_model"] = document_metrics(document)
        require(report["first_model"]["size_mm"][0] == 127, "First edit did not propagate")
        require(app.writeRecoverySnapshotToTransientDir(document), "First recovery write failed")
        first = archive.read_bytes()
        first_metadata = metadata.read_bytes()
        report["first_archive"] = zip_metrics(first)
        require(report["first_archive"]["valid"], "Initial recovery archive has invalid ZIP data")
        require(len(first_metadata) < FILE_SIZE_LIMIT < len(first),
                "File-size limit must permit metadata and fail while writing the archive")

        document.Outline.setDatum("Length", app.Units.Quantity("131 mm"))
        document.recompute()
        report["retry_model"] = document_metrics(document)
        require(report["retry_model"]["size_mm"][0] == 131, "Retry edit did not propagate")
        report["limited_write"] = forced_write_failure(app, document)
        after_failure = archive.read_bytes() if archive.exists() else b""
        report["after_failure_archive"] = zip_metrics(after_failure)
        report["old_bytes_preserved"] = after_failure == first
        report["metadata_preserved"] = metadata.exists() and metadata.read_bytes() == first_metadata
        report["temporary_archives_after_failure"] = sorted(
            item.name for item in transient.glob("fc_recovery_file.fcstd.*")
        )

        require(app.writeRecoverySnapshotToTransientDir(document), "Retry recovery write failed")
        retry = archive.read_bytes()
        report["retry_archive"] = zip_metrics(retry)
        require(report["retry_archive"]["valid"] and retry != first, "Retry must replace the ZIP")
        info = ElementTree.parse(metadata).getroot()
        report["recovery_label"] = info.findtext("Label")
        require(info.findtext("Status") == "Created", "Retry metadata status is invalid")
        require(Path(info.findtext("FileName")).resolve() == original, "Recovery target changed")
        report["original_unchanged"] = digest(original.read_bytes()) == report["original_sha256"]
        require(report["original_unchanged"], "The original file was modified by recovery writes")
        report["status"] = "observed"
    except Exception:
        report["error"] = traceback.format_exc()
    write_report(output / "write.json", report)
    # Leave FreeCAD's normal shutdown and transient-directory cleanup uncalled.
    os._exit(ABNORMAL_EXIT if report["status"] == "observed" else 1)


def recover_at_startup(app, gui, qt_core, qt_widgets, original, output):
    report = {"status": "failed", "dialog_steps": []}
    write_result = json.loads((output / "write.json").read_text(encoding="utf-8"))
    deadline = time.monotonic() + 30
    timer = qt_core.QTimer(gui.getMainWindow())

    def poll():
        try:
            require(time.monotonic() <= deadline, "Timed out waiting for the startup recovery dialog")
            dialog = qt_widgets.QApplication.activeModalWidget()
            if dialog is not None and dialog.metaObject().className().endswith("DocumentRecovery"):
                tree = dialog.findChild(qt_widgets.QTreeWidget)
                buttons = dialog.findChild(qt_widgets.QDialogButtonBox)
                require(tree is not None and buttons is not None, "Recovery dialog controls missing")
                require(tree.topLevelItemCount() == 1, "Expected exactly one recovery document")
                item = tree.topLevelItem(0)
                require(item.text(0) == write_result["recovery_label"], "Unexpected recovery item")
                button = buttons.button(qt_widgets.QDialogButtonBox.StandardButton.Ok)
                require(button is not None, "Missing recovery confirmation button")
                require(len(report["dialog_steps"]) < 2, "Recovery dialog did not finish")
                report["dialog_steps"].append({"button": button.text(), "item_status": item.text(1)})
                button.click()
                return
            if len(report["dialog_steps"]) < 2:
                return

            documents = list(app.listDocuments().values())
            require(len(documents) == 1, "Startup did not recover the single fixture")
            document = documents[0]
            require(Path(document.FileName).resolve() == original, "Recovered original path changed")
            document.recompute()
            report["recovered_model"] = document_metrics(document)
            require(report["recovered_model"] == write_result["retry_model"],
                    "The recovered geometry or sketch constraints differ from the retry snapshot")
            report["original_sha256"] = digest(original.read_bytes())
            require(report["original_sha256"] == write_result["original_sha256"],
                    "The original file was modified during startup recovery")
            report["status"] = "passed"
            timer.stop()
            write_report(output / "recover.json", report)
            app.closeDocument(document.Name)
            qt_core.QTimer.singleShot(150, lambda: gui.runCommand("Std_Quit", 0))
        except Exception:
            timer.stop()
            report["status"] = "failed"
            report["error"] = traceback.format_exc()
            write_report(output / "recover.json", report)
            os._exit(1)

    timer.timeout.connect(poll)
    timer.start(100)


def run_macro():
    import FreeCAD as App
    import FreeCADGui as Gui
    from PySide import QtCore, QtWidgets

    original = Path(os.environ[ENV_PREFIX + "ORIGINAL"]).resolve()
    output = Path(os.environ[ENV_PREFIX + "OUTPUT"]).resolve()
    settings = App.ParamGet("User parameter:BaseApp/Preferences/Document")
    settings.SetBool("CreateNewDoc", False)
    settings.SetBool("RecoveryEnabled", True)
    if os.environ[ENV_PREFIX + "PHASE"] == "write":
        QtCore.QTimer.singleShot(150, lambda: write_snapshots(App, original, output))
    else:
        recover_at_startup(App, Gui, QtCore, QtWidgets, original, output)


def launch(executable, profile, output, phase, timeout):
    command = [
        str(executable), "--user-cfg", str(profile / "user.cfg"),
        "--system-cfg", str(profile / "system.cfg"), str(profile / "RecoveryQA.FCMacro"),
    ]
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(profile),
        "TMPDIR": str(profile / "temp"),
        "LANG": "en_US.UTF-8",
        "FREECAD_USER_HOME": str(profile),
        "FREECAD_USER_DATA": str(profile / "data"),
        "FREECAD_USER_TEMP": str(profile / "temp"),
        "PYTHONDONTWRITEBYTECODE": "1",
        ENV_PREFIX + "ORIGINAL": str(profile / "original.FCStd"),
        ENV_PREFIX + "OUTPUT": str(output),
        ENV_PREFIX + "PHASE": phase,
    }
    with (output / f"{phase}.log").open("wb") as log:
        return subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT,
                              timeout=timeout, check=False).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path, help="Path to a macOS app bundle")
    parser.add_argument("--fixture", required=True, type=Path, help="Synthetic keyboard-housing.FCStd")
    parser.add_argument("--output", required=True, type=Path, help="A new evidence directory")
    parser.add_argument("--mode", choices=("personal", "baseline"), default="personal")
    parser.add_argument("--timeout", type=int, default=90, help="Seconds per launch")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    app = args.app.resolve()
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    executable = app / "Contents/MacOS" / info["CFBundleExecutable"]
    if not os.access(executable, os.X_OK):
        parser.error(f"Cannot execute {executable}")
    fixture = args.fixture.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True)
    source_hash = digest(fixture.read_bytes())
    result = {"status": "failed", "mode": args.mode, "application": str(app),
              "fixture_sha256": source_hash}
    try:
        with tempfile.TemporaryDirectory(prefix="freecad-recovery-qa-") as path:
            profile = Path(path)
            (profile / "data").mkdir()
            (profile / "temp").mkdir()
            original = profile / "original.FCStd"
            shutil.copyfile(fixture, original)
            code = str(Path(__file__).resolve().parent)
            (profile / "RecoveryQA.FCMacro").write_text(
                f"import sys\nsys.path.insert(0, {code!r})\n"
                "from recovery_qa import run_macro\nrun_macro()\n",
                encoding="utf-8",
            )
            result["write_exit"] = launch(executable, profile, output, "write", args.timeout)
            require(result["write_exit"] == ABNORMAL_EXIT,
                    "Writer did not reach the controlled abnormal exit. See write.json and write.log.")
            write_result = json.loads((output / "write.json").read_text(encoding="utf-8"))
            require(write_result["status"] == "observed", "Writer report did not complete")
            result["recover_exit"] = launch(executable, profile, output, "recover", args.timeout)
            require(result["recover_exit"] == 0, "Startup recovery failed. See recover.json and recover.log.")
            recover_result = json.loads((output / "recover.json").read_text(encoding="utf-8"))
            require(recover_result["status"] == "passed", "Recovery verification failed")
            require(digest(original.read_bytes()) == source_hash, "The isolated original was modified")
            require(digest(fixture.read_bytes()) == source_hash, "The source fixture was modified")
            result["limited_write"] = write_result["limited_write"]
            result["old_bytes_preserved"] = write_result["old_bytes_preserved"]
            result["old_zip_valid"] = write_result["after_failure_archive"]["valid"]
            result["metadata_preserved"] = write_result["metadata_preserved"]
            if args.mode == "personal":
                require("exception" in result["limited_write"]
                        or result["limited_write"].get("returned") is False,
                        "The limited recovery write did not report a failure")
                require(result["old_bytes_preserved"] and result["old_zip_valid"],
                        "A failed write damaged the last valid recovery archive")
                require(result["metadata_preserved"], "A failed write damaged recovery metadata")
                require(not write_result["temporary_archives_after_failure"],
                        "The failed write left partial recovery archives")
            result["status"] = "passed" if args.mode == "personal" else "baseline-observed"
    except Exception:
        result["error"] = traceback.format_exc()
    write_report(output / "run-result.json", result)
    print(f"{result['status']} {output / 'run-result.json'}")
    return 0 if result["status"] != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
