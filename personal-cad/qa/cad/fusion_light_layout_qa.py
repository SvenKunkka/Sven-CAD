"""Verify the Fusion-style light workspace and migration of an older saved layout."""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import traceback
from xml.etree import ElementTree


ENV = "PERSONAL_CAD_LIGHT_QA_"
RIBBON = "PersonalCadDesignToolbar"
LEGACY = ("View", "Part Design Helper Features", "Part Design Modeling Features")
CUSTOM_BACKGROUND = 0x6E849AFF


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def run_macro():
    import FreeCAD as App
    import FreeCADGui as Gui
    import Part
    from PySide6 import QtCore, QtGui, QtTest, QtWidgets

    mode = os.environ[ENV + "CASE"]
    output = Path(os.environ[ENV + "OUTPUT"])
    window = Gui.getMainWindow()
    application = QtWidgets.QApplication.instance()
    main_params = App.ParamGet("User parameter:BaseApp/Preferences/MainWindow")
    view_params = App.ParamGet("User parameter:BaseApp/Preferences/View")
    toolbar_params = App.ParamGet("User parameter:BaseApp/MainWindow/Toolbars")
    wait = QtTest.QTest.qWait
    report = {
        "case": mode,
        "status": "failed",
        "theme": main_params.GetString("Theme"),
        "background": view_params.GetUnsigned("BackgroundColor"),
    }

    def find_toolbar(name):
        result = window.findChild(QtWidgets.QToolBar, name)
        require(result is not None, f"Missing toolbar {name}")
        return result

    def visible_top_bars():
        return [
            bar for bar in window.findChildren(QtWidgets.QToolBar)
            if bar.isVisible() and window.toolBarArea(bar) == QtCore.Qt.ToolBarArea.TopToolBarArea
        ]

    def color_grid(widget):
        image = widget.grab().toImage()
        require(not image.isNull(), f"No pixels from {widget.objectName()}")
        return [
            image.pixelColor(image.width() * col // 19, image.height() * row // 13)
            for col in range(1, 19)
            for row in range(1, 13)
        ]

    def light_fraction(colors):
        return sum(min(c.red(), c.green(), c.blue()) >= 220 for c in colors) / len(colors)

    def luminance(color):
        channels = [color.redF(), color.greenF(), color.blueF()]
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
        return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

    def capture(name):
        # Capturing the native window includes the OpenGL view.
        screenshot = window.screen().grabWindow(window.winId())
        if screenshot.isNull():
            screenshot = window.grab()
        require(screenshot.save(str(output / f"{mode}-{name}.png")), "Cannot capture QA window")
        report[f"{name}_capture_size"] = [screenshot.width(), screenshot.height()]
        return screenshot.toImage()

    def seed_old_layout():
        Gui.activateWorkbench("PartDesignWorkbench")
        wait(350)
        ribbon = find_toolbar(RIBBON)
        view = find_toolbar(LEGACY[0])
        native = find_toolbar(LEGACY[2])
        window.removeToolBarBreak(ribbon)
        window.insertToolBarBreak(native)
        for name in LEGACY + ("Clipboard",):
            toolbar_params.SetBool(name, True)
            find_toolbar(name).show()
        wait(250)
        report["visible_top_bars"] = [bar.objectName() for bar in visible_top_bars()]
        require(view.isVisible() and native.isVisible(), "Could not seed native toolbar visibility")
        report["restored_flag"] = main_params.GetBool("WindowStateRestored", False)
        capture("old-toolbar-state")

    def build_test_part():
        doc = App.newDocument("LightWorkspaceQA")
        body = doc.addObject("PartDesign::Body", "Body")
        sketch = body.newObject("Sketcher::SketchObject", "Profile")
        corners = [(-54, -27), (54, -27), (54, 27), (-54, 27)]
        for (x1, y1), (x2, y2) in zip(corners, corners[1:] + corners[:1]):
            sketch.addGeometry(Part.LineSegment(App.Vector(x1, y1, 0), App.Vector(x2, y2, 0)))
        for x in (-45, 45):
            for y in (-19, 19):
                sketch.addGeometry(Part.Circle(App.Vector(x, y, 0), App.Vector(0, 0, 1), 3.5))
        doc.recompute()
        return doc, body, sketch

    def verify_workspace():
        require(Gui.activeWorkbench().name() == "PartDesignWorkbench",
                "Expected startup to select the design workbench")
        report["restored_flag"] = main_params.GetBool("WindowStateRestored", False)
        window.resize(1320, 850)
        ribbon = find_toolbar(RIBBON)
        doc, body, sketch = build_test_part()
        wait(400)
        groups = ribbon.findChild(QtWidgets.QScrollArea, "PersonalCadCommandGroups")
        tabs = ribbon.findChild(QtWidgets.QTabBar, "PersonalCadDesignTabs")
        extrude = ribbon.findChild(QtWidgets.QToolButton, "PersonalCAD_Extrude")
        require(groups and tabs and extrude, "Missing ribbon tabs, groups, or labeled Extrude button")
        bar_geometries = {
            bar.objectName(): [bar.x(), bar.y(), bar.width(), bar.height()]
            for bar in visible_top_bars()
        }
        report["top_toolbars"] = bar_geometries
        report["window_width"] = window.width()
        report["native_style"] = application.style().objectName()
        report["color_scheme"] = application.styleHints().colorScheme().name
        report["application_palette"] = {
            role.name: application.palette().color(role).name()
            for role in (QtGui.QPalette.ColorRole.Window, QtGui.QPalette.ColorRole.Base,
                         QtGui.QPalette.ColorRole.WindowText)
        }
        shell_regions = {
            "quick_controls": find_toolbar("File"),
            "model_browser": window.findChild(QtWidgets.QDockWidget, "Model"),
            "status_bar": window.statusBar(),
        }
        report["light_shell"] = {
            name: round(light_fraction(color_grid(widget)), 3)
            for name, widget in shell_regions.items() if widget and widget.isVisible()
        }
        colors = color_grid(groups.viewport())
        report["ribbon_light_fraction"] = round(light_fraction(colors), 3)
        foreground = extrude.palette().color(QtGui.QPalette.ColorRole.ButtonText)
        disabled = extrude.palette().color(QtGui.QPalette.ColorGroup.Disabled,
                                           QtGui.QPalette.ColorRole.ButtonText)
        background = QtGui.QColor.fromRgba(Counter(color.rgba() for color in colors).most_common(1)[0][0])
        contrast = (max(luminance(foreground), luminance(background)) + 0.05) / (
            min(luminance(foreground), luminance(background)) + 0.05
        )
        report["extrude_label"] = {
            "foreground": foreground.name(),
            "disabled": disabled.name(),
            "background": background.name(),
            "contrast": round(contrast, 2),
        }

        Gui.activeDocument().activeView().setActiveObject("pdbody", body)
        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(doc.Name, sketch.Name)
        wait(200)
        require(extrude.isVisible() and extrude.isEnabled() and extrude.text(),
                "Extrude button should be visible, enabled, and labeled")
        QtTest.QTest.mouseClick(extrude, QtCore.Qt.MouseButton.LeftButton)
        wait(350)
        task = Gui.Control.activeTaskDialog()
        pad = next((obj for obj in doc.Objects if obj.TypeId == "PartDesign::Pad"), None)
        require(task is not None and pad is not None and pad.Profile[0] == sketch,
                "Clicking Extrude did not start a native pad with the selected sketch")
        task.accept()
        wait(250)
        require(not Gui.Control.activeDialog() and pad.Shape.isValid() and pad.Shape.Volume > 0,
                "Extrude did not complete as a valid solid")
        report["extrude_volume_mm3"] = round(pad.Shape.Volume, 2)
        pad.ViewObject.ShapeColor = (0.61, 0.69, 0.78)
        Gui.Selection.clearSelection()
        view = Gui.activeDocument().activeView()
        view.viewAxonometric()
        view.fitAll()
        wait(350)
        # Dismiss the transient font-initialization preview in the isolated test.
        for widget in application.topLevelWidgets():
            if (isinstance(widget, QtWidgets.QLabel) and widget.objectName() == "NotificationBox_label"
                    and "qt.qpa.fonts" in widget.text()):
                widget.hide()
        wait(150)
        frame = capture("workspace")
        viewport = view.graphicsView().viewport()
        viewport_origin = viewport.mapTo(window, QtCore.QPoint())
        x_scale = frame.width() / window.width()
        y_scale = frame.height() / window.height()

        def canvas_pixel(x, y):
            return frame.pixelColor(int((viewport_origin.x() + viewport.width() * x) * x_scale),
                                    int((viewport_origin.y() + viewport.height() * y) * y_scale))

        background_samples = [canvas_pixel(x, y) for x in (0.10, 0.18, 0.82, 0.90)
                              for y in (0.13, 0.25, 0.75, 0.87)]
        report["canvas_light_fraction"] = round(light_fraction(background_samples), 3)
        model_sample = canvas_pixel(0.5, 0.5)
        require(model_sample.isValid() and min(model_sample.red(), model_sample.green(), model_sample.blue()) < 220,
                "Native capture did not include the synthetic model")
        require(ribbon.grab().save(str(output / f"{mode}-ribbon.png")), "Cannot capture QA ribbon")
        require(set(bar_geometries) == {"File", "Edit", RIBBON},
                f"Duplicate built-in toolbars remain: {sorted(bar_geometries)}")
        require(bar_geometries[RIBBON][0] <= 12 and bar_geometries[RIBBON][2] >= window.width() * 0.94,
                "The ribbon shares a row with older toolbars")
        file_top, file_height = bar_geometries["File"][1::2]
        edit_top, edit_height = bar_geometries["Edit"][1::2]
        gap = bar_geometries[RIBBON][1] - max(file_top + file_height, edit_top + edit_height)
        require(abs(file_top - edit_top) <= 4 and 0 <= gap <= 8,
                f"File/Edit row is not adjacent to the ribbon: {bar_geometries}")
        require(report["color_scheme"] == "Light", "Native macOS controls are not in the light color scheme")
        require(report["ribbon_light_fraction"] >= 0.76,
                "Ribbon command groups do not have a consistent light background")
        require(contrast >= 4.5 and luminance(foreground) < luminance(background),
                "Ribbon labels do not have sufficient dark-on-light contrast")
        require(luminance(foreground) < luminance(disabled) < luminance(background),
                "Disabled ribbon labels are not muted")
        selector = ribbon.findChild(QtWidgets.QToolButton, "PersonalCadWorkspaceSelector")
        origin = QtCore.QPoint()
        require(selector and selector.isVisible() and selector.mapTo(window, origin).x()
                < tabs.mapTo(window, origin).x(), "Workspace selector is missing from the left of the design tabs")
        require(tabs.currentIndex() == 0, "Completing Extrude changed the active Solid tab")
        tab_image = tabs.grab().toImage()
        dpr = tab_image.devicePixelRatio()
        active_tab = tabs.tabRect(tabs.currentIndex())
        accented_columns = set()
        for x in range(active_tab.left() + 1, active_tab.right()):
            for y in range(max(0, active_tab.bottom() - 5), active_tab.bottom() + 1):
                color = tab_image.pixelColor(int(x * dpr), int(y * dpr))
                if color.blue() - color.red() >= 25 and color.blue() - color.green() >= 8:
                    accented_columns.add(x)
        report["active_tab_accent_columns"] = len(accented_columns)
        require(len(accented_columns) >= active_tab.width() / 3,
                "Active Solid tab does not have a visible blue underline")
        menu = selector.menu()
        require(menu is not None, "Workspace selector did not expose its menu")
        actions = {action.objectName(): action for action in menu.actions() if action.isEnabled()}
        require({"DraftWorkbench", "PartDesignWorkbench"} <= actions.keys(),
                "Native Design and Draft actions are missing from the workspace menu")
        draft = actions["DraftWorkbench"]
        design = actions["PartDesignWorkbench"]
        draft.trigger()
        wait(350)
        require(Gui.activeWorkbench().name() == "DraftWorkbench" and not ribbon.isVisible()
                and find_toolbar("View").isVisible(),
                "The non-design workbench lost its native tools")
        workbench_bar = find_toolbar("Workbench")
        combo = workbench_bar.findChild(QtWidgets.QComboBox)
        index = int(design.data())
        require(workbench_bar.isVisible() and combo is not None and combo.itemText(index) == design.text(),
                "The non-design workbench offers no native selector to return to Design")
        combo.setCurrentIndex(index)
        combo.activated.emit(index)
        wait(350)
        require(Gui.activeWorkbench().name() == "PartDesignWorkbench" and ribbon.isVisible()
                and not find_toolbar("View").isVisible(),
                "Legacy tools reappeared after switching back to design")
        report["workspace_selector_round_trip"] = True
        require(report["light_shell"]["quick_controls"] >= 0.68
                and report["light_shell"]["model_browser"] >= 0.68
                and report["light_shell"]["status_bar"] >= 0.68,
                f"The shell surrounding the light ribbon is still dark: {report['light_shell']}")
        require(report["canvas_light_fraction"] >= 0.75, "The default viewport background is not light")
        App.closeDocument(doc.Name)

    def execute():
        try:
            if mode == "seed":
                seed_old_layout()
            elif mode == "custom":
                require(report["theme"] == "FreeCAD Light" and report["background"] == CUSTOM_BACKGROUND,
                        f"Theme upgrade changed the customized canvas: {report}")
            else:
                verify_workspace()
            report["status"] = "passed"
        except Exception:
            report["error"] = traceback.format_exc()
        (output / f"{mode}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if report["status"] != "passed":
            os._exit(1)
        Gui.runCommand("Std_Quit", 0)

    QtCore.QTimer.singleShot(550, execute)


def launch(app, profile, output, case, timeout, seed=None):
    profile.mkdir()
    for name in ("cache", "data", "temp"):
        (profile / name).mkdir()
    cfg = profile / "user.cfg"
    if seed:
        shutil.copyfile(seed, cfg)
    if case == "seed":
        preference_pack = (app / "Contents/Resources/share/Gui/PreferencePacks"
                           / "FreeCAD Dark/FreeCAD Dark.cfg")
        shutil.copyfile(preference_pack, cfg)
    elif case == "custom":
        tree = ElementTree.parse(cfg)
        background = tree.find(".//FCParamGroup[@Name='View']/FCUInt[@Name='BackgroundColor']")
        require(background is not None, "Missing stock canvas preference in the migration fixture")
        background.set("Value", str(CUSTOM_BACKGROUND))
        tree.write(cfg, encoding="utf-8", xml_declaration=True)
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    executable = app / "Contents/MacOS" / info["CFBundleExecutable"]
    macro = profile / "LightLayoutQA.FCMacro"
    macro.write_text(
        f"import sys\nsys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
        "from fusion_light_layout_qa import run_macro\nrun_macro()\n",
        encoding="utf-8",
    )
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(profile),
        "TMPDIR": str(profile / "temp"),
        "LANG": "en_US.UTF-8",
        "XDG_CACHE_HOME": str(profile / "cache"),
        "FREECAD_USER_HOME": str(profile),
        "FREECAD_USER_DATA": str(profile / "data"),
        "FREECAD_USER_TEMP": str(profile / "temp"),
        "PYTHONDONTWRITEBYTECODE": "1",
        ENV + "CASE": case,
        ENV + "OUTPUT": str(output),
    }
    command = [str(executable), "--user-cfg", str(cfg), "--system-cfg", str(profile / "system.cfg"),
               str(macro)]
    with (output / f"{case}.log").open("wb") as log:
        process = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT,
                                 timeout=timeout, check=False)
    report_path = output / f"{case}.json"
    require(report_path.is_file(), f"{case} exited {process.returncode} without a report")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["exit_code"] = process.returncode
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--seed-app", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args()
    app, seed_app = args.app.resolve(), args.seed_app.resolve()
    for bundle in (app, seed_app):
        if bundle.is_relative_to("/Applications") or bundle.is_relative_to(Path.home() / "Applications"):
            parser.error("Use isolated build artifacts, not an installed app")
    output = args.output.resolve()
    output.mkdir(parents=True)
    summary = {"status": "failed"}
    try:
        with tempfile.TemporaryDirectory(prefix="personalcad-light-layout-qa-") as scratch:
            profile = Path(scratch)
            summary["seed"] = launch(seed_app, profile / "seed", output, "seed", args.timeout)
            require(summary["seed"]["status"] == "passed" and summary["seed"]["exit_code"] == 0,
                    "Could not create a saved legacy layout")
            summary["fresh"] = launch(app, profile / "fresh", output, "fresh", args.timeout)
            summary["migrated"] = launch(app, profile / "migrated", output, "migrated", args.timeout,
                                         seed=profile / "seed/user.cfg")
            summary["custom"] = launch(app, profile / "custom", output, "custom", args.timeout,
                                       seed=profile / "seed/user.cfg")
            require(summary["migrated"]["restored_flag"] != summary["seed"]["restored_flag"],
                    "Legacy Qt state was not restored")
            require(all(summary[case]["status"] == "passed" and summary[case]["exit_code"] == 0
                        for case in ("fresh", "migrated", "custom")), "Workspace acceptance failed")
        summary["status"] = "passed"
    except Exception:
        summary["error"] = traceback.format_exc()
    path = output / "result.json"
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"{summary['status']}: {path}")
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
