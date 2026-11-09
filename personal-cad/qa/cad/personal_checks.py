"""Runtime checks specific to the trimmed Personal CAD application."""

import csv
import importlib.util
import json
import os
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET
from zipfile import ZipFile

import FreeCAD as App
import FreeCADGui as Gui
from PySide import QtCore, QtWidgets

from models import color_counts, require, same_shape_metrics, shape_metrics


EXCLUDED_MODULES = (
    "Spreadsheet", "Material", "Mesh", "MeshPart", "Fem", "Robot", "CAM", "BIM",
    "Points", "Inspection", "ReverseEngineering",
)
CAD_WORKBENCHES = (
    "SketcherWorkbench", "PartDesignWorkbench", "DraftWorkbench", "SurfaceWorkbench",
    "TechDrawWorkbench", "AssemblyWorkbench", "PartWorkbench",
)


def check_configuration():
    import AssemblyApp

    identity = {key: App.ConfigGet(key) for key in ("ExeName", "Application", "DesktopFileName")}
    require(identity == {
        "ExeName": "PersonalCAD",
        "Application": "个人 CAD",
        "DesktopFileName": "local.personalcad.PersonalCAD",
    }, f"Unexpected runtime identity: {identity}")

    home = Path(os.environ["FREECAD_USER_HOME"]).resolve()
    paths = {
        "data": Path(App.getUserAppDataDir()).resolve(),
        "cache": Path(App.getUserCachePath()).resolve(),
        "user_settings": Path(App.ConfigGet("UserParameter")).resolve(),
        "system_settings": Path(App.ConfigGet("SystemParameter")).resolve(),
    }
    require(all(path.is_relative_to(home) for path in paths.values()), f"Profile is not isolated: {paths}")
    require(paths["data"] == (home / "data"), f"Unexpected isolated data path: {paths['data']}")

    resources = Path(App.getResourceDir()) / "Mod"
    require(resources.is_dir(), "Installed module directory is missing")
    installed = [name for name in EXCLUDED_MODULES if (resources / name).exists()]
    require(not installed, f"Excluded module directories are installed: {installed}")
    import_names = (*EXCLUDED_MODULES, "Path", "bim", "Reen")
    importable = [name for name in import_names if importlib.util.find_spec(name) is not None]
    require(not importable, f"Excluded modules are importable: {importable}")

    workbenches = sorted(Gui.listWorkbenches())
    excluded_workbenches = {f"{name}Workbench" for name in EXCLUDED_MODULES}
    require(excluded_workbenches.isdisjoint(workbenches), f"Unexpected workbench list: {workbenches}")
    for name in CAD_WORKBENCHES:
        require(name in workbenches, f"Missing CAD workbench: {name}")
        Gui.activateWorkbench(name)
        require(Gui.activeWorkbench().name() == name, f"Workbench failed to activate: {name}")
    commands = Gui.Command.listAll()
    require("Assembly_CreateBom" in commands, "Static Assembly BOM command is missing")
    require(not AssemblyApp.hasMotionSimulation, "Assembly reports motion simulation enabled")
    require("Assembly_CreateSimulation" not in commands, "Assembly simulation command is available")
    return {
        "identity": identity,
        "isolated_profile_paths": {key: str(path) for key, path in paths.items()},
        "workbenches": workbenches,
        "activated_workbenches": list(CAD_WORKBENCHES),
        "verified_excluded_modules": list(EXCLUDED_MODULES),
    }


def check_bom(output):
    import AssemblyApp
    import StaticBom
    import UtilsAssembly

    require(AssemblyApp.hasStaticBom, "Assembly did not enable its static BOM")
    doc = App.newDocument("PersonalCadBom")
    assembly = doc.addObject("Assembly::AssemblyObject", "Assembly")
    plate = doc.addObject("Part::Box", "FixturePlate")
    plate.Label = "Fixture plate"
    fastener = doc.addObject("Part::Box", "FixtureFastener")
    fastener.Label = "Fixture fastener"
    for name, source in (("PlateLink", plate), ("FastenerA", fastener), ("FastenerB", fastener)):
        assembly.newObject("App::Link", name).LinkedObject = source
    bom = UtilsAssembly.getBomGroup(assembly).newObject("Assembly::BomObject", "FixtureBom")
    bom.columnsNames = ["Name", "Quantity"]
    doc.recompute()
    require(bom.recompute(), "Static BOM failed to recompute")
    rows = list(bom.BomRows)
    require(len(rows) == 4, f"Unexpected flat BOM rows: {rows}")
    quantities = dict(zip(rows[::2], rows[1::2], strict=True))
    require(quantities == {"Fixture plate": "1", "Fixture fastener": "2"}, f"Incorrect BOM: {quantities}")

    path = output / "keyboard-static-bom.FCStd"
    doc.saveAs(str(path))
    App.closeDocument(doc.Name)
    doc = App.openDocument(str(path))
    bom = doc.getObject("FixtureBom")
    require(bom.TypeId == "Assembly::BomObject" and list(bom.BomRows) == rows,
            "Static BOM rows did not survive reopening")
    require(bom.recompute() and list(bom.BomRows) == rows, "Recomputation changed the reopened BOM")

    StaticBom.showPreview(bom)
    QtCore.QCoreApplication.processEvents()
    dialogs = [dialog for dialog in Gui.getMainWindow().findChildren(QtWidgets.QDialog)
               if dialog.isVisible() and dialog.windowTitle() == bom.Label]
    require(len(dialogs) == 1, "The static BOM did not open its preview dialog")
    preview = dialogs[0].findChild(QtWidgets.QTableView).model()
    require(preview.rowCount() == 2 and preview.columnCount() == 2, "Incorrect BOM preview dimensions")
    preview_rows = [preview.data(preview.index(row, column))
                    for row in range(preview.rowCount()) for column in range(preview.columnCount())]
    require(preview_rows == rows, "BOM preview did not show the saved rows")
    dialogs[0].close()

    csv_path = output / "keyboard-static-bom.csv"
    StaticBom.writeCsv(str(csv_path), bom.columnsNames, bom.BomRows)
    with csv_path.open(encoding="utf-8", newline="") as stream:
        exported = list(csv.reader(stream))
    require(exported == [["Name", "Quantity"], rows[:2], rows[2:]], "CSV differs from the BOM preview")
    App.closeDocument(doc.Name)
    return {"counts": quantities, "preview_rows": 2, "preview_columns": 2, "reopened": True, "csv": exported}


def check_disabled_mesh(output):
    import Part

    shape = Part.makeBox(10, 10, 2)
    topology = shape.tessellate(0.1)
    require(bool(topology[0]) and bool(topology[1]), "Geometric tessellation is unavailable")
    direct = output / "disabled-direct.stl"
    try:
        shape.exportStl(str(direct))
    except NotImplementedError as error:
        require("STL export is disabled" in str(error), f"Unexpected STL error: {error}")
    else:
        raise RuntimeError("The direct STL export API accepted the request")
    require(not direct.exists(), "Direct STL export created a file")

    target = Part.Shape()
    try:
        target.makeShapeFromMesh(topology, 0.1)
    except NotImplementedError as error:
        require("Mesh conversion is disabled" in str(error), f"Unexpected mesh error: {error}")
    else:
        raise RuntimeError("The mesh conversion API accepted the request")
    require(target.isNull(), "The rejected mesh conversion changed the target shape")

    doc = App.newDocument("PersonalCadStlGuard")
    feature = doc.addObject("Part::Feature", "Box")
    feature.Shape = shape
    doc.recompute()
    guarded = output / "disabled-writer.stl"
    initial = b"Existing synthetic fixture\n"
    guarded.write_bytes(initial)
    # Part.export calls the native TopoShape::write for the supplied extension.
    try:
        Part.export([feature], str(guarded))
    except Exception as error:
        require("format not supported" in str(error).lower(), f"Unexpected Part writer error: {error}")
    else:
        raise RuntimeError("The native Part writer accepted STL")
    require(guarded.read_bytes() == initial, "The rejected native writer modified an existing file")
    App.closeDocument(doc.Name)
    return {"export_stl": "rejected", "convert_mesh": "rejected", "native_writer": "rejected",
            "existing_file_preserved": True, "tessellation_triangles": len(topology[1])}


def check_legacy(source, output, file_metrics):
    inputs = output / "legacy-input"
    inputs.mkdir()
    names = ("legacy-mixed.FCStd", "legacy-link-source.FCStd")
    original = {name: file_metrics(source / name) for name in names}
    for name in names:
        shutil.copyfile(source / name, inputs / name)
    before = {name: file_metrics(inputs / name) for name in names}
    require(before == original, "Copied legacy fixtures differ from the baseline")

    open_documents = set(App.listDocuments())
    try:
        App.openDocument(str(inputs / "legacy-mixed.FCStd"))
    except OSError as error:
        message = str(error)
    else:
        raise RuntimeError("Personal CAD accepted a legacy file requiring excluded modules")
    required_text = (
        "without losing unsupported objects", "LegacyParameters (Spreadsheet::Sheet)",
        "LegacyMesh (Mesh::Feature)", "ParametricLegacyBox depends on LegacyParameters",
        "and has expressions", "External project: legacy-link-source.FCStd",
    )
    require(all(fragment in message for fragment in required_text), f"Unexpected legacy diagnostic: {message}")
    require(set(App.listDocuments()) == open_documents, "Legacy rejection left a partial document open")
    after = {name: file_metrics(inputs / name) for name in names}
    require(before == after, "Legacy rejection changed a fixture file")
    require(original == {name: file_metrics(source / name) for name in names}, "Baseline input changed")
    return {"diagnostic": message, "hashes_before": before, "hashes_after": after, "partial_documents": 0}


def saved_material(path, object_name):
    with ZipFile(path) as archive:
        xml = ET.fromstring(archive.read("Document.xml"))
    node = xml.find(f"./ObjectData/Object[@name='{object_name}']/Properties/Property[@name='ShapeMaterial']")
    require(node is not None, f"{path.name} lacks the saved ShapeMaterial")
    return {"type": node.attrib["type"], "uuid": node.find("PropertyMaterial").attrib["uuid"].strip("{}").lower()}


def check_material_migration(source, output, file_metrics):
    baseline = json.loads((source / "report.json").read_text())
    expected = baseline["fixtures"]["step_roundtrip"]
    original = source / "keyboard-step-roundtrip.FCStd"
    copy = output / "stock-colored-input.FCStd"
    before = file_metrics(original)
    require(before == baseline["artifacts"][original.name], "Baseline color fixture differs from its receipt")
    shutil.copyfile(original, copy)

    def inspect(doc):
        features = [obj for obj in doc.Objects if obj.isDerivedFrom("Part::Feature")]
        require(len(features) == 1, "The stock color fixture should contain one Part feature")
        feature = features[0]
        shape = shape_metrics(feature.Shape)
        colors = [list(rgb) + [count] for rgb, count in sorted(color_counts(feature).items())]
        require(same_shape_metrics(shape, expected["shape"]) and shape["face_count"] == expected["shape"]["face_count"],
                "Opening or resaving the stock color fixture changed its geometry")
        require(colors == expected["face_colors"], f"Stock face colors changed: {colors}")
        return feature.Name, shape, colors

    doc = App.openDocument(str(copy))
    name, _, _ = inspect(doc)
    old = saved_material(copy, name)
    require(old["type"] == "Materials::PropertyMaterial" and bool(old["uuid"]),
            f"The stock fixture does not exercise library-backed material migration: {old}")
    migrated = output / "personal-migrated-colors.FCStd"
    doc.saveCopy(str(migrated))
    App.closeDocument(doc.Name)
    new = saved_material(migrated, name)
    require(new == {"type": "App::PropertyMaterial", "uuid": old["uuid"]},
            f"Migrated ShapeMaterial did not preserve the UUID: {new}")
    doc = App.openDocument(str(migrated))
    _, shape, colors = inspect(doc)
    App.closeDocument(doc.Name)
    require(file_metrics(copy) == file_metrics(original) == before, "Material migration changed its source")
    return {"source_material": old, "migrated_material": new, "shape": shape, "face_colors": colors,
            "unchanged_source": before}


def check_missing_link(source, output, file_metrics):
    baseline = json.loads((source / "report.json").read_text())
    assembly = source / "keyboard-assembly.FCStd"
    link_filename = baseline["fixtures"]["assembly"]["reopened"]["external_links"]["HousingLink"][0]
    before = file_metrics(assembly)
    require(before == baseline["artifacts"][assembly.name], "Baseline linked assembly differs from its receipt")
    isolated = output / "missing-link-input"
    isolated.mkdir()
    copy = isolated / assembly.name
    shutil.copyfile(assembly, copy)
    require(not (isolated / link_filename).exists(), "The missing link fixture unexpectedly has its source")

    existing = set(App.listDocuments())
    try:
        doc = App.openDocument(str(copy))
    except OSError as error:
        message = str(error)
        require(link_filename in message or "missing" in message.lower() or "fully restored" in message,
                f"Unexpected missing-link refusal: {message}")
        result = {"open_refused": message}
    else:
        require(doc.getObject("HousingLink").LinkedObject is None, "The absent assembly source resolved unexpectedly")
        require(not doc.canWriteRecoverySnapshot(), "The incomplete assembly allows a recovery snapshot")
        blocked = {}
        for command, args in (
            ("save", ()),
            ("saveAs", (str(isolated / "unsafe-save-as.FCStd"),)),
            ("saveCopy", (str(isolated / "unsafe-save-copy.FCStd"),)),
        ):
            try:
                getattr(doc, command)(*args)
            except Exception as error:
                message = str(error)
                require("Saving is disabled" in message, f"Unexpected {command} refusal: {message}")
                blocked[command] = message
            else:
                raise RuntimeError(f"The incomplete assembly allowed {command}")
            require(file_metrics(copy) == before, f"The rejected {command} changed the input copy")
        require(Path(doc.FileName).resolve() == copy.resolve(), "The rejected Save As changed the document path")
        App.closeDocument(doc.Name)
        result = {"save_operations_refused": blocked}

    require(set(App.listDocuments()) == existing, "The missing-link check left an unexpected document open")
    require(set(isolated.iterdir()) == {copy}, "The missing-link workflow wrote an unexpected output")
    require(file_metrics(copy) == file_metrics(assembly) == before, "The missing-link workflow changed the original")
    return {**result, "missing_source": link_filename, "unchanged_input": before}
