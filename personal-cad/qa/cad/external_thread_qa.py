"""Verify nominal external threads through native features and the Sven CAD ribbon."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import traceback


OUTPUT_ENV = "PERSONAL_CAD_EXTERNAL_THREAD_QA"
SIZES = (("M2x0.4", 2.0, 0.4), ("M2.5x0.45", 2.5, 0.45),
         ("M3x0.5", 3.0, 0.5), ("M4x0.7", 4.0, 0.7))


def run_macro():
    import FreeCAD as App
    import FreeCADGui as Gui
    import Part
    from PySide6 import QtCore, QtTest, QtWidgets

    output = Path(os.environ[OUTPUT_ENV])
    report = {"status": "failed", "version": list(App.Version()), "checks": {}}
    checks = report["checks"]
    command = "PartDesign_ExternalThread"
    window = Gui.getMainWindow()

    def cylindrical_face(shape, radius):
        return next(f"Face{index}" for index, face in enumerate(shape.Faces, 1)
                    if isinstance(face.Surface, Part.Cylinder) and abs(face.Surface.Radius - radius) < 1e-6)

    def add_source(doc, shape, name="Shaft"):
        source = doc.addObject("Part::Feature", name)
        source.Shape = shape
        doc.recompute()
        return source

    def thread_feature(doc, source, subname, spec, length, left=False, modeled=True):
        feature = doc.addObject("PartDesign::ExternalThread", "ExternalThread")
        feature.Support = (source, [subname])
        feature.ThreadSize = spec
        feature.ThreadLength = length
        feature.LeftHanded = left
        feature.Modeled = modeled
        doc.recompute()
        return feature

    def phase(shape, radius, z, pitch, azimuth):
        xy = App.Vector(radius * math.cos(azimuth), radius * math.sin(azimuth), 0)
        sample_count = 96
        angles = []
        for index in range(sample_count):
            offset = pitch * index / sample_count
            if not shape.isInside(xy + App.Vector(0, 0, z + offset), 1e-7, True):
                angles.append(math.tau * index / sample_count)
        assert 3 < len(angles) < sample_count - 3, "The groove and crest must both appear"
        return math.atan2(sum(math.sin(angle) for angle in angles),
                          sum(math.cos(angle) for angle in angles)) / math.tau

    def check_dimensions_and_hand():
        cases = []
        for spec, diameter, pitch in SIZES:
            length = 4.0 * pitch
            radius = diameter / 2.0
            fundamental_height = math.sqrt(3.0) * pitch / 2.0
            minor = diameter - 17.0 * fundamental_height / 12.0
            for left in (False, True):
                doc = App.newDocument("NominalThreadQA")
                source = add_source(doc, Part.makeCylinder(radius, length + 2.0 * pitch))
                original = source.Shape.exportBrepToString()
                result = thread_feature(doc, source, cylindrical_face(source.Shape, radius), spec, length, left)
                shape = result.Shape
                assert result.isValid() and shape.isValid() and len(shape.Solids) == 1, (spec, left, result.State)
                assert len(shape.Faces) > len(source.Shape.Faces)
                assert 0.0 < shape.Volume < source.Shape.Volume
                assert result.ThreadSize == spec and abs(result.Pitch.Value - pitch) < 1e-10
                assert abs(result.MajorDiameter.Value - diameter) < 1e-10
                assert abs(result.MinorDiameter.Value - minor) < 1e-10
                assert abs(result.ThreadLength.Value - length) < 1e-10
                assert abs(shape.BoundBox.ZLength - source.Shape.BoundBox.ZLength) < 2e-5
                assert shape.isInside(App.Vector(radius - pitch / 16.0, 0, length + pitch), 1e-7, True)
                probe_radius = (minor / 2.0 + radius - 3.0 * fundamental_height / 8.0) / 2.0
                first = phase(shape, probe_radius, 2.0 * pitch, pitch, 0.0)
                quarter = phase(shape, probe_radius, 2.0 * pitch, pitch, math.pi / 2.0)
                repeat = phase(shape, probe_radius, pitch, pitch, 0.0)
                advance = math.remainder(quarter - first, 1.0)
                assert abs(advance - (-0.25 if left else 0.25)) <= 0.03, (spec, left, advance)
                assert abs(math.remainder(repeat - first, 1.0)) < 0.03, (spec, left, repeat, first)
                assert shape.isInside(App.Vector(minor / 2.0 - 0.004, 0, 2.0 * pitch), 1e-7, True)
                assert not shape.isInside(App.Vector(minor / 2.0 + 0.004, 0, 2.0 * pitch), 1e-7, True)
                assert source.Shape.exportBrepToString() == original
                cases.append({"spec": spec, "left_handed": left, "phase_advance_turns": advance,
                              "pitch_mm": result.Pitch.Value, "minor_diameter_mm": minor,
                              "removed_volume_mm3": source.Shape.Volume - shape.Volume})
                App.closeDocument(doc.Name)
        checks["four_sizes_and_both_hands"] = cases

    def invoke(button, handle_modal):
        callback_error = []

        def respond():
            modal = QtWidgets.QApplication.activeModalWidget()
            try:
                assert modal is not None, "No external-thread modal dialog"
                handle_modal(modal)
            except Exception:
                callback_error.append(traceback.format_exc())
                if modal is not None:
                    modal.reject()

        QtCore.QTimer.singleShot(120, respond)
        QtTest.QTest.mouseClick(button, QtCore.Qt.MouseButton.LeftButton)
        assert not callback_error, callback_error

    def select(doc, obj, subname):
        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(doc.Name, obj.Name, subname)
        QtTest.QTest.qWait(80)
        assert Gui.Selection.hasSelection()

    def snapshot(doc, source):
        return (tuple(obj.Name for obj in doc.Objects), doc.UndoCount, doc.RedoCount,
                source.ViewObject.Visibility, source.Shape.exportBrepToString())

    def check_ribbon_and_reopen():
        doc = App.newDocument("ExternalThreadWorkflowQA")
        export_source = add_source(doc, Part.makeCylinder(1.5, 5.0), "ExportShaft")
        step_path = output / "thread-shaft.step"
        Part.export([export_source], str(step_path))
        doc.removeObject(export_source.Name)
        part = doc.addObject("App::Part", "ImportedPart")
        part.Placement = App.Placement(App.Vector(12, 5, -8), App.Rotation(App.Vector(0, 1, 0), 60))
        source = add_source(doc, Part.read(str(step_path)))
        part.addObject(source)
        source.Placement = App.Placement(App.Vector(2, 3, 4), App.Rotation(App.Vector(1, 0, 0), 32))
        doc.recompute()

        Gui.activateWorkbench("PartDesignWorkbench")
        Gui.activeDocument().activeView().fitAll()
        QtTest.QTest.qWait(300)
        ribbon = window.findChild(QtWidgets.QToolBar, "PersonalCadDesignToolbar")
        assert ribbon is not None
        button = ribbon.findChild(QtWidgets.QToolButton, command)
        assert button and button.isVisible() and button.isEnabled()
        assert button.text() and not button.icon().isNull() and not Gui.Command.get(command).getShortcut()
        subname = cylindrical_face(source.Shape, 1.5)
        select(doc, part, f"{source.Name}.{subname}")
        before = snapshot(doc, source)

        def choose(modal, *, accept, modeled=True, wrong_size=False):
            assert modal.objectName() == "ExternalThreadDialog"
            size = modal.findChild(QtWidgets.QComboBox, "ExternalThreadSize")
            assert size.currentText() == "M3x0.5"
            assert snapshot(doc, source) == before
            if wrong_size:
                size.setCurrentIndex(0)
                box = modal.findChild(QtWidgets.QDialogButtonBox)
                box.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).click()
                assert modal.isVisible(), "Mismatched size was accepted"
            if not accept:
                modal.reject()
                return
            length = modal.findChild(QtWidgets.QAbstractSpinBox, "ExternalThreadLength")
            assert length.setProperty("rawValue", 1.75)
            modal.findChild(QtWidgets.QComboBox, "ExternalThreadHand").setCurrentIndex(1)
            modal.findChild(QtWidgets.QCheckBox, "ExternalThreadModeled").setChecked(modeled)
            modal.findChild(QtWidgets.QDialogButtonBox).button(QtWidgets.QDialogButtonBox.StandardButton.Ok).click()

        invoke(button, lambda modal: choose(modal, accept=False, wrong_size=True))
        assert snapshot(doc, source) == before
        invoke(button, lambda modal: choose(modal, accept=True))
        threads = [obj for obj in doc.Objects if obj.TypeId == "PartDesign::ExternalThread"]
        assert len(threads) == 1
        thread = threads[0]
        name = thread.Name
        assert thread.getParentGeoFeatureGroup() == part and thread.Support[0] == source
        assert thread.Placement.isSame(source.Placement, 1e-7)
        assert thread.getGlobalPlacement().isSame(source.getGlobalPlacement(), 1e-7)
        assert thread.LeftHanded and thread.Modeled and abs(thread.ThreadLength.Value - 1.75) < 1e-8
        assert thread.Shape.isValid() and 0 < thread.Shape.Volume < source.Shape.Volume
        assert source.Shape.exportBrepToString() == before[4] and not source.ViewObject.Visibility
        assert doc.UndoCount == before[1] + 1
        original_volume = thread.Shape.Volume
        doc.undo()
        assert doc.getObject(name) is None and source.ViewObject.Visibility and doc.UndoCount == before[1]
        doc.redo()
        thread = doc.getObject(name)
        assert thread and thread.Shape.isValid() and not source.ViewObject.Visibility
        assert abs(thread.Shape.Volume - original_volume) < 1e-7

        path = output / "metric-thread.FCStd"
        source_name = source.Name
        doc.saveAs(str(path))
        App.closeDocument(doc.Name)
        doc = App.openDocument(str(path))
        source = doc.getObject(source_name)
        thread = doc.getObject(name)
        assert thread.Support[0] == source and thread.LeftHanded and thread.Modeled
        assert abs(thread.Shape.Volume - original_volume) < 1e-7
        thread.ThreadLength = 1.25
        doc.recompute()
        assert thread.isValid() and thread.Shape.isValid() and thread.Shape.Volume > original_volume
        thread.Modeled = False
        doc.recompute()
        assert thread.isValid() and abs(thread.Shape.Volume - source.Shape.Volume) < 1e-7
        assert len(thread.Shape.Faces) == len(source.Shape.Faces)
        assert thread.ThreadSize == "M3x0.5" and abs(thread.Pitch.Value - 0.5) < 1e-10
        checks["ribbon_cancel_nested_STEP_undo_redo_reopen_and_schematic"] = True
        App.closeDocument(doc.Name)

    def check_rejection():
        doc = App.newDocument("ExternalThreadRejectQA")
        tube = add_source(doc, Part.makeCylinder(1.5, 5.0).cut(Part.makeCylinder(1.4, 5.0)), "Tube")
        other = add_source(doc, Part.makeCylinder(1.5, 5.0), "SolidShaft")
        Gui.activateWorkbench("PartDesignWorkbench")
        QtTest.QTest.qWait(250)
        ribbon = window.findChild(QtWidgets.QToolBar, "PersonalCadDesignToolbar")
        button = ribbon.findChild(QtWidgets.QToolButton, command)
        inner = cylindrical_face(tube.Shape, 1.4)
        select(doc, tube, inner)
        before = snapshot(doc, tube)
        invoke(button, lambda modal: modal.accept())
        assert snapshot(doc, tube) == before

        outer = cylindrical_face(tube.Shape, 1.5)
        original = tube.Shape.exportBrepToString()
        thin = thread_feature(doc, tube, outer, "M3x0.5", 2.0)
        assert not thin.isValid() and thin.Shape.isNull()
        assert tube.Shape.exportBrepToString() == original
        outside = cylindrical_face(other.Shape, 1.5)
        oversize = thread_feature(doc, other, outside, "M3x0.5", 5.5)
        assert not oversize.isValid() and oversize.Shape.isNull()
        checks["hole_selection_and_bad_length_and_thin_wall_rejected"] = True
        App.closeDocument(doc.Name)

    def execute():
        try:
            Gui.activateWorkbench("PartDesignWorkbench")
            check_dimensions_and_hand()
            check_ribbon_and_reopen()
            check_rejection()
            report["status"] = "passed"
        except Exception:
            report["error"] = traceback.format_exc()
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        if report["status"] == "passed":
            Gui.runCommand("Std_Quit", 0)
        else:
            os._exit(1)

    QtCore.QTimer.singleShot(500, execute)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    app = args.app.resolve()
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    executable = app / "Contents/MacOS" / info["CFBundleExecutable"]
    output = args.output.resolve()
    output.mkdir(parents=True)
    signed_resources = app / "Contents/_CodeSignature/CodeResources"
    signature = hashlib.sha256(signed_resources.read_bytes()).hexdigest()

    with tempfile.TemporaryDirectory(prefix="sven-external-thread-qa-") as scratch:
        profile = Path(scratch)
        for name in ("cache", "data", "tmp"):
            (profile / name).mkdir()
        macro = profile / "ExternalThreadQA.FCMacro"
        macro.write_text(
            f"import sys\nsys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
            "from external_thread_qa import run_macro\nrun_macro()\n",
            encoding="utf-8",
        )
        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8", "HOME": str(profile),
            "TMPDIR": str(profile / "tmp"), "XDG_CACHE_HOME": str(profile / "cache"),
            "FREECAD_USER_HOME": str(profile), "FREECAD_USER_DATA": str(profile / "data"),
            "FREECAD_USER_TEMP": str(profile / "tmp"), "PYTHONDONTWRITEBYTECODE": "1",
            OUTPUT_ENV: str(output),
        }
        with (output / "freecad.log").open("wb") as log:
            process = subprocess.run(
                [str(executable), "--user-cfg", str(profile / "user.cfg"),
                 "--system-cfg", str(profile / "system.cfg"), str(macro)],
                env=env, stdout=log, stderr=subprocess.STDOUT, timeout=args.timeout, check=False,
            )
    report_file = output / "report.json"
    report = json.loads(report_file.read_text()) if report_file.exists() else {"status": "failed"}
    report.update({"app": str(app), "signed_resource_sha256": signature, "exit": process.returncode})
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if report["status"] != "passed" or process.returncode:
        print(report.get("error", f"App exited {process.returncode}. See {output / 'freecad.log'}."))
        return 1
    print(f"Passed: {report_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
