# SPDX-License-Identifier: LGPL-2.1-or-later
"""Assembly drawing sheet from a named state: three views, an optional section, a static
exploded view with trail lines, a template title block, a dimension and the BOM.

The views read a StateShape, a feature that recomputes the visible parts of the assembly
for a named state (and, for the exploded view, their exploded positions) without moving
anything in the assembly."""

import datetime
import os

import FreeCAD as App
import Part

TEMPLATE = "Templates/ISO/A3_Landscape_ISO5457_advanced.svg"
STANDARD_SCALES = (10.0, 5.0, 2.0, 1.0, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01)
PROJECTIONS = ("Front", "Top", "Right")


def _replace_module():
    import SvenReplace
    return SvenReplace


def _interference_module():
    import SvenInterference
    return SvenInterference


# ---------------------------------------------------------------- state shape

def hidden_nodes(assembly, state):
    """Node paths hidden by the state, using the shared-visibility rule of apply_state."""
    if state is None:
        return None
    R = _replace_module()
    hidden = set(R.state_hidden(state))
    wanted = {}
    nodes = R._nodes(assembly)
    for path, parent, name, _child in nodes:
        owner = parent.getLinkedObject(True) or parent
        key = (owner.Document.Name, owner.Name, name)
        wanted[key] = wanted.get(key, True) and path not in hidden
    out = set()
    for path, parent, name, _child in nodes:
        owner = parent.getLinkedObject(True) or parent
        if not wanted[(owner.Document.Name, owner.Name, name)]:
            out.add(path)
    return out


def _prefixes(sub):
    parts = sub.rstrip(".").split(".")
    return [".".join(parts[: i + 1]) + "." for i in range(len(parts))]


def state_instances(assembly, state=None):
    """Visible solid instances of the assembly under a state (or as currently shown)."""
    SI = _interference_module()
    hidden = hidden_nodes(assembly, state)
    if hidden is None:
        return SI.collect([assembly], visible_only=True)
    found = SI.collect([assembly], visible_only=False)
    out = []
    for inst in found:
        sub = inst.sub
        if not any(p in hidden for p in _prefixes(sub)):
            out.append(inst)
    return out


def exploded_moves(assembly, view):
    """{object name: global delta placement} for every object an exploded view moves, computed in memory."""
    if view is None or not hasattr(view, "Proxy") or not hasattr(view.Proxy, "_calculateExplodedPlacements"):
        return {}
    final, _lines = view.Proxy._calculateExplodedPlacements(view)
    return {obj.Name: (obj, placement) for obj, placement in final.items()}


def exploded_shape(assembly, instances, view, trails=True):
    """Instance shapes moved to their exploded positions, plus one trail line per moved component."""
    moves = exploded_moves(assembly, view)
    shapes, groups = [], {}
    for inst in instances:
        chain = assembly.getSubObjectList(inst.sub)
        parts = inst.sub.rstrip(".").split(".")
        delta = None
        for depth, obj in enumerate(chain[1:], 1):
            if obj.Name in moves:
                prefix = ".".join(parts[:depth]) + "."
                g_obj = assembly.getSubObject(prefix, retType=3)
                new = moves[obj.Name][1]
                g_parent = g_obj.multiply(obj.Placement.inverse())
                delta = g_parent.multiply(new).multiply(g_obj.inverse())
                groups.setdefault(prefix, [[], delta])[0].append(inst.shape)
                break
        shapes.append(inst.shape.transformed(delta.toMatrix()) if delta is not None else inst.shape)
    lines = []
    if trails:
        for prefix, (members, delta) in sorted(groups.items()):
            start = Part.makeCompound(members).BoundBox.Center
            end = delta.multVec(start)
            if (end - start).Length > 1e-6:
                lines.append(Part.LineSegment(start, end).toShape())
    return shapes, lines


class StateShape:
    """Part feature showing an assembly under a named state, optionally exploded."""

    def __init__(self, obj):
        obj.Proxy = self
        obj.addProperty("App::PropertyLinkGlobal", "SvenAssembly", "Sven sheet", "Assembly shown")
        obj.addProperty("App::PropertyLinkGlobal", "SvenState", "Sven sheet", "Named state; none shows the assembly as it is")
        obj.addProperty("App::PropertyBool", "SvenExploded", "Sven sheet", "Use the state's exploded view")
        obj.addProperty("App::PropertyBool", "SvenTrails", "Sven sheet", "Draw trail lines for exploded parts")
        obj.addProperty("App::PropertyInteger", "SvenSolids", "Sven sheet", "Solids shown", 0, True)
        obj.addProperty("App::PropertyInteger", "SvenTrailCount", "Sven sheet", "Trail lines shown", 0, True)
        obj.SvenTrails = True

    def execute(self, obj):
        assembly = obj.SvenAssembly
        if assembly is None:
            obj.Shape = Part.Shape()
            return
        state = obj.SvenState
        instances = state_instances(assembly, state)
        view = getattr(state, "SvenExplodedView", None) if (state is not None and obj.SvenExploded) else None
        if view is not None:
            shapes, lines = exploded_shape(assembly, instances, view, obj.SvenTrails)
        else:
            shapes, lines = [i.shape for i in instances], []
        obj.SvenSolids = len(shapes)
        obj.SvenTrailCount = len(lines)
        obj.Shape = Part.makeCompound(shapes + lines) if shapes or lines else Part.Shape()

    def onDocumentRestored(self, obj):
        obj.Proxy = self

    def dumps(self):
        return None

    def loads(self, state):
        return None


def make_state_shape(doc, assembly, state=None, exploded=False, name="SvenStateShape"):
    obj = doc.addObject("Part::FeaturePython", name)
    StateShape(obj)
    obj.SvenAssembly = assembly
    obj.SvenState = state
    obj.SvenExploded = exploded
    obj.recompute()
    if obj.ViewObject is not None:
        obj.ViewObject.Proxy = 0
        obj.Visibility = False
    else:
        obj.Visibility = False
    return obj


# ---------------------------------------------------------------- sheet

def template_path():
    return os.path.join(App.getResourceDir(), "Mod", "TechDraw", TEMPLATE)


def scale_text(scale):
    if scale >= 1:
        return f"{scale:g}:1"
    return f"1:{1 / scale:g}"


def pick_scale(box, area):
    """Largest standard scale at which three views of the box fit the area (width, height) in mm."""
    w = box.XLength + box.YLength
    h = box.ZLength + box.YLength
    for scale in STANDARD_SCALES:
        if w * scale <= area[0] and h * scale <= area[1]:
            return scale
    return STANDARD_SCALES[-1]


KEEP_FIELDS = ("language_code",)


def _fill_template(template, values):
    """Fill the title block. Sample entries of the template (names, material, numbers) are cleared,
    not printed: what is not known from the model is left blank for the user."""
    texts = dict(template.EditableTexts)
    filled = {}
    for key in texts:
        if key in values:
            texts[key] = values[key]
            filled[key] = values[key]
        elif key not in KEEP_FIELDS:
            texts[key] = ""
    template.EditableTexts = texts
    return filled


def _extreme_vertices(view):
    """Indices of the leftmost and rightmost visible vertices of a view (for an overall width)."""
    verts = view.getVisibleVertexes()
    if not verts:
        return None
    left = min(range(len(verts)), key=lambda i: (verts[i].x, verts[i].y))
    right = max(range(len(verts)), key=lambda i: (verts[i].x, -verts[i].y))
    return left, right


def _wait_for_views(page):
    """Hidden line removal runs in the background in the GUI; vertices exist only after it."""
    if not App.GuiUp:
        return True
    import SvenDrawingGui
    return SvenDrawingGui.wait_for_views(page)


def make_sheet(assembly, state=None, section=True, exploded=True, fields=None, name="AssemblySheet"):
    """Create the drawing page. Returns a dict of the created objects."""
    import TechDraw
    doc = assembly.Document
    doc.openTransaction("Assembly sheet")
    try:
        page = doc.addObject("TechDraw::DrawPage", name)
        page.Label = f"{assembly.Label} sheet" + (f" ({state.Label})" if state is not None else "")
        template = doc.addObject("TechDraw::DrawSVGTemplate", name + "Template")
        template.Template = template_path()
        page.Template = template
        width, height = float(template.Width), float(template.Height)
        source = make_state_shape(doc, assembly, state, False, name + "Shape")
        box = source.Shape.BoundBox
        scale = pick_scale(box, (width * 0.45, height * 0.55))
        group = doc.addObject("TechDraw::DrawProjGroup", name + "Views")
        page.addView(group)
        group.Source = [source]
        group.ProjectionType = "First angle"
        group.ScaleType = "Custom"
        group.Scale = scale
        views = {}
        for kind in PROJECTIONS:
            views[kind] = group.addProjection(kind)
        group.X = width * 0.28
        group.Y = height * 0.58
        group.recompute()
        made = {"page": page, "template": template, "source": source, "group": group, "views": views,
                "scale": scale}
        if section:
            front = views["Front"]
            sec = doc.addObject("TechDraw::DrawViewSection", name + "Section")
            page.addView(sec)
            sec.BaseView = front
            sec.Source = [source]
            sec.ScaleType = "Custom"
            sec.Scale = scale
            sec.SectionNormal = App.Vector(1, 0, 0)
            sec.Direction = App.Vector(1, 0, 0)
            sec.XDirection = App.Vector(0, 1, 0)
            sec.SectionOrigin = box.Center
            sec.SectionSymbol = "A"
            sec.SectionDirection = "Right" if "Right" in sec.getEnumerationsOfProperty("SectionDirection") else \
                sec.SectionDirection
            sec.X = width * 0.58
            sec.Y = height * 0.58
            made["section"] = sec
        view_obj = getattr(state, "SvenExplodedView", None) if state is not None else None
        if exploded and view_obj is not None:
            xsource = make_state_shape(doc, assembly, state, True, name + "ExplodedShape")
            xview = doc.addObject("TechDraw::DrawViewPart", name + "Exploded")
            page.addView(xview)
            xview.Source = [xsource]
            xview.Direction = App.Vector(1, -1, 1)
            xview.XDirection = App.Vector(1, 1, 0)
            xview.ScaleType = "Custom"
            xbox = xsource.Shape.BoundBox
            xview.Scale = pick_scale(xbox, (width * 0.35, height * 0.45))
            xview.X = width * 0.80
            xview.Y = height * 0.62
            made["exploded_source"] = xsource
            made["exploded"] = xview
        doc.recompute()
        _wait_for_views(page)
        front = views["Front"]
        ends = _extreme_vertices(front)
        if ends is not None:
            dim = doc.addObject("TechDraw::DrawViewDimension", name + "Width")
            dim.Type = "DistanceX"
            dim.References2D = [(front, f"Vertex{ends[0]}"), (front, f"Vertex{ends[1]}")]
            page.addView(dim)
            dim.X = 0.0
            dim.Y = -(box.ZLength * scale / 2.0 + 8.0)  # below the view, clear of the geometry
            made["dimension"] = dim
        values = {"title": assembly.Label, "scale": scale_text(scale),
                  "date_of_issue": datetime.date.today().isoformat(), "sheet_number": "1/1"}
        if state is not None:
            values["supplementary_title_1"] = state.Label
        values.update(fields or {})
        made["fields"] = _fill_template(template, values)
        try:
            import SvenDrawing
            bom = SvenDrawing.make_bom(assembly)
            SvenDrawing.place_bom(page, bom, right=width - 15.0, top=height - 15.0)
            made["bom"] = bom
        except Exception as exc:
            App.Console.PrintWarning(f"Sven sheet: BOM not placed: {exc}\n")
        doc.recompute()
    except Exception:
        doc.abortTransaction()
        raise
    doc.commitTransaction()
    return made
