"""Exercise PersonalCAD's design toolbar and modeling shortcuts in an isolated app launch."""

import argparse
import json
import math
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import traceback


REPORT_ENV = "PERSONAL_CAD_FUSION_QA_OUTPUT"
SKIP_OUTER_ENV = "PERSONAL_CAD_FUSION_QA_SKIP_OUTER"
ALLOW_PREVIEW_ICON_ENV = "PERSONAL_CAD_FUSION_QA_ALLOW_PREVIEW_ICON"


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def run_macro():
    import FreeCAD as App
    import FreeCADGui as Gui
    import Part
    from PySide6 import QtCore, QtGui, QtTest, QtWidgets

    output = Path(os.environ[REPORT_ENV])
    window = Gui.getMainWindow()
    report = {"status": "failed", "checks": {}, "shortcuts": {}}
    checks = report["checks"]
    wait = QtTest.QTest.qWait
    left = QtCore.Qt.MouseButton.LeftButton
    key = QtCore.Qt.Key
    portable = QtGui.QKeySequence.SequenceFormat.PortableText
    preview_gaps = os.environ[ALLOW_PREVIEW_ICON_ENV] == "1"

    def widget(parent, widget_type, name):
        result = parent.findChild(widget_type, name)
        require(result is not None, f"Missing widget {name}")
        return result

    def button(toolbar, command):
        result = widget(toolbar, QtWidgets.QToolButton, command)
        require(result.text(), f"Missing label for {command}")
        if command == "PersonalCAD_Joint" and result.icon().isNull() and preview_gaps:
            checks["preview_joint_icon"] = "missing in pre-fix preview"
        else:
            require(not result.icon().isNull(), f"Missing icon for {command}")
        require(result.accessibleName() == result.text(), f"Accessible label differs for {command}")
        require(
            result.toolButtonStyle() == QtCore.Qt.ToolButtonStyle.ToolButtonTextUnderIcon,
            f"Text is not displayed below the icon for {command}",
        )
        return result

    def check_shortcuts(bindings):
        for command, expected in bindings.items():
            binding = Gui.Command.get(command)
            require(binding is not None, f"Missing command {command}")
            actual = binding.getShortcut()
            report["shortcuts"][command] = actual
            require(
                QtGui.QKeySequence(actual) == QtGui.QKeySequence(expected, portable),
                f"{command} shortcut {actual!r} differs from {expected!r}",
            )

    def press(value, modifiers=QtCore.Qt.KeyboardModifier.NoModifier):
        graphics = Gui.activeDocument().activeView().graphicsView()
        viewport = graphics.viewport()
        window.raise_()
        window.activateWindow()
        viewport.setFocus()
        wait(70)
        focus = QtWidgets.QApplication.focusWidget()
        require(focus is not None and (focus == graphics or graphics.isAncestorOf(focus)),
                f"Model viewport lacks keyboard focus: {focus}")
        QtTest.QTest.keyClick(focus, value, modifiers)
        wait(350)
        return viewport

    def switch(tabs, index):
        require(tabs.isTabVisible(index), f"Expected visible design tab {index}")
        QtTest.QTest.mouseClick(tabs, left, pos=tabs.tabRect(index).center())
        wait(350)
        require(tabs.currentIndex() == index, f"Design tab {index} did not become active")

    def rectangle(body, name):
        sketch = body.newObject("Sketcher::SketchObject", name)
        corners = [(0, 0), (10, 0), (10, 6), (0, 6)]
        for start, end in zip(corners, corners[1:] + corners[:1]):
            sketch.addGeometry(Part.LineSegment(App.Vector(*start, 0), App.Vector(*end, 0)))
        body.Document.recompute()
        return sketch

    def select(document, object_name, element=""):
        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(document, object_name, element)
        wait(140)
        require(Gui.Selection.hasSelection(), f"Could not select {object_name} {element}")

    def active_feature(document, type_name):
        edit = Gui.activeDocument().getInEdit()
        matches = [obj for obj in document.Objects if obj.TypeId == type_name]
        require(edit is not None and matches and Gui.Control.activeDialog(), f"{type_name} edit task missing")
        return matches[-1]

    def draw_circle(viewport, sketch):
        for x, y in ((0.44, 0.43), (0.61, 0.56)):
            point = QtCore.QPoint(int(viewport.width() * x), int(viewport.height() * y))
            QtTest.QTest.mouseMove(viewport, point)
            QtTest.QTest.mouseClick(viewport, left, pos=point)
            wait(120)
        require(sketch.GeometryCount == 1 and sketch.Geometry[0].TypeId == "Part::GeomCircle",
                "Two viewport clicks did not create a center-diameter circle")

    def check_outer_shortcuts(tabs):
        Gui.activateWorkbench("PartDesignWorkbench")
        scratch = App.newDocument("FusionSketchStartQA")
        scratch_body = scratch.addObject("PartDesign::Body", "Body")
        scratch.recompute()
        Gui.activeDocument().activeView().setActiveObject("pdbody", scratch_body)
        Gui.Selection.clearSelection()
        wait(300)
        original_objects = {obj.Name for obj in scratch.Objects}
        for shortcut in (key.Key_L, key.Key_R, key.Key_C):
            press(shortcut)
            attach = Gui.Control.activeTaskDialog()
            require(attach is not None and widget(window, QtWidgets.QLineEdit, "lineRef1").isVisible(),
                    f"{shortcut.name} did not open the native sketch reference picker")
            attach.reject()
            wait(280)
            require(not Gui.Control.activeDialog() and Gui.activeDocument().getInEdit() is None
                    and {obj.Name for obj in scratch.Objects} == original_objects,
                    f"Canceling {shortcut.name} left a sketch or pending edit")
        checks["L_R_C_open_and_cancel_native_picker"] = True

        press(key.Key_C)
        plane = scratch.getObject("XY_Plane")
        require(plane is not None, "Scratch body has no XY plane")
        select(scratch.Name, plane.Name)
        ref = widget(window, QtWidgets.QLineEdit, "lineRef1")
        require(plane.Name in ref.text(), "Sketch picker did not use the selected XY plane")
        Gui.Control.activeTaskDialog().accept()
        wait(450)
        edit = Gui.activeDocument().getInEdit()
        require(edit is not None and edit.isDerivedFrom("SketcherGui::ViewProviderSketch")
                and tabs.isTabVisible(3), "Accepting the plane did not enter Sketch edit")
        draw_circle(Gui.activeDocument().activeView().graphicsView().viewport(), edit.Object)
        checks["C_creates_circle_after_choosing_plane"] = edit.Object.Name
        Gui.activeDocument().resetEdit()
        App.closeDocument(scratch.Name)

    def check_joint_shortcut():
        Gui.activateWorkbench("AssemblyWorkbench")
        assembly_doc = App.newDocument("FusionJointQA")
        assembly = assembly_doc.addObject("Assembly::AssemblyObject", "Assembly")
        assembly.newObject("Assembly::JointGroup", "Joints")
        for index in range(2):
            source = assembly_doc.addObject("Part::Box", f"Component{index + 1}")
            link = assembly.newObject("App::Link", f"Link{index + 1}")
            link.LinkedObject = source
            link.Placement = App.Placement(App.Vector(30 * index, 0, 0), App.Rotation())
            source.Visibility = False
        assembly_doc.recompute()
        require(Gui.activeDocument().setEdit(assembly.Name), "Cannot activate synthetic assembly")
        wait(350)
        require(assembly.ViewObject.isInEditMode(), "Assembly did not enter its editing mode")
        original_objects = {obj.Name for obj in assembly_doc.Objects}
        Gui.Selection.clearSelection()
        press(key.Key_J)
        joint_type = widget(window, QtWidgets.QComboBox, "jointType")
        require(Gui.Control.activeDialog() and joint_type.isVisible() and joint_type.count() > 1,
                "J did not open the native editable joint type chooser")
        checks["J_opens_native_joint_type_chooser"] = joint_type.count()
        Gui.Control.activeTaskDialog().reject()
        wait(250)
        require(not Gui.Control.activeDialog() and {obj.Name for obj in assembly_doc.Objects} == original_objects,
                "Canceling J left a joint object or active task")
        Gui.activeDocument().resetEdit()
        App.closeDocument(assembly_doc.Name)

    def execute():
        try:
            print("QA phase: toolbar and workbench switching", file=sys.stderr, flush=True)
            Gui.activateWorkbench("PartDesignWorkbench")
            doc = App.newDocument("FusionWorkflowQA")
            body = doc.addObject("PartDesign::Body", "Body")
            sketch = rectangle(body, "Profile")
            view = Gui.activeDocument().activeView()
            view.fitAll()
            wait(400)

            toolbar = widget(window, QtWidgets.QToolBar, "PersonalCadDesignToolbar")
            tabs = widget(toolbar, QtWidgets.QTabBar, "PersonalCadDesignTabs")
            scroll = widget(toolbar, QtWidgets.QScrollArea, "PersonalCadCommandGroups")
            require(toolbar.isVisible() and window.toolBarArea(toolbar) == QtCore.Qt.ToolBarArea.TopToolBarArea,
                    "Design toolbar is not visible in the top toolbar area")
            require(tabs.currentIndex() == 0 and not tabs.isTabVisible(3), "Sketch tab is not contextual")
            checks["design_tabs"] = [tabs.tabText(index) for index in range(tabs.count()) if tabs.isTabVisible(index)]
            require(checks["design_tabs"] in (["实体", "曲面", "装配"], ["Solid", "Surface", "Assembly"]),
                    f"Unexpected design tabs {checks['design_tabs']}")

            positions = []
            for group in ("Create", "Modify", "Assemble", "Construct", "Inspect", "Insert", "Select"):
                menu = widget(toolbar, QtWidgets.QToolButton, "PersonalCadGroup_" + group)
                require(menu.text() and menu.menu() and menu.menu().actions(), f"Group {group} lacks its dropdown")
                positions.append(menu.mapTo(scroll.widget(), QtCore.QPoint()).x())
            require(positions == sorted(set(positions)), f"Solid group order differs: {positions}")
            checks["solid_labeled_buttons"] = [
                button(toolbar, name).text() for name in (
                    "PartDesign_NewSketch", "PersonalCAD_Extrude", "PartDesign_Hole",
                    "PersonalCAD_PressPull", "PartDesign_Fillet", "Std_Transform", "Std_Measure",
                )
            ]
            check_shortcuts({
                "PersonalCAD_Extrude": "E", "PartDesign_Hole": "H", "PersonalCAD_PressPull": "Q",
                "PartDesign_Fillet": "F", "Std_Transform": "M", "Std_ToggleVisibility": "V",
                "PersonalCAD_ModelToolbox": "S", "Part_ColorPerFace": "A", "Std_Refresh": "Ctrl+B",
                "PersonalCAD_Joint": "J", "Std_Measure": "I", "Std_BoxSelection": "1",
                "PersonalCAD_FreeformSelection": "2", "PersonalCAD_PaintSelection": "3", "Std_ViewFitAll": "F6",
            })
            require(toolbar.grab().save(str(output / "solid-toolbar.png")), "Cannot capture Solid toolbar")

            original_size = window.size()
            window.resize(760, original_size.height())
            wait(200)
            horizontal = scroll.horizontalScrollBar()
            require(horizontal.maximum() > 0, "Narrow window does not expose horizontal tool scrolling")
            horizontal.setValue(horizontal.maximum())
            require(horizontal.value() == horizontal.maximum(), "Right-hand groups cannot be reached by scrolling")
            checks["narrow_window"] = {"window_width": window.width(), "horizontal_scroll": horizontal.maximum()}
            window.resize(original_size)
            wait(150)

            for index, workbench, expected_button in (
                (1, "SurfaceWorkbench", "Surface_Filling"),
                (2, "AssemblyWorkbench", "PersonalCAD_Joint"),
                (0, "PartDesignWorkbench", "PersonalCAD_Extrude"),
            ):
                switch(tabs, index)
                require(Gui.activeWorkbench().name() == workbench, f"Tab {index} did not activate {workbench}")
                button(toolbar, expected_button)
            Gui.activateWorkbench("TechDrawWorkbench")
            wait(200)
            require(not toolbar.isVisible(), "Design ribbon stayed visible in the drawing workspace")
            Gui.activateWorkbench("PartDesignWorkbench")
            wait(250)
            second = App.newDocument("FusionSecondDocumentQA")
            wait(250)
            require(toolbar.isVisible() and tabs.currentIndex() == 0 and not tabs.isTabVisible(3),
                    "Creating a second document corrupted design toolbar state")
            App.closeDocument(second.Name)
            App.setActiveDocument(doc.Name)
            wait(200)
            checks["workbench_and_document_switching"] = True

            Gui.activeDocument().activeView().setActiveObject("pdbody", body)
            require(Gui.activeDocument().setEdit(sketch.Name), "Cannot enter synthetic sketch edit")
            wait(450)
            require(tabs.currentIndex() == 3 and tabs.isTabVisible(3), "Sketch edit did not open the contextual tab")
            checks["sketch_labeled_buttons"] = [button(toolbar, name).text() for name in (
                "Sketcher_CreateLine", "Sketcher_CreateRectangle", "Sketcher_CreateCircle", "Sketcher_LeaveSketch",
            )]
            launchers = {"PersonalCAD_SketchLine": "L", "PersonalCAD_SketchRectangle": "R",
                         "PersonalCAD_SketchCircle": "C"}
            preview = os.environ[SKIP_OUTER_ENV] == "1"
            if preview and not all(Gui.Command.get(name) is not None for name in launchers):
                launchers = {"Sketcher_CreateLine": "L", "Sketcher_CreateRectangle": "R", "Sketcher_CreateCircle": "C"}
            check_shortcuts({
                **launchers,
                "Sketcher_Trimming": "T", "Sketcher_Offset": "O", "Sketcher_Projection": "P",
                "Sketcher_ToggleConstruction": "X", "Sketcher_Dimension": "D",
            })
            require(toolbar.grab().save(str(output / "sketch-toolbar.png")), "Cannot capture Sketch toolbar")

            switch(tabs, 0)
            require(Gui.activeDocument().getInEdit() is not None and tabs.isTabVisible(3),
                    "Switching tool tabs cancelled the active sketch")
            for name in ("PersonalCAD_Extrude", "PersonalCAD_PressPull", "PersonalCAD_ModelToolbox"):
                require(Gui.Command.get(name).getAction()[0].isEnabled(),
                        f"{name} is disabled by the Sketcher task while editing")
            switch(tabs, 3)
            print("QA phase: E and Q", file=sys.stderr, flush=True)
            press(key.Key_E)
            pad = active_feature(doc, "PartDesign::Pad")
            require(pad.Profile[0] == sketch and not tabs.isTabVisible(3),
                    "E failed to finish the sketch and use it as the Pad profile")
            Gui.Control.activeTaskDialog().accept()
            wait(250)
            require(not Gui.Control.activeDialog() and not pad.Shape.isNull() and pad.Shape.isValid()
                    and math.isclose(pad.Shape.Volume, 600.0, abs_tol=1e-6),
                    "E created an incorrect or unaccepted extrusion")
            checks["E_from_active_sketch"] = {"profile": sketch.Name, "volume_mm3": pad.Shape.Volume}

            select(doc.Name, pad.Name, "Edge1")
            press(key.Key_Q)
            fillet = active_feature(doc, "PartDesign::Fillet")
            require(fillet.Base[0] == pad and "Edge1" in fillet.Base[1], "Q did not pass the selected edge to Fillet")
            Gui.Control.activeTaskDialog().reject()
            wait(200)
            require(not Gui.Control.activeDialog(), "Canceling the fillet left a task open")
            checks["Q_edge_to_fillet"] = True

            object_count = len(doc.Objects)
            select(doc.Name, pad.Name, "Face1")
            press(key.Key_Q)
            require(len(doc.Objects) == object_count and not Gui.Control.activeDialog(),
                    "Q on a solid face unexpectedly altered the model")
            face_notice = next((label.text() for label in window.findChildren(QtWidgets.QLabel)
                                if "Individual face offset is unavailable" in label.text()), "")
            require(face_notice, "Q face dispatch did not show its unavailable-operation explanation")
            checks["Q_unsupported_face_unchanged"] = face_notice

            standalone = doc.addObject("Part::Box", "RecomputeProbe")
            doc.recompute()
            print("QA phase: V and S", file=sys.stderr, flush=True)
            select(doc.Name, standalone.Name)
            require(standalone.ViewObject.Visibility, "Box should be visible before the visibility shortcut")
            press(key.Key_V)
            if standalone.ViewObject.Visibility and preview_gaps:
                action = Gui.Command.get("Std_ToggleVisibility").getAction()[0]
                checks["V_toggles_visibility"] = {
                    "status": "did not toggle in pre-fix build",
                    "action_associations": [obj.objectName() for obj in action.associatedObjects()],
                }
            else:
                require(not standalone.ViewObject.Visibility, "V did not toggle selected feature visibility")
                select(doc.Name, standalone.Name)
                press(key.Key_V)
                require(standalone.ViewObject.Visibility, "V did not restore selected feature visibility")
                checks["V_toggles_visibility"] = True

            press(key.Key_S)
            toolbox = widget(window, QtWidgets.QDialog, "PersonalCadModelToolbox")
            require(toolbox.isVisible(), "S did not open the command search palette")
            field = toolbox.findChild(QtWidgets.QLineEdit)
            require(field is not None and QtWidgets.QApplication.focusWidget() is field,
                    "S did not focus the search input")
            QtTest.QTest.keyClicks(field, "eqs")
            wait(250)
            require(field.text() == "eqs" and toolbox.isVisible() and not Gui.Control.activeDialog(),
                    "A modeling shortcut intercepted text typed in the palette")
            toolbox.reject()
            wait(200)
            checks["S_opens_search_and_preserves_text_input"] = True

            circle = body.newObject("Sketcher::SketchObject", "CircleShortcutProfile")
            doc.recompute()
            print("QA phase: C in active sketch", file=sys.stderr, flush=True)
            require(Gui.activeDocument().setEdit(circle.Name), "Cannot edit scratch sketch for the C shortcut")
            wait(350)
            draw_circle(press(key.Key_C), circle)
            Gui.activeDocument().resetEdit()
            wait(200)
            checks["C_creates_a_circle"] = True

            standalone.Length = 18
            wait(150)
            print("QA phase: Command+B", file=sys.stderr, flush=True)
            press(key.Key_B, QtCore.Qt.KeyboardModifier.ControlModifier)
            if not math.isclose(standalone.Shape.BoundBox.XLength, 18.0, abs_tol=1e-6) and preview_gaps:
                checks["Command_B_recomputes_model"] = "did not recompute in pre-fix build"
            else:
                require(math.isclose(standalone.Shape.BoundBox.XLength, 18.0, abs_tol=1e-6),
                        "Command+B did not recompute the changed object")
                checks["Command_B_recomputes_model"] = True

            App.closeDocument(doc.Name)
            if preview:
                checks["L_R_C_open_and_cancel_native_picker"] = "skipped by --skip-outer"
            else:
                print("QA phase: L/R/C from solid mode", file=sys.stderr, flush=True)
                check_outer_shortcuts(tabs)
            print("QA phase: J", file=sys.stderr, flush=True)
            check_joint_shortcut()
            wait(200)
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
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--skip-outer", action="store_true", help="Skip sketch creation from solid mode")
    parser.add_argument("--allow-preview-joint-icon", action="store_true",
                        help="Record known preview icon and action-association failures and keep testing")
    parser.add_argument("--require-no-main-thread-warnings", action="store_true",
                        help="Fail if a GUI API reports being called outside the main thread")
    args = parser.parse_args()
    app = args.app.resolve()
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    executable = app / "Contents/MacOS" / info["CFBundleExecutable"]
    require(os.access(executable, os.X_OK), f"Cannot execute {executable}")
    output = args.output.resolve()
    output.mkdir(parents=True)

    with tempfile.TemporaryDirectory(prefix="personal-cad-fusion-qa-") as scratch:
        profile = Path(scratch)
        for dirname in ("cache", "data", "tmp"):
            (profile / dirname).mkdir()
        macro = profile / "FusionWorkflowQA.FCMacro"
        macro.write_text(
            f"import sys\nsys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
            "from fusion_workflow_qa import run_macro\nrun_macro()\n",
            encoding="utf-8",
        )
        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(profile),
            "LANG": "en_US.UTF-8",
            "TMPDIR": str(profile / "tmp"),
            "XDG_CACHE_HOME": str(profile / "cache"),
            "FREECAD_USER_HOME": str(profile),
            "FREECAD_USER_DATA": str(profile / "data"),
            "FREECAD_USER_TEMP": str(profile / "tmp"),
            "PYTHONDONTWRITEBYTECODE": "1",
            REPORT_ENV: str(output),
            SKIP_OUTER_ENV: "1" if args.skip_outer else "0",
            ALLOW_PREVIEW_ICON_ENV: "1" if args.allow_preview_joint_icon else "0",
        }
        command = [str(executable), "--user-cfg", str(profile / "user.cfg"),
                   "--system-cfg", str(profile / "system.cfg"), str(macro)]
        try:
            with (output / "freecad.log").open("wb") as log:
                status = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT,
                                        timeout=args.timeout, check=False).returncode
        except subprocess.TimeoutExpired:
            print(f"Timed out after {args.timeout}s. See {output / 'freecad.log'}.")
            return 1

    report_file = output / "report.json"
    if not report_file.is_file():
        print(f"App exited {status} without a report. See {output / 'freecad.log'}.")
        return 1
    report = json.loads(report_file.read_text())
    if report["status"] != "passed" or status != 0:
        print(report.get("error", f"App exited {status}"))
        return 1
    log = (output / "freecad.log").read_text(errors="replace")
    thread_warnings = [line for line in log.splitlines()
                       if "GUI API " in line and "may only be used from the main thread" in line]
    report["main_thread_warning_lines"] = thread_warnings
    if args.require_no_main_thread_warnings and thread_warnings:
        report["status"] = "failed"
        report["error"] = "A GUI API reported being called outside the main thread"
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if report["status"] != "passed":
        print(f"{report['error']}. See {output / 'freecad.log'}.")
        return 1
    print(f"Passed: {report_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
