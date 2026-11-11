# SPDX-License-Identifier: LGPL-2.1-or-later
"""Static interference and minimum clearance between the solids of an assembly.

Every visible solid under the chosen roots becomes one instance with its placement in the
document, so linked and repeated parts are checked where they sit. Pairs whose boxes cannot
touch are skipped; the rest are measured with exact geometry and sorted into contact, overlap
(with the shared volume), clearance below a limit, or undetermined when the geometry cannot
answer, for example an open shell or a failed boolean."""

import math
import time

import FreeCAD as App
import Part

CONTACT = "contact"
OVERLAP = "overlap"
CLEARANCE = "clearance"
UNDETERMINED = "undetermined"
KINDS = (OVERLAP, CONTACT, CLEARANCE, UNDETERMINED)

SKIP_TYPES = ("App::Origin", "App::OriginFeature", "App::Line", "App::Plane", "App::Point",
              "PartDesign::Plane", "PartDesign::Line", "PartDesign::Point", "PartDesign::CoordinateSystem",
              "Sketcher::SketchObject", "Spreadsheet::Sheet")
LEAF_TYPES = ("PartDesign::Body", "Part::Feature")


class InterferenceError(ValueError):
    """A check that cannot start, such as an empty scope."""


class Instance:
    """One solid where it sits in the document, reached from a root through a sub-element path."""

    __slots__ = ("root", "sub", "obj", "label", "shape", "box", "closed", "volume", "source")

    def __init__(self, root, sub, obj, label, shape):
        self.root = root
        self.sub = sub
        self.obj = obj
        self.label = label
        self.shape = shape
        self.box = shape.BoundBox
        solids = shape.Solids
        self.closed = bool(solids) and all(s.isClosed() and s.isValid() for s in solids)
        self.volume = sum(s.Volume for s in solids) if self.closed else 0.0
        linked = obj.getLinkedObject(True)
        self.source = linked.Name if linked is not None else obj.Name

    @property
    def key(self):
        return f"{self.root.Name}.{self.sub}" if self.sub else self.root.Name

    def describe(self):
        return {"key": self.key, "label": self.label, "object": self.obj.Name, "source": self.source}


def _is_leaf(obj):
    linked = obj.getLinkedObject(True)
    if linked is None:
        return False
    if linked.isDerivedFrom("App::Part") or linked.isDerivedFrom("App::DocumentObjectGroup"):
        return False
    if linked.TypeId == "App::Link" or linked.isDerivedFrom("App::LinkGroup"):
        return False
    return any(linked.isDerivedFrom(t) for t in LEAF_TYPES)


def _skipped(obj):
    linked = obj.getLinkedObject(True) or obj
    return any(linked.isDerivedFrom(t) for t in SKIP_TYPES) or linked.TypeId.startswith("Assembly::Joint") \
        or linked.TypeId in ("Assembly::JointGroup", "Assembly::ViewGroup", "Assembly::BomGroup",
                             "Assembly::SimulationGroup")


def _visible(parent, name, child):
    try:
        state = parent.isElementVisible(name)
    except Exception:
        state = -1
    if state >= 0:
        return bool(state)
    return bool(getattr(child, "Visibility", True))


def collect(roots, visible_only=True):
    """Return the solid instances under the roots, each with its global shape.

    A root is a document object or a (root object, sub-element path) pair, as a selection gives."""
    found = []
    seen = set()

    def add(root, sub, obj, labels):
        shape = Part.getShape(root, sub, needSubElement=False, transform=True) if sub else \
            Part.getShape(root, "", needSubElement=False, transform=True)
        if shape.isNull() or not (shape.Solids or shape.Faces):
            return
        inst = Instance(root, sub, obj, " / ".join(labels), shape)
        if inst.key not in seen:
            seen.add(inst.key)
            found.append(inst)

    def walk(root, parent, sub, labels, depth):
        if depth > 32:
            return
        for name in parent.getSubObjects():
            child = parent.getSubObject(name, retType=1)
            if child is None or _skipped(child):
                continue
            if visible_only and not _visible(parent, name.rstrip("."), child):
                continue
            path = sub + name if name.endswith(".") else sub + name + "."
            if _is_leaf(child):
                add(root, path, child, labels + [child.Label])
            else:
                walk(root, child, path, labels + [child.Label], depth + 1)

    for entry in roots:
        root, sub = entry if isinstance(entry, tuple) else (entry, "")
        if sub and not sub.endswith("."):
            sub = sub[: sub.rfind(".") + 1]  # drop a selected face or edge name
        target = root.getSubObject(sub, retType=1) if sub else root
        if target is None or _skipped(target):
            continue
        chain = [o.Label for o in root.getSubObjectList(sub)] if sub else [root.Label]
        if _is_leaf(target):
            if sub or not visible_only or root.Visibility:
                add(root, sub, target, chain)
        else:
            walk(root, target, sub, chain, 0)
    return found


def default_roots(doc):
    """Visible top-level containers and solids of a document."""
    roots = []
    for obj in doc.RootObjects:
        if _skipped(obj) or not getattr(obj, "Visibility", True):
            continue
        if obj.isDerivedFrom("App::Part") or obj.TypeId == "App::Link" or _is_leaf(obj) \
                or obj.isDerivedFrom("App::DocumentObjectGroup"):
            roots.append(obj)
    return roots


def _candidate_pairs(instances, margin):
    """Pairs whose bounding boxes, grown by margin, overlap. Sweep along X."""
    order = sorted(range(len(instances)), key=lambda i: instances[i].box.XMin)
    active = []
    pairs = []
    for i in order:
        bi = instances[i].box
        x0 = bi.XMin - margin
        active = [j for j in active if instances[j].box.XMax >= x0]
        for j in active:
            bj = instances[j].box
            if bj.YMax + margin < bi.YMin or bi.YMax + margin < bj.YMin:
                continue
            if bj.ZMax + margin < bi.ZMin or bi.ZMax + margin < bj.ZMin:
                continue
            pairs.append((min(i, j), max(i, j)))
        active.append(i)
    pairs.sort()
    return pairs


def _vec(v):
    return [round(v.x, 6), round(v.y, 6), round(v.z, 6)]


def _inside(a, b, tolerance):
    """True when a vertex of a lies inside the closed solid b."""
    if not (a.closed and b.closed):
        return False
    if not b.box.isInside(a.box.Center) and not any(b.box.isInside(v.Point) for v in a.shape.Vertexes[:1]):
        return False
    point = a.shape.Vertexes[0].Point if a.shape.Vertexes else a.box.Center
    return any(s.isInside(point, tolerance, True) for s in b.shape.Solids)


def _match(rule, inst):
    return rule == inst.key or rule == inst.obj.Name or rule == inst.source or rule == inst.root.Name \
        or (inst.sub and rule in inst.sub.split("."))


def measure(a, b, clearance_limit, contact_tolerance):
    """Classify one pair. Returns a result dict, or None when the pair is clear of the limit."""
    result = {"a": a.describe(), "b": b.describe()}
    try:
        distance, points, _ = a.shape.distToShape(b.shape)
    except Exception as error:
        result.update(kind=UNDETERMINED, reason=f"distance failed: {error}")
        return result
    if distance > contact_tolerance:
        if _inside(a, b, contact_tolerance) or _inside(b, a, contact_tolerance):
            distance = 0.0
        else:
            if clearance_limit is None or distance > clearance_limit:
                return None
            result.update(kind=CLEARANCE, distance=round(distance, 9),
                          location=_vec((points[0][0] + points[0][1]) * 0.5),
                          points=[_vec(points[0][0]), _vec(points[0][1])])
            return result
    if not (a.closed and b.closed):
        result.update(kind=UNDETERMINED, distance=round(distance, 9), location=_vec(points[0][0]),
                      reason="not a closed solid")
        return result
    try:
        common = a.shape.common(b.shape)
        volume = sum(s.Volume for s in common.Solids) if common.Solids else 0.0
    except Exception as error:
        result.update(kind=UNDETERMINED, distance=0.0, location=_vec(points[0][0]),
                      reason=f"boolean failed: {error}")
        return result
    volume_tolerance = max(1e-6, 1e-7 * min(a.volume, b.volume))
    if volume > volume_tolerance:
        box = common.BoundBox
        result.update(kind=OVERLAP, distance=0.0, volume=round(volume, 9), location=_vec(box.Center),
                      box=[round(box.XMin, 6), round(box.YMin, 6), round(box.ZMin, 6),
                           round(box.XMax, 6), round(box.YMax, 6), round(box.ZMax, 6)])
        smaller = min(a.volume, b.volume)
        if a.source == b.source and smaller > 0 and abs(volume - smaller) <= 1e-6 * smaller + 1e-9 \
                and abs(a.volume - b.volume) <= 1e-6 * smaller:
            result["duplicate"] = True
    else:
        result.update(kind=CONTACT, distance=round(distance, 9), location=_vec(points[0][0]))
    return result


def check(roots=None, doc=None, *, visible_only=True, clearance_limit=None, contact_tolerance=1e-6,
          allowed=(), include_contacts=True, cancel=None, progress=None, instances=None):
    """Check every candidate pair under the roots.

    allowed: pairs of rules, each matching an instance key, object, source or root name; a matching
    pair is still reported but marked allowed and left out of the problem count.
    cancel(): return True to stop; the report then has cancelled=True and the pairs done so far.
    progress(done, total): called between pairs."""
    started = time.monotonic()
    if instances is None:
        if roots is None:
            if doc is None:
                raise InterferenceError("Nothing to check.")
            roots = default_roots(doc)
        instances = collect(roots, visible_only)
    if len(instances) < 2:
        raise InterferenceError("Select at least two solids or an assembly that contains them.")
    collected = time.monotonic()
    margin = clearance_limit or 0.0
    pairs = _candidate_pairs(instances, margin + contact_tolerance)
    results = []
    cancelled = False
    done = 0
    for done, (i, j) in enumerate(pairs, 1):
        if cancel is not None and cancel():
            cancelled = True
            done -= 1
            break
        a, b = instances[i], instances[j]
        result = measure(a, b, clearance_limit, contact_tolerance)
        if result is not None and (include_contacts or result["kind"] != CONTACT):
            result["allowed"] = any((_match(x, a) and _match(y, b)) or (_match(x, b) and _match(y, a))
                                    for x, y in allowed)
            results.append(result)
        if progress is not None:
            progress(done, len(pairs))
    order = {k: n for n, k in enumerate(KINDS)}
    results.sort(key=lambda r: (r["allowed"], order[r["kind"]], -r.get("volume", 0.0), r.get("distance", 0.0)))
    counts = {k: sum(1 for r in results if r["kind"] == k) for k in KINDS}
    problems = {k: sum(1 for r in results if r["kind"] == k and not r["allowed"]) for k in KINDS}
    return {"instances": len(instances), "candidate_pairs": len(pairs), "pairs_checked": done,
            "cancelled": cancelled, "clearance_limit": clearance_limit, "contact_tolerance": contact_tolerance,
            "counts": counts, "problems": problems, "allowed": sum(1 for r in results if r["allowed"]),
            "duplicates": sum(1 for r in results if r.get("duplicate")),
            "results": results, "collect_seconds": round(collected - started, 3),
            "seconds": round(time.monotonic() - started, 3)}


def overlap_shape(doc, result):
    """The shared volume of an overlap result, recomputed from the two instances."""
    def shape_of(entry):
        root_name, _, sub = entry["key"].partition(".")
        root = doc.getObject(root_name)
        return Part.getShape(root, sub, needSubElement=False, transform=True)
    return shape_of(result["a"]).common(shape_of(result["b"]))
