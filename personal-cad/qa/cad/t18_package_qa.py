"""Verify the one-step delivery package (T18) on a Mac bundle.

Synthetic assembly: coloured parts, a named state and the T17 sheet. The package is written
with one simulated PDF failure that the retry absorbs; then a run whose DXF keeps failing must
leave the last good package untouched; then a run after a model change replaces it and keeps
the previous one. The STEP is re-imported and compared for units, positions, part count,
colours and key sizes; PDF, DXF and CSV are read outside the app for the title block, the
dimension and the BOM. Optionally a local private model gets the same comparison (counts,
times and memory only)."""

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

OUTPUT_ENV = "PERSONAL_CAD_T18_QA"
CONFIG_ENV = "PERSONAL_CAD_T18_CONFIG"
PDFTOTEXT = "/opt/homebrew/bin/pdftotext"
PACKAGE = "PersonalCAD_ExportPackage"


def run_macro():
    import sys
    import FreeCAD as App
    import FreeCADGui as Gui
    import Part
    from PySide6 import QtCore, QtTest, QtWidgets

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from m2_thread_assembly_qa import keep_awake, screen_state
    dev_paths = [p for p in os.environ.get("PERSONAL_CAD_T18_QA_DEV_PATH", "").split(os.pathsep) if p]
    for path in dev_paths:
        sys.path.insert(0, path)
    if dev_paths:
        for name in ("SvenInterference", "SvenReplace", "SvenReplaceGui", "SvenSheets", "SvenPackage"):
            sys.modules.pop(name, None)
    import SvenReplace as R
    import SvenSheets as S
    import SvenPackage as P
    import SvenDrawing as D
    import SvenInterference as SI

    output = Path(os.environ[OUTPUT_ENV])
    config = json.loads(os.environ[CONFIG_ENV])
    shots = output / "screenshots"
    shots.mkdir(exist_ok=True)
    work = output / "files"
    work.mkdir(exist_ok=True)
    report = {"status": "failed", "version": list(App.Version()), "config": config, "checks": {},
              "module": P.__file__, "app_nap_disabled": keep_awake(), "screen_at_start": screen_state()}
    checks = report["checks"]
    started = time.monotonic()

    def log(step, **data):
        with (output / "progress.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"t": round(time.monotonic() - started, 3), "step": step, **data},
                                    ensure_ascii=False) + "\n")

    def require(condition, message):
        if not condition:
            raise AssertionError(message)

    def rss_mib():
        out = subprocess.run(["/bin/ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True).stdout
        return round(int(out.strip()) / 1024, 1)

    def colour_of(obj):
        if obj.TypeId == "App::Link" and not getattr(obj, "LinkCopyOnChange", None) and \
                not getattr(getattr(obj, "ViewObject", None), "OverrideMaterial", False):
            obj = obj.getLinkedObject(True)
        vo = obj.ViewObject
        if vo is None:
            return None
        try:
            mats = vo.ShapeAppearance
            c = mats[0].DiffuseColor
        except Exception:
            c = vo.ShapeColor
        return tuple(round(v, 3) for v in c[:3])

    COLOURS = {"Housing": (0.8, 0.1, 0.1), "Shaft": (0.5, 0.5, 0.5), "Clip": (0.1, 0.2, 0.9), "Plate": (0.1, 0.6, 0.2)}

    def set_colour(obj, rgb):
        mat = App.Material()
        mat.DiffuseColor = rgb + (1.0,)
        obj.ViewObject.ShapeAppearance = [mat]

    def build():
        import CommandCreateView as V
        doc = App.newDocument("T18Assembly")
        motor = doc.addObject("App::Part", "Motor")
        motor.addProperty("App::PropertyString", "PartNumber", "Base", "Supplier part number")
        motor.PartNumber = "FIXTURE-M"  # fixture value, not a company part number
        for label, shape in (("Housing", Part.makeBox(20, 10, 8)),
                             ("Shaft", Part.makeCylinder(2, 10, App.Vector(10, 5, 8))),
                             ("Clip", Part.makeBox(2, 2, 1, App.Vector(5, 7, 8)))):
            f = doc.addObject("Part::Feature", label)
            f.Shape = shape
            motor.addObject(f)
            set_colour(f, COLOURS[label])
        plate = doc.addObject("Part::Feature", "Plate")
        plate.Shape = Part.makeBox(60, 40, 5)
        set_colour(plate, COLOURS["Plate"])
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
        step = view.newObject("App::FeaturePython", "Lift")
        V.ExplodedViewStep(step)
        V.ViewProviderExplodedViewStep(step.ViewObject)
        step.References = [asm, ["Motor3."]]
        step.MovementTransform = App.Placement(App.Vector(0, 0, 30), App.Rotation())
        step.Visibility = False
        doc.recompute()
        state = R.capture(asm, "Delivery", view)
        doc.saveAs(str(work / "t18-assembly.FCStd"))
        return doc, asm, plate, links, state

    def reimport(path):
        """Import a STEP into a fresh document; returns (doc, solids [(centre, size, colour)])."""
        import ImportGui
        doc = App.newDocument("T18Reimport")
        ImportGui.insert(str(path), doc.Name)
        doc.recompute()
        out = []
        for obj in doc.Objects:
            if obj.TypeId == "App::Link" or not hasattr(obj, "Shape") or not obj.Shape.Solids:
                continue
            if any(p.isDerivedFrom("Part::Feature") for p in obj.InList):
                continue
            if obj.isDerivedFrom("App::Part"):
                continue
            for inst in SI.collect([obj], visible_only=False) if False else [None]:
                pass
            out.append(obj)
        roots = [o for o in doc.RootObjects if hasattr(o, "Placement")]
        instances = SI.collect(roots, visible_only=False)
        solids = []
        for inst in instances:
            colour = colour_of(inst.obj)
            for solid in inst.shape.Solids:
                solids.append(signature(solid, colour))
        return doc, solids

    def signature(solid, colour):
        """Centre of mass and (volume, area): the bounding box of curved faces is not exact."""
        com = solid.CenterOfMass
        return ([round(com.x, 5), round(com.y, 5), round(com.z, 5)],
                [round(solid.Volume, 4), round(solid.Area, 4)], colour, solid.BoundBox)

    def model_solids(asm, intended=None):
        out = []
        for inst in SI.collect([asm], visible_only=False):
            colour = tuple(round(v, 3) for v in intended[inst.obj.getLinkedObject(True).Label]) if intended else colour_of(inst.obj)
            for solid in inst.shape.Solids:
                out.append(signature(solid, colour))
        return out

    def compare(model, exported, tolerance=1e-3, relative=1e-6):
        """Match each model solid to an exported solid at the same place and size.

        relative: allowed relative change of volume and area. STEP writes curved faces as
        B-splines, so on a real model the integrated volume moves by up to a few 1e-4."""
        free = list(exported)
        unmatched, colour_mismatch, worst, mismatches = [], 0, 0.0, []
        worst_relative = 0.0
        for centre, size, colour, _box in model:
            best = None
            for i, (c2, s2, col2, _b2) in enumerate(free):
                d = max(abs(a - b) for a, b in zip(centre, c2))
                if d <= tolerance and all(abs(a - b) <= max(tolerance, relative * abs(a)) for a, b in zip(size, s2)):
                    best = (d, i)
                    worst_relative = max([worst_relative] + [abs(a - b) / max(abs(a), 1e-12) for a, b in zip(size, s2)])
                    break
            if best is None:
                unmatched.append(centre)
                continue
            d, i = best
            worst = max(worst, d)
            c2, s2, col2, _b2 = free.pop(i)
            if colour is not None and col2 is not None and max(abs(a - b) for a, b in zip(colour, col2)) > 0.01:
                colour_mismatch += 1
                mismatches.append((colour, col2))
        return {"model": len(model), "exported": len(exported), "unmatched": len(unmatched),
                "unmatched_sample": unmatched[:3], "extra_sample": [f[0] for f in free[:3]],
                "colour_mismatch_sample": mismatches[:3],
                "extra": len(free), "colour_mismatch": colour_mismatch, "worst_offset": worst,
                "model_colours": len({m[2] for m in model}), "exported_colours": len({e[2] for e in exported}),
                "tolerance_mm": tolerance, "relative_size_tolerance": relative,
                "worst_relative_size_change": worst_relative}

    def files_state(folder):
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(folder).iterdir())}

    def wait_page(page):
        import SvenDrawingGui
        return SvenDrawingGui.wait_for_views(page)

    def synthetic():
        doc, asm, plate, links, state = build()
        made = S.make_sheet(asm, state)
        page = made["page"]
        require(wait_page(page), "views")
        doc.save()
        out = output / "packages"
        target = out / "T18"
        attempts = []
        t0 = time.monotonic()
        manifest = P.export_package(asm, page, str(out), name="T18", retries=1, fail={"pdf": 1},
                                    progress=lambda kind, n: attempts.append((kind, n)))
        checks["first_package_s"] = round(time.monotonic() - t0, 2)
        checks["first_attempts"] = manifest["attempts"]
        require(manifest["attempts"]["pdf"][0].startswith("pdf: simulated failure") and
                manifest["attempts"]["pdf"][-1] == "ok", manifest["attempts"])
        require(sorted(o["kind"] for o in manifest["outputs"]) == ["csv", "dxf", "pdf", "step"], manifest)
        require(P.verify(str(target)) == [], "manifest hashes")
        require(not [p for p in out.iterdir() if "staging" in p.name], "staging left")
        good = files_state(target)
        checks["first_manifest"] = {k: manifest[k] for k in ("package", "units", "source", "outputs", "seconds")}

        # content of the first package
        step_path = target / "T18.step"
        head = step_path.read_text(errors="replace")[:20000]
        checks["step_units_mm"] = "SI_UNIT(.MILLI.,.METRE.)" in head.replace(" ", "")
        require(checks["step_units_mm"], "STEP is not in millimetres")
        rdoc, exported = reimport(step_path)
        comparison = compare(model_solids(asm, COLOURS), exported)
        checks["plate_viewobject_colour"] = colour_of(plate)
        checks["step_comparison"] = comparison
        require(comparison["unmatched"] == 0 and comparison["extra"] == 0 and comparison["model"] == 10
                and comparison["colour_mismatch"] == 0 and comparison["exported_colours"] == 4, comparison)
        App.closeDocument(rdoc.Name)
        Gui.getMainWindow().grab().save(str(shots / "after-package.png"))

        # a run whose DXF keeps failing leaves the good package as it was
        plate.Shape = Part.makeBox(70, 40, 5)
        doc.recompute()
        require(wait_page(page), "views after change")
        try:
            P.export_package(asm, page, str(out), name="T18", retries=1, fail={"dxf": 5})
            failed = None
        except P.PackageError as error:
            failed = str(error)
        checks["failed_run"] = failed
        require(failed and "dxf failed after 2 attempts" in failed, failed)
        require(files_state(target) == good, "last good package changed by a failed run")
        require(not [p for p in out.iterdir() if "staging" in p.name], "staging left after failure")

        # a good run after the change replaces it and keeps the previous one
        doc.save()
        manifest2 = P.export_package(asm, page, str(out), name="T18", retries=1)
        require((out / "T18.previous").is_dir() and files_state(out / "T18.previous") == good, "previous kept")
        require(manifest2["source"]["sha256"] != manifest["source"]["sha256"], "revision did not change")
        checks["second_manifest_source"] = manifest2["source"]
        rdoc, exported2 = reimport(target / "T18.step")
        boxes = sorted((e[3] for e in exported2), key=lambda b: -b.XLength)
        plate_size = [round(boxes[0].XLength, 4), round(boxes[0].YLength, 4), round(boxes[0].ZLength, 4)]
        checks["plate_after_change"] = plate_size
        require(plate_size == [70.0, 40.0, 5.0], plate_size)
        App.closeDocument(rdoc.Name)
        checks["package_dir"] = str(target)
        return doc

    def private(path, budget):
        """Real-scale package. Hidden line removal of full views of this model takes longer than half
        an hour (run m3final-8b68639-t18), so the sheet here carries the title block and the BOM;
        the views, dimension and section are covered by the synthetic assembly."""
        sha_before = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        info = {}
        checks["private"] = info  # partial results stay in the report if a later step fails
        t0 = time.monotonic()
        doc = App.openDocument(path)
        info["open_s"] = round(time.monotonic() - t0, 2)
        info["rss_after_open_mib"] = rss_mib()
        assemblies = [o for o in doc.Objects if o.isDerivedFrom("Assembly::AssemblyObject")]
        roots = assemblies[:1] or [o for o in doc.RootObjects if o.isDerivedFrom("App::Part")][:1]
        require(roots, "no assembly in the private model")
        asm = roots[0]
        t1 = time.monotonic()
        model = model_solids(asm)
        info["model_solids"] = len(model)
        info["collect_s"] = round(time.monotonic() - t1, 2)
        t2 = time.monotonic()
        page = doc.addObject("TechDraw::DrawPage", "PrivateSheet")
        template = doc.addObject("TechDraw::DrawSVGTemplate", "PrivateSheetTemplate")
        template.Template = S.template_path()
        page.Template = template
        S._fill_template(template, {"title": "Private model", "scale": "1:1", "sheet_number": "1/1"})
        bom = D.make_bom(asm)
        D.place_bom(page, bom, right=float(template.Width) - 15.0, top=float(template.Height) - 15.0)
        doc.recompute()
        rows = D.bom_rows(bom)
        at = list(bom.columnsNames).index(".PartNumber")
        bom_numbers = sorted({r[at] for r in rows if r[at]})
        info["sheet_s"] = round(time.monotonic() - t2, 2)
        info["bom_rows"] = len(rows)
        out = output / "private-package"
        t3 = time.monotonic()
        manifest = P.export_package(asm, page, str(out), name="private", retries=1, fail={"csv": 1})
        info["package_s"] = round(time.monotonic() - t3, 2)
        info["rss_after_package_mib"] = rss_mib()
        info["attempts"] = {k: len(v) for k, v in manifest["attempts"].items()}
        info["outputs"] = {o["kind"]: o["bytes"] for o in manifest["outputs"]}
        target = out / "private"
        (work / "private-bom-numbers.json").write_text(json.dumps(bom_numbers))  # local only, read by main()
        head = (target / "private.step").read_text(errors="replace")[:20000]
        info["step_units_mm"] = "SI_UNIT(.MILLI.,.METRE.)" in head.replace(" ", "")
        t4 = time.monotonic()
        rdoc, exported = reimport(target / "private.step")
        info["reimport_s"] = round(time.monotonic() - t4, 2)
        t5 = time.monotonic()
        info["comparison"] = compare(model, exported, tolerance=1e-2, relative=1e-3)
        info["compare_s"] = round(time.monotonic() - t5, 2)
        for key in ("unmatched_sample", "extra_sample", "colour_mismatch_sample"):
            info["comparison"].pop(key, None)  # coordinates of the private model stay out of the report
        App.closeDocument(rdoc.Name)
        App.closeDocument(doc.Name)  # never saved
        info["private_model_unchanged"] = hashlib.sha256(Path(path).read_bytes()).hexdigest() == sha_before
        c = info["comparison"]
        require(info["step_units_mm"] and c["unmatched"] == 0 and c["extra"] == 0 and c["colour_mismatch"] == 0
                and info["attempts"].get("csv") == 2, ("private comparison", c, info["attempts"]))
        return info

    def execute():
        try:
            log("start", name="synthetic")
            doc = synthetic()
            App.closeDocument(doc.Name)
            log("done", name="synthetic", rss_mib=rss_mib())
            if config.get("private"):
                log("start", name="private")
                private(config["private"], config.get("private_budget", 900))
                log("done", name="private", rss_mib=rss_mib())
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

def pdf_text(path):
    out = subprocess.run([PDFTOTEXT, "-bbox", str(path), "-"], capture_output=True, text=True, check=True).stdout
    page = re.search(r'<page width="([\d.]+)" height="([\d.]+)"', out)
    words = [html.unescape(m.group(1)) for m in re.finditer(r"<word [^>]*>([^<]*)</word>", out)]
    return words, (float(page.group(1)) * 25.4 / 72, float(page.group(2)) * 25.4 / 72)


def dxf_texts(path):
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    texts, kind = [], None
    for i in range(0, len(lines) - 1, 2):
        code, value = lines[i].strip(), lines[i + 1]
        if code == "0":
            kind = value.strip()
        elif code == "1" and kind in ("TEXT", "MTEXT"):
            texts.append(value.strip())
    return texts


def check_files(target, expect):
    out = {}
    words, size = pdf_text(target / f"{expect['name']}.pdf")
    out["pdf_page_mm"] = [round(v, 1) for v in size]
    out["pdf_has"] = {w: w in words for w in expect["words"]}
    texts = dxf_texts(target / f"{expect['name']}.dxf")
    out["dxf_has"] = {w: any(w == t or w in t.split() for t in texts) for w in expect["words"]}
    with open(target / f"{expect['name']}-bom.csv", encoding="utf-8") as stream:
        rows = list(csv.reader(stream))
    out["csv_rows"] = rows
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--private", type=Path)
    parser.add_argument("--private-budget", type=float, default=900.0)
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()
    app = args.app.resolve()
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    executable = app / "Contents/MacOS" / info["CFBundleExecutable"]
    output = args.output.resolve()
    output.mkdir(parents=True)
    signature = hashlib.sha256((app / "Contents/_CodeSignature/CodeResources").read_bytes()).hexdigest()
    config = {"private_budget": args.private_budget}
    if args.private:
        config["private"] = str(args.private.resolve())
    with tempfile.TemporaryDirectory(prefix="sven-t18-qa-") as scratch:
        profile = Path(scratch)
        for name in ("cache", "data", "tmp"):
            (profile / name).mkdir()
        macro = profile / "T18QA.FCMacro"
        macro.write_text(
            f"import sys\nsys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
            "from t18_package_qa import run_macro\nrun_macro()\n", encoding="utf-8")
        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8", "HOME": str(profile),
            "TMPDIR": str(profile / "tmp"), "XDG_CACHE_HOME": str(profile / "cache"),
            "FREECAD_USER_HOME": str(profile), "FREECAD_USER_DATA": str(profile / "data"),
            "FREECAD_USER_TEMP": str(profile / "tmp"), "PYTHONDONTWRITEBYTECODE": "1",
            OUTPUT_ENV: str(output), CONFIG_ENV: json.dumps(config),
        }
        if os.environ.get("PERSONAL_CAD_T18_QA_DEV_PATH"):
            env["PERSONAL_CAD_T18_QA_DEV_PATH"] = os.environ["PERSONAL_CAD_T18_QA_DEV_PATH"]
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
        try:
            files = check_files(output / "packages" / "T18",
                                {"name": "T18", "words": ["Product", "Delivery", "FIXTURE-M", "70", "1:1"]})
            report["checks"]["files"] = files
            # TechDraw's DXF writer leaves out the SVG template, so the title block is checked in the PDF only.
            ok = (all(files["pdf_has"].values()) and files["dxf_has"]["FIXTURE-M"] and files["dxf_has"]["70"]
                  and abs(files["pdf_page_mm"][0] - 420) < 1 and abs(files["pdf_page_mm"][1] - 297) < 1
                  and files["csv_rows"][0][:2] == ["Item", "Name"]
                  and any(r[2] == "FIXTURE-M" and r[5] == "3" for r in files["csv_rows"][1:]))
            if args.private:
                numbers = json.loads((output / "files" / "private-bom-numbers.json").read_text())
                target = output / "private-package" / "private"
                pfiles = check_files(target, {"name": "private", "words": ["Private", "1:1"]})
                words, _size = pdf_text(target / "private.pdf")
                pdf_joined = " ".join(words)
                dxf_joined = "\n".join(dxf_texts(target / "private.dxf"))
                priv = report["checks"].get("private", {})
                at = pfiles["csv_rows"][0].index("Part Number")
                csv_numbers = sorted({r[at] for r in pfiles["csv_rows"][1:] if len(r) > at and r[at]})
                pinfo = {"pdf_page_mm": pfiles["pdf_page_mm"], "csv_rows": len(pfiles["csv_rows"]) - 1,
                         "bom_rows": priv.get("bom_rows"), "part_numbers": len(numbers),
                         "csv_part_numbers_match": csv_numbers == numbers,
                         "pdf_title": pfiles["pdf_has"]["Private"] and pfiles["pdf_has"]["1:1"],
                         "pdf_part_numbers_found": sum(n in pdf_joined for n in numbers),
                         "dxf_part_numbers_found": sum(n in dxf_joined for n in numbers)}
                report["checks"]["private_files"] = pinfo
                ok = ok and (pinfo["csv_rows"] == pinfo["bom_rows"] and pinfo["csv_part_numbers_match"]
                             and pinfo["pdf_title"] and pinfo["pdf_part_numbers_found"] == len(numbers)
                             and abs(pfiles["pdf_page_mm"][0] - 420) < 1)
            if not ok:
                report["status"] = "failed"
                report["error"] = "file checks failed"
        except Exception:
            report["status"] = "failed"
            report["error"] = traceback.format_exc()
    if args.private:
        priv = report.get("checks", {}).get("private", {})
        if priv and not priv.get("private_model_unchanged"):
            report["status"] = "failed"
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
    if report["status"] != "passed" or code:
        print(report.get("error", f"App exited {code}. See {output / 'freecad.log'}."))
        return 1
    print(f"Passed: {report_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
