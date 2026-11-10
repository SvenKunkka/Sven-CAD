"""Verify the Sven CAD fastener catalog, linked instances, replacement and counts on a Mac bundle."""

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
import zipfile


OUTPUT_ENV = "PERSONAL_CAD_FASTENER_QA"
INSERT = "PersonalCAD_InsertFastener"
REPLACE = "PersonalCAD_ReplaceFastener"
SUMMARY = "PersonalCAD_FastenerSummary"
HOLES = ((6, 6), (34, 6), (6, 14), (34, 14))
PLATE = (40.0, 20.0, 5.0)
HOLE_DIAMETER = 3.2


def run_macro():
    import FreeCAD as App
    import FreeCADGui as Gui
    import Part
    from PySide6 import QtCore, QtTest, QtWidgets
    import sys
    dev_path = os.environ.get("PERSONAL_CAD_FASTENER_QA_DEV_PATH")
    if dev_path:
        sys.path.insert(0, dev_path)
        for name in ("SvenFasteners", "SvenFastenersGui"):
            sys.modules.pop(name, None)
    import SvenFasteners as F

    output = Path(os.environ[OUTPUT_ENV])
    shots = output / "screenshots"
    shots.mkdir(exist_ok=True)
    report = {"status": "failed", "version": list(App.Version()), "checks": {}, "messages": {},
              "fastener_module": F.__file__, "dev_module_path": dev_path}
    checks = report["checks"]
    messages = report["messages"]
    window = Gui.getMainWindow()
    started = time.monotonic()

    def log(step, **data):
        with (output / "progress.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"t": round(time.monotonic() - started, 3), "step": step, **data},
                                    ensure_ascii=False) + "\n")

    def require(condition, message):
        if not condition:
            raise AssertionError(message)

    # ---------- GUI harness ----------
    def ribbon_button(name):
        ribbon = window.findChild(QtWidgets.QToolBar, "PersonalCadDesignToolbar")
        require(ribbon is not None, "Design ribbon missing")
        button = ribbon.findChild(QtWidgets.QToolButton, name)
        require(button is not None and button.isVisible() and button.isEnabled(), f"{name} button unavailable")
        return button

    def invoke(trigger, handlers, timeout_s=60):
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
        return lambda: QtTest.QTest.mouseClick(ribbon_button(name), QtCore.Qt.MouseButton.LeftButton)

    def run(name):
        return lambda: Gui.runCommand(name, 0)

    def ok_button(modal):
        return modal.findChild(QtWidgets.QDialogButtonBox).button(QtWidgets.QDialogButtonBox.StandardButton.Ok)

    def choose(query, expected, *, quantity=None, flip=False, kind=None, accept=True, shot=None, info=None):
        def handle(modal):
            require(modal.objectName() == "SvenFastenerDialog", modal.objectName())
            search = modal.findChild(QtWidgets.QLineEdit, "SvenFastenerSearch")
            table = modal.findChild(QtWidgets.QTableWidget, "SvenFastenerResults")
            all_rows = table.rowCount()
            if kind is not None:
                combo = modal.findChild(QtWidgets.QComboBox, "SvenFastenerKind")
                require(combo.currentData() == kind, (combo.currentData(), kind))
            if query.isascii():
                QtTest.QTest.keyClicks(search, query)
            else:
                # QTest cannot type non-ASCII keys; commit the text the way an input method does.
                search.insert(query)
            QtTest.QTest.qWait(50)
            rows = [table.item(r, 0).text() for r in range(table.rowCount())]
            if info is not None:
                info.update({"title": modal.windowTitle(), "rows_before_query": all_rows, "rows": rows,
                             "source_text": modal.findChild(QtWidgets.QLabel, "SvenFastenerSource").text(),
                             "placement_text": modal.findChild(QtWidgets.QLabel, "SvenFastenerPlacementInfo").text(),
                             "ok_enabled": ok_button(modal).isEnabled()})
            if expected is None:
                require(not rows and not ok_button(modal).isEnabled(), rows)
            else:
                require(expected in rows, (expected, rows))
                table.selectRow(rows.index(expected))
                QtTest.QTest.qWait(30)
                require(ok_button(modal).isEnabled(), "OK disabled with a selected item")
            if quantity is not None:
                modal.findChild(QtWidgets.QSpinBox, "SvenFastenerQuantity").setValue(quantity)
            if flip:
                modal.findChild(QtWidgets.QCheckBox, "SvenFastenerFlip").setChecked(True)
            if shot:
                QtTest.QTest.qWait(50)
                require(modal.grab().save(str(shots / shot)), "screenshot")
            if accept:
                ok_button(modal).click()
            else:
                modal.reject()
        return handle

    def message(key, expected, shot=None):
        def handle(modal):
            require(isinstance(modal, QtWidgets.QMessageBox), modal.metaObject().className())
            messages[key] = modal.text()
            require(expected in modal.text(), (expected, modal.text()))
            if shot:
                require(modal.grab().save(str(shots / shot)), "screenshot")
            modal.accept()
        return handle

    def doc_state(doc):
        return (tuple(o.Name for o in doc.Objects), doc.UndoCount, doc.RedoCount)

    # ---------- fixtures ----------
    def plate_shape():
        shape = Part.makeBox(*PLATE)
        for x, y in HOLES:
            shape = shape.cut(Part.makeCylinder(HOLE_DIAMETER / 2, PLATE[2] + 2, App.Vector(x, y, -1)))
        return shape

    def import_plate(doc):
        path = output / "plate-4-holes.step"
        if not path.exists():
            plate_shape().exportStep(str(path))
        part = doc.addObject("App::Part", "SupplierPart")
        part.Placement = App.Placement(App.Vector(30, -10, 5), App.Rotation(App.Vector(1, 1, 0), 35))
        plate = doc.addObject("Part::Feature", "Plate")
        plate.Shape = Part.read(str(path))
        part.addObject(plate)
        plate.Placement = App.Placement(App.Vector(2, 1, 0), App.Rotation(App.Vector(0, 0, 1), 15))
        doc.recompute()
        return part, plate

    def top_edges(plate):
        names = []
        for index, edge in enumerate(plate.Shape.Edges, 1):
            curve = edge.Curve
            if isinstance(curve, Part.Circle) and abs(curve.Radius - HOLE_DIAMETER / 2) < 1e-6:
                local = plate.Placement.inverse().multVec(curve.Center)
                if abs(local.z - PLATE[2]) < 1e-6:
                    names.append((f"Edge{index}", (round(local.x, 6), round(local.y, 6))))
        names.sort(key=lambda item: HOLES.index(item[1]))
        require(len(names) == 4, names)
        return [name for name, _ in names]

    def select_edges(doc, part, plate, edges):
        Gui.Selection.clearSelection()
        for edge in edges:
            Gui.Selection.addSelection(doc.Name, part.Name, f"{plate.Name}.{edge}")
        QtTest.QTest.qWait(80)

    def global_placement(obj):
        parent = obj.getParentGeoFeatureGroup()
        return parent.getGlobalPlacement().multiply(obj.Placement) if parent else obj.Placement

    def global_shape(obj):
        shape = Part.getShape(obj, "", needSubElement=False, transform=False).copy()
        shape.Placement = global_placement(obj)
        return shape

    def hole_frames(part, plate):
        frames = []
        placement = part.Placement.multiply(plate.Placement)
        for x, y in HOLES:
            frames.append((placement.multVec(App.Vector(x, y, PLATE[2])),
                           placement.Rotation.multVec(App.Vector(0, 0, 1))))
        return frames

    def check_seated(link, frame, item, plate_global):
        center, axis = frame
        placement = global_placement(link)
        require(placement.Base.distanceToPoint(center) < 1e-7, (placement.Base, center))
        z = placement.Rotation.multVec(App.Vector(0, 0, 1))
        require(z.getAngle(axis) < 1e-7, ("axis", z, axis))
        shape = global_shape(link)
        overlap = shape.common(plate_global).Volume
        require(overlap < 1e-6, f"Fastener intersects the plate by {overlap}")
        dist = shape.distToShape(plate_global)[0]
        require(dist < 1e-6, f"Fastener does not bear on the plate: {dist}")
        return {"center": [round(v, 6) for v in center], "overlap_mm3": overlap, "gap_mm": dist}

    # ---------- checks ----------
    def check_catalog():
        data = F.catalog()
        items = F.items()
        require(len(items) == 32, len(items))
        for item in items:
            require(item["source"]["url"].startswith("https://") and item["source"]["retrieved"] == "2026-11-10",
                    item["part_number"])
            require(item["catalog_version"] == data["catalog_version"] and item["edition"], item["part_number"])
            shape = F.make_shape(item)
            require(shape.isValid() and len(shape.Solids) == 1, item["part_number"])
        cases = {
            "M3": 9, "M3 8": ["ISO4762-M3x8"], "m3x8": ["ISO4762-M3x8"], "ISO4762-M3x8": ["ISO4762-M3x8"],
            "螺母 M2.5": ["ISO4032-M2.5"], "垫圈 M4": ["ISO7089-M4"], "washer m4": ["ISO7089-M4"],
            "4762 m4 20": ["ISO4762-M4x20"], "nut 8": [], "M2": 7, "m2x1": [], "iso7089-m3x8": [], "M2.5": 8, "螺钉 10": 4, "screw L12": 3,
        }
        results = {}
        for query, expected in cases.items():
            found = [i["part_number"] for i in F.search(query)]
            results[query] = found
            require(len(found) == expected if isinstance(expected, int) else found == expected, (query, found))
        dims = {}
        for number, check in (("ISO4762-M3x8", (5.5, 3.0 + 8.0)), ("ISO4762-M2x4", (3.8, 2.0 + 4.0)),
                              ("ISO4032-M4", (7.0 * 2 / math.sqrt(3), 3.2)), ("ISO7089-M2.5", (6.0, 0.5))):
            box = F.make_shape(F.find(number)).BoundBox
            dims[number] = {"xy_max": max(box.XLength, box.YLength), "z": box.ZLength}
            require(math.isclose(max(box.XLength, box.YLength), check[0], abs_tol=1e-6), (number, box))
            require(math.isclose(box.ZLength, check[1], abs_tol=1e-6), (number, box))
        checks["catalog"] = {"version": data["catalog_version"], "items": len(items), "sources": data["sources"],
                             "search": results, "dimensions": dims}

    def check_ribbon():
        doc = App.newDocument("FastenerRibbonQA")
        import_plate(doc)
        Gui.activateWorkbench("PartDesignWorkbench")
        Gui.activeDocument().activeView().fitAll()
        QtTest.QTest.qWait(300)
        info = {"default_locale": Gui.getLocale(), "solid": ribbon_button(INSERT).text()}
        require(info["solid"] in ("紧固件", "Fastener"), info)
        require(all(Gui.Command.get(name) is not None for name in (INSERT, REPLACE, SUMMARY)), "commands missing")
        english = next(k for k, c in Gui.supportedLocales().items() if c in ("en", "en-US", "en_US"))
        Gui.setLocale(english)
        QtTest.QTest.qWait(600)
        Gui.activateWorkbench("PartDesignWorkbench")
        QtTest.QTest.qWait(300)
        info["solid_en"] = ribbon_button(INSERT).text()
        require(info["solid_en"] == "Fastener", info)
        require(window.grab().save(str(shots / "ribbon-solid-fastener.png")), "screenshot")
        Gui.activateWorkbench("AssemblyWorkbench")
        QtTest.QTest.qWait(400)
        info["assembly_tab"] = ribbon_button(INSERT).text()
        require(window.grab().save(str(shots / "ribbon-assembly-fastener.png")), "screenshot")
        Gui.activateWorkbench("PartDesignWorkbench")
        QtTest.QTest.qWait(300)
        App.closeDocument(doc.Name)
        checks["ribbon"] = info

    def check_workflow():
        result = {}
        doc = App.newDocument("FastenerWorkflowQA")
        doc.UndoMode = 1
        part, plate = import_plate(doc)
        Gui.activeDocument().activeView().fitAll()
        plate_before = plate.Shape.exportBrepToString()
        plate_global = global_shape(plate)
        edges = top_edges(plate)
        frames = hole_frames(part, plate)

        # Cancel leaves the document as it was.
        select_edges(doc, part, plate, edges)
        before = doc_state(doc)
        invoke(click(INSERT), [choose("m3x8", "ISO4762-M3x8", accept=False)])
        require(doc_state(doc) == before, "Cancel changed the document")
        # No match keeps OK disabled.
        empty = {}
        invoke(click(INSERT), [choose("zzz", None, accept=False, info=empty)])
        require(doc_state(doc) == before, "Empty search changed the document")
        result["cancel_and_empty_search_unchanged"] = {"empty_ok_enabled": empty["ok_enabled"]}

        # Insert one screw per selected hole edge.
        info = {}
        t0 = time.monotonic()
        invoke(click(INSERT), [choose("m3x8", "ISO4762-M3x8", info=info, shot="dialog-insert-edges.png")])
        result["insert_edges_seconds"] = round(time.monotonic() - t0, 3)
        links = F.instances(doc)
        require(len(links) == 4, len(links))
        sources = F.sources(doc)
        require(len(sources) == 1, [s.Name for s in sources])
        source = sources[0]
        require(all(link.LinkedObject == source for link in links), "Links do not share one source")
        require(not source.ViewObject.Visibility and all(link.ViewObject.Visibility for link in links),
                "Source should be hidden and instances visible")
        require(source.getParentGroup() == doc.getObject(F.LIBRARY_NAME), "Source outside the library group")
        require(doc.UndoCount == before[1] + 1, (doc.UndoCount, before[1]))
        require(plate.Shape.exportBrepToString() == plate_before, "Plate changed")
        seated = [check_seated(link, frame, F.find("ISO4762-M3x8"), plate_global)
                  for link, frame in zip(sorted(links, key=lambda l: l.Label), frames)]
        props = {name: str(getattr(source, name)) for name, *_ in F.PROPERTIES}
        require(props["DataSourceUrl"] == "https://www.fasteners.eu/standards/ISO/4762/", props)
        result["insert_on_hole_edges"] = {"dialog": info, "labels": sorted(l.Label for l in links),
                                          "seated": seated, "source": source.Name, "source_properties": props}
        view = Gui.activeDocument().activeView()
        view.viewIsometric()
        view.fitAll()
        QtTest.QTest.qWait(200)
        view.saveImage(str(shots / "viewport-four-screws.png"), 1200, 900, "White")

        doc.undo()
        require(not F.instances(doc) and not F.sources(doc) and doc.getObject(F.LIBRARY_NAME) is None,
                "Undo left fastener objects")
        doc.redo()
        links = F.instances(doc)
        require(len(links) == 4 and len(F.sources(doc)) == 1, "Redo did not restore the instances")
        result["undo_redo_single_transaction"] = True

        # Second insert reuses the same source at the origin.
        Gui.Selection.clearSelection()
        invoke(click(INSERT), [choose("ISO4762-M3x8", "ISO4762-M3x8", quantity=3, flip=True)])
        links = F.instances(doc)
        require(len(links) == 7 and len(F.sources(doc)) == 1, (len(links), len(F.sources(doc))))
        origin_links = sorted(links, key=lambda l: l.Name)[4:]
        bases = [l.Placement.Base for l in origin_links]
        require(all(abs(b.y) < 1e-9 and abs(b.z) < 1e-9 for b in bases) and len({round(b.x, 6) for b in bases}) == 3,
                bases)
        down = origin_links[0].Placement.Rotation.multVec(App.Vector(0, 0, 1))
        require(down.getAngle(App.Vector(0, 0, -1)) < 1e-9, down)
        result["origin_quantity_flip_reuses_source"] = {"bases_x": [b.x for b in bases], "axis": list(down)}

        # Independent placement.
        moved = origin_links[0]
        others = {l.Name: l.Placement for l in links if l != moved}
        moved.Placement = App.Placement(App.Vector(-20, 5, 3), App.Rotation(App.Vector(0, 1, 0), 30))
        doc.recompute()
        require(all(doc.getObject(n).Placement.isSame(p, 1e-12) for n, p in others.items()), "Other instances moved")
        result["independent_placement"] = True

        # Replace only the selected instance.
        target = sorted(F.instances(doc), key=lambda l: l.Label)[0]
        untouched = {l.Name: (l.LinkedObject.Name, l.Placement) for l in F.instances(doc) if l != target}
        m3x8_brep = source.Shape.exportBrepToString()
        target_placement = target.Placement
        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(doc.Name, target.Name)
        rinfo = {}
        before_replace = doc.UndoCount
        invoke(run(REPLACE), [choose("m3x10", "ISO4762-M3x10", kind="screw", info=rinfo,
                                     shot="dialog-replace.png")])
        require(target.LinkedObject.PartNumber == "ISO4762-M3x10", target.LinkedObject.PartNumber)
        require(target.Placement.isSame(target_placement, 1e-12), "Replace moved the instance")
        require(all(doc.getObject(n).LinkedObject.Name == s and doc.getObject(n).Placement.isSame(p, 1e-12)
                    for n, (s, p) in untouched.items()), "Replace changed another instance")
        require(source.Shape.exportBrepToString() == m3x8_brep, "Replace changed the shared source")
        require(len(F.sources(doc)) == 2 and doc.UndoCount == before_replace + 1, "Replace transaction")
        doc.undo()
        require(target.LinkedObject == source, "Undo did not restore the replaced instance")
        doc.redo()
        require(target.LinkedObject.PartNumber == "ISO4762-M3x10", "Redo did not replace again")
        result["replace_only_selected"] = {"dialog": rinfo, "target": target.Label, "untouched": len(untouched)}

        # Summary through the GUI.
        summary = {}

        def read_summary(modal):
            require(modal.objectName() == "SvenFastenerSummary", modal.objectName())
            table = modal.findChild(QtWidgets.QTableWidget, "SvenFastenerSummaryTable")
            summary["rows"] = [[table.item(r, c).text() for c in range(table.columnCount())]
                               for r in range(table.rowCount())]
            summary["total"] = modal.findChild(QtWidgets.QLabel, "SvenFastenerSummaryTotal").text()
            require(modal.grab().save(str(shots / "dialog-summary.png")), "screenshot")
            modal.reject()

        invoke(run(SUMMARY), [read_summary])
        counts = {row[0]: int(row[2]) for row in summary["rows"]}
        require(counts == {"ISO4762-M3x10": 1, "ISO4762-M3x8": 6}, counts)
        require("7" in summary["total"], summary["total"])
        result["summary_dialog"] = summary

        # Rejections.
        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(doc.Name, part.Name, f"{plate.Name}.Face1")
        state = doc_state(doc)
        invoke(click(INSERT), [message("insert_face", "circular hole edges", "reject-face.png")])
        straight = next(f"Edge{i}" for i, e in enumerate(plate.Shape.Edges, 1) if isinstance(e.Curve, Part.Line))
        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(doc.Name, part.Name, f"{plate.Name}.{straight}")
        invoke(click(INSERT), [message("insert_straight_edge", "Select circular hole edges.")])
        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(doc.Name, part.Name, f"{plate.Name}.{straight}")
        invoke(run(REPLACE), [message("replace_non_fastener", "Select fastener instances to replace.")])
        require(doc_state(doc) == state, "A rejected command changed the document")
        result["rejections_unchanged"] = True

        # Link arrays count every element.
        arr_link = sorted(F.instances(doc), key=lambda l: l.Name)[-1]
        arr_link.ElementCount = 3
        doc.recompute()
        counted = {r["part_number"]: r["count"] for r in F.summary(doc)}
        require(counted["ISO4762-M3x8"] == 8, counted)
        arr_link.ElementCount = 0
        doc.recompute()
        result["link_array_counted"] = True

        # Save, reopen and keep editing.
        path = output / "fastener-assembly.FCStd"
        doc.saveAs(str(path))
        names = {l.Name: (l.LinkedObject.Name, l.Placement) for l in F.instances(doc)}
        source_name, part_name, plate_name = source.Name, part.Name, plate.Name
        App.closeDocument(doc.Name)
        with zipfile.ZipFile(path) as archive:
            breps = sorted(n for n in archive.namelist() if n.lower().endswith((".brp", ".brep")))
        doc = App.openDocument(str(path))
        reopened = F.instances(doc)
        require({l.Name: (l.LinkedObject.Name, l.Placement) for l in reopened}.keys() == names.keys(), "Reopen")
        require(all(doc.getObject(n).LinkedObject.Name == s and doc.getObject(n).Placement.isSame(p, 1e-9)
                    for n, (s, p) in names.items()), "Reopen changed links")
        require(all(not global_shape(l).isNull() for l in reopened), "Reopened link has no shape")
        src = doc.getObject(source_name)
        require(src.DataRetrieved == "2026-11-10" and src.CatalogVersion == F.catalog()["catalog_version"], "props")
        part = doc.getObject(part_name)
        plate = doc.getObject(plate_name)
        select_edges(doc, part, plate, top_edges(plate)[:1])
        invoke(click(INSERT), [choose("m3x8", "ISO4762-M3x8")])
        require(len(F.sources(doc)) == 2 and len(F.instances(doc)) == 8, "Reopened insert did not reuse the source")
        counted = {r["part_number"]: r["count"] for r in F.summary(doc)}
        require(counted == {"ISO4762-M3x10": 1, "ISO4762-M3x8": 7}, counted)
        result["save_reopen_continue"] = {"brep_entries": breps, "counts": counted}
        App.closeDocument(doc.Name)

        # Insert into an active part container: placements become local to the container.
        doc = App.newDocument("FastenerContainerQA")
        part, plate = import_plate(doc)
        assembly = doc.addObject("App::Part", "ProductAssembly")
        assembly.Placement = App.Placement(App.Vector(-7, 4, 2), App.Rotation(App.Vector(0, 0, 1), 50))
        doc.recompute()
        Gui.activeDocument().activeView().setActiveObject("part", assembly)
        select_edges(doc, part, plate, top_edges(plate)[2:])
        invoke(click(INSERT), [choose("washer m3", "ISO7089-M3")])
        washers = F.instances(doc)
        require(len(washers) == 2 and all(w.getParentGeoFeatureGroup() == assembly for w in washers), "container")
        seated = [check_seated(w, f, F.find("ISO7089-M3"), global_shape(plate))
                  for w, f in zip(sorted(washers, key=lambda l: l.Label), hole_frames(part, plate)[2:])]
        Gui.activeDocument().activeView().setActiveObject("part", None)
        result["active_container_local_placement"] = seated

        # The same insert into an active Assembly object.
        product = doc.addObject("Assembly::AssemblyObject", "Assembly")
        product.Placement = App.Placement(App.Vector(3, -2, 1), App.Rotation(App.Vector(1, 0, 0), 20))
        doc.recompute()
        Gui.activeDocument().activeView().setActiveObject("assembly", product)
        select_edges(doc, part, plate, top_edges(plate)[:1])
        invoke(click(INSERT), [choose("nut m3", "ISO4032-M3")])
        nuts = [l for l in F.instances(doc) if l.LinkedObject.PartNumber == "ISO4032-M3"]
        require(len(nuts) == 1 and nuts[0].getParentGeoFeatureGroup() == product, "assembly parent")
        require(nuts[0] in product.Group, "Nut not listed in the assembly")
        assembly_seat = check_seated(nuts[0], hole_frames(part, plate)[0], F.find("ISO4032-M3"),
                                     global_shape(plate))
        Gui.activeDocument().activeView().setActiveObject("assembly", None)
        result["active_assembly_local_placement"] = assembly_seat
        App.closeDocument(doc.Name)
        checks["workflow"] = result

    def check_scale():
        doc = App.newDocument("FastenerScaleQA")
        Gui.Selection.clearSelection()
        t0 = time.monotonic()
        invoke(click(INSERT), [choose("m2x5", "ISO4762-M2x5", quantity=50)])
        seconds = time.monotonic() - t0
        require(len(F.instances(doc)) == 50 and len(F.sources(doc)) == 1, "50 instances")
        t1 = time.monotonic()
        doc.recompute()
        path = output / "fastener-50.FCStd"
        doc.saveAs(str(path))
        save_seconds = time.monotonic() - t1
        size = path.stat().st_size
        App.closeDocument(doc.Name)
        with zipfile.ZipFile(path) as archive:
            breps = [n for n in archive.namelist() if n.lower().endswith((".brp", ".brep"))]
        require(len(breps) == 1, breps)
        checks["fifty_instances"] = {"insert_seconds": round(seconds, 3), "save_seconds": round(save_seconds, 3),
                                     "file_bytes": size, "brep_entries": breps}

    def check_chinese():
        name = next(k for k, c in Gui.supportedLocales().items() if c in ("zh-CN", "zh_CN"))
        previous = Gui.getLocale()
        Gui.setLocale(name)
        QtTest.QTest.qWait(600)
        Gui.activateWorkbench("PartDesignWorkbench")
        try:
            doc = App.newDocument("FastenerZhQA")
            part, plate = import_plate(doc)
            Gui.activeDocument().activeView().fitAll()
            QtTest.QTest.qWait(300)
            info = {"button": ribbon_button(INSERT).text()}
            require(info["button"] == "紧固件", info)
            Gui.Selection.clearSelection()
            dialog = {}
            invoke(click(INSERT), [choose("螺母 M3", "ISO4032-M3", accept=False, info=dialog, shot="dialog-zh.png")])
            info["dialog"] = dialog
            require(dialog["title"] == "插入紧固件" and dialog["rows"] == ["ISO4032-M3"], dialog)
            Gui.Selection.addSelection(doc.Name, part.Name, f"{plate.Name}.Face1")
            invoke(click(INSERT), [message("zh_insert_face", "请选择圆形孔边")])
            App.closeDocument(doc.Name)
        finally:
            Gui.setLocale(previous)
            QtTest.QTest.qWait(400)
        checks["chinese_ui"] = info

    def execute():
        steps = (check_catalog, check_ribbon, check_workflow, check_scale, check_chinese)
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
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
        if report["status"] == "passed":
            Gui.runCommand("Std_Quit", 0)
        else:
            os._exit(1)

    QtCore.QTimer.singleShot(500, execute)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    app = args.app.resolve()
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    executable = app / "Contents/MacOS" / info["CFBundleExecutable"]
    output = args.output.resolve()
    output.mkdir(parents=True)
    signature = hashlib.sha256((app / "Contents/_CodeSignature/CodeResources").read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="sven-fastener-qa-") as scratch:
        profile = Path(scratch)
        for name in ("cache", "data", "tmp"):
            (profile / name).mkdir()
        macro = profile / "FastenerQA.FCMacro"
        macro.write_text(
            f"import sys\nsys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
            "from fastener_library_qa import run_macro\nrun_macro()\n", encoding="utf-8")
        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8", "HOME": str(profile),
            "TMPDIR": str(profile / "tmp"), "XDG_CACHE_HOME": str(profile / "cache"),
            "FREECAD_USER_HOME": str(profile), "FREECAD_USER_DATA": str(profile / "data"),
            "FREECAD_USER_TEMP": str(profile / "tmp"), "PYTHONDONTWRITEBYTECODE": "1", OUTPUT_ENV: str(output),
        }
        if os.environ.get("PERSONAL_CAD_FASTENER_QA_DEV_PATH"):
            env["PERSONAL_CAD_FASTENER_QA_DEV_PATH"] = os.environ["PERSONAL_CAD_FASTENER_QA_DEV_PATH"]
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
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
    if report["status"] != "passed" or code:
        print(report.get("error", f"App exited {code}. See {output / 'freecad.log'}."))
        return 1
    print(f"Passed: {report_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
