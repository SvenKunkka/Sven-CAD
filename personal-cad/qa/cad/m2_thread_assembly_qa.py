"""Measure a large assembly with modeled threads, fasteners, a BOM and a drawing on a Mac bundle.

The synthetic run builds linked threaded modules and fasteners. With --carbon the same set is added
to a local native copy of a private model, which stays on this Mac; the report holds counts, times
and memory only, no names or geometry of the private model.
"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import threading
import time
import traceback


OUTPUT_ENV = "PERSONAL_CAD_M2_QA"
CONFIG_ENV = "PERSONAL_CAD_M2_QA_CONFIG"
# size, tap drill, shaft diameter, screw, nut
VARIANTS = (
    ("M2x0.4", 1.6, 2.0, "ISO4762-M2x8", "ISO4032-M2"),
    ("M2.5x0.45", 2.05, 2.5, "ISO4762-M2.5x8", "ISO4032-M2.5"),
    ("M3x0.5", 2.5, 3.0, "ISO4762-M3x8", "ISO4032-M3"),
    ("M4x0.7", 3.3, 4.0, "ISO4762-M4x8", "ISO4032-M4"),
)
PLATE = (64.0, 40.0, 8.0)
BORES = [(8.0 + 16.0 * i, 8.0 + 24.0 * j) for j in range(2) for i in range(4)]   # 8 through bores
HOLES = [(12.0 + 16.0 * i, 20.0) for i in range(3)]                               # 3 native hole positions
SHAFT_AT = (60.0, 20.0)
SPACING = (90.0, 60.0)


_ACTIVITY = []


def keep_awake():
    """Hold an NSProcessInfo activity so App Nap does not throttle a run while the display is off.

    With the screen locked and the display asleep, macOS napped the app: GUI stages ran about five
    times slower and hidden line removal moved to slow cores. Returns True when the activity is held."""
    import ctypes
    import ctypes.util
    objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    send = objc.objc_msgSend
    send.restype = ctypes.c_void_p
    send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    info = send(objc.objc_getClass(b"NSProcessInfo"), objc.sel_registerName(b"processInfo"))
    make = ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                                           ctypes.c_char_p))
    reason = make(objc.objc_getClass(b"NSString"), objc.sel_registerName(b"stringWithUTF8String:"), b"M2 QA")
    begin = ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                                            ctypes.c_uint64, ctypes.c_void_p))
    # NSActivityUserInitiated | NSActivityLatencyCritical
    token = begin(info, objc.sel_registerName(b"beginActivityWithOptions:reason:"), 0x00FFFFFF | 0xFF00000000, reason)
    if token:
        send(token, objc.sel_registerName(b"retain"))
        _ACTIVITY.append(token)
    return bool(token)


def screen_state():
    out = subprocess.run(["/usr/sbin/ioreg", "-n", "Root", "-d1", "-a"], capture_output=True, text=True).stdout
    try:
        root = plistlib.loads(out.encode())
        sessions = root.get("IOConsoleUsers", [])
        return {"screen_locked": any(s.get("CGSSessionScreenIsLocked", False) for s in sessions)}
    except Exception as error:
        return {"error": str(error)}


def run_macro():
    import resource
    import sys
    import FreeCAD as App
    import FreeCADGui as Gui
    import Part
    from PySide6 import QtTest

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from assembly_performance_qa import interaction, percentile95
    import SvenDrawing as D
    import SvenFasteners as F

    output = Path(os.environ[OUTPUT_ENV])
    config = json.loads(os.environ[CONFIG_ENV])
    report = {"status": "failed", "version": list(App.Version()), "config": config, "stages": {}, "checks": {}}
    stages = report["stages"]
    checks = report["checks"]
    started = time.monotonic()
    report["app_nap_disabled"] = keep_awake()
    report["screen_at_start"] = screen_state()

    def rss_mib():
        out = subprocess.run(["/bin/ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True).stdout
        return round(int(out.strip()) / 1024, 1)

    def peak_mib():
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20, 1)

    def log(step, **data):
        with (output / "progress.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"t": round(time.monotonic() - started, 3), "step": step,
                                     "rss_mib": rss_mib(), **data}, default=str) + "\n")

    def require(condition, message):
        if not condition:
            raise AssertionError(message)

    def stage(name, fn):
        log(name + " start")
        t0 = time.monotonic()
        result = fn()
        Gui.updateGui()
        seconds = round(time.monotonic() - t0, 3)
        stages[name] = {"seconds": seconds, "rss_mib": rss_mib(), "peak_rss_mib": peak_mib()}
        log(name, seconds=seconds)
        return result

    def wait_views(page, timeout):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if not any(v.isDerivedFrom("TechDraw::DrawViewPart") and v.waitingForResult() for v in page.getAllViews()):
                return round(time.monotonic() - t0, 3)
            QtTest.QTest.qWait(100)
        raise AssertionError(f"Drawing views still computing after {timeout} s")

    def recompute_all(doc, objs):
        for obj in objs:
            obj.touch()
        doc.recompute()
        bad = [o.Name for o in objs if not o.isValid()]
        require(not bad, ("invalid after recompute", bad[:10]))

    # ---------- threaded modules ----------
    def plate_shape(drill):
        shape = Part.makeBox(*PLATE)
        for x, y in BORES:
            shape = shape.cut(Part.makeCylinder(drill / 2, PLATE[2] + 2, App.Vector(x, y, -1)))
        return shape

    def bore_face(feature, x, y, radius):
        for i, face in enumerate(feature.Shape.Faces, 1):
            surf = face.Surface
            if isinstance(surf, Part.Cylinder) and abs(surf.Radius - radius) < 1e-6:
                local = feature.Placement.inverse().multVec(surf.Center)
                if abs(local.x - x) < 1e-6 and abs(local.y - y) < 1e-6:
                    return f"Face{i}"
        raise AssertionError(f"No bore at {x},{y}")

    def build_module(doc, size, drill, shaft_d):
        tag = size.split("x")[0].replace(".", "_")
        part = doc.addObject("App::Part", f"Module_{tag}")
        plate = doc.addObject("Part::Feature", f"Plate_{tag}")
        plate.Shape = plate_shape(drill)
        part.addObject(plate)
        doc.recompute()
        support, internal = plate, []
        for x, y in BORES:
            tap = doc.addObject("PartDesign::InternalThread", f"Tap_{tag}")
            part.addObject(tap)
            tap.Support = (support, [bore_face(support, x, y, drill / 2)])
            tap.ThreadSize = size
            tap.ThreadLength = 6.0
            tap.Modeled = False
            doc.recompute()
            require(tap.isValid(), f"{tap.Name} invalid")
            support.Visibility = False
            internal.append(tap)
            support = tap

        body = doc.addObject("PartDesign::Body", f"Boss_{tag}")
        part.addObject(body)
        body.Placement = App.Placement(App.Vector(0, 44, 0), App.Rotation())
        sketch = body.newObject("Sketcher::SketchObject", f"BossSketch_{tag}")
        sketch.AttachmentSupport = [(body.Origin.OriginFeatures[3], "")]
        sketch.MapMode = "FlatFace"
        pts = [App.Vector(0, 0, 0), App.Vector(48, 0, 0), App.Vector(48, 12, 0), App.Vector(0, 12, 0)]
        for i in range(4):
            sketch.addGeometry(Part.LineSegment(pts[i], pts[(i + 1) % 4]))
        pad = body.newObject("PartDesign::Pad", f"BossPad_{tag}")
        pad.Profile = sketch
        pad.Length = 8
        doc.recompute()
        top = next(f"Face{i}" for i, f in enumerate(pad.Shape.Faces, 1)
                   if abs(f.CenterOfMass.z - 8) < 1e-6 and abs(f.normalAt(0, 0).z - 1) < 1e-6)
        hs = body.newObject("Sketcher::SketchObject", f"HoleSketch_{tag}")
        hs.AttachmentSupport = [(pad, top)]
        hs.MapMode = "FlatFace"
        doc.recompute()
        inverse = hs.Placement.inverse()
        for x, _ in HOLES:
            local = inverse.multVec(App.Vector(x, 6, 8))
            hs.addGeometry(Part.Circle(App.Vector(local.x, local.y, 0), App.Vector(0, 0, 1), drill / 2))
        doc.recompute()
        hole = body.newObject("PartDesign::Hole", f"Hole_{tag}")
        hole.Profile = hs
        hole.Threaded = True
        hole.ThreadType = "ISOMetricProfile"
        hole.ThreadSize = size
        hole.ThreadClass = "6H"
        hole.DepthType = "Dimension"
        hole.Depth = 7
        hole.ThreadDepthType = "Dimension"
        hole.ThreadDepth = 6
        doc.recompute()
        require(hole.isValid() and len(hole.ThreadFrames) == len(HOLES), f"{hole.Name} invalid")

        shaft = doc.addObject("Part::Feature", f"Shaft_{tag}")
        shaft.Shape = Part.makeCylinder(shaft_d / 2, 14, App.Vector(SHAFT_AT[0] + 10, SHAFT_AT[1], 0))
        part.addObject(shaft)
        doc.recompute()
        face = next(f"Face{i}" for i, f in enumerate(shaft.Shape.Faces, 1) if isinstance(f.Surface, Part.Cylinder))
        ext = doc.addObject("PartDesign::ExternalThread", f"ShaftThread_{tag}")
        part.addObject(ext)
        ext.Support = (shaft, [face])
        ext.ThreadSize = size
        ext.ThreadLength = 10
        ext.ManufacturingClass = "6g"
        ext.Modeled = False
        doc.recompute()
        require(ext.isValid(), f"{ext.Name} invalid")
        shaft.Visibility = False
        return {"part": part, "internal": internal, "hole": hole, "external": ext, "size": size}

    def thread_points():
        """Fastener spots in module coordinates: bore tops, hole tops, shaft end."""
        screws = [App.Vector(x, y, PLATE[2]) for x, y in BORES] + [App.Vector(x, 50, 8) for x, _ in HOLES]
        nut = App.Vector(SHAFT_AT[0] + 10, SHAFT_AT[1], 14)
        return screws, nut

    def build_set(doc, product, links_per_variant, origin):
        modules = [build_module(doc, *v[:3]) for v in VARIANTS]
        library = doc.addObject("App::DocumentObjectGroup", "M2ModuleLibrary")
        for m in modules:
            library.addObject(m["part"])
            m["part"].Visibility = False
        screws_at, nut_at = thread_points()
        instances = []
        fastener_links = []
        for vi, (m, variant) in enumerate(zip(modules, VARIANTS)):
            screw_places, nut_places = [], []
            for k in range(links_per_variant):
                link = doc.addObject("App::Link", f"Unit_{m['part'].Name}")
                link.setLink(m["part"])
                link.Placement = App.Placement(
                    origin + App.Vector(k * SPACING[0], vi * SPACING[1], 0), App.Rotation())
                product.addObject(link)
                instances.append(link)
                screw_places += [App.Placement(link.Placement.multVec(p), App.Rotation()) for p in screws_at]
                nut_places.append(App.Placement(link.Placement.multVec(nut_at), App.Rotation()))
            fastener_links += F.insert(doc, F.find(variant[3]), screw_places, product)[0]
            fastener_links += F.insert(doc, F.find(variant[4]), nut_places, product)[0]
        doc.recompute()
        return modules, instances, fastener_links

    def threads_of(modules):
        return [t for m in modules for t in m["internal"] + [m["external"]]], [m["hole"] for m in modules]

    def set_modeled(modules, modeled):
        threads, holes = threads_of(modules)
        for t in threads:
            t.Modeled = modeled
        for h in holes:
            h.ModelThread = modeled

    def page_for(doc, sources, scale, page_name, view_name):
        page = doc.addObject("TechDraw::DrawPage", page_name)
        template = doc.addObject("TechDraw::DrawSVGTemplate", page_name + "Template")
        template.Template = App.getResourceDir() + "Mod/TechDraw/Templates/ISO/A3_Landscape_ISO5457_minimal.svg"
        page.Template = template
        view = doc.addObject("TechDraw::DrawViewPart", view_name)
        page.addView(view)
        view.Source = sources
        view.Direction = App.Vector(0, 0, 1)
        view.XDirection = App.Vector(1, 0, 0)
        view.ScaleType = "Custom"
        view.Scale = scale
        view.X, view.Y = 150, 150
        return page, view

    def counts(doc):
        shapes = [o for o in doc.Objects if o.isDerivedFrom("Part::Feature")]
        return {"objects": len(doc.Objects), "part_features": len(shapes),
                "links": sum(1 for o in doc.Objects if o.TypeId == "App::Link")}

    try:
        log("start", peak_mib=peak_mib())
        carbon = config.get("carbon")
        if carbon:
            before = config["carbon_sha256"]
            doc = stage("open_private", lambda: App.openDocument(carbon))
            roots = [o for o in doc.RootObjects if o.TypeId in ("App::Part", "Assembly::AssemblyObject")
                     and o.Visibility]
            require(roots, "private model has no visible part root")
            report["private_model"] = {"root_containers": len(roots), **counts(doc)}
            product = doc.addObject("Assembly::AssemblyObject", "M2Product")
            for root in roots:
                link = doc.addObject("App::Link", "PrivateModel")
                link.setLink(root)
                product.addObject(link)
                root.Visibility = False
            box = App.BoundBox()
            for root in roots:
                box.add(Part.getShape(root).BoundBox)
            # The threaded set sits beside the private model, clear of it.
            origin = App.Vector(box.XMin, box.YMax + 40, 0)
        else:
            doc = App.newDocument("M2Large")
            product = doc.addObject("Assembly::AssemblyObject", "M2Product")
            origin = App.Vector(0, 0, 0)
        doc.UndoMode = 0
        modules, instances, fasteners = stage("build_schematic",
                                              lambda: build_set(doc, product, config["links_per_variant"], origin))
        threads, holes = threads_of(modules)
        thread_instances = len(instances) * (len(BORES) + len(HOLES) + 1)
        report["model"] = {"modules": len(modules), "module_links": len(instances),
                           "thread_features": len(threads) + len(holes),
                           "threads_in_assembly": thread_instances, "fastener_links": len(fasteners),
                           "fastener_sources": len(F.sources(doc)), **counts(doc)}
        require(len(F.sources(doc)) == 2 * len(VARIANTS), "fasteners must share one source per part number")
        gui_doc = Gui.getDocument(doc.Name)
        Gui.SendMsgToActiveView("ViewFit")
        Gui.updateGui()

        stage("recompute_schematic", lambda: recompute_all(doc, threads + holes))
        if not carbon:
            report["interaction_schematic"] = interaction(doc, config["repetitions"], None)

        set_modeled(modules, True)
        stage("recompute_modeled", lambda: (doc.recompute(), require(all(o.isValid() for o in threads + holes),
                                                                       "modeled threads invalid")))
        def show():
            Gui.SendMsgToActiveView("ViewFit")
            for _ in range(3):
                Gui.updateGui()
                QtTest.QTest.qWait(200)
        stage("display_modeled", show)
        modeled_faces = sum(len(t.Shape.Faces) for t in threads + holes)
        checks["modeled_faces_in_features"] = modeled_faces
        report["interaction_modeled"] = interaction(doc, config["repetitions"], None)

        bom = stage("bom", lambda: (D.make_bom(product)))
        headings, rows = D.bom_table(bom)
        pn, qty = headings.index("Part Number"), headings.index("Qty")
        fastener_rows = {r[pn]: int(r[qty]) for r in rows if r[pn]}
        expected = {r["part_number"]: r["count"] for r in F.summary(doc)}
        require(fastener_rows == expected, (fastener_rows, expected))
        per_variant = config["links_per_variant"]
        for v in VARIANTS:
            require(expected.get(v[3]) == per_variant * (len(BORES) + len(HOLES)), (v[3], expected.get(v[3])))
            require(expected.get(v[4]) == per_variant, (v[4], expected.get(v[4])))
        checks["bom"] = {"rows": len(rows), "fasteners": fastener_rows}

        # Drawing of the whole threaded set with schematic threads: callouts, BOM and export.
        set_modeled(modules, False)
        doc.recompute()
        page, view = page_for(doc, instances, config["drawing_scale"], "M2Drawing", "M2Top")

        def project(pg):
            doc.recompute()
            return wait_views(pg, config["drawing_timeout"])
        stage("drawing_schematic", lambda: project(page))
        checks["drawing_edges_schematic"] = len(view.getVisibleEdges())
        if config.get("profile"):
            import cProfile
            import io
            import pstats
            profiler = cProfile.Profile()
            profiler.enable()
        stage("callouts", lambda: (D.add_callouts(view), doc.recompute()))
        if config.get("profile"):
            profiler.disable()
            text = io.StringIO()
            pstats.Stats(profiler, stream=text).sort_stats("cumulative").print_stats(30)
            (output / "callouts-profile.txt").write_text(text.getvalue(), encoding="utf-8")
        calls = D.callouts(view)
        starts = {tuple(round(c, 4) for c in c_.Proxy.spec(c_)["start"]) for c_ in calls}
        keys = {(c_.ThreadFeature.Name, c_.HoleIndex, c_.InstancePath) for c_ in calls}
        checks["callouts"] = {"count": len(calls), "distinct_threads": len(keys), "distinct_starts": len(starts)}
        require(len(calls) == thread_instances == len(keys) == len(starts), checks["callouts"])
        stage("bom_table", lambda: (D.place_bom(page, bom), doc.recompute()))
        require(D.bom_grid(page) == [headings] + rows, "printed BOM differs from the BOM")

        # A page that was never opened must not crash the PDF writer.
        import TechDrawGui
        try:
            TechDrawGui.exportPageAsPdf(page, str(output / "unopened.pdf"))
            checks["unopened_page_pdf"] = "written"
        except Exception as error:
            checks["unopened_page_pdf"] = f"refused: {error}"
        exported = stage("drawing_export", lambda: __import__("SvenDrawingGui").export_drawing(page, str(output / "m2.pdf")))
        require(len(exported) == 3 and all(Path(f).stat().st_size > 0 for f in exported), exported)
        checks["export_bytes"] = {Path(f).suffix: Path(f).stat().st_size for f in exported}
        page.KeepUpdated = False

        # Modeled threads on a sample page: one instance of every module.
        sample_page, sample = page_for(doc, [instances[i * per_variant] for i in range(len(VARIANTS))],
                                       config["drawing_scale"] * 2, "M2Sample", "M2SampleTop")
        stage("sample_schematic", lambda: project(sample_page))
        checks["sample_edges_schematic"] = len(sample.getVisibleEdges())
        set_modeled(modules, True)
        stage("recompute_modeled_again", lambda: doc.recompute())
        stage("sample_modeled", lambda: project(sample_page))
        checks["sample_edges_modeled"] = len(sample.getVisibleEdges())
        stage("sample_export", lambda: __import__("SvenDrawingGui").export_drawing(sample_page,
                                                                                    str(output / "m2-sample.pdf")))

        def busy_views():
            return sorted(v.Name for v in doc.Objects
                          if v.isDerivedFrom("TechDraw::DrawViewPart") and v.waitingForResult())
        checks["touched_after_sample_export"] = sorted(o.Name for o in doc.Objects if "Touched" in o.State)[:20]
        checks["busy_views_after_sample_export"] = busy_views()
        # Exporting must not start another hidden line removal in the background.
        require(not checks["busy_views_after_sample_export"], ("views recomputing after export",
                                                                checks["busy_views_after_sample_export"]))
        path = output / "m2-assembly.FCStd"
        stage("save", lambda: doc.saveAs(str(path)))
        checks["busy_views_before_close"] = busy_views()
        report["fcstd_bytes"] = path.stat().st_size
        name = doc.Name
        stage("close", lambda: App.closeDocument(name))
        doc = stage("reopen", lambda: App.openDocument(str(path)))
        touched = [o.Name for o in doc.Objects if "Touched" in o.State]
        checks["touched_after_reopen"] = len(touched)
        stage("recompute_after_reopen", lambda: doc.recompute())
        page = doc.getObject("M2Drawing")
        checks["callouts_after_reopen"] = len(D.callouts(doc.getObject("M2Top")))
        require(checks["callouts_after_reopen"] == len(calls), "callouts lost on reopen")
        bom = D.page_bom(page)
        require(bom is not None and D.bom_table(bom)[1] == rows, "BOM changed on reopen")
        if carbon:
            sha = hashlib.sha256(Path(carbon).read_bytes()).hexdigest()
            require(sha == before, "private model file changed")
            checks["private_model_unchanged"] = True
        report["peak_rss_mib"] = peak_mib()
        report["status"] = "passed"
    except Exception:
        report["error"] = traceback.format_exc()
        log("error", error=report["error"])
    report["elapsed_s"] = round(time.monotonic() - started, 3)
    (output / "report.json").write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    App.Console.PrintMessage("M2_QA_DONE\n")
    import os as _os
    _os._exit(0 if report["status"] == "passed" else 1)


def sample_rss(pid_holder, stop, samples):
    while not stop.is_set():
        pid = pid_holder.get("pid")
        if pid:
            out = subprocess.run(["/bin/ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout
            if out.strip():
                samples.append((time.monotonic(), int(out.strip()) / 1024))
        stop.wait(0.5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--links-per-variant", type=int, default=12)
    parser.add_argument("--carbon", type=Path, help="Local native FCStd of a private model, opened read-only")
    parser.add_argument("--repetitions", type=int, default=20)
    parser.add_argument("--drawing-scale", type=float, default=0.25)
    parser.add_argument("--drawing-timeout", type=int, default=900)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--profile", action="store_true", help="Profile the callout step")
    args = parser.parse_args()
    app = args.app.resolve()
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    executable = app / "Contents/MacOS" / info["CFBundleExecutable"]
    output = args.output.resolve()
    output.mkdir(parents=True)
    signature = hashlib.sha256((app / "Contents/_CodeSignature/CodeResources").read_bytes()).hexdigest()
    config = {"links_per_variant": args.links_per_variant, "repetitions": args.repetitions,
              "drawing_scale": args.drawing_scale, "drawing_timeout": args.drawing_timeout, "profile": args.profile}
    if args.carbon:
        carbon = args.carbon.resolve()
        config["carbon"] = str(carbon)
        config["carbon_sha256"] = hashlib.sha256(carbon.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="sven-m2-qa-") as scratch:
        profile = Path(scratch)
        for name in ("cache", "data", "tmp"):
            (profile / name).mkdir()
        macro = profile / "M2QA.FCMacro"
        macro.write_text(
            f"import sys\nsys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
            "from m2_thread_assembly_qa import run_macro\nrun_macro()\n", encoding="utf-8")
        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8", "HOME": str(profile),
            "TMPDIR": str(profile / "tmp"), "XDG_CACHE_HOME": str(profile / "cache"),
            "FREECAD_USER_HOME": str(profile), "FREECAD_USER_DATA": str(profile / "data"),
            "FREECAD_USER_TEMP": str(profile / "tmp"), "PYTHONDONTWRITEBYTECODE": "1",
            OUTPUT_ENV: str(output), CONFIG_ENV: json.dumps(config),
        }
        if os.environ.get("SVEN_CAD_TRACE_HLR"):
            env["SVEN_CAD_TRACE_HLR"] = "1"
        holder, stop, samples = {}, threading.Event(), []
        sampler = threading.Thread(target=sample_rss, args=(holder, stop, samples), daemon=True)
        sampler.start()
        started = time.monotonic()
        with (output / "freecad.log").open("wb") as log:
            proc = subprocess.Popen(
                [str(executable), "--user-cfg", str(profile / "user.cfg"),
                 "--system-cfg", str(profile / "system.cfg"), str(macro)],
                env=env, stdout=log, stderr=subprocess.STDOUT)
            holder["pid"] = proc.pid
            try:
                code = proc.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = "timeout"
        stop.set()
        sampler.join()
    report_file = output / "report.json"
    report = json.loads(report_file.read_text()) if report_file.exists() else {"status": "failed"}
    report.pop("config", None)
    report.update({"app": str(app), "signed_resource_sha256": signature, "exit": code,
                   "elapsed_s_outside": round(time.monotonic() - started, 1),
                   "sampled_peak_rss_mib": round(max((s[1] for s in samples), default=0), 1),
                   "private_model": report.get("private_model") if args.carbon else None,
                   "run": {"links_per_variant": args.links_per_variant, "with_private_model": bool(args.carbon),
                           "drawing_scale": args.drawing_scale}})
    report_file.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    if report.get("status") != "passed" or code:
        print(report.get("error", f"App exited {code}. See {output / 'freecad.log'}."))
        return 1
    print(f"Passed: {report_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
