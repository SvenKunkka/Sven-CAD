"""Verify Sven CAD thread callouts, the drawing BOM and the PDF, DXF and CSV export on a Mac bundle."""

import argparse
import csv
import hashlib
import html
import json
import math
import os
from pathlib import Path
import plistlib
import re
import subprocess
import tempfile
import time
import traceback


OUTPUT_ENV = "PERSONAL_CAD_DRAWING_QA"
DEV_ENV = "PERSONAL_CAD_DRAWING_QA_DEV_PATH"
CALLOUTS = "PersonalCAD_ThreadCallouts"
BOM = "PersonalCAD_DrawingBom"
EXPORT = "PersonalCAD_ExportDrawing"
PLATE = (40.0, 24.0, 8.0)
BORES = ((8.0, 8.0, None), (32.0, 8.0, None), (20.0, 16.0, 6.0))   # x, y, blind depth
BORE_DIAMETER = 2.5
PDFTOTEXT = "/opt/homebrew/bin/pdftotext"


def run_macro():
    import sys
    import FreeCAD as App
    import FreeCADGui as Gui
    import Part
    from PySide6 import QtCore, QtTest, QtWidgets

    dev_path = os.environ.get(DEV_ENV)
    if dev_path:
        for path in dev_path.split(os.pathsep):
            sys.path.insert(0, path)
        for name in ("SvenDrawing", "SvenDrawingGui", "SvenFasteners"):
            sys.modules.pop(name, None)
    import SvenDrawing as D
    import SvenFasteners as F

    output = Path(os.environ[OUTPUT_ENV])
    shots = output / "screenshots"
    shots.mkdir(exist_ok=True)
    exports = output / "exports"
    exports.mkdir(exist_ok=True)
    report = {"status": "failed", "version": list(App.Version()), "checks": {}, "messages": {},
              "drawing_module": D.__file__, "dev_module_path": dev_path, "exports": {}}
    checks = report["checks"]
    messages = report["messages"]
    window = Gui.getMainWindow()
    started = time.monotonic()

    def log(step, **data):
        with (output / "progress.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"t": round(time.monotonic() - started, 3), "step": step, **data},
                                    ensure_ascii=False, default=str) + "\n")

    def require(condition, message):
        if not condition:
            raise AssertionError(message)

    # ---------- GUI harness ----------
    def toolbar_button(name):
        bar = next((b for b in window.findChildren(QtWidgets.QToolBar)
                    if b.objectName() == "Sven Drawing" and b.isVisible()), None)
        require(bar is not None, "Sven Drawing toolbar missing")
        action = next((a for a in bar.actions() if a.objectName() == name), None)
        require(action is not None, f"{name} not on the Sven Drawing toolbar: {[a.objectName() for a in bar.actions()]}")
        button = bar.widgetForAction(action)
        require(button is not None and button.isVisible() and button.isEnabled(), f"{name} button unavailable")
        return button, action

    def invoke(trigger, handlers, timeout_s=180):
        pending = list(handlers)
        errors = []
        state = {"last": None}
        deadline = time.monotonic() + timeout_s

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
        trigger()
        QtTest.QTest.qWait(150)
        timer.stop()
        require(not errors, errors)
        require(not pending, f"{len(pending)} expected modal window(s) never appeared")

    def click(name):
        return lambda: QtTest.QTest.mouseClick(toolbar_button(name)[0], QtCore.Qt.MouseButton.LeftButton)

    def message(key, expected, shot=None):
        def handle(modal):
            require(isinstance(modal, QtWidgets.QMessageBox), modal.metaObject().className())
            messages[key] = modal.text()
            require(expected in modal.text(), (expected, modal.text()))
            if shot:
                require(modal.grab().save(str(shots / shot)), "screenshot")
            modal.accept()
        return handle

    def save_as(path):
        def handle(modal):
            require(isinstance(modal, QtWidgets.QFileDialog) and modal.objectName() == "SvenDrawingExportDialog",
                    modal.objectName())
            # The dialog changes folder asynchronously; set the folder first and
            # accept only once the name field shows the requested file.
            modal.setDirectory(str(path.parent))
            name_field = modal.findChild(QtWidgets.QLineEdit, "fileNameEdit")
            require(name_field is not None, "file dialog has no name field")
            for _ in range(100):
                modal.selectFile(path.name)
                if name_field.text() != path.name:
                    name_field.setText(path.name)
                QtTest.QTest.qWait(50)
                if (Path(modal.directory().absolutePath()).resolve() == path.parent.resolve()
                        and name_field.text() == path.name
                        and [Path(f).resolve() for f in modal.selectedFiles()] == [path.resolve()]):
                    break
            else:
                raise AssertionError(("file dialog did not take", str(path), modal.directory().absolutePath(),
                                      name_field.text() if name_field else None, modal.selectedFiles()))
            modal.accept()
        return handle

    def select(doc, *objs):
        Gui.Selection.clearSelection()
        for obj in objs:
            Gui.Selection.addSelection(doc.Name, obj.Name)
        QtTest.QTest.qWait(60)

    def wait_views(page, timeout=240):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            busy = [v.Name for v in page.getAllViews()
                    if v.isDerivedFrom("TechDraw::DrawViewPart") and v.waitingForResult()]
            if not busy:
                return round(time.monotonic() - t0, 3)
            QtTest.QTest.qWait(50)
        raise AssertionError(f"Views still computing after {timeout} s: {busy}")

    def recompute(doc, page):
        t0 = time.monotonic()
        doc.recompute()
        wait_views(page)
        doc.recompute()
        wait_views(page)
        # Let the BOM table refresh that follows a recompute run, as the event loop would.
        QtTest.QTest.qWait(100)
        wait_views(page)
        return round(time.monotonic() - t0, 3)

    def page_shot(name):
        QtTest.QTest.qWait(300)
        area = window.findChild(QtWidgets.QMdiArea)
        sub = area.activeSubWindow() if area else None
        widget = sub.widget() if sub else window
        require(widget.grab().save(str(shots / name)), "screenshot")

    # ---------- fixture ----------
    def plate_step():
        path = output / "housing.step"
        shape = Part.makeBox(*PLATE)
        for x, y, depth in BORES:
            if depth is None:
                shape = shape.cut(Part.makeCylinder(BORE_DIAMETER / 2, PLATE[2] + 2, App.Vector(x, y, -1)))
            else:
                shape = shape.cut(Part.makeCylinder(BORE_DIAMETER / 2, depth + 1,
                                                    App.Vector(x, y, PLATE[2] - depth)))
        shape.exportStep(str(path))
        return path

    def bore_face(feature, x, y):
        for i, face in enumerate(feature.Shape.Faces, 1):
            surf = face.Surface
            if isinstance(surf, Part.Cylinder) and abs(surf.Radius - BORE_DIAMETER / 2) < 1e-6:
                local = feature.Placement.inverse().multVec(surf.Center)
                if abs(local.x - x) < 1e-6 and abs(local.y - y) < 1e-6:
                    return f"Face{i}"
        raise AssertionError(f"No bore at {x},{y} on {feature.Name}")

    def build_fixture(doc):
        product = doc.addObject("Assembly::AssemblyObject", "Product")
        housing = doc.addObject("App::Part", "Housing")
        product.addObject(housing)
        housing.Placement = App.Placement(App.Vector(0, 0, 0), App.Rotation())
        plate = doc.addObject("Part::Feature", "Plate")
        plate.Shape = Part.read(str(plate_step()))
        housing.addObject(plate)
        doc.recompute()
        taps = []
        support = plate
        for (x, y, _), length in zip(BORES, (6.0, 5.0, 4.0)):
            tap = doc.addObject("PartDesign::InternalThread", "Tap")
            housing.addObject(tap)
            tap.Support = (support, [bore_face(support, x, y)])
            tap.ThreadSize = "M3x0.5"
            tap.ThreadLength = length
            tap.Modeled = False
            doc.recompute()
            require(tap.isValid(), f"{tap.Name} invalid")
            support.Visibility = False
            taps.append(tap)
            support = tap

        bracket = doc.addObject("PartDesign::Body", "Bracket")
        product.addObject(bracket)
        bracket.Placement = App.Placement(App.Vector(0, -30, 0), App.Rotation())
        sketch = bracket.newObject("Sketcher::SketchObject", "BracketSketch")
        sketch.AttachmentSupport = [(bracket.Origin.OriginFeatures[3], "")]
        sketch.MapMode = "FlatFace"
        pts = [App.Vector(0, 0, 0), App.Vector(24, 0, 0), App.Vector(24, 14, 0), App.Vector(0, 14, 0)]
        for i in range(4):
            sketch.addGeometry(Part.LineSegment(pts[i], pts[(i + 1) % 4]))
        pad = bracket.newObject("PartDesign::Pad", "Pad")
        pad.Profile = sketch
        pad.Length = 8
        doc.recompute()
        top_face = next(f"Face{i}" for i, f in enumerate(pad.Shape.Faces, 1)
                        if abs(f.CenterOfMass.z - 8) < 1e-6 and abs(f.normalAt(0, 0).z - 1) < 1e-6)
        hole_sketch = bracket.newObject("Sketcher::SketchObject", "HoleSketch")
        hole_sketch.AttachmentSupport = [(pad, top_face)]
        hole_sketch.MapMode = "FlatFace"
        doc.recompute()
        inverse = hole_sketch.Placement.inverse()
        for x in (6.0, 18.0):
            local = inverse.multVec(App.Vector(x, 7, 8))
            hole_sketch.addGeometry(Part.Circle(App.Vector(local.x, local.y, 0), App.Vector(0, 0, 1), 1.5))
        doc.recompute()
        hole = bracket.newObject("PartDesign::Hole", "Hole")
        hole.Profile = hole_sketch
        hole.Threaded = True
        hole.ThreadType = "ISOMetricProfile"
        hole.ThreadSize = "M3x0.5"
        hole.ThreadClass = "6H"
        hole.DepthType = "Dimension"
        hole.Depth = 6
        hole.ThreadDepthType = "Dimension"
        hole.ThreadDepth = 5
        doc.recompute()
        require(hole.isValid() and len(hole.ThreadFrames) == 2, "native hole")

        shaft_part = doc.addObject("App::Part", "ShaftPart")
        product.addObject(shaft_part)
        shaft_part.Placement = App.Placement(App.Vector(56, 12, 0), App.Rotation())
        shaft = doc.addObject("Part::Feature", "Shaft")
        shaft.Shape = Part.makeCylinder(2, 12)
        shaft_part.addObject(shaft)
        doc.recompute()
        face = next(f"Face{i}" for i, f in enumerate(shaft.Shape.Faces, 1) if isinstance(f.Surface, Part.Cylinder))
        ext = doc.addObject("PartDesign::ExternalThread", "ShaftThread")
        shaft_part.addObject(ext)
        ext.Support = (shaft, [face])
        ext.ThreadSize = "M4x0.7"
        ext.ThreadLength = 8
        ext.ManufacturingClass = "6g"
        ext.Modeled = False
        doc.recompute()
        require(ext.isValid(), "external thread")
        shaft.Visibility = False

        screws = F.insert(doc, F.find("ISO4762-M3x8"), F.default_placements(F.find("ISO4762-M3x8"), 3), product)[0]
        nuts = F.insert(doc, F.find("ISO4032-M3"), F.default_placements(F.find("ISO4032-M3"), 1), product)[0]
        nuts[0].ElementCount = 2
        doc.recompute()
        return {"product": product, "housing": housing, "taps": taps, "bracket": bracket, "hole": hole,
                "shaft_part": shaft_part, "ext": ext, "screws": screws, "nuts": nuts}

    def make_page(doc, fx):
        page = doc.addObject("TechDraw::DrawPage", "Drawing")
        template = doc.addObject("TechDraw::DrawSVGTemplate", "Template")
        template.Template = App.getResourceDir() + "Mod/TechDraw/Templates/ISO/A3_Landscape_ISO5457_minimal.svg"
        page.Template = template
        sources = [fx["housing"], fx["bracket"], fx["shaft_part"]]
        views = {}
        for name, direction, x, y in (("Top", App.Vector(0, 0, 1), 130, 200), ("Front", App.Vector(0, -1, 0), 130, 80)):
            view = doc.addObject("TechDraw::DrawViewPart", name)
            page.addView(view)
            view.Source = sources
            view.Direction = direction
            view.XDirection = App.Vector(1, 0, 0)
            view.Scale = 2.0
            view.ScaleType = "Custom"
            view.X = x
            view.Y = y
            views[name] = view
        empty = doc.addObject("TechDraw::DrawViewPart", "FastenerOnly")
        page.addView(empty)
        empty.Source = [fx["screws"][0]]
        empty.Direction = App.Vector(0, 0, 1)
        empty.Scale = 2.0
        empty.ScaleType = "Custom"
        empty.X = 330
        empty.Y = 60
        doc.recompute()
        page.ViewObject.doubleClicked()
        wait_views(page)
        doc.recompute()
        wait_views(page)
        return page, views, empty

    # ---------- independent expectations ----------
    def expected_threads(fx):
        found = []
        for tap in fx["taps"]:
            found.append((tap, 0, f"M3x0.5-6H DEPTH {tap.ThreadLength.Value:g}", "internal",
                          tap.MajorDiameter.Value / 2, tap.ThreadLength.Value))
        hole = fx["hole"]
        for i in range(len(hole.ThreadFrames)):
            found.append((hole, i, f"{hole.ThreadSize}-{hole.ThreadClass} DEPTH {hole.ThreadDepth.Value:g}",
                          "internal", hole.ThreadDiameter.Value / 2, hole.ThreadDepth.Value))
        ext = fx["ext"]
        cls = "" if ext.ManufacturingClass == "Unspecified" else "-" + ext.ManufacturingClass
        found.append((ext, 0, f"{ext.ThreadSize}{cls} LENGTH {ext.ThreadLength.Value:g}", "external",
                      ext.MinorDiameter.Value / 2, ext.ThreadLength.Value))
        return found

    def hole_start(fx, index):
        """Hole opening from the sketch circle, independent of ThreadFrames."""
        hole = fx["hole"]
        circle = [g for g in fx["hole"].Profile[0].Geometry if isinstance(g, Part.Circle)][index]
        return fx["bracket"].Placement.multiply(hole.Profile[0].Placement).multVec(circle.Center)

    def thread_start(fx, feature, index):
        if feature.TypeId == "PartDesign::Hole":
            return hole_start(fx, index), App.Vector(0, 0, -1)
        if feature.TypeId == "PartDesign::ExternalThread":
            base = fx["shaft_part"].Placement.multVec(App.Vector(0, 0, 0))
            return base, App.Vector(0, 0, 1)
        x, y, depth = BORES[fx["taps"].index(feature)]
        if depth is None:
            return App.Vector(x, y, 0), App.Vector(0, 0, 1)
        return App.Vector(x, y, PLATE[2]), App.Vector(0, 0, -1)

    def edge_record(view, tag):
        edge = view.getCosmeticEdge(tag)
        record = {"start": edge.Start, "end": edge.End, "center": None, "radius": None}
        try:
            record["center"], record["radius"] = edge.Center, edge.Radius
        except TypeError:
            pass
        return record

    def check_callouts(fx, views, expectation, modeled=False):
        result = {}
        expected = expectation
        for name, view in views.items():
            center = view.getOriginalCentroid()
            calls = D.callouts(view)
            require(len(calls) == len(expected), (name, len(calls), len(expected)))
            rows = []
            for feature, index, text, kind, radius, length in expected:
                call = next((c for c in calls if c.ThreadFeature == feature and c.HoleIndex == index), None)
                require(call is not None, (name, feature.Name, index))
                require(list(call.Text) == [text], (name, feature.Name, list(call.Text), text))
                start, axis = thread_start(fx, feature, index)
                anchor = view.projectPoint(start - center)
                mode = "end" if name == "Top" else "side"
                require(call.Representation == mode, (name, feature.Name, call.Representation))
                edges = [edge_record(view, t) for t in call.CosmeticTags]
                # The leader starts on the thread axis at the opening.
                leader = edges[-2]
                lead_ok = min((leader["start"] - App.Vector(anchor.x, s * anchor.y, 0)).Length for s in (1, -1))
                require(lead_ok < 1e-6, (name, feature.Name, "leader", leader["start"], anchor))
                thread_edges = edges[:-2]
                if modeled:
                    require(not thread_edges, (name, feature.Name, "modeled thread drew cosmetic lines"))
                elif mode == "end":
                    require(len(thread_edges) == 1, (name, feature.Name, len(thread_edges)))
                    arc = thread_edges[0]
                    require(abs(arc["radius"] - radius) < 1e-6, (name, feature.Name, arc["radius"], radius))
                    off = min((arc["center"] - App.Vector(anchor.x, s * anchor.y, 0)).Length for s in (1, -1))
                    require(off < 1e-6, (name, feature.Name, "arc centre", arc["center"], anchor))
                else:
                    require(len(thread_edges) == 3, (name, feature.Name, len(thread_edges)))
                    for line in thread_edges[:2]:
                        span = (line["end"] - line["start"]).Length
                        require(abs(span - length) < 1e-6, (name, feature.Name, span, length))
                    gap = (thread_edges[0]["start"] - thread_edges[1]["start"]).Length
                    require(abs(gap - 2 * radius) < 1e-6, (name, feature.Name, gap, 2 * radius))
                page_x, page_y = call.X.Value, call.Y.Value
                require(0 < page_x < 420 and 0 < page_y < 297, (name, feature.Name, page_x, page_y))
                rows.append({"feature": feature.Name, "index": index, "text": text, "mode": call.Representation,
                             "edges": len(edges), "page_xy": [round(page_x, 3), round(page_y, 3)]})
            result[name] = rows
        return result

    def expected_bom(fx):
        counts = {r["part_number"]: r["count"] for r in F.summary(fx["product"].Document)}
        return counts

    def check_bom(page, fx):
        bom = D.page_bom(page)
        require(bom is not None, "page BOM")
        headings, rows = D.bom_table(bom)
        cells = D.bom_cells(page)
        require(len(cells) == len(D.BOM_COLUMNS) * (len(rows) + 1), (len(cells), len(rows)))
        require(not D.bom_needs_layout(page), "BOM table out of step with the BOM")
        grid = D.bom_grid(page)
        require(grid == [headings] + rows, (grid, headings, rows))
        # Cells of a row share a baseline, cells of a column share a centre, rows go down the page.
        by_row = {}
        for c in cells:
            by_row.setdefault(c.Row, []).append(c)
        for r, row_cells in by_row.items():
            require(len({round(c.Y.Value, 6) for c in row_cells}) == 1, ("row baseline", r))
        for col in bom.columnsNames:
            col_cells = sorted((c for c in cells if c.Column == col), key=lambda c: c.Row)
            require(len({round(c.X.Value, 6) for c in col_cells}) == 1, ("column centre", col))
            ys = [c.Y.Value for c in col_cells]
            require(all(a > b for a, b in zip(ys, ys[1:])), ("rows order", col, ys))
        pn = headings.index("Part Number")
        qty = headings.index("Qty")
        fastener_rows = {r[pn]: int(r[qty]) for r in rows if r[pn]}
        require(fastener_rows == expected_bom(fx), (fastener_rows, expected_bom(fx)))
        names = {r[headings.index("Name")] for r in rows if not r[pn]}
        require(names == {"Housing", "Bracket", "ShaftPart"}, names)
        spec = headings.index("Specification")
        for r in rows:
            if r[pn]:
                require(r[spec] == F.specification(F.find(r[pn])), r)
                require(r[headings.index("Company P/N")] == "", r)
        return {"headings": headings, "rows": rows}

    def check_layout(page, tag):
        """No callout or BOM text covers another, and all of it stays inside the frame."""
        width, height = D._page_size(page)
        texts = [v for v in page.Views if (D.is_callout(v) or D.is_bom_cell(v)) and any(list(v.Text))]
        boxes = {v.Name: D.text_box(App.Vector(v.X.Value, v.Y.Value, 0), list(v.Text), v.TextSize.Value)
                 for v in texts}
        names = sorted(boxes)
        clashes = [(a, b) for i, a in enumerate(names) for b in names[i + 1:]
                   if D._overlap(boxes[a], boxes[b], gap=0.0)]
        outside = [n for n, b in boxes.items() if b[0] < 10 or b[1] < 10 or b[2] > width - 10 or b[3] > height - 10]
        require(not clashes and not outside, (tag, "text layout", clashes, outside))
        return {"texts": len(boxes), "clashes": clashes, "outside": outside}

    def check_no_stale_edges(doc, page, tag):
        """The drawn edges equal those of a view rebuilt from scratch: no leftover callout lines."""
        views_ = [v for v in page.getAllViews() if v.isDerivedFrom("TechDraw::DrawViewPart")]
        before = {v.Name: len(v.getVisibleEdges()) for v in views_}
        for v in views_:
            v.touch()
        doc.recompute()
        wait_views(page)
        QtTest.QTest.qWait(100)
        after = {v.Name: len(v.getVisibleEdges()) for v in views_}
        require(before == after, (tag, "stale cosmetic edges", before, after))
        return after

    def export(doc, page, tag):
        pdf = exports / f"{tag}.pdf"
        checks.setdefault("edges_before_export", {})[tag] = check_no_stale_edges(doc, page, tag)
        checks.setdefault("layout", {})[tag] = check_layout(page, tag)
        select(doc, page)
        result = {}

        def read(modal):
            require(isinstance(modal, QtWidgets.QMessageBox) and modal.objectName() == "SvenDrawingExportResult",
                    modal.objectName())
            result["text"] = modal.text()
            modal.accept()

        t0 = time.monotonic()
        invoke(click(EXPORT), [save_as(pdf), read])
        written = result["text"].split("\n")
        require(written == [str(exports / f"{tag}.{ext}") for ext in ("pdf", "dxf", "csv")], written)
        for path in written:
            require(Path(path).stat().st_size > 0, path)
        return {"seconds": round(time.monotonic() - t0, 3), "files": written}

    def expected_texts(views, page):
        callouts = sorted({c.Text[0] for v in views.values() for c in D.callouts(v)})
        headings, rows = D.bom_table(D.page_bom(page))
        width, height = D._page_size(page)
        placed = [{"text": v.Text[0], "x": v.X.Value, "y": v.Y.Value} for v in page.Views
                  if (D.is_callout(v) or D.is_bom_cell(v)) and list(v.Text) and v.Text[0]]
        return {"callouts": callouts, "bom": [headings] + rows, "placed": placed, "page_mm": [width, height]}

    # ---------- scenario ----------
    def execute():
        try:
            log("start")
            doc = App.newDocument("DrawingQA")
            doc.UndoMode = 1
            fx = build_fixture(doc)
            page, views, empty = make_page(doc, fx)
            Gui.activateWorkbench("TechDrawWorkbench")
            QtTest.QTest.qWait(500)
            log("fixture")
            locale = Gui.getLocale()
            labels = {name: toolbar_button(name)[1].text() for name in (CALLOUTS, BOM, EXPORT)}
            checks["toolbar"] = {"locale": locale, "labels": labels}
            if locale == "Chinese (Simplified)":
                require(labels == {CALLOUTS: "螺纹标注", BOM: "BOM 表", EXPORT: "导出图纸"}, labels)
            window.grab().save(str(shots / "techdraw-toolbar.png"))

            # Rejections leave the document unchanged.
            before = (len(doc.Objects), doc.UndoCount)
            Gui.Selection.clearSelection()
            invoke(click(CALLOUTS), [message("no_view", "")])
            select(doc, empty)
            invoke(click(CALLOUTS), [message("no_threads", "")])
            require((len(doc.Objects), doc.UndoCount) == before, "rejection changed the document")

            # Callouts for both views in one transaction.
            select(doc, views["Top"], views["Front"])
            t0 = time.monotonic()
            invoke(click(CALLOUTS), [])
            callout_seconds = round(time.monotonic() - t0, 3)
            require(doc.UndoCount == before[1] + 1, "callouts not one transaction")
            wait_views(page)
            expectation = expected_threads(fx)
            checks["callouts_schematic"] = check_callouts(fx, views, expectation)
            checks["callouts_schematic"]["seconds"] = callout_seconds
            page_shot("page-callouts.png")
            edges_before = {n: len(v.CosmeticEdges) for n, v in views.items()}
            doc.undo()
            require(not D.callouts(views["Top"]) and all(len(v.CosmeticEdges) == 0 for v in views.values()),
                    "undo left callouts or cosmetic edges")
            doc.redo()
            recompute(doc, page)
            require({n: len(v.CosmeticEdges) for n, v in views.items()} == edges_before, "redo")
            check_callouts(fx, views, expectation)
            select(doc, views["Top"])
            invoke(click(CALLOUTS), [message("already", "", None)])
            checks["undo_redo_and_repeat"] = {"edges": edges_before}
            log("callouts")

            # BOM table and first export.
            select(doc, page)
            invoke(click(BOM), [])
            recompute(doc, page)
            checks["bom_initial"] = check_bom(page, fx)
            page_shot("page-callouts-bom.png")
            report["exports"]["initial"] = {**export(doc, page, "initial"), **expected_texts(views, page)}
            log("export initial")

            # Change sizes and lengths, then recompute the same page.
            fx["hole"].ThreadSize = "M4x0.7"
            fx["taps"][0].ThreadLength = 4
            fx["ext"].ThreadLength = 6
            F.replace([fx["screws"][0]], F.find("ISO4762-M3x10"))
            t0 = time.monotonic()
            change_seconds = recompute(doc, page)
            require(all(o.isValid() for o in doc.Objects), [o.Name for o in doc.Objects if not o.isValid()])
            expectation = expected_threads(fx)
            require(any("M4x0.7-6H DEPTH 5" == e[2] for e in expectation), expectation)
            checks["callouts_changed"] = check_callouts(fx, views, expectation)
            checks["bom_changed"] = check_bom(page, fx)
            require({r[2]: r[5] for r in checks["bom_changed"]["rows"] if r[2]}
                    == {"ISO4762-M3x10": "1", "ISO4762-M3x8": "2", "ISO4032-M3": "2"}, checks["bom_changed"])
            checks["changed_recompute_seconds"] = change_seconds
            page_shot("page-changed.png")
            report["exports"]["changed"] = {**export(doc, page, "changed"), **expected_texts(views, page)}
            log("export changed")

            # Modeled threads project as real geometry and keep the same callouts.
            for tap in fx["taps"]:
                tap.Modeled = True
            fx["ext"].Modeled = True
            fx["hole"].ModelThread = True
            modeled_seconds = recompute(doc, page)
            require(all(o.isValid() for o in doc.Objects), [o.Name for o in doc.Objects if not o.isValid()])
            checks["callouts_modeled"] = check_callouts(fx, views, expected_threads(fx), modeled=True)
            edge_counts = {n: len(v.getVisibleEdges()) for n, v in views.items()}
            checks["modeled"] = {"recompute_seconds": modeled_seconds,
                                 "schematic_recompute_seconds": change_seconds, "visible_edges": edge_counts}
            require(modeled_seconds < 120, f"Modeled page took {modeled_seconds} s")
            page_shot("page-modeled.png")
            report["exports"]["modeled"] = {**export(doc, page, "modeled"), **expected_texts(views, page)}
            log("export modeled")

            # Save, reopen and recompute without drift.
            path = output / "drawing.FCStd"
            doc.saveAs(str(path))
            snapshot = {c.Name: (list(c.Text), list(c.CosmeticTags), c.Signature)
                        for v in views.values() for c in D.callouts(v)}
            rows = list(D.page_bom(page).BomRows)
            App.closeDocument(doc.Name)
            doc = App.openDocument(str(path))
            page = doc.getObject("Drawing")
            page.ViewObject.doubleClicked()
            views = {n: doc.getObject(n) for n in ("Top", "Front")}
            wait_views(page)
            reopened = {c.Name: (list(c.Text), list(c.CosmeticTags), c.Signature)
                        for v in views.values() for c in D.callouts(v)}
            require(reopened == snapshot, ("callouts changed on reopen",
                    {k: (snapshot.get(k), reopened.get(k)) for k in set(snapshot) | set(reopened)
                     if snapshot.get(k) != reopened.get(k)}))
            require(all(D._has_edge(c.SourceView, t) for v in views.values() for c in D.callouts(v)
                        for t in c.CosmeticTags), "cosmetic edges lost on reopen")
            require(list(D.page_bom(page).BomRows) == rows, "BOM changed on reopen")
            recompute(doc, page)
            again = {c.Name: (list(c.Text), list(c.CosmeticTags), c.Signature)
                     for v in views.values() for c in D.callouts(v)}
            require(again == snapshot, "recompute after reopen changed callouts")
            require(all(o.isValid() for o in doc.Objects), [o.Name for o in doc.Objects if not o.isValid()])
            touched = [o.Name for o in doc.Objects if "Touched" in o.State]
            require(not touched, touched)
            checks["save_reopen"] = {"callouts": len(snapshot), "bom_rows": len(rows) // len(D.BOM_COLUMNS)}
            App.closeDocument(doc.Name)

            # BOM needs an assembly.
            other = App.newDocument("NoAssemblyQA")
            other_page = other.addObject("TechDraw::DrawPage", "Sheet")
            tpl = other.addObject("TechDraw::DrawSVGTemplate", "Template")
            tpl.Template = App.getResourceDir() + "Mod/TechDraw/Templates/ISO/A3_Landscape_ISO5457_minimal.svg"
            other_page.Template = tpl
            other.recompute()
            select(other, other_page)
            invoke(click(BOM), [message("no_assembly", "")])
            App.closeDocument(other.Name)
            report["status"] = "passed"
        except Exception:
            report["error"] = traceback.format_exc()
            log("error", error=report["error"])
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
        if report["status"] == "passed":
            Gui.runCommand("Std_Quit", 0)
        else:
            os._exit(1)

    QtCore.QTimer.singleShot(500, execute)


# ---------------------------------------------------------------- file checks (outside the app)

def dxf_entities(path):
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    pairs = [(lines[i].strip(), lines[i + 1]) for i in range(0, len(lines) - 1, 2)]
    entities = []
    current = None
    in_entities = False
    for code, value in pairs:
        if code == "2" and value.strip() == "ENTITIES":
            in_entities = True
            continue
        if not in_entities:
            continue
        if code == "0":
            if current:
                entities.append(current)
            if value.strip() == "ENDSEC":
                break
            current = {"type": value.strip(), "layer": None, "text": None, "x": None, "y": None}
        elif current is not None and code == "10" and current["x"] is None:
            current["x"] = float(value)
        elif current is not None and code == "20" and current["y"] is None:
            current["y"] = float(value)
        elif current is not None and code == "8":
            current["layer"] = value.strip()
        elif current is not None and code == "1":
            current["text"] = value
    return entities


def pdf_words(path):
    """Words of the PDF with their boxes in page millimetres, Y up."""
    out = subprocess.run([PDFTOTEXT, "-bbox", path, "-"], capture_output=True, text=True, check=True).stdout
    page = re.search(r'<page width="([\d.]+)" height="([\d.]+)"', out)
    width_pt, height_pt = float(page.group(1)), float(page.group(2))
    words = []
    for m in re.finditer(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">([^<]*)</word>', out):
        x0, y0, x1, y1 = (float(m.group(i)) for i in range(1, 5))
        words.append({"text": html.unescape(m.group(5)), "box": (x0, y0, x1, y1)})
    return words, width_pt, height_pt


def locate(words, text, width_pt, height_pt, page_mm):
    """Centres, in page mm, of every run of PDF words that spells the text."""
    tokens = text.split()
    sx, sy = page_mm[0] / width_pt, page_mm[1] / height_pt
    found = []
    for i in range(len(words) - len(tokens) + 1):
        run = words[i:i + len(tokens)]
        if [w["text"] for w in run] != tokens:
            continue
        x0 = min(w["box"][0] for w in run)
        x1 = max(w["box"][2] for w in run)
        y0 = min(w["box"][1] for w in run)
        y1 = max(w["box"][3] for w in run)
        found.append(((x0 + x1) / 2 * sx, page_mm[1] - (y0 + y1) / 2 * sy))
    return found


def check_positions(info, pdf, entities, tolerance=1.5):
    """Each callout and BOM cell sits at its page position in the PDF and in the DXF."""
    words, width_pt, height_pt = pdf_words(pdf)
    dxf = [e for e in entities if e["type"] == "TEXT" and e["text"]]
    worst_pdf = worst_dxf = 0.0
    misplaced = []
    for item in info["placed"]:
        spots = locate(words, item["text"], width_pt, height_pt, info["page_mm"])
        d_pdf = min((math.hypot(x - item["x"], y - item["y"]) for x, y in spots), default=float("inf"))
        d_dxf = min((math.hypot(e["x"] - item["x"], e["y"] - item["y"]) for e in dxf if e["text"] == item["text"]),
                    default=float("inf"))
        worst_pdf, worst_dxf = max(worst_pdf, d_pdf), max(worst_dxf, d_dxf)
        if d_pdf > tolerance or d_dxf > 1e-6:
            misplaced.append({**item, "pdf_mm": round(d_pdf, 3), "dxf_mm": round(d_dxf, 6)})
    return {"checked": len(info["placed"]), "worst_pdf_mm": round(worst_pdf, 3),
            "worst_dxf_mm": round(worst_dxf, 6), "misplaced": misplaced}


def normalize(text):
    return re.sub(r"\s+", " ", text).strip()


def check_exports(report):
    results = {}
    for tag, info in report.get("exports", {}).items():
        pdf, dxf, csv_path = info["files"]
        entities = dxf_entities(dxf)
        dxf_texts = [e["text"] for e in entities if e["type"] in ("TEXT", "MTEXT") and e["text"]]
        layers = {}
        for e in entities:
            layers.setdefault(e["layer"], {}).setdefault(e["type"], 0)
            layers[e["layer"]][e["type"]] += 1
        pdf_text = subprocess.run([PDFTOTEXT, "-layout", pdf, "-"], capture_output=True, text=True,
                                  check=True).stdout
        flat_pdf = normalize(pdf_text)
        with open(csv_path, encoding="utf-8", newline="") as stream:
            csv_rows = list(csv.reader(stream))
        expected_cells = [c for row in info["bom"] for c in row if c]
        missing_dxf = [t for t in info["callouts"] if dxf_texts.count(t) < 1]
        missing_dxf_cells = [c for c in expected_cells if c not in dxf_texts]
        missing_pdf = [t for t in info["callouts"] if t not in flat_pdf]
        missing_pdf_cells = [c for c in expected_cells if c not in flat_pdf]
        callout_counts = {t: dxf_texts.count(t) for t in info["callouts"]}
        results[tag] = {
            "dxf_texts": len(dxf_texts), "layers": layers, "callout_counts_in_dxf": callout_counts,
            "csv_matches_bom": csv_rows == info["bom"], "missing_in_dxf": missing_dxf + missing_dxf_cells,
            "missing_in_pdf": missing_pdf + missing_pdf_cells, "pdf_bytes": Path(pdf).stat().st_size,
            "dxf_bytes": Path(dxf).stat().st_size,
        }
        results[tag]["positions"] = check_positions(info, pdf, entities)
        ok = csv_rows == info["bom"] and not (missing_dxf or missing_dxf_cells or missing_pdf or missing_pdf_cells) \
            and not results[tag]["positions"]["misplaced"]
        results[tag]["passed"] = ok
    # Stale text from before the change must be gone from the changed export.
    if "initial" in report.get("exports", {}) and "changed" in report["exports"]:
        stale = sorted(set(report["exports"]["initial"]["callouts"]) - set(report["exports"]["changed"]["callouts"]))
        changed_pdf = normalize(subprocess.run([PDFTOTEXT, "-layout", report["exports"]["changed"]["files"][0], "-"],
                                               capture_output=True, text=True, check=True).stdout)
        changed_dxf = [e["text"] for e in dxf_entities(report["exports"]["changed"]["files"][1]) if e["text"]]
        results["stale_texts"] = {"texts": stale,
                                  "in_pdf": [t for t in stale if t in changed_pdf],
                                  "in_dxf": [t for t in stale if t in changed_dxf]}
        results["stale_texts"]["passed"] = bool(stale) and not results["stale_texts"]["in_pdf"] \
            and not results["stale_texts"]["in_dxf"]
    return results


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
    with tempfile.TemporaryDirectory(prefix="sven-drawing-qa-") as scratch:
        profile = Path(scratch)
        for name in ("cache", "data", "tmp"):
            (profile / name).mkdir()
        macro = profile / "DrawingQA.FCMacro"
        macro.write_text(
            f"import sys\nsys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
            "from drawing_thread_bom_qa import run_macro\nrun_macro()\n", encoding="utf-8")
        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8", "HOME": str(profile),
            "TMPDIR": str(profile / "tmp"), "XDG_CACHE_HOME": str(profile / "cache"),
            "FREECAD_USER_HOME": str(profile), "FREECAD_USER_DATA": str(profile / "data"),
            "FREECAD_USER_TEMP": str(profile / "tmp"), "PYTHONDONTWRITEBYTECODE": "1", OUTPUT_ENV: str(output),
        }
        if os.environ.get(DEV_ENV):
            env[DEV_ENV] = os.environ[DEV_ENV]
        started = time.monotonic()
        with (output / "freecad.log").open("wb") as log:
            try:
                code = subprocess.run(
                    [str(executable), "--user-cfg", str(profile / "user.cfg"),
                     "--system-cfg", str(profile / "system.cfg"), str(macro)],
                    env=env, stdout=log, stderr=subprocess.STDOUT, timeout=args.timeout, check=False).returncode
            except subprocess.TimeoutExpired:
                code = "timeout"
    report_file = output / "report.json"
    report = json.loads(report_file.read_text()) if report_file.exists() else {"status": "failed"}
    report.update({"app": str(app), "signed_resource_sha256": signature, "exit": code,
                   "elapsed_s": round(time.monotonic() - started, 1)})
    if report.get("status") == "passed":
        try:
            report["file_checks"] = check_exports(report)
            if not all(v.get("passed") for v in report["file_checks"].values()):
                report["status"] = "failed"
                report["error"] = "Exported files do not match the drawing"
        except Exception:
            report["status"] = "failed"
            report["error"] = traceback.format_exc()
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
    if report["status"] != "passed" or code:
        print(report.get("error", f"App exited {code}. See {output / 'freecad.log'}."))
        return 1
    print(f"Passed: {report_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
