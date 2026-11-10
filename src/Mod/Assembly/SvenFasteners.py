# SPDX-License-Identifier: LGPL-2.1-or-later
"""Common fastener catalog, shared source solids and linked instances for Sven CAD."""

import json
import math
import re
from functools import lru_cache
from pathlib import Path

import FreeCAD as App
import Part

CATALOG_PATH = Path(__file__).with_name("SvenFastenersCatalog.json")
LIBRARY_NAME = "SvenFastenerLibrary"
GROUP = "Fastener"
KIND_WORDS = {
    "screw": {"screw", "screws", "bolt", "bolts", "capscrew", "螺钉", "螺丝", "螺栓"},
    "nut": {"nut", "nuts", "螺母"},
    "washer": {"washer", "washers", "垫圈", "垫片", "平垫"},
}
PROPERTIES = (
    ("FastenerKind", "App::PropertyString", "Fastener kind"),
    ("PartNumber", "App::PropertyString", "Catalog part number"),
    ("CompanyPartNumber", "App::PropertyString", "Company part number, empty until assigned"),
    ("Standard", "App::PropertyString", "Fastener standard"),
    ("StandardEdition", "App::PropertyString", "Standard edition as recorded by the data source"),
    ("Description", "App::PropertyString", "Fastener description"),
    ("Size", "App::PropertyString", "Nominal metric thread size"),
    ("ThreadPitch", "App::PropertyLength", "Nominal coarse thread pitch"),
    ("NominalLength", "App::PropertyLength", "Nominal screw length, zero for nuts and washers"),
    ("ThreadRepresentation", "App::PropertyString", "How the thread is represented"),
    ("DataSource", "App::PropertyString", "Source of the dimensions"),
    ("DataSourceUrl", "App::PropertyString", "Address of the dimension source"),
    ("DataRetrieved", "App::PropertyString", "Date the dimension source was read"),
    ("CatalogVersion", "App::PropertyString", "Sven CAD fastener catalog version"),
)


class FastenerError(ValueError):
    """A request that the fastener catalog cannot satisfy."""


@lru_cache(maxsize=1)
def catalog():
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def _number(value):
    return f"{value:g}"


@lru_cache(maxsize=1)
def items():
    data = catalog()
    result = []
    for family in data["families"]:
        source = data["sources"][family["source"]]
        for size in family["sizes"]:
            lengths = size.get("lengths") or [None]
            for length in lengths:
                suffix = f"x{_number(length)}" if length is not None else ""
                item = {
                    "kind": family["kind"],
                    "part_number": f"{family['code']}-{size['size']}{suffix}",
                    "company_part_number": "",
                    "standard": family["standard"],
                    "edition": family["edition"],
                    "name": family["name"],
                    "size": size["size"],
                    "pitch": size.get("pitch", 0.0),
                    "length": float(length) if length is not None else 0.0,
                    "dimensions": {k: v for k, v in size.items() if k not in ("size", "lengths")},
                    "source_id": family["source"],
                    "source": source,
                    "catalog_version": data["catalog_version"],
                }
                item["description"] = (f"{family['standard']} {size['size']}"
                                       + (f"x{_number(length)}" if length is not None else "")
                                       + f" {family['name']['en']}")
                result.append(item)
    return tuple(result)


def find(part_number):
    for item in items():
        if item["part_number"].lower() == part_number.lower():
            return item
    raise FastenerError(f"Unknown fastener part number: {part_number}")


def _token_matches(token, item):
    standard_number = item["standard"].split()[-1]
    length = re.fullmatch(r"(?:l|x|×)?(\d+(?:\.\d+)?)(?:mm)?", token)
    if length:
        # Bare numbers mean a length, except the number of a standard such as 4762.
        if token == standard_number:
            return True
        return bool(item["length"]) and math.isclose(item["length"], float(length.group(1)))
    if token in item["part_number"].lower():
        return True
    if token == item["size"].lower():
        return True
    kind = [k for k, words in KIND_WORDS.items() if token in words]
    if kind:
        return item["kind"] == kind[0]
    standard = item["standard"].lower()
    if token in standard or token == standard.replace(" ", "") or token in standard.split():
        return True
    if token in item["name"]["en"].lower() or token in item["name"]["zh"]:
        return True
    sized = re.fullmatch(r"(m\d+(?:\.\d+)?)[x×*](\d+(?:\.\d+)?)", token)
    if sized:
        return (item["size"].lower() == sized.group(1) and item["length"]
                and math.isclose(item["length"], float(sized.group(2))))
    return False


def search(query="", kind=None):
    """Return catalog items whose fields match every token of the query."""
    tokens = [t for t in re.split(r"[\s,，;；]+", query.strip().lower()) if t]
    found = []
    for item in items():
        if kind and item["kind"] != kind:
            continue
        if all(_token_matches(token, item) for token in tokens):
            found.append(item)
    return found


def _hexagon(across_flats, height, z0=0.0):
    radius = across_flats / math.sqrt(3.0)
    points = [App.Vector(radius * math.cos(math.radians(30 + 60 * i)),
                         radius * math.sin(math.radians(30 + 60 * i)), z0) for i in range(6)]
    face = Part.Face(Part.makePolygon(points + [points[0]]))
    return face.extrude(App.Vector(0, 0, height))


def make_shape(item):
    """Build the local solid. The bearing face lies on z = 0 and the axis is +Z."""
    dims = item["dimensions"]
    if item["kind"] == "screw":
        head = Part.makeCylinder(dims["dk"] / 2, dims["k"])
        socket = _hexagon(dims["s"], dims["t"] + 0.01, dims["k"] - dims["t"])
        shank = Part.makeCylinder(dims["d"] / 2, item["length"], App.Vector(0, 0, -item["length"]))
        shape = head.cut(socket).fuse(shank).removeSplitter()
    elif item["kind"] == "nut":
        shape = _hexagon(dims["s"], dims["m"]).cut(
            Part.makeCylinder(dims["d"] / 2, dims["m"] + 2, App.Vector(0, 0, -1))).removeSplitter()
    elif item["kind"] == "washer":
        shape = Part.makeCylinder(dims["d2"] / 2, dims["h"]).cut(
            Part.makeCylinder(dims["d1"] / 2, dims["h"] + 2, App.Vector(0, 0, -1))).removeSplitter()
    else:
        raise FastenerError(f"Unsupported fastener kind: {item['kind']}")
    solids = shape.Solids
    if len(solids) != 1 or not solids[0].isValid():
        raise FastenerError(f"Could not build a valid solid for {item['part_number']}")
    return solids[0]


def library(doc, create=True):
    group = doc.getObject(LIBRARY_NAME)
    if group is None and create:
        group = doc.addObject("App::DocumentObjectGroup", LIBRARY_NAME)
        group.Label = "Fastener Library"
    return group


def is_source(obj):
    return obj is not None and obj.isDerivedFrom("Part::Feature") and "PartNumber" in obj.PropertiesList \
        and "CatalogVersion" in obj.PropertiesList


def sources(doc):
    return [obj for obj in doc.Objects if is_source(obj)]


def find_source(doc, item):
    for obj in sources(doc):
        if obj.PartNumber == item["part_number"] and obj.CatalogVersion == item["catalog_version"]:
            return obj
    return None


def ensure_source(doc, item):
    """Return the one source solid of a catalog item, creating it once per document."""
    existing = find_source(doc, item)
    if existing is not None:
        return existing, False
    source = doc.addObject("Part::Feature", "Fastener_" + re.sub(r"\W", "_", item["part_number"]))
    for name, kind, doc_string in PROPERTIES:
        source.addProperty(kind, name, GROUP, doc_string)
    source.FastenerKind = item["kind"]
    source.PartNumber = item["part_number"]
    source.CompanyPartNumber = item["company_part_number"]
    source.Standard = item["standard"]
    source.StandardEdition = item["edition"]
    source.Description = item["description"]
    source.Size = item["size"]
    source.ThreadPitch = item["pitch"]
    source.NominalLength = item["length"]
    source.ThreadRepresentation = "Schematic, nominal diameter"
    source.DataSource = f"{item['source']['publisher']}: {item['source']['title']}"
    source.DataSourceUrl = item["source"]["url"]
    source.DataRetrieved = item["source"]["retrieved"]
    source.CatalogVersion = item["catalog_version"]
    for name, *_ in PROPERTIES:
        source.setEditorMode(name, 1)
    source.Shape = make_shape(item)
    source.Label = item["part_number"]
    library(doc).addObject(source)
    return source, True


def is_instance(obj):
    return obj is not None and obj.TypeId == "App::Link" and is_source(obj.getLinkedObject(False))


def instances(doc):
    return [obj for obj in doc.Objects if is_instance(obj)]


def _next_label(doc, part_number):
    used = set()
    for link in instances(doc):
        match = re.fullmatch(re.escape(part_number) + r" #(\d+)", link.Label)
        if match:
            used.add(int(match.group(1)))
    number = 1
    while number in used:
        number += 1
    return f"{part_number} #{number}"


def insert(doc, item, placements, parent=None):
    """Create one link per placement to a shared source. Returns (links, source, created)."""
    if not placements:
        raise FastenerError("Choose at least one placement.")
    source, created = ensure_source(doc, item)
    links = []
    for placement in placements:
        link = doc.addObject("App::Link", "Fastener")
        link.setLink(source)
        link.Placement = placement
        link.Label = _next_label(doc, item["part_number"])
        if parent is not None:
            parent.addObject(link)
        links.append(link)
    return links, source, created


def replace(links, item):
    """Point only the given links at another catalog item; other instances stay as they are."""
    if not links or not all(is_instance(link) for link in links):
        raise FastenerError("Select fastener instances to replace.")
    doc = links[0].Document
    if any(link.Document != doc for link in links):
        raise FastenerError("Select fastener instances from one document.")
    source, created = ensure_source(doc, item)
    for link in links:
        if link.getLinkedObject(False) != source:
            link.setLink(source)
            link.Label = _next_label(doc, item["part_number"])
    return source, created


def summary(doc):
    """Count fastener instances per part number, including link arrays."""
    rows = {}
    for link in instances(doc):
        source = link.getLinkedObject(False)
        count = link.ElementCount if getattr(link, "ElementCount", 0) else 1
        row = rows.setdefault(source.PartNumber, {
            "part_number": source.PartNumber, "company_part_number": source.CompanyPartNumber,
            "description": source.Description, "standard": source.Standard, "size": source.Size,
            "length_mm": source.NominalLength.Value, "kind": source.FastenerKind,
            "source": source.DataSource, "catalog_version": source.CatalogVersion,
            "count": 0, "instances": [],
        })
        row["count"] += count
        row["instances"].append(link.Name)
    return sorted(rows.values(), key=lambda row: row["part_number"])


def edge_placement(solid, edge_name):
    """Place a fastener on a circular hole edge, bearing on the adjacent planar face.

    ``solid`` is the shape that owns the edge, already in the target coordinates.
    """
    try:
        edge = solid.getElement(edge_name)
    except Exception as error:
        raise FastenerError("Select circular hole edges.") from error
    curve = edge.Curve
    if edge.ShapeType != "Edge" or not isinstance(curve, Part.Circle) or not edge.isClosed():
        raise FastenerError("Select circular hole edges.")
    for face in solid.ancestorsOfType(edge, Part.Face):
        if isinstance(face.Surface, Part.Plane):
            u, v = face.Surface.parameter(curve.Center)
            normal = face.normalAt(u, v)
            return App.Placement(curve.Center, App.Rotation(App.Vector(0, 0, 1), normal))
    raise FastenerError("The hole edge must lie on a planar face.")


def default_placements(item, count):
    spacing = 2.0 * max(item["dimensions"].get(k, 0.0) for k in ("dk", "s", "d2", "d"))
    return [App.Placement(App.Vector(i * spacing, 0, 0), App.Rotation()) for i in range(count)]
