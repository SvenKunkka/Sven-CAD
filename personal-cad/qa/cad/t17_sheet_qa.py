"""Verify the assembly drawing sheet (T17) on a Mac bundle.

An assembly of a plate and three instances of a three-part component has an exploded view
(two components moved 30 mm) and a named state that hides one component and uses the
exploded view. The sheet command builds three views, a section, the static exploded view
with trail lines, the title block, an overall dimension and the BOM. Checks: hidden part
left out, trail lines, view directions and layout, dimension value and update, template
fields, assembly untouched, exploded vs. original, save and reopen."""

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

OUTPUT_ENV = "PERSONAL_CAD_T17_QA"
SHEET = "PersonalCAD_AssemblySheet"


def run_macro():
    import sys
    import FreeCAD as App
    import FreeCADGui as Gui
    import Part
    from PySide6 import QtCore, QtTest, QtWidgets

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from m2_thread_assembly_qa import keep_awake, screen_state
    dev_paths = [p for p in os.environ.get("PERSONAL_CAD_T17_QA_DEV_PATH", "").split(os.pathsep) if p]
    for path in dev_paths:
        sys.path.insert(0, path)
    if dev_paths:
        for name in ("SvenInterference", "SvenReplace", "SvenReplaceGui", "SvenSheets", "SvenDrawing"):
            sys.modules.pop(name, None)
    import SvenReplace as R
    import SvenReplaceGui as RG
    import SvenSheets as S
    import SvenDrawing as D

    output = Path(os.environ[OUTPUT_ENV])
    shots = output / "screenshots"
    shots.mkdir(exist_ok=True)
    work = output / "files"
    work.mkdir(exist_ok=True)
    report = {"status": "failed", "version": list(App.Version()), "checks": {}, "module": S.__file__,
              "app_nap_disabled": keep_awake(), "screen_at_start": screen_state()}
    checks = report["checks"]
    started = time.monotonic()

    def log(step, **data):
        with (output / "progress.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"t": round(time.monotonic() - started, 3), "step": step, **data},
                                    ensure_ascii=False) + "\n")

    def require(condition, message):
        if not condition:
            raise AssertionError(message)

    def click(widget, name):
        button = widget.findChild(QtWidgets.QPushButton, name)
        require(button is not None, f"no {name}")
        QtTest.QTest.mouseClick(button, QtCore.Qt.MouseButton.LeftButton)
        QtTest.QTest.qWait(200)

    def wait_views(objs, timeout=60):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            QtWidgets.QApplication.processEvents()
            if all(not o.waitingForResult() and o.getVisibleEdges() for o in objs):
                return True
            QtTest.QTest.qWait(100)
        return False

    def build():
        import CommandCreateView as V
        doc = App.newDocument("T17Assembly")
        doc.UndoMode = 1
        motor = doc.addObject("App::Part", "Motor")
        motor.addProperty("App::PropertyString", "PartNumber", "Base", "Supplier part number")
        motor.PartNumber = "FIXTURE-M"  # fixture value, not a company part number
        for label, shape in (("Housing", Part.makeBox(20, 10, 8)),
                             ("Shaft", Part.makeCylinder(2, 10, App.Vector(10, 5, 8))),
                             ("Clip", Part.makeBox(2, 2, 1, App.Vector(5, 7, 8)))):
            f = doc.addObject("Part::Feature", label)
            f.Shape = shape
            motor.addObject(f)
        plate = doc.addObject("Part::Feature", "Plate")
        plate.Shape = Part.makeBox(60, 40, 5)
        asm = doc.addObject("Assembly::AssemblyObject", "Product")
        base = asm.newObject("App::Link", "Base")
        base.LinkedObject = plate
        links = {}
        for name, at in (("Motor1", (5, 5, 5)), ("Motor2", (35, 5, 5)), ("Motor3", (5, 25, 5))):
            link = asm.newObject("App::Link", name)
            link.LinkedObject = motor
            link.Placement = App.Placement(App.Vector(*at), App.Rotation())
            links[name] = link
        doc.recompute()
        motor.Visibility = False
        plate.Visibility = False
        views = asm.newObject("Assembly::ViewGroup", "Exploded Views")
        view = views.newObject("App::FeaturePython", "Exploded")
        V.ExplodedView(view)
        V.ViewProviderExplodedView(view.ViewObject)
        for name, move, ref in (("Lift", (0, 0, 30), "Motor3."), ("Pull", (-30, 0, 0), "Motor1.")):
            step = view.newObject("App::FeaturePython", name)
            V.ExplodedViewStep(step)
            V.ViewProviderExplodedViewStep(step.ViewObject)
            step.References = [asm, [ref]]
            step.MovementTransform = App.Placement(App.Vector(*move), App.Rotation())
            step.Visibility = False
        doc.recompute()
        links["Motor2"].Visibility = False
        state = R.capture(asm, "Service", view)
        links["Motor2"].Visibility = True
        doc.recompute()
        return doc, asm, plate, links, view, state

    def placements(doc):
        return {o.Name: [round(v, 9) for v in list(o.Placement.Base) + list(o.Placement.Rotation.Q)]
                for o in doc.Objects if hasattr(o, "Placement") and o.TypeId in ("App::Link", "Part::Feature")}

    def execute():
        try:
            doc, asm, plate, links, view, state = build()
            Gui.activateWorkbench("AssemblyWorkbench")
            QtTest.QTest.qWait(500)
            ribbon = Gui.getMainWindow().findChild(QtWidgets.QToolBar, "PersonalCadDesignToolbar")
            tabs = ribbon.findChild(QtWidgets.QTabBar) if ribbon else None
            if tabs is not None:
                for index in range(tabs.count()):
                    if tabs.tabText(index) in ("Assembly", "装配") and tabs.isTabVisible(index):
                        tabs.setCurrentIndex(index)
                QtTest.QTest.qWait(400)
            button = ribbon.findChild(QtWidgets.QToolButton, SHEET) if ribbon else None
            checks["ribbon"] = button.text() if button is not None and button.isVisible() else None
            zh = RG.chinese()
            require(checks["ribbon"] == ("出图" if zh else "Drawing"), checks["ribbon"])
            before = placements(doc)
            total_leaves = len(S.state_instances(asm, None))
            Gui.runCommand(SHEET, 0)
            dialog = RG.open_dialog()
            checks["command_opened_dialog"] = isinstance(dialog, RG.SheetDialog)
            if not isinstance(dialog, RG.SheetDialog):
                RG.SheetCommand().Activated()
                dialog = RG.open_dialog()
            require(isinstance(dialog, RG.SheetDialog) and dialog.dialog.isVisible(), "sheet dialog")
            dialog.state.setCurrentIndex(dialog.state.findData(state.Name))
            t0 = time.monotonic()
            click(dialog.dialog, "SvenSheetCreate")
            made = dialog.made
            require(made is not None, "sheet not created")
            page = made["page"]
            parts = list(made["views"].values()) + [made["section"], made["exploded"]]
            require(wait_views(parts), "views did not finish")
            checks["create_s"] = round(time.monotonic() - t0, 2)
            checks["dialog_status"] = dialog.dialog.findChild(QtWidgets.QLabel, "SvenSheetStatus").text()
            dialog.dialog.grab().save(str(shots / "sheet-dialog.png"))
            click(dialog.dialog, "SvenSheetClose")

            # hidden part and assembly untouched
            source, xsource = made["source"], made["exploded_source"]
            checks["solids"] = {"assembly": total_leaves, "state": source.SvenSolids, "exploded": xsource.SvenSolids}
            require(total_leaves == 10 and source.SvenSolids == 7 and xsource.SvenSolids == 7, checks["solids"])
            motor2_centre = links["Motor2"].Placement.multVec(App.Vector(10, 5, 4))
            require(all(s.BoundBox.Center.distanceToPoint(motor2_centre) > 1 for s in source.Shape.Solids),
                    "hidden component drawn")
            require(placements(doc) == before, "sheet changed the assembly")

            # exploded vs original, trails
            box, xbox = source.Shape.BoundBox, xsource.Shape.BoundBox
            lines = [e for e in xsource.Shape.Edges if e not in [x for s in xsource.Shape.Solids for x in s.Edges]]
            loose = [e for e in xsource.Shape.Edges if not any(e.isSame(x) for s in xsource.Shape.Solids for x in s.Edges)]
            checks["exploded"] = {"trail_count": xsource.SvenTrailCount,
                                  "trail_lengths": sorted(round(e.Length, 6) for e in loose),
                                  "box": [round(v, 6) for v in (box.XMin, box.ZMax)],
                                  "exploded_box": [round(v, 6) for v in (xbox.XMin, xbox.ZMax)]}
            require(xsource.SvenTrailCount == 2 and checks["exploded"]["trail_lengths"] == [30.0, 30.0],
                    checks["exploded"])
            require(abs(xbox.XMin - (box.XMin - 25)) < 1e-6 and abs(xbox.ZMax - (box.ZMax + 30)) < 1e-6,
                    checks["exploded"])

            # view directions and first-angle layout
            v = made["views"]
            dirs = {k: [round(c, 9) for c in v[k].Direction] for k in v}
            checks["directions"] = dirs
            require(dirs["Front"] == [0, -1, 0] and dirs["Top"] == [0, 0, 1] and dirs["Right"] == [1, 0, 0], dirs)
            checks["layout"] = {k: [round(v[k].X, 3), round(v[k].Y, 3)] for k in v}
            require(v["Top"].Y < v["Front"].Y and v["Right"].X < v["Front"].X, checks["layout"])
            sec = made["section"]
            checks["section"] = {"edges": len(sec.getVisibleEdges()), "symbol": sec.SectionSymbol,
                                 "base": sec.BaseView.Name}
            checks["section"]["front"] = v["Front"].Name
            require(checks["section"]["edges"] > 0 and sec.BaseView.Name == v["Front"].Name, checks["section"])
            xview = made["exploded"]

            def edge_count(view_obj):
                hidden = view_obj.getHiddenEdges() if hasattr(view_obj, "getHiddenEdges") else []
                return len(view_obj.getVisibleEdges()) + len(hidden)

            with_trails = edge_count(xview)
            xsource.SvenTrails = False
            doc.recompute()
            require(wait_views([xview]), "exploded view did not update")
            without_trails = edge_count(xview)
            xsource.SvenTrails = True
            doc.recompute()
            require(wait_views([xview]), "exploded view did not update back")
            checks["exploded_view_edges"] = {"with_trails": with_trails, "without_trails": without_trails,
                                             "restored": edge_count(xview)}
            require(with_trails > without_trails and edge_count(xview) == with_trails,
                    checks["exploded_view_edges"])
            require(placements(doc) == before, "trail toggle changed the assembly")

            # dimension
            dim = made["dimension"]
            value = dim.getRawValue()
            checks["dimension"] = {"refs": [(o.Name, s) for o, s in dim.References2D], "value": value}
            require(abs(value - 60.0) < 1e-6, checks["dimension"])
            plate.Shape = Part.makeBox(70, 40, 5)
            doc.recompute()
            require(wait_views(parts), "views did not update")
            dim.recompute()
            checks["dimension"]["after_plate_70"] = dim.getRawValue()
            require(abs(dim.getRawValue() - 70.0) < 1e-6, checks["dimension"])
            plate.Shape = Part.makeBox(60, 40, 5)
            doc.recompute()
            require(wait_views(parts), "views did not update back")

            # template
            texts = dict(made["template"].EditableTexts)
            checks["template_fields"] = made["fields"]
            leftover = {k: t for k, t in texts.items() if t and k not in made["fields"] and k != "language_code"}
            checks["template_cleared"] = sorted(k for k, t in texts.items() if not t)
            require(not leftover, ("template sample values left on the sheet", leftover))
            require(texts.get("title") == "Product" and texts.get("supplementary_title_1") == "Service"
                    and texts.get("scale") == S.scale_text(made["scale"]) and texts.get("sheet_number") == "1/1",
                    texts)

            # BOM on the page
            headings, rows = D.bom_table(D.page_bom(page))
            checks["bom"] = rows
            require(any(r[2] == "FIXTURE-M" and r[5] == "3" for r in rows), rows)

            page.ViewObject.doubleClicked()
            QtTest.QTest.qWait(1500)
            Gui.SendMsgToActiveView("ViewFit")
            QtTest.QTest.qWait(500)
            Gui.getMainWindow().grab().save(str(shots / "sheet-page.png"))

            # save and reopen
            saved = work / "t17-sheet.FCStd"
            names = {"page": page.Name, "source": source.Name, "xsource": xsource.Name, "dim": made["dimension"].Name}
            doc.saveAs(str(saved))
            App.closeDocument(doc.Name)
            doc2 = App.openDocument(str(saved))
            page2 = doc2.getObject(names["page"])
            src2 = doc2.getObject(names["source"])
            src2.touch()
            doc2.recompute()
            views2 = [o for o in page2.Views if o.isDerivedFrom("TechDraw::DrawViewPart")]
            require(wait_views(views2), "reopened views did not compute")
            dim2 = doc2.getObject(names["dim"])
            checks["reopened"] = {"views": len(page2.Views), "solids": src2.SvenSolids,
                                  "trails": doc2.getObject(names["xsource"]).SvenTrailCount,
                                  "dimension": dim2.getRawValue(),
                                  "title": dict(page2.Template.EditableTexts).get("title")}
            require(src2.SvenSolids == 7 and abs(dim2.getRawValue() - 60) < 1e-6
                    and checks["reopened"]["title"] == "Product", checks["reopened"])
            App.closeDocument(doc2.Name)
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
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    app = args.app.resolve()
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    executable = app / "Contents/MacOS" / info["CFBundleExecutable"]
    output = args.output.resolve()
    output.mkdir(parents=True)
    signature = hashlib.sha256((app / "Contents/_CodeSignature/CodeResources").read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="sven-t17-qa-") as scratch:
        profile = Path(scratch)
        for name in ("cache", "data", "tmp"):
            (profile / name).mkdir()
        macro = profile / "T17QA.FCMacro"
        macro.write_text(
            f"import sys\nsys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
            "from t17_sheet_qa import run_macro\nrun_macro()\n", encoding="utf-8")
        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8", "HOME": str(profile),
            "TMPDIR": str(profile / "tmp"), "XDG_CACHE_HOME": str(profile / "cache"),
            "FREECAD_USER_HOME": str(profile), "FREECAD_USER_DATA": str(profile / "data"),
            "FREECAD_USER_TEMP": str(profile / "tmp"), "PYTHONDONTWRITEBYTECODE": "1",
            OUTPUT_ENV: str(output),
        }
        if os.environ.get("PERSONAL_CAD_T17_QA_DEV_PATH"):
            env["PERSONAL_CAD_T17_QA_DEV_PATH"] = os.environ["PERSONAL_CAD_T17_QA_DEV_PATH"]
        started = time.monotonic()
        with (output / "freecad.log").open("wb") as log:
            proc = subprocess.Popen(
                [str(executable), "--user-cfg", str(profile / "user.cfg"),
                 "--system-cfg", str(profile / "system.cfg"), str(macro)],
                env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                code = proc.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
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
