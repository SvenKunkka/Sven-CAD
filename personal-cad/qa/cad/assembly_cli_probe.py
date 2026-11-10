
import json
import os
from pathlib import Path
import traceback
import unittest
import FreeCAD as App
import AssemblyApp
from AssemblyTests.TestCore import TestCore

result = {"status": "failed"}
try:
    result["motion_enabled"] = AssemblyApp.hasMotionSimulation
    result["static_bom_enabled"] = AssemblyApp.hasStaticBom
    assert not result["motion_enabled"] and result["static_bom_enabled"]
    doc = App.newDocument("PersonalAssemblyApi")
    assembly = doc.addObject("Assembly::AssemblyObject", "Assembly")
    result["missing_simulation_api"] = [
        name for name in ("generateSimulation", "updateForFrame", "numberOfFrames")
        if not hasattr(assembly, name)
    ]
    assert len(result["missing_simulation_api"]) == 3 and hasattr(assembly, "solve")
    App.closeDocument(doc.Name)
    suite = unittest.TestSuite([TestCore("test_static_bom_quantities_survive_save_and_reopen")])
    tests = unittest.TextTestRunner(verbosity=2).run(suite)
    result["tests_run"] = tests.testsRun
    result["failures"] = len(tests.failures)
    result["errors"] = len(tests.errors)
    assert tests.wasSuccessful() and tests.testsRun == 1 and not tests.skipped
    result["status"] = "passed"
except Exception:
    result["error"] = traceback.format_exc()
Path(os.environ["ASSEMBLY_QA_REPORT"]).write_text(json.dumps(result, indent=2) + "\n")
