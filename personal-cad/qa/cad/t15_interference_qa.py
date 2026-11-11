"""Verify static interference and minimum clearance (T15) on a Mac bundle.

Known-answer scene: contact, overlap with a known volume, clearance, a part fully inside another,
a rotated linked instance, coincident duplicates, an open shell and a hidden part. Then the
command dialog, a large grid of repeated parts with cancellation, and optionally a local private
model for timing only."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import threading
import time
import traceback

OUTPUT_ENV = "PERSONAL_CAD_T15_QA"
CONFIG_ENV = "PERSONAL_CAD_T15_CONFIG"
COMMAND = "PersonalCAD_Interference"


def run_macro():
    import sys
    import FreeCAD as App
    import FreeCADGui as Gui
    import Part
    from PySide6 import QtCore, QtTest, QtWidgets

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from m2_thread_assembly_qa import keep_awake, screen_state
    dev_path = os.environ.get("PERSONAL_CAD_T15_QA_DEV_PATH")
    if dev_path:
        sys.path.insert(0, dev_path)
        for name in ("SvenInterference", "SvenInterferenceGui"):
            sys.modules.pop(name, None)
    import SvenInterference as I
    import SvenInterferenceGui as IG

    output = Path(os.environ[OUTPUT_ENV])
    config = json.loads(os.environ[CONFIG_ENV])
    shots = output / "screenshots"
    shots.mkdir(exist_ok=True)
    report = {"status": "failed", "version": list(App.Version()), "config": config, "checks": {},
              "module": I.__file__, "dev_module_path": dev_path,
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

    def rss_mib():
        out = subprocess.run(["/bin/ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True).stdout
        return round(int(out.strip()) / 1024, 1)

    def close_enough(a, b, tol=1e-6):
        return abs(a - b) <= tol * max(1.0, abs(b))

    # ---------- known-answer scene ----------
    def known_scene():
        doc = App.newDocument("T15Known")
        product = doc.addObject("Assembly::AssemblyObject", "Product")

        def box(name, size, at, parent=product):
            b = doc.addObject("Part::Box", name)
            b.Length, b.Width, b.Height = size
            b.Placement.Base = App.Vector(*at)
            parent.addObject(b)
            return b

        box("Base", (20, 20, 10), (0, 0, 0))
        box("Contact", (5, 5, 5), (2, 2, 10))
        box("Overlap", (4, 4, 4), (19, 18, 7))
        box("Gap", (5, 5, 5), (20.5, 5, 0))
        box("Far", (5, 5, 5), (40, 0, 0))
        box("Inner", (2, 2, 2), (9, 9, 4))
        hidden = box("Hidden", (3, 3, 3), (1, 15, 2))
        library = doc.addObject("App::DocumentObjectGroup", "Library")
        pin_part = doc.addObject("App::Part", "PinPart")
        library.addObject(pin_part)
        box("Pin", (2, 2, 8), (-1, -1, 0), pin_part)
        library.Visibility = False
        pin_part.Visibility = False

        def link(name, placement):
            obj = doc.addObject("App::Link", name)
            obj.setLink(pin_part)
            obj.Placement = placement
            product.addObject(obj)
            return obj

        link("PinRotated", App.Placement(App.Vector(10, -3, 5), App.Rotation(App.Vector(1, 0, 0), -90)))
        link("PinA", App.Placement(App.Vector(30, 30, 0), App.Rotation()))
        link("PinB", App.Placement(App.Vector(30, 30, 0), App.Rotation()))
        sheet = doc.addObject("Part::Feature", "Sheet")
        sheet.Shape = Part.makePlane(4, 4, App.Vector(0, 2, 2), App.Vector(-1, 0, 0))
        product.addObject(sheet)
        doc.recompute()
        hidden.Visibility = False
        if hidden.ViewObject:
            hidden.ViewObject.Visibility = False
        doc.recompute()
        return doc, product

    EXPECTED = {
        ("Base", "Overlap"): ("overlap", 6.0, (19.5, 19.0, 8.5)),
        ("Base", "Inner"): ("overlap", 8.0, (10.0, 10.0, 5.0)),
        ("Base", "PinRotated"): ("overlap", 20.0, (10.0, 2.5, 5.0)),
        ("PinA", "PinB"): ("overlap", 32.0, (30.0, 30.0, 4.0)),
        ("Base", "Contact"): ("contact", None, None),
        ("Base", "Gap"): ("clearance", 0.5, None),
        ("Base", "Sheet"): ("undetermined", None, None),
    }

    def pair_name(result):
        def short(entry):
            parts = [p for p in entry["key"].split(".") if p]
            return parts[1] if len(parts) > 1 else parts[0]
        return tuple(sorted((short(result["a"]), short(result["b"]))))

    def check_known():
        doc, product = known_scene()
        n_objects = len(doc.Objects)
        undo = doc.UndoCount
        instances = I.collect([product])
        keys = sorted(i.key for i in instances)
        checks["known_instances"] = keys
        require(len(instances) == 10 and not any("Hidden" in k for k in keys), ("instances", keys))
        report_full = I.check([product], clearance_limit=1.0, allowed=[("Base", "Contact")])
        got = {pair_name(r): r for r in report_full["results"]}
        rows = {}
        for pair, (kind, value, center) in EXPECTED.items():
            key = tuple(sorted(pair))
            require(key in got, ("missing pair", key, sorted(got)))
            r = got[key]
            require(r["kind"] == kind, (key, r))
            if kind == "overlap":
                require(close_enough(r["volume"], value), (key, r["volume"], value))
                require(all(abs(a - b) < 1e-6 for a, b in zip(r["location"], center)), (key, r["location"], center))
            if kind == "clearance":
                require(close_enough(r["distance"], value), (key, r["distance"]))
            rows["-".join(key)] = {k: r.get(k) for k in ("kind", "volume", "distance", "location", "allowed",
                                                            "duplicate", "reason")}
        require(len(got) == len(EXPECTED), ("unexpected pairs", sorted(set(got) - {tuple(sorted(p)) for p in EXPECTED})))
        require(got[("Base", "Contact")]["allowed"] and not got[("Base", "Gap")]["allowed"], "allowed flag")
        require(got[("PinA", "PinB")].get("duplicate"), "duplicates not flagged")
        require(report_full["problems"]["contact"] == 0 and report_full["allowed"] == 1, report_full["problems"])
        checks["known_results"] = rows
        checks["known_summary"] = {k: report_full[k] for k in report_full if k != "results"}

        tight = I.check([product], clearance_limit=0.2, include_contacts=False)
        tight_pairs = sorted(pair_name(r) for r in tight["results"])
        require(("Base", "Gap") not in tight_pairs and ("Base", "Contact") not in tight_pairs, tight_pairs)
        require(len(tight_pairs) == 5, tight_pairs)
        checks["limit_0_2_without_contacts"] = tight_pairs

        scoped = I.check([(product, "Base."), (product, "Inner.")])
        require([pair_name(r) for r in scoped["results"]] == [("Base", "Inner")] and scoped["instances"] == 2, scoped)
        with_hidden = I.check([(product, "Base."), (product, "Hidden.")])
        require(with_hidden["results"] and with_hidden["results"][0]["kind"] == "overlap"
                and close_enough(with_hidden["results"][0]["volume"], 27.0), with_hidden)
        checks["scope"] = {"selected_two": scoped["instances"], "selected_hidden_overlap": with_hidden["results"][0]["volume"]}

        moved = doc.getObject("Overlap")
        moved.Placement.Base = App.Vector(21.5, 18, 7)
        doc.recompute()
        after = I.check([product], clearance_limit=1.0)
        require(("Base", "Overlap") not in {pair_name(r) for r in after["results"]}, "moved part still reported")
        moved.Placement.Base = App.Vector(20, 18, 7)
        doc.recompute()
        touching = {pair_name(r): r for r in I.check([product], clearance_limit=1.0)["results"]}
        require(touching[("Base", "Overlap")]["kind"] == "contact", touching.get(("Base", "Overlap")))
        moved.Placement.Base = App.Vector(19, 18, 7)
        doc.recompute()
        checks["moved_part"] = "clear at 1 mm, contact when touching, overlap restored"
        require(len(doc.Objects) == n_objects and doc.UndoCount == undo, "check changed the document")
        return doc, product

    # ---------- dialog ----------
    def wait_until(predicate, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            QtTest.QTest.qWait(50)
        return False

    def dialog_run(doc, chinese=False):
        Gui.Selection.clearSelection()
        Gui.runCommand(COMMAND, 0)
        dialog = IG.open_dialog()
        require(dialog is not None and dialog.dialog.isVisible(), "Interference dialog did not open")
        widget = dialog.dialog
        require(widget.windowTitle() == ("干涉检查" if chinese else "Interference"), widget.windowTitle())
        dialog.limit.setValue(1.0)
        compute = widget.findChild(QtWidgets.QPushButton, "SvenInterferenceCompute")
        QtTest.QTest.mouseClick(compute, QtCore.Qt.MouseButton.LeftButton)
        require(wait_until(lambda: dialog.report is not None and compute.isEnabled()), "compute did not finish")
        table = widget.findChild(QtWidgets.QTableWidget, "SvenInterferenceResults")
        status = widget.findChild(QtWidgets.QLabel, "SvenInterferenceStatus").text()
        info = {"rows": table.rowCount(), "status": status,
                "first_row": [table.item(0, c).text() for c in range(table.columnCount())]}
        require(table.rowCount() == len(EXPECTED), info)
        overlap_row = next(i for i in range(table.rowCount()) if dialog.report["results"][i]["kind"] == "overlap")
        table.selectRow(overlap_row)
        QtTest.QTest.qWait(300)
        selected = [(s.ObjectName, list(s.SubElementNames)) for s in Gui.Selection.getSelectionEx("", 0)]
        info["selected"] = selected
        require(sum(len(s[1]) or 1 for s in selected) == 2, selected)
        require(dialog.overlay.node is not None, "overlap volume not shown")
        Gui.SendMsgToActiveView("ViewFit")
        QtTest.QTest.qWait(300)
        suffix = "-zh" if chinese else ""
        widget.grab().save(str(shots / f"dialog{suffix}.png"))
        Gui.getMainWindow().grab().save(str(shots / f"window{suffix}.png"))
        if chinese:
            require("穿透" in info["first_row"][2] and "实体" in status, info)
        close = widget.findChild(QtWidgets.QPushButton, "SvenInterferenceClose")
        QtTest.QTest.mouseClick(close, QtCore.Qt.MouseButton.LeftButton)
        QtTest.QTest.qWait(200)
        require(dialog.overlay.node is None, "overlay left in the view after closing")
        return info

    def check_dialog():
        doc, _ = known_scene()
        Gui.activateWorkbench("AssemblyWorkbench")
        QtTest.QTest.qWait(500)
        Gui.SendMsgToActiveView("ViewFit")
        ribbon = Gui.getMainWindow().findChild(QtWidgets.QToolBar, "PersonalCadDesignToolbar")
        tabs = ribbon.findChild(QtWidgets.QTabBar) if ribbon else None
        info = {}
        if tabs is not None:
            for index in range(tabs.count()):
                if tabs.tabText(index) in ("Assembly", "装配") and tabs.isTabVisible(index):
                    tabs.setCurrentIndex(index)
            QtTest.QTest.qWait(400)
            button = ribbon.findChild(QtWidgets.QToolButton, COMMAND)
            info["ribbon_button"] = button.text() if button is not None and button.isVisible() else None
        require(info.get("ribbon_button") == "Interference", ("ribbon", info))
        info.update(dialog_run(doc))
        undo = doc.UndoCount
        require(doc.UndoCount == undo, "dialog changed the document")
        App.closeDocument(doc.Name)
        name = next(k for k, c in Gui.supportedLocales().items() if c in ("zh-CN", "zh_CN"))
        previous = Gui.getLocale()
        Gui.setLocale(name)
        QtTest.QTest.qWait(600)
        try:
            doc, _ = known_scene()
            info["chinese"] = dialog_run(doc, chinese=True)
            App.closeDocument(doc.Name)
        finally:
            Gui.setLocale(previous)
            QtTest.QTest.qWait(400)
        checks["dialog"] = info

    # ---------- large grid ----------
    def grid_scene(side, fasteners):
        doc = App.newDocument("T15Grid")
        product = doc.addObject("Assembly::AssemblyObject", "Product")
        library = doc.addObject("App::DocumentObjectGroup", "Library")
        module = doc.addObject("App::Part", "Module")
        library.addObject(module)
        plate = doc.addObject("Part::Box", "Plate")
        plate.Length, plate.Width, plate.Height = 10, 10, 2
        boss = doc.addObject("Part::Cylinder", "Boss")
        boss.Radius, boss.Height = 2, 4
        boss.Placement.Base = App.Vector(5, 5, 2)
        module.addObject(plate)
        module.addObject(boss)
        library.Visibility = False
        module.Visibility = False
        expected = {"clearance_rows": 0, "overlap_rows": 0}
        for row in range(side):
            # Row spacing 10.1 leaves 0.1 mm; every fifth row is 9.9 and overlaps its neighbour.
            for col in range(side):
                x = col * (9.9 if row % 5 == 4 else 10.1)
                link = doc.addObject("App::Link", "Unit")
                link.setLink(module)
                link.Placement.Base = App.Vector(x, row * 12.0, 0)
                product.addObject(link)
        import SvenFasteners as F
        placements = [App.Placement(App.Vector((k % side) * 10.1 + 5, (k // side) * 12.0 + 5, 6), App.Rotation())
                      for k in range(fasteners)]
        F.insert(doc, F.find("ISO4762-M3x8"), placements, product)
        doc.recompute()
        return doc, product

    def check_grid():
        side, fasteners = config["grid_side"], config["grid_fasteners"]
        t0 = time.monotonic()
        doc, product = grid_scene(side, fasteners)
        build = time.monotonic() - t0
        rss0 = rss_mib()
        full = I.check([product], clearance_limit=0.2)
        counts = full["counts"]
        info = {"instances": full["instances"], "candidate_pairs": full["candidate_pairs"],
                "counts": counts, "duplicates": full["duplicates"], "build_s": round(build, 2),
                "collect_s": full["collect_seconds"], "check_s": full["seconds"],
                "pairs_per_s": round(full["pairs_checked"] / max(full["seconds"], 1e-9), 1),
                "rss_before_mib": rss0, "rss_after_mib": rss_mib()}
        require(full["instances"] == side * side * 2 + fasteners, info)
        require(counts["clearance"] > 0 and counts["overlap"] > 0 and counts["undetermined"] == 0, info)
        # Every fastener sits on its boss: contact or overlap with the boss only.
        state = {"asked": None}

        def cancel():
            now = time.monotonic()
            if state["asked"] is None and now - t1 >= config["cancel_after_s"]:
                state["asked"] = now
            return state["asked"] is not None

        t1 = time.monotonic()
        stopped = I.check([product], clearance_limit=0.2, cancel=cancel)
        returned = time.monotonic()
        require(stopped["cancelled"] and stopped["pairs_checked"] < stopped["candidate_pairs"], stopped["pairs_checked"])
        info["cancel"] = {"requested_after_s": round(state["asked"] - t1, 3),
                          "response_ms": round((returned - state["asked"]) * 1000, 1),
                          "pairs_done": stopped["pairs_checked"], "of": stopped["candidate_pairs"]}
        require(info["cancel"]["response_ms"] < 1000, info["cancel"])
        # Cancel from the dialog while it computes.
        Gui.Selection.clearSelection()
        Gui.runCommand(COMMAND, 0)
        dialog = IG.open_dialog()
        dialog.limit.setValue(0.2)
        widget = dialog.dialog
        compute = widget.findChild(QtWidgets.QPushButton, "SvenInterferenceCompute")
        cancel_button = widget.findChild(QtWidgets.QPushButton, "SvenInterferenceCancel")
        clicked = {}

        def press_cancel():
            clicked["t"] = time.monotonic()
            QtTest.QTest.mouseClick(cancel_button, QtCore.Qt.MouseButton.LeftButton)

        QtCore.QTimer.singleShot(int(config["cancel_after_s"] * 1000), press_cancel)
        QtTest.QTest.mouseClick(compute, QtCore.Qt.MouseButton.LeftButton)
        done_at = time.monotonic()
        require(dialog.report is not None and dialog.report["cancelled"], "dialog cancel did not stop the check")
        info["dialog_cancel"] = {"response_ms": round((done_at - clicked["t"]) * 1000, 1),
                                 "pairs_done": dialog.report["pairs_checked"],
                                 "status": widget.findChild(QtWidgets.QLabel, "SvenInterferenceStatus").text()}
        require(info["dialog_cancel"]["response_ms"] < 1000, info["dialog_cancel"])
        widget.close()
        App.closeDocument(doc.Name)
        checks["grid"] = info

    # ---------- private model (local only, counts and timings) ----------
    def check_private():
        path = config.get("private")
        if not path:
            return
        t0 = time.monotonic()
        doc = App.openDocument(path)
        opened = time.monotonic() - t0
        roots = I.default_roots(doc)
        t1 = time.monotonic()
        instances = I.collect(roots)
        collected = time.monotonic() - t1
        budget = config["private_budget_s"]
        state = {"asked": None}

        def cancel():
            now = time.monotonic()
            if state["asked"] is None and now - t2 >= budget:
                state["asked"] = now
            return state["asked"] is not None

        def progress(done, total):
            if done % 200 == 0:
                log("private_progress", done=done, total=total, rss_mib=rss_mib())

        t2 = time.monotonic()
        result = I.check(instances=instances, clearance_limit=0.2, cancel=cancel, progress=progress)
        finished = time.monotonic()
        info = {"open_s": round(opened, 2), "collect_s": round(collected, 2), "instances": result["instances"],
                "candidate_pairs": result["candidate_pairs"], "pairs_checked": result["pairs_checked"],
                "check_s": round(finished - t2, 2), "completed": not result["cancelled"],
                "counts": result["counts"], "duplicates": result["duplicates"],
                "pairs_per_s": round(result["pairs_checked"] / max(finished - t2, 1e-9), 2), "rss_mib": rss_mib()}
        if result["cancelled"]:
            info["cancel_response_ms"] = round((finished - state["asked"]) * 1000, 1)
        slow = sorted(((r.get("kind"), r.get("reason")) for r in result["results"] if r["kind"] == "undetermined"))[:10]
        info["undetermined_reasons"] = sorted({s[1] for s in slow if s[1]})
        App.closeDocument(doc.Name)
        checks["private"] = info

    def execute():
        steps = [("known", check_known), ("dialog", check_dialog), ("grid", check_grid), ("private", check_private)]
        try:
            for name, step in steps:
                log("start", name=name, rss_mib=rss_mib())
                t0 = time.monotonic()
                result = step()
                if name == "known" and result:
                    App.closeDocument(result[0].Name)
                report.setdefault("timings_s", {})[name] = round(time.monotonic() - t0, 2)
                log("done", name=name, rss_mib=rss_mib())
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


def sample_rss(holder, stop, samples):
    while not stop.is_set():
        pid = holder.get("pid")
        if pid:
            out = subprocess.run(["/bin/ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout
            if out.strip():
                samples.append(int(out.strip()) / 1024)
        stop.wait(0.5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--grid-side", type=int, default=20)
    parser.add_argument("--grid-fasteners", type=int, default=100)
    parser.add_argument("--cancel-after", type=float, default=1.0)
    parser.add_argument("--private", type=Path, help="Local native FCStd of a private model, opened read-only")
    parser.add_argument("--private-budget", type=float, default=900.0)
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()
    app = args.app.resolve()
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    executable = app / "Contents/MacOS" / info["CFBundleExecutable"]
    output = args.output.resolve()
    output.mkdir(parents=True)
    signature = hashlib.sha256((app / "Contents/_CodeSignature/CodeResources").read_bytes()).hexdigest()
    config = {"grid_side": args.grid_side, "grid_fasteners": args.grid_fasteners,
              "cancel_after_s": args.cancel_after, "private_budget_s": args.private_budget}
    private_sha = None
    if args.private:
        config["private"] = str(args.private.resolve())
        private_sha = hashlib.sha256(args.private.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="sven-t15-qa-") as scratch:
        profile = Path(scratch)
        for name in ("cache", "data", "tmp"):
            (profile / name).mkdir()
        macro = profile / "T15QA.FCMacro"
        macro.write_text(
            f"import sys\nsys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
            "from t15_interference_qa import run_macro\nrun_macro()\n", encoding="utf-8")
        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8", "HOME": str(profile),
            "TMPDIR": str(profile / "tmp"), "XDG_CACHE_HOME": str(profile / "cache"),
            "FREECAD_USER_HOME": str(profile), "FREECAD_USER_DATA": str(profile / "data"),
            "FREECAD_USER_TEMP": str(profile / "tmp"), "PYTHONDONTWRITEBYTECODE": "1",
            OUTPUT_ENV: str(output), CONFIG_ENV: json.dumps(config),
        }
        if os.environ.get("PERSONAL_CAD_T15_QA_DEV_PATH"):
            env["PERSONAL_CAD_T15_QA_DEV_PATH"] = os.environ["PERSONAL_CAD_T15_QA_DEV_PATH"]
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
    report_file = output / "report.json"
    report = json.loads(report_file.read_text()) if report_file.exists() else {"status": "failed"}
    report.update({"app": str(app), "signed_resource_sha256": signature, "exit": code,
                   "elapsed_s": round(time.monotonic() - started, 1),
                   "sampled_peak_rss_mib": round(max(samples), 1) if samples else None})
    if args.private:
        report.setdefault("checks", {})["private_model_unchanged"] = \
            hashlib.sha256(args.private.read_bytes()).hexdigest() == private_sha
        if not report["checks"]["private_model_unchanged"]:
            report["status"] = "failed"
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
    if report["status"] != "passed" or code:
        print(report.get("error", f"App exited {code}. See {output / 'freecad.log'}."))
        return 1
    print(f"Passed: {report_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
