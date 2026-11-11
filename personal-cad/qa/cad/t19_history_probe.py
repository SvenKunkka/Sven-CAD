"""T19 geometry probe. Run with FreeCADCmd; not a replacement for GUI acceptance."""
import json
import math
import os
from pathlib import Path

import FreeCAD as App
import Part

output = Path(os.environ["SVEN_T19_OUTPUT"])
output.mkdir(parents=True, exist_ok=False)
doc = App.newDocument("T19History")
body = doc.addObject("PartDesign::Body", "Body")
sketch = body.newObject("Sketcher::SketchObject", "BaseSketch")
V = App.Vector
points = [(0, 0), (40, 0), (40, 30), (0, 30)]
for a, b in zip(points, points[1:] + points[:1]):
    sketch.addGeometry(Part.LineSegment(V(*a, 0), V(*b, 0)))
pad = body.newObject("PartDesign::Pad", "Pad")
pad.Profile = sketch
pad.Length = 10
doc.recompute()
hole = body.newObject("Sketcher::SketchObject", "HoleSketch")
hole.addGeometry(Part.Circle(V(10, 10, 0), V(0, 0, 1), 2))
hole.Placement.Base.z = 10
pocket = body.newObject("PartDesign::Pocket", "Pocket")
pocket.Profile = hole
pocket.Length = 10
doc.recompute()
assert [o.Name for o in body.Group] == ["BaseSketch", "Pad", "HoleSketch", "Pocket"]
assert pocket.isValid() and math.isclose(body.Shape.Volume, 12000 - 40 * math.pi, abs_tol=1e-5)
pad.Length = 15
doc.recompute()
expected = 18000 - 40 * math.pi
assert pocket.isValid() and math.isclose(body.Shape.Volume, expected, abs_tol=1e-5)
pocket.Suppressed = True
doc.recompute()
assert math.isclose(body.Shape.Volume, 18000, abs_tol=1e-5)
pocket.Suppressed = False
doc.recompute()
assert math.isclose(body.Shape.Volume, expected, abs_tol=1e-5)
pocket.Profile = None
doc.recompute()
error = pocket.getStatusString()
assert not pocket.isValid() and error
pocket.Profile = hole
doc.recompute()
assert pocket.isValid() and math.isclose(body.Shape.Volume, expected, abs_tol=1e-5)
path = output / "history.FCStd"
doc.saveAs(str(path))
App.closeDocument(doc.Name)
doc = App.openDocument(str(path))
body = doc.getObject("Body")
doc.recompute()
assert doc.getObject("Pocket").isValid()
assert math.isclose(body.Shape.Volume, expected, abs_tol=1e-5)
result = {"status": "passed", "scope": "geometry probe only", "version": list(App.Version()),
          "order": [o.Name for o in body.Group], "restored_volume": body.Shape.Volume,
          "missing_profile_error": error}
App.closeDocument(doc.Name)
(output / "report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result))
