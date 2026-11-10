"""Synthetic RC2-compatible CAD models for the personal-edition acceptance run."""

from collections import Counter
import math
from pathlib import Path
from time import monotonic, sleep

import FreeCAD as App
import Part
import Sketcher


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def vector(value):
    return [value.x, value.y, value.z]


def placement(value):
    return {"base_mm": vector(value.Base), "quaternion": list(value.Rotation.Q)}


def shape_metrics(shape):
    require(not shape.isNull() and shape.isValid(), "Empty or invalid CAD shape")
    box = shape.BoundBox
    return {
        "solid_count": len(shape.Solids),
        "face_count": len(shape.Faces),
        "size_mm": [box.XLength, box.YLength, box.ZLength],
        "origin_mm": [box.XMin, box.YMin, box.ZMin],
        "volume_mm3": shape.Volume,
    }


def same_shape_metrics(left, right):
    close = lambda a, b: math.isclose(a, b, rel_tol=1e-8, abs_tol=1e-5)
    return (
        left["solid_count"] == right["solid_count"]
        and all(close(a, b) for a, b in zip(left["size_mm"], right["size_mm"]))
        and all(close(a, b) for a, b in zip(left["origin_mm"], right["origin_mm"]))
        and close(left["volume_mm3"], right["volume_mm3"])
    )


def color_counts(feature):
    return Counter(tuple(round(component, 4) for component in color[:3])
                   for color in feature.ViewObject.DiffuseColor)


def rectangle(body, name, x, y, length, width):
    sketch = body.newObject("Sketcher::SketchObject", name)
    corners = [(x, y), (x + length, y), (x + length, y + width), (x, y + width)]
    for start, end in zip(corners, corners[1:] + corners[:1]):
        sketch.addGeometry(
            Part.LineSegment(App.Vector(*start, 0), App.Vector(*end, 0)), False
        )
    for edge in range(4):
        sketch.addConstraint(Sketcher.Constraint("Coincident", edge, 2, (edge + 1) % 4, 1))
        kind = "Horizontal" if edge % 2 == 0 else "Vertical"
        sketch.addConstraint(Sketcher.Constraint(kind, edge))
    if x == y == 0:
        sketch.addConstraint(Sketcher.Constraint("Coincident", 0, 1, -1, 1))
    else:
        sketch.addConstraint(Sketcher.Constraint("DistanceX", 0, 1, x))
        sketch.addConstraint(Sketcher.Constraint("DistanceY", 0, 1, y))
    length_id = sketch.addConstraint(Sketcher.Constraint("Distance", 0, length))
    width_id = sketch.addConstraint(Sketcher.Constraint("Distance", 1, width))
    sketch.renameConstraint(length_id, "Length")
    sketch.renameConstraint(width_id, "Width")
    return sketch, length_id


def make_housing():
    doc = App.newDocument("KeyboardHousing")
    body = doc.addObject("PartDesign::Body", "Housing")
    body.Label = "Keyboard Housing"
    outline, length_constraint = rectangle(body, "Outline", 0.0, 0.0, 120.0, 45.0)
    pad = body.newObject("PartDesign::Pad", "Blank")
    pad.Profile = outline
    pad.Length = 8.0
    doc.recompute()
    outline.Visibility = False

    cavity, _ = rectangle(body, "CavityProfile", 3.0, 3.0, 114.0, 39.0)
    cavity.setExpression("Placement.Base.z", "Blank.Length")
    cavity.setExpression("Constraints.Length", "Outline.Constraints.Length - 6 mm")
    cavity.setExpression("Constraints.Width", "Outline.Constraints.Width - 6 mm")
    pocket = body.newObject("PartDesign::Pocket", "Cavity")
    pocket.Profile = cavity
    pocket.Length = 5.0
    doc.recompute()
    pad.Visibility = False
    cavity.Visibility = False

    before = shape_metrics(body.Shape)
    require(outline.FullyConstrained and cavity.FullyConstrained, "Housing sketch underconstrained")
    require(before["solid_count"] == 1, "Housing should contain exactly one solid")
    require(math.isclose(before["volume_mm3"], 20970.0, abs_tol=1e-5), "Incorrect cavity volume")
    outline.setDatum(length_constraint, App.Units.Quantity("124 mm"))
    doc.recompute()
    after = shape_metrics(body.Shape)
    require(math.isclose(after["size_mm"][0], 124.0, abs_tol=1e-5), "Length edit did not propagate")
    require(math.isclose(after["volume_mm3"], 21630.0, abs_tol=1e-5), "Cavity expression did not update")
    return doc, body, before, after


def make_assembly(housing, path):
    import Assembly
    import JointObject

    doc = App.newDocument("KeyboardAssembly")
    doc.saveAs(str(path))
    assembly = doc.addObject("Assembly::AssemblyObject", "Assembly")
    joints = assembly.newObject("Assembly::JointGroup", "Joints")
    base = assembly.newObject("App::Link", "HousingLink")
    base.LinkedObject = housing
    lid_source = doc.addObject("Part::Box", "Lid")
    lid_source.Length = 124.0
    lid_source.Width = 45.0
    lid_source.Height = 2.0
    lid = assembly.newObject("App::Link", "LidLink")
    lid.LinkedObject = lid_source
    doc.recompute()
    lid_source.Visibility = False

    ground = joints.newObject("App::FeaturePython", "GroundHousing")
    JointObject.GroundedJoint(ground, base)
    JointObject.ViewProviderGroundedJoint(ground.ViewObject)
    fixed = joints.newObject("App::FeaturePython", "FixedLid")
    JointObject.Joint(fixed, JointObject.JointTypes.index("Fixed"))
    JointObject.ViewProviderJoint(fixed.ViewObject)
    fixed.Detach1 = True
    fixed.Detach2 = True
    fixed.Reference1 = (base, ["Face1", "Face1"])
    fixed.Reference2 = (lid, ["Face1", "Face1"])
    fixed.Placement1 = App.Placement(App.Vector(0, 0, 8), App.Rotation())
    fixed.Placement2 = App.Placement()
    doc.recompute()
    # Set the displaced pose after creating the joint, whose property callbacks
    # may already have run the assembly solver.
    initial = App.Vector(10, 5, 15)
    lid.Placement = App.Placement(initial, App.Rotation())
    result = assembly.solve()
    doc.recompute()
    require(result == 0, f"Static assembly solve failed: {result}")
    actual = lid.Placement.Base
    require((initial - actual).Length > 1.0, "Static solve did not move lid")
    require((actual - App.Vector(0, 0, 8)).Length < 1e-5, f"Incorrect solved lid position: {vector(actual)}")
    return doc


def wait_for_edges(view):
    from PySide import QtCore

    deadline = monotonic() + 20.0
    while monotonic() < deadline:
        QtCore.QCoreApplication.processEvents()
        edges = view.getVisibleEdges()
        if edges:
            return len(edges)
        sleep(0.02)
    raise RuntimeError(f"{view.Name} has no visible edges after recompute: {view.State}")


def drawing_page(doc, page_name, template_name):
    page = doc.addObject("TechDraw::DrawPage", page_name)
    template = doc.addObject("TechDraw::DrawSVGTemplate", template_name)
    template_path = Path(App.getResourceDir()) / "Mod/TechDraw/Templates/Default_Template_A4_Landscape.svg"
    require(template_path.is_file(), "Installed TechDraw A4 template is missing")
    template.Template = str(template_path)
    page.Template = template
    return page


def make_drawing(doc, body, output):
    import TechDraw
    import TechDrawGui

    page = drawing_page(doc, "Drawing", "Template")
    top = doc.addObject("TechDraw::DrawViewPart", "Top")
    page.addView(top)
    top.Source = [body]
    top.Direction = (0.0, 0.0, 1.0)
    top.X = 130.0
    top.Y = 140.0
    doc.recompute()
    top_edges = wait_for_edges(top)

    section = doc.addObject("TechDraw::DrawViewSection", "Section")
    page.addView(section)
    section.Source = [body]
    section.BaseView = top
    section.Direction = (0.0, 1.0, 0.0)
    section.SectionNormal = (0.0, 1.0, 0.0)
    section.SectionOrigin = (62.0, 22.5, 4.0)
    section.X = 130.0
    section.Y = 65.0
    doc.recompute()
    section_edges = wait_for_edges(section)
    page.ViewObject.show()
    TechDrawGui.exportPageAsPdf(page, str(output / "keyboard-drawing.pdf"))
    TechDrawGui.exportPageAsSvg(page, str(output / "keyboard-drawing.svg"))
    TechDrawGui.export([page], str(output / "keyboard-drawing.dxf"))
    return {"top_edges": top_edges, "section_edges": section_edges}


def make_assembly_drawing(doc):
    import TechDraw

    page = drawing_page(doc, "AssemblyDrawing", "AssemblyDrawingTemplate")
    page.Scale = 1.0
    front = doc.addObject("TechDraw::DrawViewPart", "AssemblyFront")
    page.addView(front)
    front.Source = [doc.HousingLink, doc.LidLink]
    front.Direction = (0.0, -1.0, 0.0)
    front.XDirection = (1.0, 0.0, 0.0)
    front.X = 145.0
    front.Y = 110.0
    doc.recompute()
    return assembly_drawing_metrics(doc)


def assembly_drawing_metrics(doc):
    front = doc.AssemblyFront
    source_links = {obj.Name: obj.TypeId for obj in front.Source}
    require(source_links == {"HousingLink": "App::Link", "LidLink": "App::Link"},
            f"Assembly drawing lost its link sources: {source_links}")
    require(any(view.Name == front.Name for view in doc.AssemblyDrawing.Views),
            "The assembly front view is missing from the drawing page")
    wait_for_edges(front)
    edges = front.getVisibleEdges(True)
    bounds = Part.makeCompound(edges).BoundBox
    width, height = bounds.XLength, bounds.YLength
    # Front view contains the full housing width and both the 8 mm housing and 2 mm lid.
    require(math.isclose(width, 124.0, abs_tol=1e-5) and math.isclose(height, 10.0, abs_tol=1e-5),
            f"The assembly front view does not include both linked solids: {width} × {height} mm")
    return {"sources": source_links, "visible_edges": len(edges),
            "projected_size_mm": [round(width, 6), round(height, 6)]}


def export_assembly_pdf(doc, output):
    import TechDrawGui

    page = doc.AssemblyDrawing
    page.ViewObject.show()
    path = output / "keyboard-assembly-drawing.pdf"
    TechDrawGui.exportPageAsPdf(page, str(path))
    require(path.read_bytes().startswith(b"%PDF-"), "Invalid assembly PDF output")


def make_legacy(output):
    import Mesh
    import Spreadsheet

    link_source = App.newDocument("LegacyLinkSource")
    link_source.addObject("Part::Box", "ReferenceBlock")
    link_source.recompute()
    link_source.saveAs(str(output / "legacy-link-source.FCStd"))

    doc = App.newDocument("LegacyMixed")
    doc.saveAs(str(output / "legacy-mixed.FCStd"))
    table = doc.addObject("Spreadsheet::Sheet", "LegacyParameters")
    table.set("A1", "27 mm")
    table.setAlias("A1", "FixtureLength")
    feature = doc.addObject("Part::Box", "ParametricLegacyBox")
    feature.setExpression("Length", "LegacyParameters.FixtureLength")
    link = doc.addObject("App::Link", "ExternalBlock")
    link.LinkedObject = link_source.ReferenceBlock
    mesh = doc.addObject("Mesh::Feature", "LegacyMesh")
    mesh.Mesh = Mesh.Mesh([[(0, 0, 0), (10, 0, 0), (0, 10, 0)]])
    doc.recompute()
    require(math.isclose(feature.Length.Value, 27.0, abs_tol=1e-6), "Legacy expression did not resolve")
    return doc, link_source
