"""M3 checkpoint: one run from an incoming STEP to a delivery package.

Import a supplier STEP housing with plain bores, tap one bore (internal thread), add a native
threaded hole and then change it ("modify the hole"), insert a standard screw, place a cover
with a known gap in an assembly, check interference and clearance, make the assembly sheet
with thread callouts, and write the package. The package files are read back outside the app."""

import argparse
import csv
import hashlib
import html
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import tempfile
import time
import traceback

OUTPUT_ENV = "PERSONAL_CAD_M3_QA"
PDFTOTEXT = "/opt/homebrew/bin/pdftotext"


def run_macro():
    import sys
    import FreeCAD as App
    import FreeCADGui as Gui
    import Part
    from PySide6 import QtCore, QtWidgets

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from m2_thread_assembly_qa import keep_awake, screen_state
    import ImportGui
    import SvenFasteners as F
    import SvenInterference as SI
    import SvenSheets as S
    import SvenPackage as P
    import SvenDrawing as D
    import SvenDrawingGui as DG

    output = Path(os.environ[OUTPUT_ENV])
    work = output / "files"
    work.mkdir(exist_ok=True)
    report = {"status": "failed", "version": list(App.Version()), "checks": {}, "stages_s": {},
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

    def stage(name, fn):
        log("start", name=name)
        t0 = time.monotonic()
        result = fn()
        report["stages_s"][name] = round(time.monotonic() - t0, 3)
        log("done", name=name)
        return result

    ctx = {}

    def supplier_step():
        doc = App.newDocument("M3Supplier")
        housing = doc.addObject("Part::Feature", "Housing")
        shape = Part.makeBox(40, 30, 10)
        for x, y in ((10, 15), (30, 15)):
            shape = shape.cut(Part.makeCylinder(1.25, 12, App.Vector(x, y, -1)))  # 2.5 mm tap drill
        housing.Shape = shape
        mat = App.Material()
        mat.DiffuseColor = (0.2, 0.3, 0.8, 1.0)
        housing.ViewObject.ShapeAppearance = [mat]
        path = work / "supplier-housing.step"
        ImportGui.export([housing], str(path))
        App.closeDocument(doc.Name)
        ctx["step"] = path
        checks["step_bytes"] = path.stat().st_size

    def import_and_modify():
        doc = App.newDocument("M3Checkpoint")
        doc.saveAs(str(work / "m3-checkpoint.FCStd"))
        before = set(o.Name for o in doc.Objects)
        ImportGui.insert(str(ctx["step"]), doc.Name)
        doc.recompute()
        imported = [o for o in doc.Objects if o.Name not in before and o.isDerivedFrom("Part::Feature")
                    and o.Shape.Solids]
        require(len(imported) == 1, [o.Label for o in imported])
        plate = imported[0]
        module = doc.addObject("App::Part", "HousingModule")
        for parent in list(plate.InList):
            if parent.isDerivedFrom("App::Part"):
                parent.removeObject(plate)
        module.addObject(plate)
        bore = next(f"Face{i}" for i, f in enumerate(plate.Shape.Faces, 1)
                    if isinstance(f.Surface, Part.Cylinder) and abs(f.Surface.Center.x - 10) < 1e-6)
        tap = doc.addObject("PartDesign::InternalThread", "Tap")
        module.addObject(tap)
        tap.Support = (plate, [bore])
        tap.ThreadSize = "M3x0.5"
        tap.ThreadLength = 8.0
        tap.Modeled = False
        doc.recompute()
        require(tap.isValid(), "tap invalid")
        plate.Visibility = False
        body = doc.addObject("PartDesign::Body", "HousingBody")
        module.addObject(body)
        body.BaseFeature = tap
        doc.recompute()
        top = next(f"Face{i}" for i, f in enumerate(body.BaseFeature.Shape.Faces, 1)
                   if abs(f.CenterOfMass.z - 10) < 1e-6 and abs(f.normalAt(0, 0).z - 1) < 1e-6)
        sketch = body.newObject("Sketcher::SketchObject", "HoleSketch")
        sketch.AttachmentSupport = [(body.BaseFeature, top)] if hasattr(body, "BaseFeature") else []
        sketch.MapMode = "FlatFace"
        doc.recompute()
        local = sketch.Placement.inverse().multVec(App.Vector(20, 8, 10))
        sketch.addGeometry(Part.Circle(App.Vector(local.x, local.y, 0), App.Vector(0, 0, 1), 1.25))
        doc.recompute()
        hole = body.newObject("PartDesign::Hole", "Hole")
        hole.Profile = sketch
        hole.Threaded = True
        hole.ThreadType = "ISOMetricProfile"
        hole.ThreadSize = "M3x0.5"
        hole.ThreadClass = "6H"
        hole.DepthType = "Dimension"
        hole.Depth = 8
        hole.ThreadDepthType = "Dimension"
        hole.ThreadDepth = 6
        doc.recompute()
        require(hole.isValid(), "hole invalid")
        volume_m3 = body.Shape.Volume
        hole.ThreadSize = "M4x0.7"  # modify the hole
        doc.recompute()
        require(hole.isValid() and body.Shape.Volume < volume_m3, "hole change did not cut more")
        checks["hole_change"] = {"volume_m3": round(volume_m3, 4), "volume_m4": round(body.Shape.Volume, 4),
                                 "frames": len(hole.ThreadFrames)}
        tap.Visibility = False
        ctx.update(doc=doc, module=module, plate=plate, tap=tap, body=body, hole=hole)

    def assemble():
        doc, module = ctx["doc"], ctx["module"]
        asm = doc.addObject("Assembly::AssemblyObject", "Product")
        base = asm.newObject("App::Link", "Housing")
        base.LinkedObject = module
        cover_src = doc.addObject("Part::Feature", "Cover")
        cover_src.Shape = Part.makeBox(40, 10, 2, App.Vector(0, 20, 0))
        cover = asm.newObject("App::Link", "CoverLink")
        cover.LinkedObject = cover_src
        cover.Placement = App.Placement(App.Vector(0, 0, 10.1), App.Rotation())  # 0.1 mm gap
        doc.recompute()
        module.Visibility = False
        cover_src.Visibility = False
        item = F.find("ISO4762-M3x8")
        spot = App.Vector(10, 15, 10)
        links = None
        for rot in (App.Rotation(), App.Rotation(App.Vector(1, 0, 0), 180)):
            links, _source, _ = F.insert(doc, item, [App.Placement(spot, rot)], asm)
            doc.recompute()
            if links[0].Shape.BoundBox.ZMin < 9.0:
                break
            doc.removeObject(links[0].Name)
        require(links and links[0].Shape.BoundBox.ZMin < 9.0, "screw does not go into the bore")
        ctx.update(asm=asm, cover=cover, screw=links[0])
        checks["screw"] = {"part_number": item["part_number"], "zmin": round(links[0].Shape.BoundBox.ZMin, 4)}

    def interference():
        asm = ctx["asm"]
        # The screw shank fills the tapped bore, drawn at the tap drill size: an allowed fastener fit.
        result = SI.check([asm], clearance_limit=0.2, allowed=[(ctx["screw"].Name, "Housing")])
        rows = [(r["kind"], r["a"]["label"], r["b"]["label"], r.get("volume"), r.get("distance"), r.get("allowed"))
                for r in result["results"]]
        checks["interference"] = {"instances": result["instances"], "pairs": result["pairs_checked"],
                                  "counts": result["counts"], "problems": result["problems"], "rows": rows,
                                  "seconds": result["seconds"]}
        gaps = [r for r in result["results"] if r["kind"] == "clearance"]
        require(any(abs(r["distance"] - 0.1) < 1e-6 for r in gaps), rows)
        require(result["problems"]["overlap"] == 0 and result["allowed"] == 1, ("unexpected overlap", rows))

    def sheet():
        doc, asm = ctx["doc"], ctx["asm"]
        made = S.make_sheet(asm, None, section=True, exploded=False, name="M3Sheet")
        page = made["page"]
        require(DG.wait_for_views(page), "sheet views")
        detail = doc.addObject("TechDraw::DrawViewPart", "HousingTop")
        page.addView(detail)
        detail.Source = [ctx["module"]]
        detail.Direction = App.Vector(0, 0, 1)
        detail.ScaleType = "Custom"
        detail.Scale = 2.0
        detail.X = float(made["template"].Width) * 0.75
        detail.Y = float(made["template"].Height) * 0.35
        doc.recompute()
        require(DG.wait_for_views(page), "detail view")
        callouts = D.add_callouts(detail)
        doc.recompute()
        texts = sorted(c.Label for c in callouts)
        checks["callouts"] = texts
        require(any(t.startswith("M4x0.7-6H") for t in texts) and any(t.startswith("M3x0.5-6H") for t in texts),
                texts)
        ctx["page"] = page
        doc.save()

    def package():
        out = output / "package"
        manifest = P.export_package(ctx["asm"], ctx["page"], str(out), name="M3", retries=1)
        checks["manifest"] = {k: manifest[k] for k in ("source", "outputs", "attempts", "seconds")}
        require(P.verify(str(out / "M3")) == [], "package hashes")
        rdoc = App.newDocument("M3Reimport")
        ImportGui.insert(str(out / "M3" / "M3.step"), rdoc.Name)
        rdoc.recompute()
        solids = [s for i in SI.collect([o for o in rdoc.RootObjects if hasattr(o, "Placement")],
                                        visible_only=False) for s in i.shape.Solids]
        model = [s for i in SI.collect([ctx["asm"]], visible_only=False) for s in i.shape.Solids]
        checks["step_solids"] = {"model": len(model), "exported": len(solids),
                                 "volume_model": round(sum(s.Volume for s in model), 3),
                                 "volume_exported": round(sum(s.Volume for s in solids), 3)}
        require(len(model) == len(solids) and abs(sum(s.Volume for s in model) - sum(s.Volume for s in solids))
                < 1e-3, checks["step_solids"])
        App.closeDocument(rdoc.Name)

    def execute():
        try:
            stage("supplier_step", supplier_step)
            stage("import_and_modify", import_and_modify)
            stage("assemble", assemble)
            stage("interference", interference)
            stage("sheet", sheet)
            stage("package", package)
            report["status"] = "passed"
        except Exception:
            report["error"] = traceback.format_exc()
            log("error", error=report["error"])
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
        if report["status"] == "passed":
            for name in list(App.listDocuments()):
                App.closeDocument(name)  # no save prompt on quit
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
    with tempfile.TemporaryDirectory(prefix="sven-m3-qa-") as scratch:
        profile = Path(scratch)
        for name in ("cache", "data", "tmp"):
            (profile / name).mkdir()
        macro = profile / "M3QA.FCMacro"
        macro.write_text(
            f"import sys\nsys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
            "from m3_checkpoint_qa import run_macro\nrun_macro()\n", encoding="utf-8")
        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8", "HOME": str(profile),
            "TMPDIR": str(profile / "tmp"), "XDG_CACHE_HOME": str(profile / "cache"),
            "FREECAD_USER_HOME": str(profile), "FREECAD_USER_DATA": str(profile / "data"),
            "FREECAD_USER_TEMP": str(profile / "tmp"), "PYTHONDONTWRITEBYTECODE": "1",
            OUTPUT_ENV: str(output),
        }
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
    if report.get("status") == "passed":
        target = output / "package" / "M3"
        out = subprocess.run([PDFTOTEXT, str(target / "M3.pdf"), "-"], capture_output=True, text=True).stdout
        words = out.split()
        with open(target / "M3-bom.csv", encoding="utf-8") as stream:
            rows = list(csv.reader(stream))
        dxf = (target / "M3.dxf").read_text(errors="replace")
        files = {"pdf_callouts": [w for w in words if w.startswith(("M3x0.5-6H", "M4x0.7-6H"))],
                 "pdf_title": "Product" in words,
                 "csv_screw": any("ISO4762-M3x8" in r for r in rows),
                 "dxf_callouts": ("M4x0.7-6H" in dxf and "M3x0.5-6H" in dxf)}
        report["checks"]["files"] = files
        if not (files["pdf_callouts"] and files["pdf_title"] and files["csv_screw"] and files["dxf_callouts"]):
            report["status"] = "failed"
            report["error"] = f"file checks failed: {files}"
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
    if report["status"] != "passed" or code:
        print(report.get("error", f"App exited {code}. See {output / 'freecad.log'}."))
        return 1
    print(f"Passed: {report_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
