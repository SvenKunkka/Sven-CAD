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


FILE_SIZE_LIMIT = 4096
ENV_PREFIX = "FREECAD_RECOVERY_QA_"
DAMAGED_CASES = ("truncated", "bad-crc")
PROFILE_SETTINGS = """<FCParameters>
  <FCParamGroup Name="Root">
    <FCParamGroup Name="BaseApp">
      <FCParamGroup Name="Preferences">
        <FCParamGroup Name="Document">
          <FCBool Name="CreateNewDoc" Value="0"/>
          <FCBool Name="AutoSaveEnabled" Value="1"/>
          <FCInt Name="AutoSaveTimeout" Value="1"/>
          <FCBool Name="RecoveryEnabled" Value="1"/>
        </FCParamGroup>
        <FCParamGroup Name="Dialog">
          <FCBool Name="DontUseNativeDialog" Value="1"/>
        </FCParamGroup>
      </FCParamGroup>
    </FCParamGroup>
  </FCParamGroup>
</FCParameters>
"""


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
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(path)


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


def write_snapshots(app, qt_core, original, output):
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
        # Leave a third edit exclusively for the GUI's normal autosave timer.
        started = time.monotonic()
        document.Outline.setDatum("Length", app.Units.Quantity("133 mm"))
        document.recompute()
        report["autosaved_model"] = document_metrics(document)
        require(report["autosaved_model"]["size_mm"][0] == 133, "Timed edit did not propagate")
        report["transient_directory"] = str(transient)
    except Exception:
        report["error"] = traceback.format_exc()
        write_report(output / "write.json", report)
        return

    def wait_for_autosave():
        try:
            autosaved = archive.read_bytes()
            if autosaved == retry:
                qt_core.QTimer.singleShot(250, wait_for_autosave)
                return
            report["autosave_elapsed_seconds"] = round(time.monotonic() - started, 3)
            report["autosave_archive"] = zip_metrics(autosaved)
            require(report["autosave_archive"]["valid"], "Timed autosave produced an invalid archive")
            report["status"] = "observed"
        except Exception:
            report["error"] = traceback.format_exc()
        write_report(output / "write.json", report)

    qt_core.QTimer.singleShot(250, wait_for_autosave)


def save_separate_copy(gui, qt_core, qt_widgets, path):
    selected_files = []

    def select_destination():
        dialog = qt_widgets.QApplication.activeModalWidget()
        if not isinstance(dialog, qt_widgets.QFileDialog):
            return
        timer.stop()
        dialog.setDirectory(str(path.parent))
        filename = dialog.findChild(qt_widgets.QLineEdit, "fileNameEdit")
        if filename:
            filename.setText(path.name)
        selected_files.extend(dialog.selectedFiles())
        if selected_files == [str(path)]:
            dialog.accept()
        else:
            dialog.reject()

    timer = qt_core.QTimer()
    timer.timeout.connect(select_destination)
    timer.start(100)
    gui.runCommand("Std_SaveAs", 0)
    timer.stop()
    require(path.is_file(), f"File > Save As did not create the recovered copy: {selected_files}")


def recover_at_startup(app, gui, qt_core, qt_widgets, original, output):
    report = {"status": "failed", "dialog_steps": []}
    write_result = json.loads((output / "write.json").read_text(encoding="utf-8"))
    deadline = time.monotonic() + 30
    timer = qt_core.QTimer(gui.getMainWindow())
    damaged_labels = (
        [f"Damaged recovery QA {case}" for case in DAMAGED_CASES]
        if os.environ[ENV_PREFIX + "VERIFY_DAMAGE"] == "1" else []
    )

    def translate(message):
        return qt_core.QCoreApplication.translate("Gui::Dialog::DocumentRecovery", message)

    def poll():
        try:
            require(time.monotonic() <= deadline, "Timed out waiting for the startup recovery dialog")
            dialog = qt_widgets.QApplication.activeModalWidget()
            if dialog is not None and dialog.metaObject().className().endswith("DocumentRecovery"):
                tree = dialog.findChild(qt_widgets.QTreeWidget)
                buttons = dialog.findChild(qt_widgets.QDialogButtonBox)
                require(tree is not None and buttons is not None, "Recovery dialog controls missing")
                count = tree.topLevelItemCount()
                require(count == 1 + len(damaged_labels), "Unexpected count of recovery entries")
                items = {tree.topLevelItem(i).text(0): tree.topLevelItem(i) for i in range(count)}
                item = items[write_result["recovery_label"]]
                damaged_statuses = {name: items[name].text(1) for name in damaged_labels}
                button = buttons.button(qt_widgets.QDialogButtonBox.StandardButton.Ok)
                require(button is not None, "Missing recovery confirmation button")
                require(len(report["dialog_steps"]) < 2, "Recovery dialog did not finish")
                label = dialog.findChild(qt_widgets.QLabel, "label")
                report["dialog_steps"].append({
                    "button": button.text(),
                    "valid_status": item.text(1),
                    "damaged_statuses": damaged_statuses,
                    "autosave_time": item.text(2) if tree.columnCount() > 2 else "",
                    "message": label.text() if label else "",
                })
                if len(report["dialog_steps"]) == 2:
                    require(item.text(1) == translate("Successfully recovered"),
                            "Valid snapshot was not recovered")
                    require(all(status == translate("Failed to recover")
                                for status in damaged_statuses.values()),
                            "Damaged snapshot was not rejected")
                    if os.environ[ENV_PREFIX + "EXPECT_DETAILS"] == "1":
                        require(bool(item.text(2)), "Missing autosave timestamp")
                        require(label and label.text() == translate(
                            "Select Finish to review your recovered documents. To keep a separate "
                            "copy, choose File > Save As."
                        ), "Missing Save As guidance")
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
            require(report["recovered_model"] == write_result["autosaved_model"],
                    "The recovered model differs from the unsaved edit made before the timer")
            timer.stop()
            destination = output / "recovered.FCStd"
            save_separate_copy(gui, qt_core, qt_widgets, destination)
            app.closeDocument(document.Name)
            document = app.openDocument(str(destination))
            document.recompute()
            report["reopened_model"] = document_metrics(document)
            require(report["reopened_model"] == report["recovered_model"], "Saved copy differs after reopen")
            document.Outline.setDatum("Length", app.Units.Quantity("135 mm"))
            document.recompute()
            report["subsequent_edit"] = document_metrics(document)
            require(report["subsequent_edit"]["size_mm"][0] == 135, "Recovered sketch cannot be edited")
            report["original_sha256"] = digest(original.read_bytes())
            require(report["original_sha256"] == write_result["original_sha256"],
                    "The original file was modified during startup recovery")
            report["status"] = "passed"
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
        QtCore.QTimer.singleShot(150, lambda: write_snapshots(App, QtCore, original, output))
    else:
        recover_at_startup(App, Gui, QtCore, QtWidgets, original, output)


def launch(executable, profile, output, phase, timeout, expect_details, verify_damage):
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
        ENV_PREFIX + "EXPECT_DETAILS": "1" if expect_details else "0",
        ENV_PREFIX + "VERIFY_DAMAGE": "1" if verify_damage else "0",
    }
    with (output / f"{phase}.log").open("wb") as log:
        with subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT) as child:
            try:
                if phase == "write":
                    ready = output / "write.json"
                    deadline = time.monotonic() + timeout
                    while not ready.is_file() and child.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.1)
                    require(ready.is_file(), "Writer did not reach the autosave checkpoint")
                    require(child.poll() is None, "Writer exited before the controlled crash")
                    child.kill()
                return child.wait(timeout=timeout)
            finally:
                if child.poll() is None:
                    child.kill()


def corrupt_payload(archive):
    output = io.BytesIO()
    with ZipFile(io.BytesIO(archive)) as original, ZipFile(output, "w") as stored:
        for name in original.namelist():
            stored.writestr(name, original.read(name))
        entry = next(item for item in stored.infolist()
                     if item.file_size and item.filename.endswith((".bin", ".brp")))
    data = bytearray(output.getvalue())
    # The local header is 30 bytes followed by the file name, extra fields, then the payload.
    offset = entry.header_offset + 30 + len(entry.filename.encode()) + len(entry.extra)
    data[offset] ^= 1
    corrupted = bytes(data)
    require(zip_metrics(corrupted)["bad_entry"] == entry.filename,
            "The damaged fixture must have a valid directory and a bad payload CRC")
    return corrupted


def inject_damaged_archives(profile, original, write_result):
    transient = Path(write_result["transient_directory"]).resolve()
    require(transient.is_relative_to(profile), "Recovery cache escaped the isolated profile")
    prefix, marker, instance = transient.name.partition("_Doc_")
    require(marker and "_" in instance, "Unrecognized transient directory name")
    suffix = instance.rsplit("_", 1)[1]
    archive = (transient / "fc_recovery_file.fcstd").read_bytes()
    preserved = {}
    for case, content in zip(DAMAGED_CASES, (archive[:len(archive) // 2], corrupt_payload(archive))):
        source = profile / f"{case}-source.FCStd"
        shutil.copyfile(original, source)
        damaged = transient.parent / f"{prefix}_Doc_{case}_{suffix}"
        damaged.mkdir()
        metadata = ElementTree.parse(transient / "fc_recovery_file.xml")
        metadata.find("Label").text = f"Damaged recovery QA {case}"
        metadata.find("FileName").text = str(source)
        metadata_file = damaged / "fc_recovery_file.xml"
        metadata.write(metadata_file, encoding="utf-8", xml_declaration=True)
        archive_file = damaged / "fc_recovery_file.fcstd"
        archive_file.write_bytes(content)
        for file in (source, metadata_file, archive_file):
            preserved[file] = digest(file.read_bytes())
    return preserved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path, help="Path to a macOS app bundle")
    parser.add_argument("--fixture", required=True, type=Path, help="Synthetic keyboard-housing.FCStd")
    parser.add_argument("--output", required=True, type=Path, help="A new evidence directory")
    parser.add_argument("--mode", choices=("personal", "baseline"), default="personal")
    parser.add_argument("--timeout", type=int, default=120, help="Seconds per launch")
    parser.add_argument("--expect-recovery-details", action="store_true",
                        help="Require timestamp and Save As guidance in the recovery dialog")
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
            profile = Path(path).resolve()
            (profile / "data").mkdir()
            (profile / "temp").mkdir()
            (profile / "user.cfg").write_text(PROFILE_SETTINGS, encoding="utf-8")
            original = profile / "original.FCStd"
            shutil.copyfile(fixture, original)
            code = str(Path(__file__).resolve().parent)
            (profile / "RecoveryQA.FCMacro").write_text(
                f"import sys\nsys.path.insert(0, {code!r})\n"
                "from recovery_qa import run_macro\nrun_macro()\n",
                encoding="utf-8",
            )
            verify_damage = args.mode == "personal"
            launch_args = (args.timeout, args.expect_recovery_details, verify_damage)
            result["write_exit"] = launch(executable, profile, output, "write", *launch_args)
            require(result["write_exit"] == -signal.SIGKILL, "Writer did not receive the controlled SIGKILL")
            write_result = json.loads((output / "write.json").read_text(encoding="utf-8"))
            require(write_result["status"] == "observed", "Writer report did not complete")
            damaged_files = (
                inject_damaged_archives(profile, original, write_result) if verify_damage else {}
            )
            result["recover_exit"] = launch(executable, profile, output, "recover", *launch_args)
            require(result["recover_exit"] == 0, "Startup recovery failed. See recover.json and recover.log.")
            recover_result = json.loads((output / "recover.json").read_text(encoding="utf-8"))
            require(recover_result["status"] == "passed", "Recovery verification failed")
            require(digest(original.read_bytes()) == source_hash, "The isolated original was modified")
            require(all(digest(file.read_bytes()) == before for file, before in damaged_files.items()),
                    "Preflight rejection modified a damaged snapshot, its metadata, or its original")
            require(digest(fixture.read_bytes()) == source_hash, "The source fixture was modified")
            result["limited_write"] = write_result["limited_write"]
            result["old_bytes_preserved"] = write_result["old_bytes_preserved"]
            result["old_zip_valid"] = write_result["after_failure_archive"]["valid"]
            result["metadata_preserved"] = write_result["metadata_preserved"]
            result["autosave_elapsed_seconds"] = write_result["autosave_elapsed_seconds"]
            result["damaged_snapshots_rejected"] = list(DAMAGED_CASES) if verify_damage else []
            if verify_damage:
                result["damaged_snapshot_bytes_preserved"] = True
            result["separate_copy_reopened"] = True
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
