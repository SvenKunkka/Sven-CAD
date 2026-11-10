# SPDX-License-Identifier: LGPL-2.1-or-later
"""Thread callouts and BOM tables for Sven CAD drawings.

Callouts and table columns are TechDraw annotations with a Python proxy. They recompute from the
model, so a changed thread size or fastener updates the page, its DXF and its CSV together.
"""

import csv
import io
import math

import FreeCAD as App
import Part


THREAD_TYPES = {"PartDesign::InternalThread": "internal", "PartDesign::ExternalThread": "external"}
CALLOUT_GROUP = "Callout"
TABLE_GROUP = "BOM Table"
TEXT_SIZE = 3.5
THIN = 0.25
THICK = 0.5
SOLID_LINE = 1
DASHED_LINE = 2
BOM_COLUMNS = (
    ("Index", "Item"),
    ("Name", "Name"),
    (".PartNumber", "Part Number"),
    (".CompanyPartNumber", "Company P/N"),
    (".Specification", "Specification"),
    ("Quantity", "Qty"),
)


class DrawingError(ValueError):
    """A drawing request that the model cannot satisfy."""


# ---------------------------------------------------------------- thread discovery

def _global(obj):
    if hasattr(obj, "getGlobalPlacement"):
        return obj.getGlobalPlacement()
    parent = obj.getParentGeoFeatureGroup()
    return parent.getGlobalPlacement().multiply(obj.Placement) if parent else obj.Placement


def _number(value):
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _thread_text(kind, size, tolerance, length, through, left):
    text = size + (f"-{tolerance}" if tolerance and tolerance not in ("None", "Unspecified") else "")
    if kind == "internal":
        text += " THRU" if through else f" DEPTH {_number(length)}"
    else:
        text += f" LENGTH {_number(length)}"
    return text + (" LH" if left else "")


def _wall_extent(shape, start, axis, radius, limit):
    """Length of material along the hole wall from the opening, at most ``limit``."""
    far = start + axis * (limit + 1.0)
    side = axis.cross(App.Vector(1, 0, 0))
    if side.Length < 1e-6:
        side = axis.cross(App.Vector(0, 1, 0))
    side.normalize()
    offset = side * radius
    line = Part.LineSegment(start + offset - axis * 1e-3, far + offset).toShape()
    pieces = line.common(shape).Edges
    lengths = [(e.Vertexes[-1].Point - start - offset).dot(axis) for e in pieces
               if min(abs((v.Point - start - offset).dot(axis)) for v in e.Vertexes) < 1e-2]
    return min(limit, max(lengths)) if lengths else limit


def _feature_threads(obj, xform):
    kind = THREAD_TYPES.get(obj.TypeId)
    placement = xform.multiply(_global(obj))
    if kind:
        frame = placement.multiply(obj.ThreadFrame)
        major = obj.MajorDiameter.Value
        if kind == "internal":
            drawn = major
            tolerance = obj.ManufacturingClass
        else:
            drawn = obj.MinorDiameter.Value
            tolerance = obj.ManufacturingClass
        length = obj.ThreadLength.Value
        return [{
            "feature": obj, "index": 0, "kind": kind, "size": obj.ThreadSize, "pitch": obj.Pitch.Value,
            "tolerance": tolerance, "length": length, "through": False, "left": obj.LeftHanded,
            "modeled": obj.Modeled, "major": major, "drawn_diameter": drawn,
            "start": frame.Base, "axis": frame.Rotation.multVec(App.Vector(0, 0, 1)),
            "text": _thread_text(kind, obj.ThreadSize, tolerance, length, False, obj.LeftHanded),
        }]
    if obj.TypeId == "PartDesign::Hole" and obj.Threaded and hasattr(obj, "ThreadFrames"):
        through = obj.DepthType == "ThroughAll" and obj.ThreadDepthType == "Hole Depth"
        major = obj.ThreadDiameter.Value or obj.Diameter.Value
        shape = obj.Shape.copy()
        shape.Placement = placement
        found = []
        for index, local in enumerate(obj.ThreadFrames):
            frame = placement.multiply(local)
            axis = frame.Rotation.multVec(App.Vector(0, 0, 1))
            length = obj.ThreadDepth.Value
            if through or obj.DepthType == "ThroughAll":
                length = _wall_extent(shape, frame.Base, axis, major / 2 + 0.05, length)
            found.append({
                "feature": obj, "index": index, "kind": "internal", "size": obj.ThreadSize,
                "pitch": obj.ThreadPitch.Value, "tolerance": obj.ThreadClass, "length": length,
                "through": through, "left": obj.ThreadDirection == "Left", "modeled": obj.ModelThread,
                "major": major, "drawn_diameter": major, "start": frame.Base, "axis": axis,
                "text": _thread_text("internal", obj.ThreadSize, obj.ThreadClass, length, through,
                                     obj.ThreadDirection == "Left"),
            })
        return found
    return []


def thread_specs(objects):
    """Threads and threaded holes reachable from the given drawing sources, in global coordinates."""
    found = []
    seen = set()

    def visit(obj, xform, visible_only):
        if obj is None or not hasattr(obj, "TypeId"):
            return
        key = (obj.Document.Name, obj.Name, tuple(round(v, 9) for v in xform.toMatrix().A))
        if key in seen:
            return
        seen.add(key)
        if obj.isDerivedFrom("App::Link"):
            target = obj.LinkedObject
            if target is not None:
                link_global = _global(obj)
                visit(target, xform.multiply(link_global).multiply(_global(target).inverse()), True)
            return
        found.extend(_feature_threads(obj, xform))
        if obj.TypeId in THREAD_TYPES:
            support = obj.Support[0] if obj.Support else None
            visit(support, xform, False)
        elif obj.TypeId == "PartDesign::Body":
            visit(obj.Tip, xform, False)
        elif hasattr(obj, "BaseFeature") and obj.TypeId.startswith("PartDesign::"):
            visit(obj.BaseFeature, xform, False)
        elif obj.isDerivedFrom("App::Part") or obj.TypeId == "Assembly::AssemblyObject":
            for child in obj.Group:
                if getattr(child, "Visibility", True):
                    visit(child, xform, True)

    for obj in objects:
        visit(obj, App.Placement(), True)
    return found


def view_threads(view):
    return thread_specs(list(view.Source) + list(getattr(view, "XSource", [])))


# ---------------------------------------------------------------- view geometry

def view_center(view):
    """The centre TechDraw used for the current projection, so cosmetic edges and callouts agree."""
    if hasattr(view, "getOriginalCentroid"):
        return view.getOriginalCentroid()
    return view.getGeometricCenter()


def _to_view(view, point, center=None):
    """Unscaled view coordinates, Y up, of a global point."""
    return view.projectPoint(point - (view_center(view) if center is None else center))


def _to_page(view, point, center=None):
    local = _to_view(view, point, center)
    angle = math.radians(view.Rotation.Value if hasattr(view.Rotation, "Value") else view.Rotation)
    x = local.x * math.cos(angle) - local.y * math.sin(angle)
    y = local.x * math.sin(angle) + local.y * math.cos(angle)
    scale = view.getScale()
    return App.Vector(view.X.Value + scale * x, view.Y.Value + scale * y, 0)


def _page_to_view(view, point):
    scale = view.getScale()
    angle = -math.radians(view.Rotation.Value if hasattr(view.Rotation, "Value") else view.Rotation)
    dx = (point.x - view.X.Value) / scale
    dy = (point.y - view.Y.Value) / scale
    return App.Vector(dx * math.cos(angle) - dy * math.sin(angle), dx * math.sin(angle) + dy * math.cos(angle), 0)


def representation(view, spec):
    """How the thread axis appears in the view: end, side or oblique."""
    start = view.projectPoint(spec["start"])
    end = view.projectPoint(spec["start"] + spec["axis"])
    length = (end - start).Length
    if length < 1e-6:
        return "end"
    if abs(length - 1.0) < 1e-6:
        return "side"
    return "oblique"


def _thread_lines(view, spec):
    """Cosmetic edges of the simplified ISO 6410 thread for one view."""
    if spec["modeled"]:
        return []
    mode = representation(view, spec)
    start, axis = spec["start"], spec["axis"]
    r_drawn = spec["drawn_diameter"] / 2
    r_major = spec["major"] / 2
    internal = spec["kind"] == "internal"
    if mode == "end":
        return [("arc", start, r_drawn, 90.0, 360.0, SOLID_LINE, THIN)]
    if mode != "side":
        return []
    view_dir = view.Direction
    side = view_dir.cross(axis)
    side.normalize()
    end = start + axis * spec["length"]
    style = DASHED_LINE if internal else SOLID_LINE
    lines = [("line", start + side * r_drawn, end + side * r_drawn, style, THIN),
             ("line", start - side * r_drawn, end - side * r_drawn, style, THIN)]
    lines.append(("line", end + side * r_major, end - side * r_major, style, THIN if internal else THICK))
    return lines


def _add_edges(view, lines):
    tags = []
    for line in lines:
        if line[0] == "arc":
            _, center, radius, a1, a2, style, weight = line
            tag = view.makeCosmeticCircleArc3d(center, radius, a1, a2, style, weight)
        elif line[0] == "line":
            _, p1, p2, style, weight = line
            tag = view.makeCosmeticLine3D(p1, p2, style, weight)
        else:
            _, p1, p2, style, weight = line
            tag = view.makeCosmeticLine(p1, p2, style, weight)
        if tag:
            tags.append(tag)
    return tags


def _remove_edges(view, tags):
    for tag in tags:
        try:
            view.removeCosmeticEdge(tag)
        except Exception:
            pass


def _signature(*parts):
    return repr([round(p, 6) if isinstance(p, float) else p for p in parts])


# ---------------------------------------------------------------- callouts

class ThreadCallout:
    """A DrawViewAnnotation that names one thread and draws it in its view."""

    def __init__(self, obj, view, spec):
        obj.Proxy = self
        obj.addProperty("App::PropertyLinkGlobal", "SourceView", CALLOUT_GROUP, "Drawing view that shows the thread")
        obj.addProperty("App::PropertyLinkGlobal", "ThreadFeature", CALLOUT_GROUP, "Thread, internal thread or hole")
        obj.addProperty("App::PropertyInteger", "HoleIndex", CALLOUT_GROUP, "Hole of a multi-hole feature")
        obj.addProperty("App::PropertyVector", "TextOffset", CALLOUT_GROUP, "Text position from the thread, page mm")
        obj.addProperty("App::PropertyString", "ThreadSpec", CALLOUT_GROUP, "Callout text from the model")
        obj.addProperty("App::PropertyString", "Representation", CALLOUT_GROUP, "End, side or oblique view")
        obj.addProperty("App::PropertyStringList", "CosmeticTags", CALLOUT_GROUP, "Cosmetic edges in the view")
        obj.addProperty("App::PropertyString", "Signature", CALLOUT_GROUP, "Geometry of the drawn edges")
        for name in ("ThreadSpec", "Representation"):
            obj.setEditorMode(name, 1)
        for name in ("CosmeticTags", "Signature"):
            obj.setEditorMode(name, 2)
        obj.SourceView = view
        obj.ThreadFeature = spec["feature"]
        obj.HoleIndex = spec["index"]
        obj.TextSize = TEXT_SIZE
        obj.TextOffset = App.Vector(12, 10, 0)

    def dumps(self):
        return None

    def loads(self, state):
        return None

    def spec(self, obj):
        view = obj.SourceView
        if view is None or obj.ThreadFeature is None:
            raise DrawingError("The callout has lost its view or thread.")
        for spec in view_threads(view):
            if spec["feature"] == obj.ThreadFeature and spec["index"] == obj.HoleIndex:
                return spec
        raise DrawingError(f"{obj.ThreadFeature.Label} is no longer shown in {view.Label}.")

    def execute(self, obj):
        view = obj.SourceView
        spec = self.spec(obj)
        anchor = _to_page(view, spec["start"], view_center(view))
        text_pos = anchor + obj.TextOffset
        width = 0.62 * obj.TextSize.Value * len(spec["text"])
        near = text_pos.x - width / 2 if obj.TextOffset.x >= 0 else text_pos.x + width / 2
        far = text_pos.x + width / 2 if obj.TextOffset.x >= 0 else text_pos.x - width / 2
        base_y = text_pos.y - 0.7 * obj.TextSize.Value
        leader = [("view", _page_to_view(view, anchor), _page_to_view(view, App.Vector(near, base_y, 0)),
                   SOLID_LINE, THIN),
                  ("view", _page_to_view(view, App.Vector(near, base_y, 0)),
                   _page_to_view(view, App.Vector(far, base_y, 0)), SOLID_LINE, THIN)]
        lines = _thread_lines(view, spec) + leader
        signature = _signature(view.Name, [tuple(round(c, 6) for c in v) if isinstance(v, App.Vector) else v
                                           for line in lines for v in line])
        mode = representation(view, spec)
        if obj.ThreadSpec != spec["text"]:
            obj.ThreadSpec = spec["text"]
        if obj.Representation != mode:
            obj.Representation = mode
        if list(obj.Text) != [spec["text"]]:
            obj.Text = [spec["text"]]
        if abs(obj.X.Value - text_pos.x) > 1e-9 or abs(obj.Y.Value - text_pos.y) > 1e-9:
            obj.X = text_pos.x
            obj.Y = text_pos.y
        if signature != obj.Signature or not all(_has_edge(view, t) for t in obj.CosmeticTags):
            _remove_edges(view, obj.CosmeticTags)
            obj.CosmeticTags = _add_edges(view, lines)
            obj.Signature = signature
            # Cosmetic edges are drawn without another hidden line pass of the view.
            view.purgeTouched()

    def onDocumentRestored(self, obj):
        obj.Proxy = self


def _has_edge(view, tag):
    try:
        return view.getCosmeticEdge(tag) is not None
    except Exception:
        return False


def callouts(view):
    return [obj for obj in view.InList if is_callout(obj) and obj.SourceView == view]


def is_callout(obj):
    return obj is not None and obj.TypeId == "TechDraw::DrawViewAnnotationPython" and hasattr(obj, "ThreadFeature")


def add_callouts(view):
    """Annotate every thread of a view that has no callout yet. Returns the new callouts."""
    page = view.findParentPage()
    if page is None:
        raise DrawingError("The view is not on a drawing page.")
    if not view.getVisibleEdges():
        raise DrawingError("The view has no geometry yet. Recompute it and try again.")
    existing = {(c.ThreadFeature, c.HoleIndex) for c in callouts(view)}
    created = []
    for spec in view_threads(view):
        if (spec["feature"], spec["index"]) in existing:
            continue
        obj = view.Document.addObject("TechDraw::DrawViewAnnotationPython", "ThreadCallout")
        ThreadCallout(obj, view, spec)
        obj.Label = spec["text"]
        page.addView(obj)
        created.append(obj)
    for obj in created:
        obj.recompute()
    return created


def remove_callout(obj):
    if obj.SourceView is not None:
        _remove_edges(obj.SourceView, obj.CosmeticTags)
    obj.Document.removeObject(obj.Name)


# ---------------------------------------------------------------- BOM

def bom_rows(bom):
    columns = list(bom.columnsNames)
    entries = list(bom.BomRows)
    if not columns:
        return []
    return [entries[start:start + len(columns)] for start in range(0, len(entries), len(columns))]


def bom_table(bom):
    """Heading and rows in the order printed on the page and written to CSV."""
    columns = list(bom.columnsNames)
    headings = dict(BOM_COLUMNS)
    return [headings.get(c, c.lstrip(".")) for c in columns], bom_rows(bom)


def make_bom(assembly):
    """Create or reuse the Sven BOM of an assembly. Fasteners group by catalog part number."""
    try:
        import SvenFasteners
        SvenFasteners.complete_sources(assembly.Document)
    except ImportError:
        pass
    group = next((o for o in assembly.Group if o.TypeId == "Assembly::BomGroup"), None)
    if group is None:
        group = assembly.newObject("Assembly::BomGroup", "Bills of Materials")
    for obj in group.Group:
        if obj.TypeId == "Assembly::BomObject" and list(obj.columnsNames) == [c for c, _ in BOM_COLUMNS]:
            obj.recompute()
            return obj
    bom = group.newObject("Assembly::BomObject", "SvenBom")
    bom.Label = "BOM"
    bom.columnsNames = [c for c, _ in BOM_COLUMNS]
    # A part container is one BOM line; its bodies and thread features are not separate parts.
    bom.detailParts = False
    bom.recompute()
    return bom


class BomColumn:
    """One BOM column printed on a drawing page."""

    def __init__(self, obj, bom, column):
        obj.Proxy = self
        obj.addProperty("App::PropertyLinkGlobal", "Bom", TABLE_GROUP, "Bill of materials shown in the table")
        obj.addProperty("App::PropertyString", "Column", TABLE_GROUP, "BOM column shown")
        obj.addProperty("App::PropertyString", "Heading", TABLE_GROUP, "Printed heading")
        obj.Bom = bom
        obj.Column = column
        obj.Heading = dict(BOM_COLUMNS).get(column, column.lstrip("."))
        obj.TextSize = TEXT_SIZE
        obj.LineSpace = 160

    def dumps(self):
        return None

    def loads(self, state):
        return None

    def execute(self, obj):
        bom = obj.Bom
        if bom is None:
            raise DrawingError("The BOM table has lost its bill of materials.")
        columns = list(bom.columnsNames)
        if obj.Column not in columns:
            raise DrawingError(f"The BOM has no column {obj.Column}.")
        index = columns.index(obj.Column)
        text = [obj.Heading] + [row[index] for row in bom_rows(bom)]
        if list(obj.Text) != text:
            obj.Text = text

    def onDocumentRestored(self, obj):
        obj.Proxy = self


def is_bom_column(obj):
    return obj is not None and obj.TypeId == "TechDraw::DrawViewAnnotationPython" and hasattr(obj, "Bom")


def bom_columns(page):
    return sorted((v for v in page.Views if is_bom_column(v)), key=lambda c: c.X.Value)


def _column_widths(headings, rows):
    widths = []
    for i, heading in enumerate(headings):
        longest = max([len(heading)] + [len(r[i]) for r in rows])
        widths.append(max(8.0, 0.62 * TEXT_SIZE * longest + 4.0))
    return widths


def place_bom(page, bom, right=None, top=None):
    """Print the BOM on a page as aligned annotation columns, reusing an existing table."""
    template = page.Template
    width = float(template.Width) if template is not None and hasattr(template, "Width") else 420.0
    height = float(template.Height) if template is not None and hasattr(template, "Height") else 297.0
    right = width - 15.0 if right is None else right
    top = height - 15.0 if top is None else top
    bom.recompute()
    headings, rows = bom_table(bom)
    columns = {c.Column: c for c in bom_columns(page) if c.Bom == bom}
    widths = _column_widths(headings, rows)
    x = right - sum(widths)
    lines = len(rows) + 1
    y = top - TEXT_SIZE * 1.6 * lines / 2
    placed = []
    for column, w in zip(bom.columnsNames, widths):
        obj = columns.pop(column, None)
        if obj is None:
            obj = page.Document.addObject("TechDraw::DrawViewAnnotationPython", "BomColumn")
            BomColumn(obj, bom, column)
            obj.Label = "BOM " + obj.Heading
            page.addView(obj)
        obj.X = x + w / 2
        obj.Y = y
        x += w
        obj.recompute()
        placed.append(obj)
    for stale in columns.values():
        page.Document.removeObject(stale.Name)
    return placed


def write_csv(path, bom):
    headings, rows = bom_table(bom)
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(headings)
    writer.writerows(rows)
    with open(path, "w", encoding="utf-8", newline="") as output:
        output.write(stream.getvalue())
    return path


def page_bom(page):
    boms = {c.Bom for c in bom_columns(page) if c.Bom is not None}
    return boms.pop() if len(boms) == 1 else None
