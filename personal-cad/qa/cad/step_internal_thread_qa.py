"""Verify Sven CAD internal threads created directly on imported STEP hole walls."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import time
import traceback


OUTPUT_ENV = "PERSONAL_CAD_STEP_INTERNAL_THREAD_QA"
COMMAND = "PartDesign_InternalThread"
# Nominal size, major diameter, pitch, tapping bore inside the 6H minor-diameter range.
SIZES = (("M2x0.4", 2.0, 0.4, 1.60), ("M2.5x0.45", 2.5, 0.45, 2.05),
         ("M3x0.5", 3.0, 0.5, 2.50), ("M4x0.7", 4.0, 0.7, 3.30))
INTERNAL_LIMITS = {
    "M2x0.4": "6H | D1 1.567–1.679 mm | D2 1.740–1.830 mm | L 1.0–3.0 mm",
    "M2.5x0.45": "6H | D1 2.013–2.138 mm | D2 2.208–2.303 mm | L 1.3–3.8 mm",
    "M3x0.5": "6H | D1 2.459–2.599 mm | D2 2.675–2.775 mm | L 1.5–4.5 mm",
    "M4x0.7": "6H | D1 3.242–3.422 mm | D2 3.545–3.663 mm | L 2.0–6.0 mm",
}
BLOCK_HEIGHT = 8.0
BLIND_DEPTH = 6.0


def run_macro():
    import FreeCAD as App
    import FreeCADGui as Gui
    import Part
    from PySide6 import QtCore, QtTest, QtWidgets

    output = Path(os.environ[OUTPUT_ENV])
    shots = output / "screenshots"
    shots.mkdir(exist_ok=True)
    fixtures = output / "fixtures"
    fixtures.mkdir(exist_ok=True)
    report = {"status": "failed", "version": list(App.Version()), "checks": {}, "messages": {}}
    checks = report["checks"]
    messages = report["messages"]
    window = Gui.getMainWindow()
    progress = output / "progress.jsonl"
    started = time.monotonic()

    def log(step, **data):
        with progress.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"t": round(time.monotonic() - started, 3), "step": step, **data},
                                    ensure_ascii=False) + "\n")

    # ---------- synthetic STEP fixtures ----------
    def block(height=BLOCK_HEIGHT, size=10.0):
        return Part.makeBox(size, size, height)

    def fixture(name, shape):
        path = fixtures / f"{name}.step"
        if not path.exists():
            shape.exportStep(str(path))
        return path

    def blind_top(bore, depth=BLIND_DEPTH, height=BLOCK_HEIGHT):
        return block(height).cut(Part.makeCylinder(bore / 2, depth + 1, App.Vector(5, 5, height - depth)))

    def blind_bottom(bore, depth=BLIND_DEPTH):
        return block().cut(Part.makeCylinder(bore / 2, depth + 1, App.Vector(5, 5, -1)).common(
            Part.makeBox(10, 10, depth)))

    def through(bore, center=(5, 5)):
        return block().cut(Part.makeCylinder(bore / 2, BLOCK_HEIGHT + 2, App.Vector(center[0], center[1], -1)))

    PART_PLACEMENT = App.Placement(App.Vector(12, 5, -8), App.Rotation(App.Vector(0, 1, 0), 60))
    SOURCE_PLACEMENT = App.Placement(App.Vector(2, 3, 4), App.Rotation(App.Vector(1, 0, 0), 32))

    def import_step(doc, path, transformed=True):
        part = doc.addObject("App::Part", "ImportedPart")
        source = doc.addObject("Part::Feature", "StepSolid")
        source.Shape = Part.read(str(path))
        part.addObject(source)
        if transformed:
            part.Placement = PART_PLACEMENT
            source.Placement = SOURCE_PLACEMENT
        doc.recompute()
        return part, source

    def bore_face(shape, radius):
        names = [f"Face{index}" for index, face in enumerate(shape.Faces, 1)
                 if isinstance(face.Surface, Part.Cylinder) and abs(face.Surface.Radius - radius) < 1e-6]
        assert len(names) == 1, names
        return names[0]

    # ---------- GUI helpers ----------
    def ribbon_button():
        ribbon = window.findChild(QtWidgets.QToolBar, "PersonalCadDesignToolbar")
        assert ribbon is not None, "Design ribbon missing"
        button = ribbon.findChild(QtWidgets.QToolButton, COMMAND)
        assert button is not None, "Internal Thread ribbon button missing"
        assert button.isVisible() and button.isEnabled(), "Internal Thread button hidden or disabled"
        return button

    def select(doc, obj, subname):
        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(doc.Name, obj.Name, subname)
        QtTest.QTest.qWait(80)
        assert Gui.Selection.hasSelection()

    def invoke(handlers, timeout_ms=60000):
        """Click the ribbon button and answer each modal window in turn."""
        button = ribbon_button()
        pending = list(handlers)
        errors = []
        state = {"last": None}
        deadline = time.monotonic() + timeout_ms / 1000

        def tick():
            modal = QtWidgets.QApplication.activeModalWidget()
            if modal is None or not modal.isVisible() or modal is state["last"]:
                if time.monotonic() > deadline and modal is not None:
                    errors.append("Timed out waiting for the next modal window")
                    modal.close()
                return
            state["last"] = modal
            if not pending:
                text = modal.text() if isinstance(modal, QtWidgets.QMessageBox) else ""
                errors.append(f"Unexpected modal {modal.metaObject().className()} {modal.objectName()} {text}")
                modal.close()
                return
            handler = pending.pop(0)
            try:
                handler(modal)
            except Exception:
                errors.append(traceback.format_exc())
                if modal.isVisible():
                    modal.close()

        timer = QtCore.QTimer()
        timer.setInterval(60)
        timer.timeout.connect(tick)
        timer.start()
        QtTest.QTest.mouseClick(button, QtCore.Qt.MouseButton.LeftButton)
        QtTest.QTest.qWait(150)
        timer.stop()
        assert not errors, errors
        assert not pending, f"{len(pending)} expected modal window(s) never appeared"

    def ok_button(modal):
        return modal.findChild(QtWidgets.QDialogButtonBox).button(QtWidgets.QDialogButtonBox.StandardButton.Ok)

    def dialog(spec, *, length=None, left=False, reverse=None, modeled=True, accept=True,
               info=None, shot=None, before=None, extra=None):
        def handle(modal):
            assert modal.objectName() == "InternalThreadDialog", modal.objectName()
            if before:
                assert before(), "Dialog opening changed the document"
            size = modal.findChild(QtWidgets.QComboBox, "InternalThreadSize")
            assert size.currentText() == spec, (size.currentText(), spec)
            klass = modal.findChild(QtWidgets.QComboBox, "InternalThreadManufacturingClass")
            assert klass.currentText() == "6H" and klass.count() == 1
            limits = modal.findChild(QtWidgets.QLabel, "InternalThreadManufacturingLimits")
            assert limits.isVisible() and INTERNAL_LIMITS[spec] in limits.text(), limits.text()
            spin = modal.findChild(QtWidgets.QAbstractSpinBox, "InternalThreadLength")
            rev = modal.findChild(QtWidgets.QCheckBox, "InternalThreadReverseStart")
            model_box = modal.findChild(QtWidgets.QCheckBox, "InternalThreadModeled")
            assert model_box.isChecked(), "Modeled grooves should be the default"
            if info is not None:
                info.update({"default_length_mm": spin.property("rawValue"),
                             "maximum_length_mm": spin.property("maximum"),
                             "reverse_option": rev is not None, "title": modal.windowTitle(),
                             "limits_text": limits.text()})
            if extra:
                extra(modal)
            if shot:
                QtTest.QTest.qWait(60)
                assert modal.grab().save(str(shots / shot))
            if not accept:
                modal.reject()
                return
            if length is not None:
                assert spin.setProperty("rawValue", length)
                assert abs(spin.property("rawValue") - length) < 1e-9, spin.property("rawValue")
            modal.findChild(QtWidgets.QComboBox, "InternalThreadHand").setCurrentIndex(1 if left else 0)
            if reverse is not None:
                assert rev is not None, "Reverse-start option missing"
                rev.setChecked(reverse)
            model_box.setChecked(modeled)
            ok_button(modal).click()
        return handle

    def message(key, expected, shot=None):
        def handle(modal):
            assert isinstance(modal, QtWidgets.QMessageBox), modal.metaObject().className()
            text = modal.text()
            messages[key] = text
            assert expected in text, (expected, text)
            if shot:
                assert modal.grab().save(str(shots / shot))
            modal.accept()
        return handle

    def snapshot(doc, source):
        return (tuple(obj.Name for obj in doc.Objects), doc.UndoCount, doc.RedoCount,
                source.ViewObject.Visibility, source.Shape.exportBrepToString(),
                tuple(o.Name for o in source.getParentGeoFeatureGroup().Group)
                if source.getParentGeoFeatureGroup() else ())

    def new_document(name):
        doc = App.newDocument(name)
        doc.UndoMode = 1
        return doc

    def internal_threads(doc):
        return [obj for obj in doc.Objects if obj.TypeId == "PartDesign::InternalThread"]

    # ---------- B-Rep section measurement ----------
    def basis(axis):
        helper = App.Vector(1, 0, 0) if abs(axis.x) < 0.9 else App.Vector(0, 1, 0)
        u = axis.cross(helper)
        u.normalize()
        v = axis.cross(u)
        v.normalize()
        return u, v

    def frame(source, origin_local, axis_local):
        origin = source.Placement.multVec(origin_local)
        axis = source.Placement.Rotation.multVec(axis_local)
        axis.normalize()
        return origin, axis

    def grooves(shape, origin, axis, radius, azimuth, s0, s1):
        """Section an axial line at one radius and azimuth; return the void intervals."""
        u, v = basis(axis)
        radial = u * math.cos(azimuth) + v * math.sin(azimuth)
        start = origin + axis * s0 + radial * radius
        end = origin + axis * s1 + radial * radius
        params = [s0, s1] + [(vertex.Point - origin).dot(axis)
                             for vertex in shape.section(Part.makeLine(start, end)).Vertexes]
        params = sorted(params)
        unique = [params[0]]
        for value in params[1:]:
            if value - unique[-1] > 1e-7:
                unique.append(value)
        voids = []
        for low, high in zip(unique, unique[1:]):
            mid = (low + high) / 2
            point = origin + axis * mid + radial * radius
            if not shape.isInside(point, 1e-7, True):
                voids.append((low, high))
        return voids

    def void_radius(shape, origin, axis, s, azimuth, inner, outer):
        u, v = basis(axis)
        radial = u * math.cos(azimuth) + v * math.sin(azimuth)
        base = origin + axis * s
        line = Part.makeLine(base + radial * inner, base + radial * outer)
        radii = sorted((vertex.Point - base).dot(radial) for vertex in shape.section(line).Vertexes)
        radii = [r for r in radii if r > inner + 1e-7]
        assert radii, "No wall found on the radial section"
        return radii[0]

    def measure(shape, origin, axis, spec, major, pitch, bore, depth, length):
        """Measure pitch, hand, start side, thread length and groove root on the B-Rep."""
        probe = (bore + major) / 4
        azimuth = math.pi / 7
        first = grooves(shape, origin, axis, probe, azimuth, 0.002, depth - 0.002)
        quarter = grooves(shape, origin, axis, probe, azimuth + math.pi / 2, 0.002, depth - 0.002)
        assert len(first) >= 3 and len(quarter) >= 3, (spec, first, quarter)
        mids = [(a + b) / 2 for a, b in first]
        inner = mids[1:-1]
        assert len(inner) >= 2, (spec, mids)
        diffs = [b - a for a, b in zip(inner, inner[1:])]
        measured_pitch = sum(diffs) / len(diffs)
        qmids = [(a + b) / 2 for a, b in quarter][1:-1]
        advances = sorted(math.remainder((q - inner[0]) / pitch, 1.0) for q in qmids)
        advance = advances[len(advances) // 2]
        spread = max(abs(math.remainder(a - advance, 1.0)) for a in advances)
        threaded_from = min(a for a, _ in first)
        threaded_to = max(b for _, b in first)
        root = void_radius(shape, origin, axis, inner[len(inner) // 2], azimuth, bore / 4, major / 2 + 1.0)
        plain = None
        if length + 0.6 < depth - 0.1:
            plain = void_radius(shape, origin, axis, (length + depth) / 2 + 0.3, azimuth, bore / 4,
                                major / 2 + 1.0)
        return {"measured_pitch_mm": measured_pitch, "phase_advance_turns": advance,
                "phase_spread_turns": spread, "groove_count": len(first),
                "threaded_from_mm": threaded_from, "threaded_to_mm": threaded_to,
                "root_radius_mm": root, "plain_bore_radius_mm": plain}

    def assert_thread(m, spec, major, pitch, bore, length, left, label):
        assert math.isclose(m["measured_pitch_mm"], pitch, abs_tol=0.005), (label, m)
        assert abs(m["phase_advance_turns"] - (-0.25 if left else 0.25)) < 0.015, (label, m)
        assert m["phase_spread_turns"] < 0.015, (label, m)
        assert m["threaded_from_mm"] < pitch, (label, m)
        assert m["threaded_to_mm"] <= length + 1e-3, (label, m)
        assert m["threaded_to_mm"] > length - pitch, (label, m)
        assert abs(m["groove_count"] - length / pitch) <= 1.5, (label, m)
        assert abs(m["root_radius_mm"] - major / 2) < 2e-3, (label, m)
        if m["plain_bore_radius_mm"] is not None:
            assert abs(m["plain_bore_radius_mm"] - bore / 2) < 1e-4, (label, m)

    def assert_result(doc, part, source, thread, spec, major, pitch, bore, length, left, modeled, before):
        assert thread.isValid(), (thread.Label, thread.getStatusString())
        shape = thread.Shape
        assert shape.isValid() and len(shape.Solids) == 1
        assert thread.getParentGeoFeatureGroup() == part
        assert thread.Support[0] == source
        assert thread.Placement.isSame(source.Placement, 1e-9)
        assert thread.getGlobalPlacement().isSame(source.getGlobalPlacement(), 1e-9)
        assert thread.ThreadSize == spec and thread.LeftHanded == left and thread.Modeled == modeled
        assert math.isclose(thread.ThreadLength.Value, length, abs_tol=1e-9)
        assert math.isclose(thread.Pitch.Value, pitch, abs_tol=1e-12)
        assert math.isclose(thread.MajorDiameter.Value, major, abs_tol=1e-12)
        assert math.isclose(thread.BoreDiameter.Value, bore, abs_tol=1e-6)
        assert thread.ManufacturingClass == "6H" and thread.ManufacturingLimits == INTERNAL_LIMITS[spec]
        assert source.Shape.exportBrepToString() == before[4], "Source B-Rep changed"
        assert not source.ViewObject.Visibility and thread.ViewObject.Visibility
        assert doc.UndoCount == before[1] + 1, (doc.UndoCount, before[1])
        if modeled:
            assert shape.Volume < source.Shape.Volume - 1e-4
            assert len(shape.Faces) > len(source.Shape.Faces)
        else:
            assert math.isclose(shape.Volume, source.Shape.Volume, abs_tol=1e-7)
            assert len(shape.Faces) == len(source.Shape.Faces)

    # ---------- checks ----------
    def check_ribbon():
        doc = new_document("StepInternalThreadRibbonQA")
        import_step(doc, fixture("through-M3x0.5", through(2.5)))
        Gui.activateWorkbench("PartDesignWorkbench")
        Gui.activeDocument().activeView().fitAll()
        QtTest.QTest.qWait(300)
        button = ribbon_button()
        cmd = Gui.Command.get(COMMAND)
        checks["ribbon_entry"] = {"text": button.text(), "tooltip": button.toolTip(),
                                  "icon": not button.icon().isNull(), "shortcut": cmd.getShortcut()}
        checks["ribbon_entry"]["default_locale"] = Gui.getLocale()
        assert button.text() in ("内螺纹", "Internal Thread") and not button.icon().isNull(), checks["ribbon_entry"]
        assert window.grab().save(str(shots / "ribbon-internal-thread-default.png"))
        # Run the measured workflow in English so message assertions use the source strings.
        english = next(key for key, code in Gui.supportedLocales().items() if code in ("en", "en-US", "en_US"))
        Gui.setLocale(english)
        QtTest.QTest.qWait(600)
        Gui.activateWorkbench("PartDesignWorkbench")
        QtTest.QTest.qWait(300)
        button = ribbon_button()
        checks["ribbon_entry"]["english"] = {"locale": english, "text": button.text()}
        assert button.text() == "Internal Thread", checks["ribbon_entry"]
        assert window.grab().save(str(shots / "ribbon-internal-thread-en.png"))
        App.closeDocument(doc.Name)

    def geometry_cases():
        cases = []
        for spec, major, pitch, bore in SIZES:
            blind_path = fixture(f"blind-top-{spec}", blind_top(bore))
            through_path = fixture(f"through-{spec}", through(bore))
            for left in (False, True):
                for kind, reverse in (("blind", None), ("through", False), ("through", True)):
                    label = f"{spec}-{'LH' if left else 'RH'}-{kind}{'-reversed' if reverse else ''}"
                    log("case", label=label)
                    doc = new_document("StepInternalThreadQA")
                    part, source = import_step(doc, blind_path if kind == "blind" else through_path)
                    face = bore_face(source.Shape, bore / 2)
                    Gui.activeDocument().activeView().fitAll()
                    select(doc, part, f"{source.Name}.{face}")
                    before = snapshot(doc, source)
                    info = {}
                    length = None if kind == "blind" else 6 * pitch
                    shot = f"dialog-{spec}-{kind}.png" if not left and not reverse else None
                    invoke([dialog(spec, length=length, left=left, reverse=reverse, info=info, shot=shot,
                                   before=lambda: snapshot(doc, source) == before)])
                    threads = internal_threads(doc)
                    assert len(threads) == 1, (label, len(threads))
                    thread = threads[0]
                    expected_length = 4 * pitch if kind == "blind" else 6 * pitch
                    assert math.isclose(info["default_length_mm"], 4 * pitch, abs_tol=1e-9), info
                    assert info["reverse_option"] == (kind == "through"), info
                    assert math.isclose(info["maximum_length_mm"],
                                        BLIND_DEPTH if kind == "blind" else BLOCK_HEIGHT, abs_tol=1e-6), info
                    assert_result(doc, part, source, thread, spec, major, pitch, bore, expected_length, left,
                                  True, before)
                    assert thread.ReverseStart == bool(reverse)
                    depth = BLIND_DEPTH if kind == "blind" else BLOCK_HEIGHT
                    top, down = frame(source, App.Vector(5, 5, BLOCK_HEIGHT), App.Vector(0, 0, -1))
                    m_top = measure(thread.Shape, top, down, spec, major, pitch, bore, depth, depth)
                    if kind == "blind":
                        start = "top"
                        m = measure(thread.Shape, top, down, spec, major, pitch, bore, depth, expected_length)
                    else:
                        bottom, up = frame(source, App.Vector(5, 5, 0), App.Vector(0, 0, 1))
                        if m_top["threaded_to_mm"] <= expected_length + 1e-3:
                            start = "top"
                            m = measure(thread.Shape, top, down, spec, major, pitch, bore, depth,
                                        expected_length)
                        else:
                            start = "bottom"
                            m = measure(thread.Shape, bottom, up, spec, major, pitch, bore, depth,
                                        expected_length)
                    assert_thread(m, spec, major, pitch, bore, expected_length, left, label)
                    removed = source.Shape.Volume - thread.Shape.Volume
                    cases.append({"case": label, "spec": spec, "hole": kind, "reverse_start": bool(reverse),
                                  "left_handed": left, "thread_length_mm": expected_length,
                                  "pitch_mm": pitch, "bore_mm": bore, "start_opening": start,
                                  "removed_volume_mm3": removed, "dialog": info, **m})
                    App.closeDocument(doc.Name)
        # Blind start must be the open (top) end; through default and reverse must be opposite ends.
        for spec, *_ in SIZES:
            for hand in (False, True):
                group = {c["hole"] + ("-r" if c["reverse_start"] else ""): c for c in cases
                         if c["spec"] == spec and c["left_handed"] == hand}
                assert group["blind"]["start_opening"] == "top", group["blind"]
                assert group["through"]["start_opening"] != group["through-r"]["start_opening"], group
        checks["four_sizes_hands_blind_through_both_ends"] = cases

    def check_extra_geometry():
        # Blind hole opening on the other side, and a full-depth through thread at the length limit.
        results = {}
        spec, major, pitch, bore = SIZES[2]
        path = fixture("blind-bottom-M3x0.5", blind_bottom(bore))
        doc = new_document("StepInternalThreadBottomQA")
        part, source = import_step(doc, path)
        select(doc, part, f"{source.Name}.{bore_face(source.Shape, bore / 2)}")
        before = snapshot(doc, source)
        invoke([dialog(spec, length=2.0)])
        thread, = internal_threads(doc)
        assert_result(doc, part, source, thread, spec, major, pitch, bore, 2.0, False, True, before)
        bottom, up = frame(source, App.Vector(5, 5, 0), App.Vector(0, 0, 1))
        m = measure(thread.Shape, bottom, up, spec, major, pitch, bore, BLIND_DEPTH, 2.0)
        assert_thread(m, spec, major, pitch, bore, 2.0, False, "blind-bottom")
        results["blind_open_at_bottom"] = m
        App.closeDocument(doc.Name)

        path = fixture("through-M3x0.5", through(bore))
        doc = new_document("StepInternalThreadFullQA")
        part, source = import_step(doc, path)
        select(doc, part, f"{source.Name}.{bore_face(source.Shape, bore / 2)}")
        before = snapshot(doc, source)
        overrun = {}

        def try_overrun(modal):
            spin = modal.findChild(QtWidgets.QAbstractSpinBox, "InternalThreadLength")
            spin.setProperty("rawValue", BLOCK_HEIGHT + 3)
            overrun["typed_mm"] = BLOCK_HEIGHT + 3
            overrun["kept_mm"] = spin.property("rawValue")
            assert overrun["kept_mm"] <= BLOCK_HEIGHT + 1e-9, overrun

        invoke([dialog(spec, length=BLOCK_HEIGHT, extra=try_overrun)])
        thread, = internal_threads(doc)
        assert_result(doc, part, source, thread, spec, major, pitch, bore, BLOCK_HEIGHT, False, True, before)
        top, down = frame(source, App.Vector(5, 5, BLOCK_HEIGHT), App.Vector(0, 0, -1))
        m = measure(thread.Shape, top, down, spec, major, pitch, bore, BLOCK_HEIGHT, BLOCK_HEIGHT)
        assert m["threaded_to_mm"] > BLOCK_HEIGHT - pitch and m["groove_count"] >= BLOCK_HEIGHT / pitch - 1.5, m
        results["through_full_depth_length_limit"] = {**m, "dialog_overrun": overrun}
        App.closeDocument(doc.Name)
        checks["blind_bottom_opening_and_full_depth_limit"] = results

    def check_workflow():
        """Cancel, wrong size, undo/redo, save/reopen/edit, schematic, transform updates."""
        spec, major, pitch, bore = SIZES[2]
        result = {}
        path = fixture("through-M3x0.5", through(bore))
        doc = new_document("StepInternalThreadWorkflowQA")
        part, source = import_step(doc, path)
        face = bore_face(source.Shape, bore / 2)
        select(doc, part, f"{source.Name}.{face}")
        before = snapshot(doc, source)

        invoke([dialog(spec, accept=False)])
        assert snapshot(doc, source) == before
        result["cancel_unchanged"] = True

        def wrong_size(modal):
            size = modal.findChild(QtWidgets.QComboBox, "InternalThreadSize")
            size.setCurrentIndex(size.findText("M4x0.7"))
            ok_button(modal).click()
            assert modal.isVisible(), "Mismatched size was accepted"
            texts = [label.text() for label in modal.findChildren(QtWidgets.QLabel) if label.text()]
            hint = next((t for t in texts if "matches the bore diameter" in t), None)
            assert hint, texts
            messages["dialog_wrong_size"] = hint
            assert modal.grab().save(str(shots / "dialog-wrong-size.png"))
            modal.reject()

        invoke([wrong_size])
        assert snapshot(doc, source) == before
        result["wrong_size_kept_dialog_open_and_unchanged"] = True

        invoke([dialog(spec, length=3.0, left=True, reverse=False)])
        thread, = internal_threads(doc)
        name = thread.Name
        assert_result(doc, part, source, thread, spec, major, pitch, bore, 3.0, True, True, before)
        volume = thread.Shape.Volume
        assert window.grab().save(str(shots / "after-create.png"))
        view = Gui.activeDocument().activeView()
        view.viewIsometric()
        view.fitAll()
        QtTest.QTest.qWait(200)
        view.saveImage(str(shots / "viewport-threaded-through-hole.png"), 1200, 900, "White")
        assert (shots / "viewport-threaded-through-hole.png").stat().st_size > 10000
        doc.undo()
        assert doc.getObject(name) is None and source.ViewObject.Visibility
        assert snapshot(doc, source)[:1] == before[:1] and doc.UndoCount == before[1]
        assert source.Shape.exportBrepToString() == before[4]
        doc.redo()
        thread = doc.getObject(name)
        assert thread is not None and thread.isValid() and not source.ViewObject.Visibility
        assert math.isclose(thread.Shape.Volume, volume, abs_tol=1e-7)
        result["undo_redo_single_transaction"] = {"volume_mm3": volume}

        # Transform: move the source inside the part and verify the groove follows the bore.
        moved = App.Placement(App.Vector(-1, 2, 0.5), App.Rotation(App.Vector(0, 0, 1), 40))
        source.Placement = moved
        doc.recompute()
        assert thread.isValid() and thread.Placement.isSame(moved, 1e-9)
        top, down = frame(source, App.Vector(5, 5, BLOCK_HEIGHT), App.Vector(0, 0, -1))
        bottom, up = frame(source, App.Vector(5, 5, 0), App.Vector(0, 0, 1))
        m_top = measure(thread.Shape, top, down, spec, major, pitch, bore, BLOCK_HEIGHT, BLOCK_HEIGHT)
        default_end = "top" if m_top["threaded_to_mm"] <= 3.0 + 1e-3 else "bottom"
        m = measure(thread.Shape, top if default_end == "top" else bottom,
                    down if default_end == "top" else up, spec, major, pitch, bore, BLOCK_HEIGHT, 3.0)
        assert_thread(m, spec, major, pitch, bore, 3.0, True, "moved")
        result["source_moved_thread_follows"] = {"default_end": default_end, **m}

        file = output / "step-internal-thread.FCStd"
        source_name = source.Name
        doc.saveAs(str(file))
        App.closeDocument(doc.Name)
        doc = App.openDocument(str(file))
        source = doc.getObject(source_name)
        thread = doc.getObject(name)
        part = source.getParentGeoFeatureGroup()
        original = source.Shape.exportBrepToString()
        assert thread.Support[0] == source and thread.LeftHanded and thread.Modeled
        assert thread.ManufacturingLimits == INTERNAL_LIMITS[spec]
        assert math.isclose(thread.Shape.Volume, volume, abs_tol=1e-6)
        doc.recompute()
        assert thread.isValid() and math.isclose(thread.Shape.Volume, volume, abs_tol=1e-6)

        def end_frame(end):
            return (top, down) if end == "top" else (bottom, up)

        top, down = frame(source, App.Vector(5, 5, BLOCK_HEIGHT), App.Vector(0, 0, -1))
        bottom, up = frame(source, App.Vector(5, 5, 0), App.Vector(0, 0, 1))
        edits = {}
        thread.ThreadLength = 4.5
        doc.recompute()
        assert thread.isValid() and thread.Shape.Volume < volume
        m = measure(thread.Shape, *end_frame(default_end), spec, major, pitch, bore, BLOCK_HEIGHT, 4.5)
        assert_thread(m, spec, major, pitch, bore, 4.5, True, "edit-length")
        edits["length_4_5"] = m
        thread.LeftHanded = False
        doc.recompute()
        m = measure(thread.Shape, *end_frame(default_end), spec, major, pitch, bore, BLOCK_HEIGHT, 4.5)
        assert_thread(m, spec, major, pitch, bore, 4.5, False, "edit-hand")
        edits["right_hand"] = m
        thread.ReverseStart = True
        doc.recompute()
        other = "bottom" if default_end == "top" else "top"
        m = measure(thread.Shape, *end_frame(other), spec, major, pitch, bore, BLOCK_HEIGHT, 4.5)
        assert_thread(m, spec, major, pitch, bore, 4.5, False, "edit-reverse")
        edits["reverse_start"] = {"end": other, **m}
        modeled_volume = thread.Shape.Volume
        thread.Modeled = False
        doc.recompute()
        assert thread.isValid() and math.isclose(thread.Shape.Volume, source.Shape.Volume, abs_tol=1e-7)
        assert len(thread.Shape.Faces) == len(source.Shape.Faces)
        assert thread.Pitch.Value == 0.5 and thread.ManufacturingLimits == INTERNAL_LIMITS[spec]
        edits["schematic"] = {"volume_mm3": thread.Shape.Volume, "source_volume_mm3": source.Shape.Volume}
        thread.Modeled = True
        doc.recompute()
        assert math.isclose(thread.Shape.Volume, modeled_volume, abs_tol=1e-7)

        # Invalid property edits report an error on the feature and leave the source untouched.
        invalid = {}
        for prop, bad, good in (("ThreadLength", BLOCK_HEIGHT + 1, 4.5), ("ThreadSize", "M4x0.7", spec),
                                ("ThreadLength", 26.0, 4.5)):
            setattr(thread, prop, bad)
            doc.recompute()
            status = thread.getStatusString()
            invalid[f"{prop}={bad}"] = {"valid": thread.isValid(), "status": status,
                                        "shape_null": thread.Shape.isNull()}
            assert not thread.isValid(), (prop, bad)
            assert source.Shape.exportBrepToString() == original
            setattr(thread, prop, good)
            doc.recompute()
            assert thread.isValid() and math.isclose(thread.Shape.Volume, modeled_volume, abs_tol=1e-7)
        edits["invalid_edits"] = invalid
        doc.save()
        App.closeDocument(doc.Name)
        doc = App.openDocument(str(file))
        thread = doc.getObject(name)
        assert thread.ReverseStart and not thread.LeftHanded and math.isclose(thread.ThreadLength.Value, 4.5)
        assert math.isclose(thread.Shape.Volume, modeled_volume, abs_tol=1e-6)
        edits["second_reopen"] = True
        result["fcstd_reopen_edit"] = edits
        App.closeDocument(doc.Name)

        # Schematic creation through the dialog.
        doc = new_document("StepInternalThreadSchematicQA")
        part, source = import_step(doc, fixture("blind-top-M3x0.5", blind_top(bore)))
        select(doc, part, f"{source.Name}.{bore_face(source.Shape, bore / 2)}")
        before = snapshot(doc, source)
        invoke([dialog(spec, modeled=False)])
        thread, = internal_threads(doc)
        assert_result(doc, part, source, thread, spec, major, pitch, bore, 2.0, False, False, before)
        thread.Modeled = True
        doc.recompute()
        assert thread.isValid() and thread.Shape.Volume < source.Shape.Volume
        thread.ReverseStart = True
        doc.recompute()
        invalid_reverse = thread.getStatusString()
        assert not thread.isValid()
        result["schematic_dialog_then_modeled"] = {"blind_reverse_status": invalid_reverse}
        App.closeDocument(doc.Name)
        checks["cancel_undo_reopen_edit_schematic_transform"] = result

    def check_rejections():
        result = {}

        def reject_case(key, shape_factory, face_picker, handlers, expect_objects_unchanged=True):
            doc = new_document("StepInternalThreadRejectQA")
            part, source = import_step(doc, fixture(key, shape_factory()))
            face = face_picker(source.Shape)
            select(doc, part, f"{source.Name}.{face}")
            before = snapshot(doc, source)
            invoke(handlers)
            after = snapshot(doc, source)
            assert after == before, key
            assert source.ViewObject.Visibility
            assert doc.HasPendingTransaction is False if hasattr(doc, "HasPendingTransaction") else True
            result[key] = {"unchanged": True, "message": messages.get(key)}
            App.closeDocument(doc.Name)

        def cylinder_face(radius):
            return lambda shape: bore_face(shape, radius)

        reject_case("outer-cylinder", lambda: Part.makeCylinder(1.5, 5.0), cylinder_face(1.5),
                    [message("outer-cylinder", "Select an internal hole wall.", "reject-outer-cylinder.png")])
        reject_case("closed-cavity",
                    lambda: block().cut(Part.makeCylinder(1.25, 4.0, App.Vector(5, 5, 2))),
                    cylinder_face(1.25),
                    [message("closed-cavity", "The hole must have an open end.", "reject-closed-cavity.png")])
        reject_case("wrong-bore", lambda: through(2.8), cylinder_face(1.4),
                    [message("wrong-bore", "6H minor diameter range", "reject-wrong-bore.png")])
        reject_case("thin-wall", lambda: through(2.5, center=(1.25 + 0.15, 5)), cylinder_face(1.25),
                    [dialog("M3x0.5", length=3.0),
                     message("thin-wall", "would break through the hole wall", "reject-thin-wall.png")])
        reject_case("too-many-turns", lambda: blind_top(1.6, depth=28.0, height=30.0), cylinder_face(0.8),
                    [dialog("M2x0.4", length=26.0),
                     message("too-many-turns", "limited to 64 turns", "reject-too-many-turns.png")])
        reject_case("chamfered",
                    lambda: through(2.5).cut(Part.makeCone(1.25, 2.25, 1.0, App.Vector(5, 5, 7.0))),
                    cylinder_face(1.25),
                    [message("chamfered", "perpendicular planar faces", "reject-chamfered.png")])

        # Nothing selected.
        doc = new_document("StepInternalThreadNoSelectionQA")
        part, source = import_step(doc, fixture("through-M3x0.5", through(2.5)))
        Gui.Selection.clearSelection()
        before = snapshot(doc, source)
        invoke([message("no-selection", "Select one internal cylindrical hole wall.")])
        assert snapshot(doc, source) == before
        result["no-selection"] = {"unchanged": True, "message": messages["no-selection"]}
        # Schematic thread past the 64-turn limit is allowed because it keeps the original bore.
        App.closeDocument(doc.Name)
        checks["rejections_unchanged_with_messages"] = result

    def check_chinese():
        locales = Gui.supportedLocales()
        name = next(key for key, code in locales.items() if code in ("zh-CN", "zh_CN"))
        previous = Gui.getLocale()
        Gui.setLocale(name)
        Gui.activateWorkbench("PartDesignWorkbench")
        QtTest.QTest.qWait(600)
        Gui.activateWorkbench("PartDesignWorkbench")
        QtTest.QTest.qWait(400)
        info = {"locale": name}
        try:
            doc = new_document("StepInternalThreadZhQA")
            part, source = import_step(doc, fixture("through-M3x0.5", through(2.5)))
            Gui.activeDocument().activeView().fitAll()
            QtTest.QTest.qWait(300)
            button = ribbon_button()
            info["button"] = button.text()
            assert button.text() == "内螺纹", button.text()
            assert window.grab().save(str(shots / "ribbon-zh.png"))
            select(doc, part, f"{source.Name}.{bore_face(source.Shape, 1.25)}")
            before = snapshot(doc, source)
            dlg = {}
            invoke([dialog("M3x0.5", accept=False, info=dlg, shot="dialog-zh.png")])
            info["dialog_title"] = dlg["title"]
            info["dialog_limits"] = dlg["limits_text"]
            assert snapshot(doc, source) == before
            App.closeDocument(doc.Name)
            doc = new_document("StepInternalThreadZhRejectQA")
            part, source = import_step(doc, fixture("outer-cylinder", Part.makeCylinder(1.5, 5.0)))
            select(doc, part, f"{source.Name}.{bore_face(source.Shape, 1.5)}")
            invoke([message("zh-outer-cylinder", "", "reject-zh-outer-cylinder.png")])
            doc2 = new_document("StepInternalThreadZhClosedQA")
            part2, source2 = import_step(doc2, fixture("closed-cavity", Part.makeBox(1, 1, 1)))
            select(doc2, part2, f"{source2.Name}.{bore_face(source2.Shape, 1.25)}")
            invoke([message("zh-closed-cavity", "", "reject-zh-closed-cavity.png")])
            App.closeDocument(doc2.Name)
            App.closeDocument(doc.Name)
            for key in ("zh-outer-cylinder", "zh-closed-cavity"):
                assert any("\u4e00" <= ch <= "\u9fff" for ch in messages[key]), (key, messages[key])
            assert any("\u4e00" <= ch <= "\u9fff" for ch in info["dialog_title"]), info
        finally:
            Gui.setLocale(previous)
            QtTest.QTest.qWait(400)
        checks["chinese_ui"] = info

    def execute():
        steps = (check_ribbon, geometry_cases, check_extra_geometry, check_workflow, check_rejections,
                 check_chinese)
        try:
            for step in steps:
                log("start", name=step.__name__)
                t0 = time.monotonic()
                step()
                report.setdefault("timings_s", {})[step.__name__] = round(time.monotonic() - t0, 2)
                log("done", name=step.__name__)
            report["status"] = "passed"
        except Exception:
            report["error"] = traceback.format_exc()
            log("error", error=report["error"])
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
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    app = args.app.resolve()
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    executable = app / "Contents/MacOS" / info["CFBundleExecutable"]
    output = args.output.resolve()
    output.mkdir(parents=True)
    signature = hashlib.sha256((app / "Contents/_CodeSignature/CodeResources").read_bytes()).hexdigest()

    with tempfile.TemporaryDirectory(prefix="sven-step-thread-qa-") as scratch:
        profile = Path(scratch)
        for name in ("cache", "data", "tmp"):
            (profile / name).mkdir()
        macro = profile / "StepInternalThreadQA.FCMacro"
        macro.write_text(
            f"import sys\nsys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
            "from step_internal_thread_qa import run_macro\nrun_macro()\n",
            encoding="utf-8",
        )
        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8", "HOME": str(profile),
            "TMPDIR": str(profile / "tmp"), "XDG_CACHE_HOME": str(profile / "cache"),
            "FREECAD_USER_HOME": str(profile), "FREECAD_USER_DATA": str(profile / "data"),
            "FREECAD_USER_TEMP": str(profile / "tmp"), "PYTHONDONTWRITEBYTECODE": "1",
            OUTPUT_ENV: str(output),
        }
        started = time.monotonic()
        with (output / "freecad.log").open("wb") as log:
            try:
                process = subprocess.run(
                    [str(executable), "--user-cfg", str(profile / "user.cfg"),
                     "--system-cfg", str(profile / "system.cfg"), str(macro)],
                    env=env, stdout=log, stderr=subprocess.STDOUT, timeout=args.timeout, check=False,
                )
                code = process.returncode
            except subprocess.TimeoutExpired:
                code = "timeout"
    report_file = output / "report.json"
    report = json.loads(report_file.read_text()) if report_file.exists() else {"status": "failed"}
    report.update({"app": str(app), "signed_resource_sha256": signature, "exit": code,
                   "elapsed_s": round(time.monotonic() - started, 1)})
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if report["status"] != "passed" or code:
        print(report.get("error", f"App exited {code}. See {output / 'freecad.log'}."))
        return 1
    print(f"Passed: {report_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
