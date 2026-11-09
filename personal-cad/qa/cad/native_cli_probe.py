import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import traceback
from zipfile import ZipFile

import FreeCAD as App

output = Path(os.environ["PERSONALCAD_QA_OUTPUT"]).resolve()
output.mkdir(parents=True)
fixtures = Path(os.environ["PERSONALCAD_QA_FIXTURES"]).resolve()
legacy = output / "legacy-mixed.FCStd"
normal = output / "keyboard-housing.FCStd"
shutil.copy2(fixtures / legacy.name, legacy)
shutil.copy2(fixtures / normal.name, normal)
digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
report = {"status": "failed", "identity": App.ConfigGet("ExeName")}
try:
    assert report["identity"] == "PersonalCAD"
    assert App.ConfigGet("ExeVendor") == "PersonalCAD"
    initial = digest(legacy)
    try:
        App.openDocument(str(legacy))
    except Exception as error:
        report["legacy_rejected"] = str(error)
    assert "LegacyParameters" in report["legacy_rejected"]
    assert "LegacyMesh" in report["legacy_rejected"]
    assert digest(legacy) == initial
    assert not App.listDocuments()

    normal_hash = digest(normal)
    doc = App.openDocument(str(normal))
    assert doc.Housing.Shape.isValid()
    doc.recompute()
    assert App.writeRecoverySnapshotToTransientDir(doc)
    archive = Path(doc.TransientDir) / "fc_recovery_file.fcstd"
    size = archive.stat().st_size
    assert size > 4096
    previous = digest(archive)
    with ZipFile(archive) as snapshot:
        assert snapshot.testzip() is None

    doc.Outline.setDatum("Length", App.Units.Quantity("131 mm"))
    doc.recompute()
    saved_limits = resource.getrlimit(resource.RLIMIT_FSIZE)
    saved_signal = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (4096, saved_limits[1]))
        try:
            App.writeRecoverySnapshotToTransientDir(doc)
        except Exception as error:
            report["injected_recovery_error"] = str(error)
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, saved_limits)
        signal.signal(signal.SIGXFSZ, saved_signal)
    assert report.get("injected_recovery_error"), "Incomplete snapshot reported success"
    assert digest(archive) == previous and archive.stat().st_size == size
    with ZipFile(archive) as snapshot:
        assert snapshot.testzip() is None
    assert digest(normal) == normal_hash
    assert not list(archive.parent.glob("fc_recovery_file.fcstd.*"))
    report["previous_snapshot_preserved"] = {"sha256": previous, "bytes": size}
    assert App.writeRecoverySnapshotToTransientDir(doc), "Recovery retry failed"
    assert digest(archive) != previous, "Retry did not update the archive"
    with ZipFile(archive) as snapshot:
        assert snapshot.testzip() is None
    recovered_copy = output / "recovered-copy.FCStd"
    shutil.copy2(archive, recovered_copy)
    App.closeDocument(doc.Name)
    reopened = App.openDocument(str(recovered_copy))
    assert abs(reopened.Housing.Shape.BoundBox.XLength - 131) < 1e-6
    report["retry_reopened_width_mm"] = reopened.Housing.Shape.BoundBox.XLength
    App.closeDocument(reopened.Name)
    report["status"] = "passed"
except Exception:
    report["error"] = traceback.format_exc()
finally:
    (output / "native-cli-result.json").write_text(json.dumps(report, indent=2) + "\n")
