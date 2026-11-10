"""Measure an installed macOS CAD bundle using synthetic or private local STEP fixtures."""

import argparse
from collections import Counter
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import platform
import plistlib
import re
import resource
import struct
import subprocess
import tempfile
from time import monotonic
import traceback


TIERS = {"small": (1, 1), "medium": (4, 12), "large": (6, 18)}
SOURCE_UNITS = re.compile(rb"LENGTH_UNIT\(\)\s*NAMED_UNIT\(\*\)\s*SI_UNIT\(([^,]+),\s*\.METRE\.\)")
PREFIX_MM = {b".MILLI.": 1.0, b".CENTI.": 10.0, b"$": 1000.0}


def write_json(path, contents):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(contents, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def step_identity(path):
    digest = sha256()
    units = set()
    size = 0
    overlap = b""
    with path.open("rb") as source:
        for block in iter(lambda: source.read(2**20), b""):
            digest.update(block)
            size += len(block)
            chunk = overlap + block
            units.update(SOURCE_UNITS.findall(chunk))
            overlap = chunk[-256:]
    return {
        "sha256": digest.hexdigest(),
        "bytes": size,
        "si_length_scales_to_mm": [PREFIX_MM.get(unit) for unit in sorted(units)],
    }


def require(condition, detail):
    if not condition:
        raise RuntimeError(detail)


def expected_fixture(tier):
    rows, columns = TIERS[tier]
    if tier == "small":
        return 1, (columns * 19 + 4, rows * 19 + 4, 3)
    return rows * columns * 2 + 1, (columns * 19 + 4, rows * 19 + 4, 13)


def box_coordinates(box):
    return (box.XMin, box.YMin, box.ZMin, box.XMax, box.YMax, box.ZMax)


def exact_bounds(shape):
    # The ordinary BoundBox depends on the viewer's tessellation. Use the B-Rep
    # for comparisons across save/reopen and STEP conversion.
    return box_coordinates(shape.optimalBoundingBox(False, False))


def color_byte(component):
    # Match Base::Color::getPackedRGB: multiply in float32, then round half up.
    scaled, = struct.unpack("f", struct.pack("f", component * 255))
    return math.floor(scaled + 0.5)


def group_signature(obj):
    if obj.TypeId == "App::Part":
        return (obj.TypeId, tuple(sorted(group_signature(child) for child in obj.Group)))
    return (obj.TypeId,)


def describe_document(document):
    import Part

    roots = [root for root in document.RootObjects if root.Visibility]
    shape = Part.makeCompound([Part.getShape(root) for root in roots])
    solids = shape.Solids
    bounds = exact_bounds(shape)
    # Compare transformed solids rather than only checking overall dimensions.
    solid_metrics = sorted((*exact_bounds(solid), solid.Volume) for solid in solids)
    colors = Counter()
    part_colors = []
    for obj in document.Objects:
        view = obj.ViewObject
        if not obj.isDerivedFrom("Part::Feature") or not hasattr(view, "DiffuseColor"):
            continue
        part = obj.Shape
        if part.isNull():
            continue
        part = part.copy(False, False)
        part.Placement = obj.getGlobalPlacement() * obj.Placement.inverse() * part.Placement
        # FCStd stores RGB channels in 8-bit form. Compare the saved precision,
        # and count a uniform style on each face the same as a per-face style.
        face_colors = [tuple(color_byte(component) for component in color[:3])
                       for color in view.DiffuseColor]
        if len(face_colors) == 1:
            face_colors *= len(part.Faces)
        palette = Counter(face_colors)
        colors.update(palette)
        part_colors.append((exact_bounds(part), sorted(palette.items())))
    return {
        "roots": [root.TypeId for root in roots],
        "assembly_tree": sorted(group_signature(root) for root in roots),
        "types": dict(Counter(obj.TypeId for obj in document.Objects)),
        "solid_count": len(solids),
        "face_count": len(shape.Faces),
        "bounds_mm": bounds,
        "size_mm": [high - low for low, high in zip(bounds[:3], bounds[3:])],
        "volume_mm3": sum(solid[-1] for solid in solid_metrics),
        "solid_metrics": solid_metrics,
        "diffuse_color_face_counts_rgb8": sorted(colors.items()),
        "part_color_metrics": sorted(part_colors),
    }


def close(left, right):
    return math.isclose(left, right, abs_tol=1e-4, rel_tol=1e-7)


def coords_match(left, right):
    return all(close(a, b) for a, b in zip(left, right))


def shape_matches(reference, result):
    if reference["solid_count"] != result["solid_count"]:
        return False

    # Tiny STEP conversion noise can reorder two solids with a shared plane.
    unmatched = result["solid_metrics"].copy()
    for original in reference["solid_metrics"]:
        for i, candidate in enumerate(unmatched):
            if coords_match(original, candidate):
                unmatched.pop(i)
                break
        else:
            return False
    return coords_match(reference["bounds_mm"], result["bounds_mm"])


def colors_match(reference, result):
    if reference["diffuse_color_face_counts_rgb8"] != result["diffuse_color_face_counts_rgb8"]:
        return False
    unmatched = result["part_color_metrics"].copy()
    for bounds, palette in reference["part_color_metrics"]:
        for index, (candidate_bounds, candidate_palette) in enumerate(unmatched):
            if palette == candidate_palette and coords_match(bounds, candidate_bounds):
                unmatched.pop(index)
                break
        else:
            return False
    return not unmatched


def interaction(document, repetitions, expected_style):
    import FreeCADGui as Gui
    from PySide6 import QtCore, QtGui, QtWidgets

    view = Gui.getDocument(document.Name).activeView()
    style = view.getNavigationType()
    if expected_style:
        require(style == expected_style, f"Unexpected navigation style: {style}")
    graphics = view.graphicsView()
    viewport = graphics.viewport()
    ratio = viewport.devicePixelRatioF()
    center = viewport.rect().center()
    Qt = QtCore.Qt
    kind = QtCore.QEvent.Type
    none = Qt.MouseButton.NoButton
    left = Qt.MouseButton.LeftButton
    middle = Qt.MouseButton.MiddleButton
    shift = Qt.KeyboardModifier.ShiftModifier
    no_modifier = Qt.KeyboardModifier.NoModifier

    def mouse(event_type, point, button, buttons, modifiers):
        event = QtGui.QMouseEvent(event_type, QtCore.QPointF(point),
                                  QtCore.QPointF(viewport.mapToGlobal(point)), button,
                                  buttons, modifiers)
        QtWidgets.QApplication.sendEvent(viewport, event)

    def pickable_pixel():
        for offset in (0, -12, 12, -24, 24, -48, 48):
            pixel = center + QtCore.QPoint(offset, 0)
            x = round(pixel.x() * ratio)
            y = round((viewport.height() - pixel.y() - 1) * ratio)
            hit = view.getObjectInfo((x, y))
            if hit and hit.get("Document") == document.Name:
                return pixel
        raise RuntimeError("No model geometry was pickable in the fitted view")

    pixel = pickable_pixel()
    selection_ms = []
    orbit_ms = []
    for _ in range(repetitions):
        Gui.Selection.clearSelection()
        start = monotonic()
        mouse(kind.MouseButtonPress, pixel, left, left, no_modifier)
        mouse(kind.MouseButtonRelease, pixel, left, none, no_modifier)
        Gui.updateGui()
        selection_ms.append((monotonic() - start) * 1000)
        require(bool(Gui.Selection.getSelectionEx(document.Name)), "Left click did not select geometry")

    Gui.Selection.clearSelection()
    camera = view.getCamera()
    initial = tuple(view.getCameraOrientation().Q)
    for _ in range(repetitions):
        view.setCamera(camera)
        start = monotonic()
        mouse(kind.MouseButtonPress, center, middle, middle, shift)
        mouse(kind.MouseMove, center + QtCore.QPoint(32, 18), none, middle, shift)
        mouse(kind.MouseMove, center + QtCore.QPoint(64, 36), none, middle, shift)
        mouse(kind.MouseButtonRelease, center + QtCore.QPoint(64, 36), middle, none, shift)
        Gui.updateGui()
        orbit_ms.append((monotonic() - start) * 1000)
        actual = tuple(view.getCameraOrientation().Q)
        delta = min(math.dist(initial, actual), math.dist(initial, tuple(-q for q in actual)))
        require(delta > 0.01, "Shift + middle drag did not rotate the view")

    view.setCamera(camera)
    return {
        "navigation": style,
        "display_scale": ratio,
        "viewport_px": [viewport.width(), viewport.height()],
        "selection": {"samples_ms": selection_ms, "p95_ms": percentile95(selection_ms)},
        "shift_middle_orbit": {"samples_ms": orbit_ms, "p95_ms": percentile95(orbit_ms)},
    }


def percentile95(samples):
    return sorted(samples)[math.ceil(len(samples) * 0.95) - 1]


def file_options(reader, source, dialog_type, dialogs):
    from PySide6 import QtCore, QtWidgets

    def accept():
        dialog = QtWidgets.QApplication.activeModalWidget()
        if dialog is None or dialog.metaObject().className().split("::")[-1] != dialog_type:
            return
        box = dialog.findChild(QtWidgets.QDialogButtonBox)
        button = box.button(QtWidgets.QDialogButtonBox.StandardButton.Ok) if box else None
        if button:
            dialogs.append(dialog.windowTitle())
            button.click()

    timer = QtCore.QTimer()
    timer.timeout.connect(accept)
    timer.start(10)
    try:
        return reader(str(source))
    finally:
        timer.stop()


def generate_fixtures(output):
    import FreeCAD as App
    import ImportGui
    import Part

    for name, (rows, columns) in TIERS.items():
        doc = App.newDocument("SyntheticAssembly")
        root = doc.addObject("App::Part", "Assembly")
        base = root.newObject("Part::Feature", "Plate")
        base.Shape = Part.makeBox(columns * 19 + 4, rows * 19 + 4, 3)
        base.ViewObject.ShapeColor = (0.2, 0.32, 0.48)
        if name == "small":
            base.Shape = base.Shape.cut(Part.makeCylinder(2, 3, App.Vector(5, 5, 0)))
            base.Shape = base.Shape.cut(Part.makeCylinder(2, 3, App.Vector(18, 18, 0)))
        else:
            keycap = doc.addObject("Part::Feature", "Keycap")
            keycap.Shape = Part.makeBox(17, 17, 6).cut(
                Part.makeBox(13, 13, 4, App.Vector(2, 2, 0)))
            keycap.ViewObject.ShapeColor = (0.46, 0.72, 0.91)
            stem = doc.addObject("Part::Feature", "Stem")
            stem.Shape = Part.makeCylinder(3, 4, App.Vector(8.5, 8.5, 0))
            stem.ViewObject.ShapeColor = (0.9, 0.45, 0.2)
            for row in range(rows):
                group = root.newObject("App::Part", "Row")
                for column in range(columns):
                    for template, height in ((keycap, 7), (stem, 3)):
                        instance = group.newObject("App::Link", template.Name + "Instance")
                        instance.LinkedObject = template
                        position = App.Vector(3 + column * 19, 3 + row * 19, height)
                        instance.Placement = App.Placement(position, App.Rotation())
            keycap.Visibility = stem.Visibility = False
        doc.recompute()
        fixture = output / f"{name}.step"
        ImportGui.export([root], str(fixture), keepPlacement=True)
        require(fixture.is_file() and fixture.stat().st_size > 0, "Synthetic STEP export failed")
        App.closeDocument(doc.Name)
    return {name: expected_fixture(name)[0] for name in TIERS}


def run_macro(settings):
    import FreeCAD as App
    import FreeCADGui as Gui
    import ImportGui
    from PySide6 import QtCore, QtTest

    output = Path(settings["output"])
    report_file = output / "report.json"

    def run():
        report = {
            "status": "running",
            "tier": settings["tier"],
            "version": App.Version(),
            "window_title": Gui.getMainWindow().windowTitle(),
            "duration_s": {"launch_to_macro": monotonic() - settings["launch"]},
            "peak_rss_mib_by_stage": {},
            "checks": {},
        }
        dialogs = []
        report["dialogs_accepted"] = dialogs

        def checkpoint(label, start):
            report["duration_s"][label] = monotonic() - start
            # On macOS getrusage returns the maximum resident set size in bytes.
            report["peak_rss_mib_by_stage"][label] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20
            write_json(report_file, report)

        def open_step(source):
            options = file_options(ImportGui.importOptions, source, "TaskImportStep", dialogs)
            document = App.newDocument()
            ImportGui.insert(str(source), document.Name, options=options)
            view = Gui.getDocument(document.Name).activeView()
            view.setAnimationEnabled(False)
            view.viewTop()
            view.fitAll()
            Gui.updateGui()
            QtTest.QTest.qWait(40)
            return document, options

        try:
            if settings["tier"] == "generate":
                report["synthetic_solids"] = generate_fixtures(output)
            else:
                source = Path(settings["step"])
                report["input_step"] = step_identity(source)
                write_json(report_file, report)
                start = monotonic()
                document, report["import_options"] = open_step(source)
                checkpoint("step_import_to_view_ready", start)
                start = monotonic()
                report["interaction"] = interaction(document, settings["repetitions"], settings["navigation"])
                checkpoint("interaction", start)
                start = monotonic()
                report["recomputed_objects"] = document.recompute(None, True)
                report["recompute_invalid_types"] = dict(Counter(
                    obj.TypeId for obj in document.Objects if "Invalid" in obj.State
                ))
                Gui.updateGui()
                checkpoint("forced_recompute", start)

                imported = describe_document(document)
                report["imported"] = imported
                checks = report["checks"]
                checks["nonempty_model"] = imported["solid_count"] > 0 and imported["volume_mm3"] > 0
                checks["recompute_no_invalid_objects"] = not report["recompute_invalid_types"]
                if settings["tier"] in TIERS:
                    expected_solids, expected_size = expected_fixture(settings["tier"])
                    checks["expected_solid_count"] = imported["solid_count"] == expected_solids
                    checks["expected_mm_size"] = coords_match(imported["size_mm"], expected_size)
                if settings["capture"]:
                    Gui.activeDocument().activeView().saveImage(str(output / "viewport.png"), 1600, 1000, "Current")

                fcstd = output / "assembly.FCStd"
                start = monotonic()
                document.saveAs(str(fcstd))
                require(fcstd.is_file() and fcstd.stat().st_size > 0, "Native save did not produce an FCStd file")
                report["fcstd_bytes"] = fcstd.stat().st_size
                App.closeDocument(document.Name)
                checkpoint("fcstd_save_and_close", start)
                start = monotonic()
                document = App.openDocument(str(fcstd))
                Gui.updateGui()
                checkpoint("fcstd_reopen", start)
                report["reopened_interaction"] = interaction(document, 1, settings["navigation"])
                reopened = describe_document(document)
                report["reopened"] = reopened
                checks["fcstd_geometry"] = shape_matches(imported, reopened)
                checks["fcstd_types"] = imported["types"] == reopened["types"]
                checks["fcstd_part_structure"] = imported["assembly_tree"] == reopened["assembly_tree"]
                checks["fcstd_colors"] = colors_match(imported, reopened)
                write_json(report_file, report)

                if settings["roundtrip"]:
                    step = output / "roundtrip.step"
                    roots = [root for root in document.RootObjects if root.Visibility]
                    start = monotonic()
                    options = file_options(ImportGui.exportOptions, step, "TaskExportStep", dialogs)
                    report["export_options"] = options
                    ImportGui.export(roots, str(step), options=options)
                    require(step.is_file() and step.stat().st_size > 0, "STEP export did not produce a file")
                    checkpoint("step_export", start)
                    report["output_step"] = step_identity(step)
                    write_json(report_file, report)
                    App.closeDocument(document.Name)
                    start = monotonic()
                    document, _ = open_step(step)
                    checkpoint("step_reimport_to_view_ready", start)
                    exported = describe_document(document)
                    report["roundtrip"] = exported
                    checks["step_geometry"] = shape_matches(imported, exported)
                    checks["step_colors"] = colors_match(imported, exported)
                    checks["step_part_structure"] = imported["assembly_tree"] == exported["assembly_tree"]
                else:
                    report["step_roundtrip"] = "omitted by --native-only"
                checks["step_input_unchanged"] = report["input_step"] == step_identity(source)
                App.closeDocument(document.Name)

            report["status"] = "passed" if all(report["checks"].values()) else "failed"
        except Exception:
            report["status"] = "failed"
            report["error"] = traceback.format_exc()
        report["peak_rss_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20
        write_json(report_file, report)
        # Do not get stuck on a save confirmation if a test stopped mid-workflow.
        for name in App.listDocuments():
            App.closeDocument(name)
        Gui.runCommand("Std_Quit", 0)

    QtCore.QTimer.singleShot(100, run)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path, help="macOS app bundle to launch")
    parser.add_argument("--output", required=True, type=Path, help="New evidence directory")
    parser.add_argument("--tier", required=True, choices=(*TIERS, "private", "generate"))
    parser.add_argument("--step", type=Path, help="Explicit local STEP input for a measurement run")
    parser.add_argument("--repetitions", default=20, type=int)
    parser.add_argument("--timeout", default=420, type=int)
    parser.add_argument("--capture", action="store_true", help="Record the imported view inside the evidence directory")
    parser.add_argument("--native-only", action="store_true", help="Omit STEP export/reimport for a private stress model")
    parser.add_argument("--navigation", default="Gui::FusionNavigationStyle")
    args = parser.parse_args()
    if args.tier != "generate" and (args.step is None or not args.step.is_file()):
        parser.error("Specify a readable --step file")
    if min(args.repetitions, args.timeout) <= 0:
        parser.error("Repetitions and timeout must be positive")
    if args.native_only and args.tier != "private":
        parser.error("--native-only is only available with --tier private")

    app = args.app.resolve()
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    executable = app / "Contents/MacOS" / info["CFBundleExecutable"]
    output = args.output.resolve()
    output.mkdir(parents=True, mode=0o700)
    code_resources = app / "Contents/_CodeSignature/CodeResources"
    host = {
        "application": str(app),
        "bundle_id": info["CFBundleIdentifier"],
        "bundle_version": info["CFBundleVersion"],
        "code_resources_sha256": sha256(code_resources.read_bytes()).hexdigest() if code_resources.is_file() else None,
        "macOS": platform.mac_ver()[0],
        "architecture": platform.machine(),
        "model": subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.model"], text=True).strip(),
    }
    with tempfile.TemporaryDirectory(prefix="sven-cad-assembly-qa-") as scratch:
        profile = Path(scratch)
        for name in ("cache", "data", "temp"):
            (profile / name).mkdir()
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
        }
        macro = profile / "AssemblyPerformanceQA.FCMacro"
        settings = {
            "tier": args.tier,
            "output": str(output),
            "step": str(args.step.resolve()) if args.step else None,
            "navigation": args.navigation,
            "repetitions": args.repetitions,
            "capture": args.capture,
            "roundtrip": not args.native_only,
            "launch": monotonic(),
        }
        macro.write_text(
            f"import sys\nsys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
            f"from assembly_performance_qa import run_macro\nrun_macro({settings!r})\n",
            encoding="utf-8",
        )
        with (output / "freecad.log").open("wb") as log:
            try:
                result = subprocess.run(
                    [str(executable), "--user-cfg", str(profile / "user.cfg"),
                     "--system-cfg", str(profile / "system.cfg"), str(macro)],
                    stdout=log, stderr=subprocess.STDOUT, env=env, timeout=args.timeout, check=False,
                )
                host["freecad_exit"] = result.returncode
            except subprocess.TimeoutExpired:
                host["error"] = f"FreeCAD did not finish within {args.timeout}s"
    report_file = output / "report.json"
    report = json.loads(report_file.read_text()) if report_file.is_file() else {}
    host["fixture_status"] = report.get("status")
    host["status"] = "passed" if host.get("freecad_exit") == 0 and report.get("status") == "passed" else "failed"
    write_json(output / "run-result.json", host)
    if args.tier != "generate":
        fields = ("status", "version", "window_title", "input_step", "output_step", "fcstd_bytes", "step_roundtrip", "duration_s",
                  "peak_rss_mib", "peak_rss_mib_by_stage", "checks", "error")
        summary = {field: report[field] for field in fields if field in report}
        summary["status"] = host["status"]
        summary["runtime"] = host
        metrics = ("solid_count", "face_count", "size_mm", "volume_mm3", "types")
        for phase in ("imported", "reopened", "roundtrip"):
            if phase in report:
                summary[phase] = {key: report[phase][key] for key in metrics}
        if "interaction" in report:
            info = report["interaction"]
            summary["interaction"] = {
                "navigation": info["navigation"],
                "selection_p95_ms": info["selection"]["p95_ms"],
                "orbit_p95_ms": info["shift_middle_orbit"]["p95_ms"],
                "sample_count_each": len(info["selection"]["samples_ms"]),
            }
        write_json(output / "summary.json", summary)
    print(f"{host['status']}: {report_file}")
    return 0 if host["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
