# SPDX-License-Identifier: LGPL-2.1-or-later
"""Replace a supplier component with a new version and keep the assembly around it.

match() pairs the solid parts of the old and new versions by name and geometry, finds
the pose that maps the new version onto the old one and sorts every part into same,
changed, renamed, ambiguous, missing or added. replace() relinks the chosen instances,
keeps their position, remaps the joint, exploded-view and named-state references that
point into the component and reports what it could not map, so a person can settle it.

Named states record which part instances are hidden and which exploded view is shown.
"""

import math
import re

import FreeCAD as App
import Part

import SvenInterference as SI

SAME = "same"
CHANGED = "changed"
RENAMED = "renamed"
AMBIGUOUS = "ambiguous"
MISSING = "missing"
ADDED = "added"
STATUSES = (SAME, CHANGED, RENAMED, AMBIGUOUS, MISSING, ADDED)

MAPPED = "mapped"
APPROXIMATE = "approximate"
UNRESOLVED = "unresolved"
DROPPED = "dropped"

LENGTH_TOLERANCE = 1e-4  # mm, for positions and lengths
RELATIVE_TOLERANCE = 1e-6
APPROXIMATE_REACH = 0.5  # mm, how far a changed face may move and still be offered as the same face
STATE_GROUP = "SvenStates"


class ReplaceError(ValueError):
    pass


def _close(a, b, rel=RELATIVE_TOLERANCE, absolute=1e-9):
    return abs(a - b) <= max(absolute, rel * max(abs(a), abs(b)))


_COPY_SUFFIX = re.compile(r"\d{3}$")


def base_label(label):
    """A label without the 001-style suffix FreeCAD adds when two objects share a name."""
    stripped = _COPY_SUFFIX.sub("", label)
    return stripped or label


class Leaf:
    """A solid part of a component, with its shape in the component's own frame."""

    __slots__ = ("path", "obj", "label", "name", "shape", "volume", "area", "center", "moments", "size")

    def __init__(self, path, obj, label, shape):
        self.path = path
        self.obj = obj
        self.label = label
        self.name = base_label(label)
        self.shape = shape
        solids = shape.Solids
        self.volume = sum(s.Volume for s in solids) if solids else 0.0
        self.area = shape.Area
        try:
            props = shape.Solids[0] if len(solids) == 1 else shape
            self.center = App.Vector(props.CenterOfMass) if hasattr(props, "CenterOfMass") \
                else shape.BoundBox.Center
        except Exception:
            self.center = shape.BoundBox.Center
        try:
            self.moments = sorted(shape.Solids[0].PrincipalProperties["Moments"]) if len(solids) == 1 \
                else sorted(shape.BoundBox.XLength * v for v in (1, 2, 3))
        except Exception:
            self.moments = [0.0, 0.0, 0.0]
        box = shape.BoundBox
        self.size = sorted((box.XLength, box.YLength, box.ZLength))

    def same_geometry(self, other):
        scale = max(self.size[-1], other.size[-1], 1.0)
        return (_close(self.volume, other.volume, absolute=1e-6 * scale ** 3)
                and _close(self.area, other.area, absolute=1e-6 * scale ** 2)
                and all(_close(a, b, rel=1e-5, absolute=1e-6 * scale ** 5)
                        for a, b in zip(self.moments, other.moments)))

    def part_number(self):
        return str(getattr(self.obj, "PartNumber", "") or "")

    def describe(self):
        return {"path": self.path, "label": self.label, "volume": round(self.volume, 6),
                "area": round(self.area, 6), "center": [round(v, 6) for v in self.center]}


def component(obj):
    """The object a replaceable instance shows: the link target, or the object itself."""
    return obj.getLinkedObject(True) if obj.TypeId == "App::Link" else obj


def leaves(root):
    """The solid parts of a component, keyed by their path below it ("" when it is a single solid)."""
    target = component(root)
    found = []
    if SI._is_leaf(target):
        found.append(Leaf("", target, target.Label, Part.getShape(target, "", needSubElement=False,
                                                                  transform=False)))
        return found

    def walk(parent, sub, depth):
        if depth > 32:
            return
        for name in parent.getSubObjects():
            child = parent.getSubObject(name, retType=1)
            if child is None or SI._skipped(child):
                continue
            path = sub + (name if name.endswith(".") else name + ".")
            if SI._is_leaf(child):
                shape = Part.getShape(target, path, needSubElement=False, transform=False)
                if not shape.isNull() and (shape.Solids or shape.Faces):
                    found.append(Leaf(path, child, child.Label, shape))
            else:
                walk(child, path, depth + 1)

    walk(target, "", 0)
    return found


# ---------------------------------------------------------------- pose

def _kabsch(pairs):
    """Rigid transform taking the new centres onto the old ones, or None when they do not fix a rotation."""
    if len(pairs) < 3:
        return None
    try:
        import numpy
    except ImportError:
        return None
    old = numpy.array([[p.x, p.y, p.z] for p, _ in pairs])
    new = numpy.array([[q.x, q.y, q.z] for _, q in pairs])
    co, cn = old.mean(axis=0), new.mean(axis=0)
    h = (new - cn).T @ (old - co)
    if numpy.linalg.matrix_rank(h, tol=1e-9 * max(1.0, numpy.abs(h).max())) < 2:
        return None
    u, _s, vt = numpy.linalg.svd(h)
    d = numpy.sign(numpy.linalg.det(vt.T @ u.T)) or 1.0
    rot = vt.T @ numpy.diag([1.0, 1.0, d]) @ u.T
    r = [[float(rot[i][j]) for j in range(3)] for i in range(3)]
    matrix = App.Matrix(r[0][0], r[0][1], r[0][2], 0, r[1][0], r[1][1], r[1][2], 0,
                        r[2][0], r[2][1], r[2][2], 0)
    rotation = App.Rotation(matrix)
    shift = App.Vector(*co) - rotation.multVec(App.Vector(*cn))
    return App.Placement(shift, rotation)


def _axes_candidates(old, new):
    """Rotations that line up the principal axes of one old and new part, for components with few parts."""
    out = []
    try:
        po = old.shape.Solids[0].PrincipalProperties
        pn = new.shape.Solids[0].PrincipalProperties
    except Exception:
        return out
    ao = [App.Vector(po[k]) for k in ("FirstAxisOfInertia", "SecondAxisOfInertia", "ThirdAxisOfInertia")]
    an = [App.Vector(pn[k]) for k in ("FirstAxisOfInertia", "SecondAxisOfInertia", "ThirdAxisOfInertia")]
    for s1, s2 in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
        o = [ao[0] * s1, ao[1] * s2]
        o.append(o[0].cross(o[1]))
        mo = App.Matrix(o[0].x, o[1].x, o[2].x, 0, o[0].y, o[1].y, o[2].y, 0, o[0].z, o[1].z, o[2].z, 0)
        n = [an[0], an[1], an[0].cross(an[1])]
        mn = App.Matrix(n[0].x, n[1].x, n[2].x, 0, n[0].y, n[1].y, n[2].y, 0, n[0].z, n[1].z, n[2].z, 0)
        rotation = App.Rotation(mo.multiply(mn.transposed()))
        out.append(App.Placement(old.center - rotation.multVec(new.center), rotation))
    return out


def _pose_error(transform, pairs):
    """Largest centre and box-corner mismatch of the paired parts after moving the new ones."""
    worst = 0.0
    for old, new in pairs:
        worst = max(worst, (transform.multVec(new.center) - old.center).Length)
        moved = new.shape.transformed(transform.toMatrix()).BoundBox
        box = old.shape.BoundBox
        worst = max(worst, abs(moved.XMin - box.XMin), abs(moved.YMin - box.YMin), abs(moved.ZMin - box.ZMin),
                    abs(moved.XMax - box.XMax), abs(moved.YMax - box.YMax), abs(moved.ZMax - box.ZMax))
    return worst


def find_pose(pairs):
    """The placement taking the new component's frame onto the old one, its residual and how it was found."""
    if not pairs:
        return App.Placement(), 0.0, "none"
    candidates = [("identity", App.Placement())]
    shift = App.Vector()
    for old, new in pairs:
        shift += old.center - new.center
    candidates.append(("translation", App.Placement(shift * (1.0 / len(pairs)), App.Rotation())))
    fit = _kabsch([(o.center, n.center) for o, n in pairs])
    if fit is not None:
        candidates.append(("rotation", fit))
    largest = max(pairs, key=lambda p: p[0].volume)
    candidates += [("axes", c) for c in _axes_candidates(*largest)]
    scored = [(_pose_error(c, pairs), i, name, c) for i, (name, c) in enumerate(candidates)]
    best = min(scored, key=lambda s: (round(s[0] / LENGTH_TOLERANCE), s[1]))
    return best[3], best[0], best[2]


# ---------------------------------------------------------------- matching

def _entry(status, old=None, new=None, candidates=(), offset=None, note=""):
    return {"status": status, "old": old.path if old else None, "new": new.path if new else None,
            "old_label": old.label if old else "", "new_label": new.label if new else "",
            "old_part_number": old.part_number() if old else "",
            "new_part_number": new.part_number() if new else "",
            "candidates": [c.path for c in candidates], "offset": offset, "note": note}


def match(old_root, new_root, resolutions=None):
    """Pair the parts of two component versions. resolutions maps an old path to a chosen new path or None."""
    old_leaves = leaves(old_root)
    new_leaves = leaves(new_root)
    if not old_leaves or not new_leaves:
        raise ReplaceError("both versions must contain solid parts")
    resolutions = dict(resolutions or {})
    by_path = {leaf.path: leaf for leaf in new_leaves}
    pairs = {}  # old path -> (status, old, new, note)
    free_new = list(new_leaves)
    rest_old = []
    for old in old_leaves:
        if old.path in resolutions:
            chosen = by_path.get(resolutions[old.path]) if resolutions[old.path] is not None else None
            if chosen is not None and chosen in free_new:
                free_new.remove(chosen)
                pairs[old.path] = (SAME if old.same_geometry(chosen) and old.name == chosen.name
                                   else CHANGED if old.name == chosen.name else RENAMED, old, chosen, "manual")
            else:
                pairs[old.path] = (MISSING, old, None, "manual")
            continue
        rest_old.append(old)

    def labelled(items):
        groups = {}
        for leaf in items:
            groups.setdefault(leaf.name, []).append(leaf)
        return groups

    # 1. names found once in each version
    new_groups = labelled(free_new)
    old_groups = labelled(rest_old)
    for label, olds in old_groups.items():
        news = new_groups.get(label, [])
        if len(olds) == 1 and len(news) == 1:
            old, new = olds[0], news[0]
            pairs[old.path] = (SAME if old.same_geometry(new) else CHANGED, old, new, "")
            free_new.remove(new)
            rest_old.remove(old)
    # 2. geometry found once among what is left
    for old in list(rest_old):
        same = [n for n in free_new if old.same_geometry(n)]
        twins = [o for o in rest_old if o.same_geometry(old)]
        if len(same) == 1 and len(twins) == 1:
            new = same[0]
            pairs[old.path] = (SAME if new.name == old.name else RENAMED, old, new, "")
            free_new.remove(new)
            rest_old.remove(old)

    rigid = [(o, n) for status, o, n, _note in pairs.values() if n is not None and o.same_geometry(n)]
    if not rigid:
        rigid = [(o, n) for status, o, n, _note in pairs.values() if n is not None]
    transform, residual, source = find_pose(rigid)

    def distance(old, new):
        return (transform.multVec(new.center) - old.center).Length

    ambiguous = {}
    # 3. repeated names and repeated geometry, by position after the pose
    for old in sorted(rest_old, key=lambda o: o.path):
        options = [n for n in free_new if n.name == old.name] or [n for n in free_new if old.same_geometry(n)]
        if not options:
            continue
        ranked = sorted(options, key=lambda n: distance(old, n))
        best = ranked[0]
        if distance(old, best) <= LENGTH_TOLERANCE * 10 and (
                len(ranked) == 1 or distance(old, ranked[1]) > distance(old, best) + LENGTH_TOLERANCE * 10):
            status = SAME if best.name == old.name and old.same_geometry(best) else \
                CHANGED if best.name == old.name else RENAMED
            pairs[old.path] = (status, old, best, "position")
            free_new.remove(best)
        else:
            ambiguous[old.path] = (old, ranked)
    for path in list(ambiguous):
        old, ranked = ambiguous[path]
        ranked = [n for n in ranked if n in free_new]
        if ranked:
            ambiguous[path] = (old, ranked)
        else:
            del ambiguous[path]
            pairs[path] = (MISSING, old, None, "")

    entries = []
    for old in old_leaves:
        if old.path in pairs:
            status, o, n, note = pairs[old.path]
            entries.append(_entry(status, o, n, offset=round(distance(o, n), 9) if n else None, note=note))
        elif old.path in ambiguous:
            o, ranked = ambiguous[old.path]
            entries.append(_entry(AMBIGUOUS, o, None, ranked, note="choose one"))
        else:
            near = sorted(free_new, key=lambda n: distance(old, n))[:3]
            entries.append(_entry(MISSING, old, None, near))
    claimed = {c for e in entries if e["status"] == AMBIGUOUS for c in e["candidates"]}
    for new in free_new:
        if new.path not in claimed:
            entries.append(_entry(ADDED, None, new))
    counts = {s: sum(1 for e in entries if e["status"] == s) for s in STATUSES}
    rotation = transform.Rotation
    return {"old": old_root.Name, "new": new_root.Name,
            "old_part_number": str(getattr(component(old_root), "PartNumber", "") or ""),
            "new_part_number": str(getattr(component(new_root), "PartNumber", "") or ""),
            "transform": transform, "pose": {"source": source, "residual": residual,
                                             "shift": [transform.Base.x, transform.Base.y, transform.Base.z],
                                             "angle_deg": math.degrees(rotation.Angle),
                                             "axis": [rotation.Axis.x, rotation.Axis.y, rotation.Axis.z]},
            "entries": entries, "counts": counts,
            "settled": counts[AMBIGUOUS] == 0,
            "_old": {l.path: l for l in old_leaves}, "_new": {l.path: l for l in new_leaves}}


def public(result):
    """The match without shapes and objects, for reports."""
    out = {k: v for k, v in result.items() if not k.startswith("_") and k != "transform"}
    return out


# ---------------------------------------------------------------- elements

def _element_name(element):
    """The plain FaceN/EdgeN/VertexN of a sub-element, which may carry a mapped name."""
    tail = element.split(".")[-1] if element else ""
    return tail


def _kind(name):
    for kind in ("Face", "Edge", "Vertex"):
        if name.startswith(kind) and name[len(kind):].isdigit():
            return kind
    return None


def _fingerprint(sub):
    if isinstance(sub, Part.Vertex):
        return ("Vertex", None, 0.0, sub.Point)
    if isinstance(sub, Part.Edge):
        return ("Edge", type(sub.Curve).__name__, sub.Length, sub.CenterOfMass)
    return ("Face", type(sub.Surface).__name__, sub.Area, sub.CenterOfMass)


def map_element(old, new, element, transform):
    """Name of the face, edge or vertex of the new part that sits where the old one was."""
    name = _element_name(element)
    kind = _kind(name)
    if kind is None:
        return element, MAPPED  # not a geometric element (empty, or an origin feature)
    try:
        ref = _fingerprint(old.shape.getElement(name))
    except Exception:
        return None, UNRESOLVED
    moved = new.shape.transformed(transform.toMatrix())
    items = {"Face": moved.Faces, "Edge": moved.Edges, "Vertex": moved.Vertexes}[kind]
    exact, near = [], []
    for index, sub in enumerate(items, 1):
        fp = _fingerprint(sub)
        if fp[1] != ref[1]:
            continue
        gap = (fp[3] - ref[3]).Length
        if gap <= LENGTH_TOLERANCE and _close(fp[2], ref[2], rel=1e-6, absolute=LENGTH_TOLERANCE ** 2):
            exact.append((gap, index))
        elif gap <= APPROXIMATE_REACH:
            near.append((gap, index))
    if len(exact) == 1:
        return f"{kind}{exact[0][1]}", MAPPED
    if len(exact) > 1:
        return None, UNRESOLVED
    near.sort()
    if near and (len(near) == 1 or near[1][0] - near[0][0] > LENGTH_TOLERANCE):
        return f"{kind}{near[0][1]}", APPROXIMATE
    return None, UNRESOLVED


# ---------------------------------------------------------------- references

LINK_SUB_TYPES = ("App::PropertyLinkSub", "App::PropertyLinkSubChild", "App::PropertyLinkSubGlobal",
                  "App::PropertyLinkSubHidden", "App::PropertyLinkSubList", "App::PropertyLinkSubListChild",
                  "App::PropertyLinkSubListGlobal", "App::PropertyLinkSubListHidden", "App::PropertyXLinkSub",
                  "App::PropertyXLinkSubHidden", "App::PropertyXLinkSubList", "App::PropertyXLinkList")


def _split(ref_obj, sub, links):
    """(link, path below the link) when the sub-path runs through one of the links, else None."""
    if ref_obj in links:
        return ref_obj, sub
    try:
        chain = ref_obj.getSubObjectList(sub)
    except Exception:
        return None
    parts = sub.split(".")
    for i, obj in enumerate(chain[1:], 1):
        if obj in links:
            return obj, ".".join(parts[i:])
    return None


def _owner(rel, paths):
    """Longest leaf path that the relative sub-path starts with."""
    best = None
    for path in paths:
        if (path == "" or rel.startswith(path)) and (best is None or len(path) > len(best)):
            best = path
    return best


def _subs(value):
    """Normalise a link-sub property value to [(obj, [subs])]."""
    if value is None:
        return []
    if isinstance(value, tuple) and len(value) == 2 and not isinstance(value[0], tuple) \
            and hasattr(value[0], "Document"):
        obj, subs = value
        return [(obj, [subs] if isinstance(subs, str) else list(subs))]
    out = []
    for item in value:
        if isinstance(item, tuple) and len(item) == 2:
            obj, subs = item
            out.append((obj, [subs] if isinstance(subs, str) else list(subs)))
        elif hasattr(item, "Document"):
            out.append((item, []))
    return out


def plan_references(doc, links, result):
    """Every reference into the replaced instances and what it becomes. Changes nothing."""
    links = set(links)
    old, new = result["_old"], result["_new"]
    leaf_map = {e["old"]: e["new"] for e in result["entries"] if e["old"] is not None}
    plan = []
    for obj in doc.Objects:
        if obj in links:
            continue
        for prop in obj.PropertiesList:
            try:
                kind = obj.getTypeIdOfProperty(prop)
            except Exception:
                continue
            if kind not in LINK_SUB_TYPES:
                continue
            for ref_obj, subs in _subs(getattr(obj, prop)):
                for index, sub in enumerate(subs):
                    hit = _split(ref_obj, sub, links)
                    if hit is None:
                        continue
                    link, rel = hit
                    owner = _owner(rel, old)
                    item = {"object": obj.Name, "label": obj.Label, "property": prop, "root": ref_obj.Name,
                            "sub": sub, "link": link.Name, "index": index}
                    if owner is None:
                        item.update(new_sub=sub, state=MAPPED)  # the instance itself, not a part inside it
                    elif leaf_map.get(owner) is None:
                        if prop == "SvenHidden":
                            item.update(new_sub="", state=DROPPED, reason="hidden part no longer exists")
                        else:
                            item.update(new_sub=None, state=UNRESOLVED, reason="part has no match")
                    else:
                        element = rel[len(owner):]
                        new_element, state = map_element(old[owner], new[leaf_map[owner]], element,
                                                         result["transform"])
                        if new_element is None:
                            item.update(new_sub=None, state=UNRESOLVED, reason="element not found")
                        else:
                            prefix = sub[: len(sub) - len(rel)]
                            item.update(new_sub=prefix + leaf_map[owner] + new_element, state=state)
                    plan.append(item)
    return plan


def _write_reference(obj, prop, root_name, index, old_sub, new_sub):
    value = getattr(obj, prop)
    items = _subs(value)
    single = isinstance(value, tuple) and len(value) == 2 and hasattr(value[0], "Document")
    changed = []
    for ref_obj, subs in items:
        if ref_obj.Name == root_name and old_sub in subs:
            subs = list(subs)
            at = index if index < len(subs) and subs[index] == old_sub else subs.index(old_sub)
            subs[at] = new_sub
        changed.append((ref_obj, subs))
    if not new_sub:
        changed = [(o, [x for x in subs if x]) for o, subs in changed]
        changed = [(o, subs) for o, subs in changed if subs or single]
    setattr(obj, prop, changed[0] if single else changed)


# ---------------------------------------------------------------- replace

def instances_of(doc, root):
    """Links in the document that show the given component."""
    target = component(root)
    return [o for o in doc.Objects if o.TypeId == "App::Link" and o.getLinkedObject(True) == target
            and o is not root]


def replace(doc, links, new_root, result=None, keep_position=True, resolutions=None, allow_unresolved=False):
    """Point the links at the new component version and carry the assembly over.

    Returns a report. Refuses (and changes nothing) when parts are ambiguous or references cannot
    be mapped, unless allow_unresolved is set, in which case those references are left as they were."""
    links = list(links)
    if not links:
        raise ReplaceError("select at least one instance to replace")
    old_root = links[0].LinkedObject
    if any(l.TypeId != "App::Link" or l.LinkedObject != old_root for l in links):
        raise ReplaceError("all instances must link to the same component")
    if component(new_root) == component(old_root):
        raise ReplaceError("the new version is the component already linked")
    if result is None or resolutions:
        result = match(old_root, new_root, resolutions)
    if not result["settled"]:
        raise ReplaceError("some parts match more than one part of the new version")
    plan = plan_references(doc, links, result)
    unresolved = [p for p in plan if p["state"] == UNRESOLVED]
    if unresolved and not allow_unresolved:
        return {"replaced": [], "references": plan, "refused": "unresolved references",
                "match": public(result)}
    transform = result["transform"] if keep_position else App.Placement()
    before = {l.Name: App.Placement(l.Placement) for l in links}
    doc.openTransaction("Replace component")
    try:
        for link in links:
            link.LinkedObject = new_root
            link.Placement = before[link.Name].multiply(transform)
        for item in plan:
            if item["new_sub"] is not None and item["new_sub"] != item["sub"]:
                _write_reference(doc.getObject(item["object"]), item["property"], item["root"], item["index"],
                                 item["sub"], item["new_sub"])
        joints = _refresh_joints(doc, plan)
        doc.recompute()
    except Exception:
        doc.abortTransaction()
        raise
    doc.commitTransaction()
    return {"replaced": [l.Name for l in links], "references": plan, "joints": joints,
            "placements": {n: [list(p.Base), list(p.Rotation.Q)] for n, p in before.items()},
            "match": public(result)}


def _refresh_joints(doc, plan):
    """Recompute the joint frames of joints whose references moved to new elements."""
    names = sorted({p["object"] for p in plan if p["property"] in ("Reference1", "Reference2")})
    done = []
    for name in names:
        joint = doc.getObject(name)
        proxy = getattr(joint, "Proxy", None)
        if proxy is not None and hasattr(proxy, "updateJCSPlacements"):
            try:
                proxy.updateJCSPlacements(joint)
                done.append(name)
            except Exception as exc:
                App.Console.PrintWarning(f"Sven replace: joint {joint.Label}: {exc}\n")
    return done


# ---------------------------------------------------------------- named states

def _state_group(doc):
    group = doc.getObject(STATE_GROUP)
    if group is None:
        group = doc.addObject("App::DocumentObjectGroup", STATE_GROUP)
        group.Label = "Named states"
    return group


def states(doc):
    group = doc.getObject(STATE_GROUP)
    return [o for o in (group.Group if group else []) if hasattr(o, "SvenHidden")]


def _parent_and_name(root, sub):
    """The object that owns the visibility of the element a path ends in."""
    parts = sub.rstrip(".").split(".")
    if len(parts) == 1:
        return root, parts[0]
    parent = root.getSubObject(".".join(parts[:-1]) + ".", retType=1)
    return parent, parts[-1]


def _holds_parts(obj):
    """Solids and the containers that can hold them; not exploded-view steps or other helpers."""
    target = obj.getLinkedObject(True) or obj
    return SI._is_leaf(target) or target.isDerivedFrom("App::Part") or \
        target.isDerivedFrom("App::DocumentObjectGroup") or target.TypeId == "App::Link" or \
        target.isDerivedFrom("Assembly::AssemblyObject")


def _nodes(assembly):
    """(path, parent, name, child) for every part container and solid below the assembly, depth first."""
    out = []

    def walk(parent, sub, depth):
        if depth > 32:
            return
        for name in parent.getSubObjects():
            child = parent.getSubObject(name, retType=1)
            if child is None or SI._skipped(child) or not _holds_parts(child):
                continue
            bare = name.rstrip(".")
            path = sub + bare + "."
            out.append((path, parent, bare, child))
            if not SI._is_leaf(child):
                walk(child, path, depth + 1)

    walk(assembly, "", 0)
    return out


def hidden_paths(assembly):
    """Paths of hidden parts and containers, stopping at the highest hidden level."""
    hidden = []
    for path, parent, name, child in _nodes(assembly):
        if any(path.startswith(h) for h in hidden):
            continue
        if not SI._visible(parent, name, child):
            hidden.append(path)
    return hidden


def _path_visible(root, sub):
    parts = sub.rstrip(".").split(".")
    parent = root
    for name in parts:
        child = parent.getSubObject(name + ".", retType=1)
        if child is None or not SI._visible(parent, name, child):
            return False
        parent = child
    return True


def _set_visible(parent, name, child, visible):
    try:
        if parent.isElementVisible(name) >= 0:
            parent.setElementVisible(name, visible)
            return
    except Exception:
        pass
    if child.Visibility != visible:
        child.Visibility = visible


def capture(assembly, name, exploded_view=None):
    """Save which parts of the assembly are hidden, and the exploded view to show, under a name."""
    doc = assembly.Document
    doc.openTransaction("Save named state")
    try:
        state = next((s for s in states(doc) if s.Label == name), None)
        if state is None:
            state = doc.addObject("App::FeaturePython", "SvenState")
            state.addProperty("App::PropertyLinkGlobal", "SvenAssembly", "Sven state", "Assembly the state applies to")
            state.addProperty("App::PropertyXLinkSubList", "SvenHidden", "Sven state", "Hidden part instances")
            state.addProperty("App::PropertyLinkGlobal", "SvenExplodedView", "Sven state", "Exploded view to show")
            state.Label = name
            _state_group(doc).addObject(state)
        state.SvenAssembly = assembly
        hidden = hidden_paths(assembly)
        state.SvenHidden = [(assembly, hidden)] if hidden else []
        state.SvenExplodedView = exploded_view
    except Exception:
        doc.abortTransaction()
        raise
    doc.commitTransaction()
    return state


def state_hidden(state):
    return [s for _obj, subs in _subs(state.SvenHidden) for s in subs]


def apply_state(state):
    """Show every part of the state's assembly except the hidden ones. Returns the paths hidden.

    Parts inside a component share their visibility across every instance of that component,
    as FreeCAD links do, so a part hidden in one instance is hidden in all of them."""
    assembly = state.SvenAssembly
    hidden = set(state_hidden(state))
    wanted = {}
    for path, parent, name, child in _nodes(assembly):
        owner = parent.getLinkedObject(True) or parent
        key = (owner.Document.Name, owner.Name, name)
        show = path not in hidden
        prev = wanted.get(key)
        wanted[key] = (parent, child, show if prev is None else (prev[2] and show))
    for (_doc, _owner, name), (parent, child, show) in wanted.items():
        _set_visible(parent, name, child, show)
    return sorted(hidden)


def explode(state):
    """Move the parts to the state's exploded positions; returns a restore callable or None."""
    view = state.SvenExplodedView
    if view is None or not hasattr(view, "Proxy"):
        return None
    view.Proxy.saveAssemblyAndExplode(view)
    return lambda: view.Proxy.restoreAssembly(view)


# ---------------------------------------------------------------- BOM

def bom_rows(assembly):
    """(name, part number, quantity) rows of the Sven BOM, recomputed."""
    try:
        import SvenDrawing
    except ImportError:
        return None
    bom = SvenDrawing.make_bom(assembly)
    bom.recompute()
    columns = list(bom.columnsNames)
    rows = SvenDrawing.bom_rows(bom)
    pick = lambda row, col: row[columns.index(col)] if col in columns else ""
    return [(pick(r, "Name"), pick(r, ".PartNumber"), pick(r, "Quantity")) for r in rows]
