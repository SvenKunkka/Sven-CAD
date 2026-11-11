"""Verify component replacement and named states (T16) on a Mac bundle.

A synthetic supplier part exists in two STEP versions. The new version has a different
origin and rotation, one part changed size, one renamed, one missing, one added, two
same-named pins and a washer that now matches two parts. The assembly places the old
version three times with joints, an exploded view and a named state; two instances are
replaced through the dialog, the third stays. Positions, joints, exploded view, named
state, BOM, undo/redo and save/reopen are checked."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import time
import traceback

OUTPUT_ENV = "PERSONAL_CAD_T16_QA"
CONFIG_ENV = "PERSONAL_CAD_T16_CONFIG"
REPLACE = "PersonalCAD_ReplaceComponent"
STATES = "PersonalCAD_NamedStates"


def run_macro():
    import sys
    import FreeCAD as App
    import FreeCADGui as Gui
    import Part
    from PySide6 import QtCore, QtTest, QtWidgets

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from m2_thread_assembly_qa import keep_awake, screen_state
    dev_path = os.environ.get("PERSONAL_CAD_T16_QA_DEV_PATH")
    if dev_path:
        sys.path.insert(0, dev_path)
        for name in ("SvenInterference", "SvenReplace", "SvenReplaceGui"):
            sys.modules.pop(name, None)
    import SvenReplace as R
    import SvenReplaceGui as RG
    import SvenInterference as SI
    if dev_path:
        RG.register()  # the commands must open the dialogs of the modules under test

    output = Path(os.environ[OUTPUT_ENV])
    config = json.loads(os.environ[CONFIG_ENV])
    shots = output / "screenshots"
    shots.mkdir(exist_ok=True)
    work = output / "files"
    work.mkdir(exist_ok=True)
    report = {"status": "failed", "version": list(App.Version()), "config": config, "checks": {},
              "module": R.__file__, "app_nap_disabled": keep_awake(), "screen_at_start": screen_state()}
    checks = report["checks"]
    started = time.monotonic()

    def log(step, **data):
        with (output / "progress.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"t": round(time.monotonic() - started, 3), "step": step, **data},
                                    ensure_ascii=False) + "\n")

    def require(condition, message):
        if not condition:
            raise AssertionError(message)

    def wait_until(predicate, timeout=30.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            QtWidgets.QApplication.processEvents()
            if predicate():
                return True
            QtTest.QTest.qWait(50)
        return False

    def click(widget, name, cls=None):
        button = widget.findChild(cls or QtWidgets.QPushButton, name)
        require(button is not None, f"no {name}")
        QtTest.QTest.mouseClick(button, QtCore.Qt.MouseButton.LeftButton)
        QtTest.QTest.qWait(200)
        return button

    # ---------- the two supplier versions as STEP files ----------
    vendor = App.Placement(App.Vector(100, 0, 0), App.Rotation(App.Vector(0, 0, 1), 90))

    def version(doc, v2):
        part = doc.addObject("App::Part", "MotorV2" if v2 else "MotorV1")
        part.Label = "Motor"
        frame = vendor if v2 else App.Placement()

        def feature(label, shape):
            f = doc.addObject("Part::Feature", "F")
            f.Shape = shape
            f.Placement = frame
            f.Label = label
            part.addObject(f)

        def box(label, l, w, h, at):
            feature(label, Part.makeBox(l, w, h, App.Vector(*at)))

        def cyl(label, r, h, at):
            feature(label, Part.makeCylinder(r, h, App.Vector(*at)))

        box("Housing", 20, 10, 8, (0, 0, 0))
        cyl("Shaft", 2.2 if v2 else 2.0, 10, (10, 5, 8))
        cyl("Pin", 0.5, 4, (3, 3, 8))
        cyl("Pin", 0.5, 4, (17, 3, 8))
        box("Retainer" if v2 else "Clip", 2, 2, 1, (5, 7, 8))
        if v2:
            box("Gasket", 20, 10, 0.5, (0, 0, -0.5))
            cyl("WasherA", 1.5, 0.5, (14, 1.5, -1.0))
            cyl("WasherB", 1.5, 0.5, (18, 1.5, -1.0))
        else:
            box("Cap", 4, 2, 1, (14, 7, 8))
            cyl("Washer", 1.5, 0.5, (16, 1.5, -0.5))
        doc.recompute()
        return part

    def write_steps():
        import Import
        paths = {}
        for v2 in (False, True):
            doc = App.newDocument("T16Supplier")
            part = version(doc, v2)
            path = work / ("motor-v2.step" if v2 else "motor-v1.step")
            Import.export([part], str(path))
            App.closeDocument(doc.Name)
            paths["v2" if v2 else "v1"] = path
            require(path.stat().st_size > 1000, f"STEP not written: {path}")
        checks["step_files"] = {k: {"bytes": p.stat().st_size,
                                    "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for k, p in paths.items()}
        return paths

    def import_root(doc, path):
        import Import
        before = set(o.Name for o in doc.Objects)
        Import.insert(str(path), doc.Name)
        doc.recompute()
        added = [o for o in doc.Objects if o.Name not in before]
        roots = [o for o in added if not any(p in added for p in o.InList)]
        require(len(roots) == 1, [o.Label for o in roots])
        return roots[0]

    def leaf_by_label(root, label):
        hits = [l for l in R.leaves(root) if R.base_label(l.label) == label]
        require(hits, (label, [l.label for l in R.leaves(root)]))
        return hits

    def build(paths):
        import JointObject
        import CommandCreateView as V
        doc = App.newDocument("T16Assembly")
        doc.UndoMode = 1
        v1 = import_root(doc, paths["v1"])
        v1.addProperty("App::PropertyString", "PartNumber", "Base", "Supplier part number")
        v1.PartNumber = "FIXTURE-V1"  # fixture value, not a company part number
        plate = doc.addObject("Part::Feature", "Plate")
        plate.Shape = Part.makeBox(60, 60, 5)
        asm = doc.addObject("Assembly::AssemblyObject", "Product")
        base = asm.newObject("App::Link", "Base")
        base.LinkedObject = plate
        poses = {"Motor1": App.Placement(App.Vector(5, 5, 5.5), App.Rotation()),
                 "Motor2": App.Placement(App.Vector(35, 20, 5.5), App.Rotation(App.Vector(0, 0, 1), 30)),
                 "Motor3": App.Placement(App.Vector(5, 40, 5.5), App.Rotation())}
        links = {}
        for name, placement in poses.items():
            link = asm.newObject("App::Link", name)
            link.LinkedObject = v1
            link.Placement = placement
            links[name] = link
        doc.recompute()
        for obj in (v1, plate):
            obj.Visibility = False
        housing = leaf_by_label(v1, "Housing")[0]
        shaft = leaf_by_label(v1, "Shaft")[0]
        housing_bottom = next(f"Face{i}" for i, f in enumerate(housing.shape.Faces, 1)
                              if abs(f.normalAt(0, 0).z + 1) < 1e-9)
        shaft_side = next(f"Face{i}" for i, f in enumerate(shaft.shape.Faces, 1)
                          if type(f.Surface).__name__ == "Cylinder")
        corner = next(f"Vertex{i}" for i, v in enumerate(housing.shape.Vertexes, 1)
                      if v.Point.distanceToPoint(housing.shape.BoundBox.getPoint(4)) < 1e-9 or
                      (abs(v.Point.z - housing.shape.BoundBox.ZMin) < 1e-9))
        group = asm.newObject("Assembly::JointGroup", "Joints")
        ground = group.newObject("App::FeaturePython", "GroundBase")
        JointObject.GroundedJoint(ground, base)
        JointObject.ViewProviderGroundedJoint(ground.ViewObject)
        joints = {}
        for name, ref2 in (("Motor1", [housing.path + housing_bottom, housing.path + corner]),
                           ("Motor2", [shaft.path + shaft_side, shaft.path + shaft_side]),
                           ("Motor3", [housing.path + housing_bottom, housing.path + corner])):
            joint = group.newObject("App::FeaturePython", "Fix" + name)
            JointObject.Joint(joint, JointObject.JointTypes.index("Fixed"))
            JointObject.ViewProviderJoint(joint.ViewObject)
            joint.Reference1 = (base, ["Face6", "Face6"])
            joint.Reference2 = (links[name], ref2)
            joint.Proxy.updateJCSPlacements(joint)
            joints[name] = joint
        doc.recompute()
        solve = asm.solve()
        doc.recompute()
        require(solve == 0, f"initial solve {solve}")
        clip = leaf_by_label(v1, "Clip")[0]
        cap = leaf_by_label(v1, "Cap")[0]
        views = asm.newObject("Assembly::ViewGroup", "Exploded Views")
        view = views.newObject("App::FeaturePython", "Exploded")
        V.ExplodedView(view)
        V.ViewProviderExplodedView(view.ViewObject)
        step = view.newObject("App::FeaturePython", "MoveClip")
        V.ExplodedViewStep(step)
        V.ViewProviderExplodedViewStep(step.ViewObject)
        step.References = [asm, ["Motor3.", "Motor2." + clip.path]]
        step.MovementTransform = App.Placement(App.Vector(0, 0, 20), App.Rotation())
        step.MoveType = "Normal" if "Normal" in step.getEnumerationsOfProperty("MoveType") else \
            step.getEnumerationsOfProperty("MoveType")[0]
        note = doc.addObject("App::FeaturePython", "CapNote")
        note.addProperty("App::PropertyXLinkSubHidden", "References", "Note", "Points at the cap of Motor1")
        note.References = [asm, ["Motor1." + cap.path + "Face6"]]
        doc.recompute()
        for move in view.Group:
            move.Visibility = False
        links["Motor2"].Visibility = False
        clip.obj.Visibility = False
        state = R.capture(asm, "Service", view)
        links["Motor2"].Visibility = True
        clip.obj.Visibility = True
        doc.recompute()
        return {"doc": doc, "asm": asm, "v1": v1, "links": links, "joints": joints, "view": view,
                "step": step, "note": note, "state": state, "faces": {"bottom": housing_bottom, "side": shaft_side}}

    def leaf_centres(asm):
        return {i.key: [round(v, 7) for v in i.shape.BoundBox.Center] for i in SI.collect([asm], visible_only=False)}

    def jcs(fx):
        out = {}
        import UtilsAssembly
        for name, joint in fx["joints"].items():
            g = UtilsAssembly.getGlobalPlacement(joint.Reference2) if hasattr(UtilsAssembly, "getGlobalPlacement") \
                else None
            g = (g.multiply(joint.Placement2) if g is not None else joint.Placement2)
            out[name] = (App.Vector(g.Base), App.Rotation(g.Rotation))
        return out

    def ribbon_buttons():
        Gui.activateWorkbench("AssemblyWorkbench")
        QtTest.QTest.qWait(500)
        ribbon = Gui.getMainWindow().findChild(QtWidgets.QToolBar, "PersonalCadDesignToolbar")
        tabs = ribbon.findChild(QtWidgets.QTabBar) if ribbon else None
        found = {}
        if tabs is not None:
            for index in range(tabs.count()):
                if tabs.tabText(index) in ("Assembly", "装配") and tabs.isTabVisible(index):
                    tabs.setCurrentIndex(index)
            QtTest.QTest.qWait(400)
            for command in (REPLACE, STATES):
                button = ribbon.findChild(QtWidgets.QToolButton, command)
                found[command] = button.text() if button is not None and button.isVisible() else None
        return found

    def execute():
        try:
            log("start", name="steps")
            paths = write_steps()
            log("start", name="build")
            fx = build(paths)
            doc, asm, links = fx["doc"], fx["asm"], fx["links"]
            Gui.SendMsgToActiveView("ViewFit")
            checks["ribbon"] = ribbon_buttons()
            zh = RG.chinese()
            checks["locale"] = Gui.getLocale()
            require(checks["ribbon"].get(REPLACE) == ("替换" if zh else "Replace"), checks["ribbon"])
            require(checks["ribbon"].get(STATES) == ("状态" if zh else "States"), checks["ribbon"])
            before = leaf_centres(asm)
            joints_before = jcs(fx)
            bom_before = R.bom_rows(asm)
            checks["bom_before"] = bom_before
            require(("Motor", "FIXTURE-V1", "3") in bom_before, bom_before)
            checks["state_before"] = R.state_hidden(fx["state"])
            require(len(checks["state_before"]) == 3 and "Motor2." in checks["state_before"], checks["state_before"])
            placements_before = {n: App.Placement(l.Placement) for n, l in links.items()}

            log("start", name="dialog")
            Gui.Selection.clearSelection()
            Gui.Selection.addSelection(doc.Name, asm.Name, "Motor1.")
            Gui.Selection.addSelection(doc.Name, asm.Name, "Motor2.")
            checks["selected_instances"] = [l.Name for l in RG.selected_instances()]
            Gui.runCommand(REPLACE, 0)
            dialog = RG.open_dialog()
            checks["replace_command_opened_dialog"] = dialog is not None
            if dialog is None:
                RG.ReplaceCommand().Activated()  # surfaces the error the command swallowed
                dialog = RG.open_dialog()
            require(isinstance(dialog, RG.ReplaceDialog) and dialog.dialog.isVisible(), "replace dialog")
            widget = dialog.dialog
            require(widget.windowTitle() == ("替换零部件" if zh else "Replace Component"), widget.windowTitle())
            require([l.Name for l in dialog.links] == ["Motor1", "Motor2"], [l.Name for l in dialog.links])
            t0 = time.monotonic()
            v2 = dialog.load_step(paths["v2"])
            v2.addProperty("App::PropertyString", "PartNumber", "Base", "Supplier part number")
            v2.PartNumber = "FIXTURE-V2"
            v2.Label = "Motor"
            result = dialog.analyze()
            checks["analyze_s"] = round(time.monotonic() - t0, 3)
            table = widget.findChild(QtWidgets.QTableWidget, "SvenReplaceTable")
            rows = [[(table.item(r, c).text() if table.item(r, c) else
                      (table.cellWidget(r, c).currentText() if table.cellWidget(r, c) else ""))
                     for c in range(table.columnCount())] for r in range(table.rowCount())]
            checks["first_match"] = {"counts": result["counts"], "pose": result["pose"], "rows": rows,
                                     "summary": widget.findChild(QtWidgets.QLabel, "SvenReplaceSummary").text()}
            counts = result["counts"]
            require(counts == {"same": 3, "changed": 1, "renamed": 1, "ambiguous": 1, "missing": 1, "added": 1},
                    counts)
            pose = result["pose"]
            require(pose["residual"] < 1e-6 and abs(pose["angle_deg"] - 90) < 1e-6, pose)
            by_old = {R.base_label(e["old_label"]): e for e in result["entries"] if e["old"]}
            require(by_old["Clip"]["status"] == "renamed" and R.base_label(by_old["Clip"]["new_label"]) == "Retainer",
                    by_old["Clip"])
            require(by_old["Shaft"]["status"] == "changed" and by_old["Cap"]["status"] == "missing", by_old)
            require(by_old["Washer"]["status"] == "ambiguous" and len(by_old["Washer"]["candidates"]) == 2,
                    by_old["Washer"])
            require(sum(1 for e in result["entries"] if R.base_label(e["old_label"]) == "Pin"
                        and e["status"] == "same") == 2, "pins")
            require(f"FIXTURE-V1 → FIXTURE-V2" in checks["first_match"]["summary"], checks["first_match"]["summary"])
            widget.grab().save(str(shots / "replace-ambiguous.png"))

            undo_before = doc.UndoCount
            click(widget, "SvenReplaceApply")
            require(links["Motor1"].LinkedObject == fx["v1"], "replaced while ambiguous")
            require(widget.findChild(QtWidgets.QLabel, "SvenReplaceStatus").text(), "no ambiguity message")
            combo = dialog.choices[by_old["Washer"]["old"]]
            washer_a = next(i for i in range(combo.count()) if combo.itemText(i).startswith("WasherA"))
            combo.setCurrentIndex(washer_a)
            click(widget, "SvenReplaceAnalyze")
            require(dialog.result["settled"], dialog.result["counts"])
            click(widget, "SvenReplaceApply")
            refused = widget.findChild(QtWidgets.QLabel, "SvenReplaceStatus").text()
            require(links["Motor1"].LinkedObject == fx["v1"] and doc.UndoCount == undo_before,
                    "replaced with an unresolved reference")
            checks["refused_message"] = refused
            unresolved = [p for p in dialog.last["references"] if p["state"] == "unresolved"]
            require(len(unresolved) == 1 and unresolved[0]["object"] == "CapNote", unresolved)
            widget.findChild(QtWidgets.QCheckBox, "SvenReplaceAllowUnresolved").setChecked(True)
            t1 = time.monotonic()
            click(widget, "SvenReplaceApply")
            checks["replace_s"] = round(time.monotonic() - t1, 3)
            rep = dialog.last
            require(rep and rep.get("replaced") == ["Motor1", "Motor2"], rep and rep.get("refused"))
            checks["references"] = [{k: p.get(k) for k in ("label", "property", "sub", "new_sub", "state")}
                                    for p in rep["references"]]
            checks["status_after"] = widget.findChild(QtWidgets.QLabel, "SvenReplaceStatus").text()
            widget.grab().save(str(shots / "replace-done.png"))
            Gui.getMainWindow().grab().save(str(shots / "window-replaced.png"))
            require(doc.UndoCount == undo_before + 1, "replace is not one undo step")

            # ---- links, positions, joints
            require(links["Motor1"].LinkedObject == v2 and links["Motor2"].LinkedObject == v2
                    and links["Motor3"].LinkedObject == fx["v1"], "links")
            after = leaf_centres(asm)
            leaf_map = {e["old"]: e["new"] for e in rep["match"]["entries"] if e["old"] and e["new"]}
            rigid = {e["old"] for e in rep["match"]["entries"]
                     if e["old"] and e["new"] and e["status"] in ("same", "renamed") and e["note"] != "manual"}
            moved = {}
            for key, centre in before.items():
                parts = key.split(".")
                link = parts[1]
                old_path = ".".join(parts[2:])
                if link in ("Motor1", "Motor2"):
                    if old_path not in rigid:
                        continue
                    new_key = ".".join(parts[:2]) + "." + leaf_map[old_path]
                else:
                    new_key = key
                require(new_key in after, (key, new_key))
                moved[key] = max(abs(a - b) for a, b in zip(after[new_key], centre))
            checks["position_change_max_mm"] = max(moved.values())
            require(checks["position_change_max_mm"] < 1e-6, sorted(moved.items(), key=lambda kv: -kv[1])[:5])
            for name, joint in fx["joints"].items():
                subs = joint.Reference2[1]
                require(all(s.split(".")[0] + "." in (leaf_map.values() if name != "Motor3" else
                                                      [l.path for l in R.leaves(fx["v1"])]) for s in subs),
                        (name, subs))
            solve = asm.solve()
            doc.recompute()
            require(solve == 0, f"solve after replace {solve}")
            drift = {}
            for name, link in links.items():
                expected = placements_before[name] if name == "Motor3" else \
                    placements_before[name].multiply(dialog.result["transform"])
                drift[name] = (link.Placement.Base - expected.Base).Length
                require(link.Placement.Rotation.isSame(expected.Rotation, 1e-9), (name, "rotation"))
            checks["solve_drift_mm"] = drift
            require(max(drift.values()) < 1e-6, drift)
            joints_after = jcs(fx)
            jdiff = {n: (joints_after[n][0] - joints_before[n][0]).Length for n in joints_before}
            checks["joint_frame_change_mm"] = jdiff
            require(max(jdiff.values()) < 1e-6, jdiff)

            # ---- exploded view and named state
            step_subs = list(fx["step"].References[1])
            checks["exploded_refs"] = step_subs
            clip_new = leaf_map[by_old["Clip"]["old"]]
            require(step_subs == ["Motor3.", "Motor2." + clip_new], step_subs)
            centre_before = asm.getSubObject("Motor2." + clip_new, retType=0).BoundBox.Center
            fx["view"].Proxy.saveAssemblyAndExplode(fx["view"])
            centre_exploded = asm.getSubObject("Motor2." + clip_new, retType=0).BoundBox.Center
            fx["view"].Proxy.restoreAssembly(fx["view"])
            lift = centre_exploded - centre_before
            checks["exploded_clip_lift"] = [round(lift.x, 6), round(lift.y, 6), round(lift.z, 6)]
            require(abs(lift.z - 20) < 1e-6 and abs(lift.x) < 1e-6, checks["exploded_clip_lift"])
            hidden = R.state_hidden(fx["state"])
            checks["state_after"] = hidden
            require("Motor2." in hidden and "Motor1." + clip_new in hidden, hidden)
            require(not any(p.startswith(("Motor1.", "Motor2.")) and p.split(".")[1] + "." in
                            [l.path for l in R.leaves(fx["v1"])] for p in hidden), hidden)
            R.apply_state(fx["state"])
            invisible = sorted(k for k in leaf_centres(asm) if not R._path_visible(asm, k.split(".", 1)[1]))
            checks["state_applied_hidden"] = invisible
            require(all(k.startswith("Product.Motor2.") or k.endswith(clip_new) or "Motor3" in k
                        for k in invisible), invisible)
            require(any(k == "Product.Motor1." + clip_new for k in invisible), invisible)

            # ---- BOM
            bom_after = R.bom_rows(asm)
            checks["bom_after"] = bom_after
            by_pn = {pn: qty for _name, pn, qty in bom_after}
            require(by_pn.get("FIXTURE-V2") == "2" and by_pn.get("FIXTURE-V1") == "1" and len(bom_after) == 3,
                    bom_after)
            require(list(fx["note"].References[1]) == [p["sub"] for p in unresolved], "unresolved ref changed")

            # ---- states dialog
            Gui.runCommand(STATES, 0)
            sd = RG.open_dialog()
            if not isinstance(sd, RG.StatesDialog):
                checks["states_command_active"] = RG.StatesCommand().IsActive()
                RG.StatesCommand().Activated()  # surfaces the error the command swallowed
                sd = RG.open_dialog()
            require(isinstance(sd, RG.StatesDialog) and sd.dialog.isVisible(), "states dialog")
            sd.name.setText("Exploded")
            sd.view.setCurrentIndex(sd.view.findData(fx["view"].Name))
            click(sd.dialog, "SvenStatesSave")
            require(sd.list.count() == 2, sd.list.count())
            sd.list.setCurrentRow(0)
            click(sd.dialog, "SvenStatesApply")
            checks["states_dialog_status"] = sd.dialog.findChild(QtWidgets.QLabel, "SvenStatesStatus").text()
            sd.dialog.grab().save(str(shots / "states.png"))
            click(sd.dialog, "SvenStatesClose")

            # ---- undo / redo
            for _ in range(doc.UndoCount - undo_before):
                doc.undo()
            require(links["Motor1"].LinkedObject == fx["v1"] and links["Motor2"].LinkedObject == fx["v1"], "undo")
            require(list(fx["step"].References[1])[1].startswith("Motor2." + by_old["Clip"]["old"]), "undo refs")
            doc.redo()
            require(links["Motor1"].LinkedObject == v2, "redo")
            checks["undo_redo"] = True

            # ---- save and reopen
            saved = work / "t16-assembly.FCStd"
            doc.saveAs(str(saved))
            App.closeDocument(doc.Name)
            reopened = App.openDocument(str(saved))
            asm2 = reopened.getObject("Product")
            state2 = R.states(reopened)[0]
            checks["reopened"] = {"state": R.state_hidden(state2),
                                  "joint": list(reopened.getObject("FixMotor2").Reference2[1]),
                                  "links": {n: reopened.getObject(n).LinkedObject.Name for n in links},
                                  "solve": asm2.solve()}
            require(checks["reopened"]["state"] == hidden and checks["reopened"]["solve"] == 0, checks["reopened"])
            App.closeDocument(reopened.Name)
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
    with tempfile.TemporaryDirectory(prefix="sven-t16-qa-") as scratch:
        profile = Path(scratch)
        for name in ("cache", "data", "tmp"):
            (profile / name).mkdir()
        macro = profile / "T16QA.FCMacro"
        macro.write_text(
            f"import sys\nsys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
            "from t16_replace_qa import run_macro\nrun_macro()\n", encoding="utf-8")
        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8", "HOME": str(profile),
            "TMPDIR": str(profile / "tmp"), "XDG_CACHE_HOME": str(profile / "cache"),
            "FREECAD_USER_HOME": str(profile), "FREECAD_USER_DATA": str(profile / "data"),
            "FREECAD_USER_TEMP": str(profile / "tmp"), "PYTHONDONTWRITEBYTECODE": "1",
            OUTPUT_ENV: str(output), CONFIG_ENV: json.dumps({}),
        }
        if os.environ.get("PERSONAL_CAD_T16_QA_DEV_PATH"):
            env["PERSONAL_CAD_T16_QA_DEV_PATH"] = os.environ["PERSONAL_CAD_T16_QA_DEV_PATH"]
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
