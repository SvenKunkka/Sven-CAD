"""Exercise Sven CAD's 3D navigation through Qt events in an isolated app launch."""

import argparse
import json
import math
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import traceback
from xml.etree import ElementTree


REPORT_ENV = "PERSONAL_CAD_NAVIGATION_QA_REPORT"
PROFILE_ENV = "PERSONAL_CAD_NAVIGATION_QA_PROFILE"
FUSION = "Gui::FusionNavigationStyle"
CAD = "Gui::CADNavigationStyle"
BLENDER = "Gui::BlenderNavigationStyle"
PROFILES = {"fresh": None, "legacy-cad": CAD, "legacy-blender": BLENDER, "reselected-cad": CAD}
EXPECTED = {"fresh": FUSION, "legacy-cad": FUSION, "legacy-blender": BLENDER, "reselected-cad": CAD}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def run_macro():
    import FreeCAD as App
    import FreeCADGui as Gui
    from PySide6 import QtCore, QtGui, QtTest, QtWidgets

    report = {"status": "failed", "profile": os.environ[PROFILE_ENV], "checks": {}}
    checks = report["checks"]
    qt = QtCore.Qt
    event_type = QtCore.QEvent.Type
    no_button = qt.MouseButton.NoButton
    left = qt.MouseButton.LeftButton
    middle = qt.MouseButton.MiddleButton
    no_modifier = qt.KeyboardModifier.NoModifier
    shift = qt.KeyboardModifier.ShiftModifier
    command = qt.KeyboardModifier.ControlModifier
    wait = QtTest.QTest.qWait

    def exercise(view):
        graphics = view.graphicsView()
        viewport = graphics.viewport()
        start = QtCore.QPoint(round(viewport.width() * 0.46), round(viewport.height() * 0.48))
        motion = QtCore.QPoint(64, 37)
        view.setCameraType("Orthographic")
        view.viewTop()
        view.fitAll()
        wait(600)
        initial_camera = view.getCamera()

        def reset():
            view.setCamera(initial_camera)
            Gui.updateGui()
            return snapshot()

        def snapshot():
            fields = {
                parts[0]: parts[1:]
                for line in view.getCamera().splitlines()
                if (parts := line.split()) and parts[0] in ("position", "height")
            }
            return {
                "position": tuple(map(float, fields["position"])),
                "orientation": tuple(view.getCameraOrientation().Q),
                "height": float(fields["height"][0]),
            }

        def position_delta(before, after):
            return math.dist(before["position"], after["position"])

        def orientation_delta(before, after):
            first = before["orientation"]
            second = after["orientation"]
            return min(math.dist(first, second), math.dist(first, tuple(-v for v in second)))

        def height_ratio(before, after):
            return after["height"] / before["height"]

        def mouse(kind, point, button, buttons, modifiers):
            local = QtCore.QPointF(point)
            global_pos = QtCore.QPointF(viewport.mapToGlobal(point))
            event = QtGui.QMouseEvent(kind, local, global_pos, button, buttons, modifiers)
            QtWidgets.QApplication.sendEvent(viewport, event)

        def wheel(point, pixels, angle, phase, modifiers):
            global_pos = viewport.mapToGlobal(point)
            event = QtGui.QWheelEvent(
                QtCore.QPointF(graphics.mapFromGlobal(global_pos)),
                QtCore.QPointF(global_pos),
                QtCore.QPoint(*pixels),
                QtCore.QPoint(*angle),
                no_button,
                modifiers,
                phase,
                False,
            )
            # Qt only bubbles spontaneous wheel events from the viewport to its viewer.
            QtWidgets.QApplication.sendEvent(graphics, event)

        def drag(modifiers, point=start, around_point=False):
            buttons = middle
            mouse(event_type.MouseButtonPress, point, middle, buttons, modifiers)
            if around_point:
                buttons |= left
                mouse(event_type.MouseButtonPress, point, left, buttons, modifiers)
            end = point + motion
            mouse(event_type.MouseMove, point + motion / 2, no_button, buttons, modifiers)
            mouse(event_type.MouseMove, end, no_button, buttons, modifiers)
            if around_point:
                mouse(event_type.MouseButtonRelease, end, left, middle, modifiers)
            mouse(event_type.MouseButtonRelease, end, middle, no_button, modifiers)
            Gui.updateGui()

        def scroll_trackpad(modifiers):
            wheel(start, (0, 0), (0, 0), qt.ScrollPhase.ScrollBegin, modifiers)
            wheel(start, (46, 14), (0, 0), qt.ScrollPhase.ScrollUpdate, modifiers)
            wheel(start, (31, 17), (0, 0), qt.ScrollPhase.ScrollUpdate, modifiers)
            wheel(start, (0, 0), (0, 0), qt.ScrollPhase.ScrollEnd, modifiers)
            Gui.updateGui()

        def expect_pan(before, after):
            moved = position_delta(before, after)
            require(moved > 0.1, "Pan did not move the camera")
            require(orientation_delta(before, after) < 1e-5, "Pan rotated the camera")
            require(math.isclose(height_ratio(before, after), 1, rel_tol=1e-5),
                    "Pan changed the zoom level")
            return moved

        def expect_orbit(before, after):
            angle = orientation_delta(before, after)
            require(angle > 0.01, "Orbit did not rotate the camera")
            require(math.isclose(height_ratio(before, after), 1, rel_tol=1e-5),
                    "Orbit changed the zoom level")
            return angle

        def check(name, test):
            try:
                checks[name] = {"passed": True, "result": test()}
            except Exception as error:
                checks[name] = {"passed": False, "error": str(error)}

        def mouse_pan():
            before = reset()
            drag(no_modifier)
            return expect_pan(before, snapshot())

        def mouse_orbit():
            before = reset()
            drag(shift)
            return expect_orbit(before, snapshot())

        def face_pixel(x, y):
            ratio = viewport.devicePixelRatioF()
            pixel = QtCore.QPoint(round(x / ratio), viewport.height() - round(y / ratio) - 1)
            require(viewport.rect().contains(pixel), "Test face lies offscreen")
            return pixel

        def point_orbit():
            before = reset()
            pivot = (18, 12, 8)
            x, y = view.getPointOnViewport(*pivot)
            pixel = face_pixel(x, y)
            drag(shift, pixel, around_point=True)
            rotation = expect_orbit(before, snapshot())
            drift = math.dist((x, y), view.getPointOnViewport(*pivot)) / viewport.devicePixelRatioF()
            require(drift <= 5, f"Picked orbit point drifted by {drift:.2f} logical pixels")
            return {"rotation_delta": rotation, "pivot_drift_px": drift}

        def drag_zoom():
            before = reset()
            drag(shift | command)
            after = snapshot()
            ratio = height_ratio(before, after)
            require(abs(ratio - 1) > 0.03, "Command+Shift+middle drag did not zoom")
            require(orientation_delta(before, after) < 1e-5, "Drag zoom rotated the camera")
            return ratio

        def mouse_wheel():
            mouse(event_type.MouseMove, start, no_button, no_button, no_modifier)
            wait(60)
            before = reset()
            ratios = []
            for step in (120, 120, 120, -120, -120, -120):
                wheel(start, (0, 0), (0, step), qt.ScrollPhase.NoScrollPhase, no_modifier)
                wait(45)
                ratios.append(height_ratio(before, snapshot()))
            require(abs(ratios[2] - 1) > 0.1, f"Discrete mouse wheel did not zoom: {ratios}")
            require(math.isclose(ratios[-1], 1, rel_tol=1e-4),
                    f"Opposite wheel steps did not restore the original zoom level: {ratios}")
            return ratios

        def trackpad_pan():
            before = reset()
            scroll_trackpad(no_modifier)
            return expect_pan(before, snapshot())

        def trackpad_orbit():
            before = reset()
            scroll_trackpad(shift)
            return expect_orbit(before, snapshot())

        def trackpad_pinch():
            before = reset()
            local = QtCore.QPointF(start)
            global_pos = QtCore.QPointF(viewport.mapToGlobal(start))
            device = QtGui.QPointingDevice.primaryPointingDevice()
            native = qt.NativeGestureType
            for kind, amount in ((native.BeginNativeGesture, 0.0),
                                 (native.ZoomNativeGesture, 0.25),
                                 (native.EndNativeGesture, 0.0)):
                event = QtGui.QNativeGestureEvent(
                    kind, device, 2, local, local, global_pos, amount, QtCore.QPointF()
                )
                QtWidgets.QApplication.sendEvent(viewport, event)
            ratio = height_ratio(before, snapshot())
            require(0.70 < ratio < 0.90, "Native trackpad spread did not zoom in")
            return ratio

        def fit_with(trigger):
            fitted = reset()
            wheel(start, (0, 0), (0, 480), qt.ScrollPhase.NoScrollPhase, no_modifier)
            displaced = snapshot()
            require(abs(height_ratio(fitted, displaced) - 1) > 0.10,
                    "Fit fixture could not displace the camera")
            trigger()
            wait(700)
            final = snapshot()
            ratio = height_ratio(fitted, final)
            require(math.isclose(ratio, 1, rel_tol=0.02)
                    and position_delta(fitted, final) < 0.5,
                    f"Fit did not restore the fitted view (height ratio {ratio:.3f})")
            return ratio

        def middle_double_click():
            mouse(event_type.MouseButtonPress, start, middle, middle, no_modifier)
            mouse(event_type.MouseButtonRelease, start, middle, no_button, no_modifier)
            mouse(event_type.MouseButtonDblClick, start, middle, middle, no_modifier)
            mouse(event_type.MouseButtonRelease, start, middle, no_button, no_modifier)

        def press_f6():
            window = Gui.getMainWindow()
            require(Gui.Command.get("Std_ViewFitAll").getShortcut() == "F6", "Fit All is not bound to F6")
            window.raise_()
            window.activateWindow()
            QtWidgets.QApplication.setActiveWindow(window)
            graphics.setFocus(qt.FocusReason.OtherFocusReason)
            QtTest.QTest.qWaitForWindowActive(window, 1000)
            focus = QtWidgets.QApplication.focusWidget()
            require(focus and (focus == graphics or graphics.isAncestorOf(focus)),
                    f"The graphics view did not receive keyboard focus: {focus}")
            QtTest.QTest.keyClick(focus, qt.Key.Key_F6)

        def mouse_state_after_double_click():
            reset()
            middle_double_click()
            wait(700)
            before = snapshot()
            mouse(event_type.MouseMove, start + motion, no_button, no_button, no_modifier)
            after = snapshot()
            require(position_delta(before, after) < 1e-5
                    and orientation_delta(before, after) < 1e-5,
                    "The middle button stayed active after a double-click release")
            drag(no_modifier)
            return expect_pan(after, snapshot())

        def left_click_selects():
            reset()
            Gui.Selection.clearSelection()
            pixel = face_pixel(*view.getPointOnViewport(18, 12, 8))
            mouse(event_type.MouseButtonPress, pixel, left, left, no_modifier)
            mouse(event_type.MouseButtonRelease, pixel, left, no_button, no_modifier)
            Gui.updateGui()
            selected = [obj.Name for obj in Gui.Selection.getSelection()]
            require(selected == ["First"], f"Left click selected {selected}")
            Gui.Selection.clearSelection()
            return selected

        for name, test in (
            ("middle_drag_pan", mouse_pan),
            ("shift_middle_orbit", mouse_orbit),
            ("shift_left_middle_point_orbit", point_orbit),
            ("command_shift_middle_zoom", drag_zoom),
            ("mouse_wheel_zoom", mouse_wheel),
            ("trackpad_pan", trackpad_pan),
            ("shift_trackpad_orbit", trackpad_orbit),
            ("trackpad_native_pinch", trackpad_pinch),
            ("double_middle_fit", lambda: fit_with(middle_double_click)),
            ("f6_fit", lambda: fit_with(press_f6)),
            ("middle_release_resets_state", mouse_state_after_double_click),
            ("left_click_selects", left_click_selects),
        ):
            check(name, test)

    def execute():
        document = None
        try:
            settings = App.ParamGet("User parameter:BaseApp/Preferences/View")

            def navigation_preferences():
                return {
                    "saved_style": settings.GetString("NavigationStyle", "<unset>"),
                    "migration_applied": settings.GetBool("PersonalCadFusionNavigationInitialized", False),
                }

            report["navigation_preferences"] = {"before_workbench": navigation_preferences()}
            Gui.activateWorkbench("PartDesignWorkbench")
            report["navigation_preferences"]["after_workbench"] = navigation_preferences()
            document = App.newDocument("FusionNavigationQA")
            for name, origin in (("First", (0, 0, 0)), ("Second", (40, 8, 4))):
                box = document.addObject("Part::Box", name)
                box.Length, box.Width, box.Height = 25, 18, 8
                box.Placement.Base = App.Vector(*origin)
            document.recompute()
            view = Gui.activeDocument().activeView()
            report["navigation_preferences"]["after_opening_view"] = navigation_preferences()
            expected = EXPECTED[report["profile"]]
            actual = view.getNavigationType()
            checks["startup_navigation_style"] = {
                "passed": actual == expected, "expected": expected, "actual": actual,
            }
            window = Gui.getMainWindow()
            indicator = window.findChild(QtWidgets.QPushButton, "NavigationIndicator")
            active_action = indicator.menu().defaultAction() if indicator and indicator.menu() else None
            active_style = active_action.data() if active_action else None
            checks["indicator_navigation_style"] = {
                "passed": active_style == expected, "expected": expected, "actual": active_style,
            }
            if report["profile"] == "fresh":
                exercise(view)
                require(indicator and indicator.menu(), "Navigation status menu is unavailable")
                actions = {action.data(): action for action in indicator.menu().actions()}
                require(CAD in actions and FUSION in actions, "Navigation menu lacks CAD or Fusion style")
                for style in (CAD, FUSION):
                    actions[style].trigger()
                    wait(120)
                    require(view.getNavigationType() == style
                            and indicator.menu().defaultAction() == actions[style],
                            f"Navigation menu did not switch to {style}")
                checks["style_switch_round_trip"] = {"passed": True}
            report["status"] = "passed" if all(check["passed"] for check in checks.values()) else "failed"
        except Exception:
            report["error"] = traceback.format_exc()

        Path(os.environ[REPORT_ENV]).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if report["status"] != "passed":
            os._exit(1)
        if document:
            App.closeDocument(document.Name)
        Gui.runCommand("Std_Quit", 0)

    QtCore.QTimer.singleShot(600, execute)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--profile", choices=PROFILES, default="fresh")
    parser.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args()
    app = args.app.resolve()
    if app.is_relative_to("/Applications") or app.is_relative_to(Path.home() / "Applications"):
        parser.error("Use an isolated build artifact, not an installed app")
    output = args.output.resolve()
    output.mkdir(parents=True)
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    executable = app / "Contents/MacOS" / info["CFBundleExecutable"]
    require(os.access(executable, os.X_OK), f"Cannot execute {executable}")
    report_file = output / "report.json"
    log_file = output / "freecad.log"
    with tempfile.TemporaryDirectory(prefix="personalcad-fusion-navigation-qa-") as scratch:
        profile = Path(scratch)
        for name in ("cache", "data", "temp"):
            (profile / name).mkdir()
        user_cfg = profile / "user.cfg"
        initial_style = PROFILES[args.profile]
        if initial_style:
            root = ElementTree.Element("FCParameters")
            view_settings = root
            for group in ("Root", "BaseApp", "Preferences", "View"):
                view_settings = ElementTree.SubElement(view_settings, "FCParamGroup", Name=group)
            ElementTree.SubElement(view_settings, "FCText", Name="NavigationStyle").text = initial_style
            if args.profile == "reselected-cad":
                ElementTree.SubElement(
                    view_settings, "FCBool", Name="PersonalCadFusionNavigationInitialized", Value="1"
                )
            ElementTree.ElementTree(root).write(user_cfg, encoding="utf-8", xml_declaration=True)
        macro = profile / "FusionNavigationQA.FCMacro"
        macro.write_text(
            f"import sys\nsys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
            "from fusion_navigation_qa import run_macro\nrun_macro()\n",
            encoding="utf-8",
        )
        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(profile),
            "LANG": "en_US.UTF-8",
            "TMPDIR": str(profile / "temp"),
            "XDG_CACHE_HOME": str(profile / "cache"),
            "FREECAD_USER_HOME": str(profile),
            "FREECAD_USER_DATA": str(profile / "data"),
            "FREECAD_USER_TEMP": str(profile / "temp"),
            "PYTHONDONTWRITEBYTECODE": "1",
            REPORT_ENV: str(report_file),
            PROFILE_ENV: args.profile,
        }
        command = [str(executable), "--user-cfg", str(user_cfg),
                   "--system-cfg", str(profile / "system.cfg"), str(macro)]
        with log_file.open("wb") as log:
            result = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    timeout=args.timeout, check=False)

    require(report_file.is_file(), f"App exited {result.returncode} without a report. See {log_file}.")
    report = json.loads(report_file.read_text(encoding="utf-8"))
    print(f"{report['status']}: {report_file}")
    return 0 if result.returncode == 0 and report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
