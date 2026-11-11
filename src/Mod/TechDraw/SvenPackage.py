# SPDX-License-Identifier: LGPL-2.1-or-later
"""One-step delivery package of one document revision: coloured STEP, PDF, DXF, BOM CSV
and a provenance manifest.

Everything is written to a staging folder first and checked. Only when every output is
good does the staging folder replace the package; until then the last good package stays
as it was. A failed output is retried before the package is given up."""

import datetime
import hashlib
import json
import os
import shutil
import tempfile
import time

import FreeCAD as App

MANIFEST = "manifest.json"
OUTPUTS = ("step", "pdf", "dxf", "csv")


class PackageError(RuntimeError):
    pass


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _check_file(path, kind):
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        raise PackageError(f"{kind}: {os.path.basename(path)} was not written")
    with open(path, "rb") as stream:
        head = stream.read(2048)
    if kind == "step" and b"ISO-10303-21" not in head:
        raise PackageError("step: not an ISO 10303-21 file")
    if kind == "pdf" and not head.startswith(b"%PDF"):
        raise PackageError("pdf: not a PDF file")
    if kind == "dxf" and b"SECTION" not in head:
        raise PackageError("dxf: not a DXF file")


def source_revision(doc, folder):
    """Hash a copy of the document as it is now, so the package names exactly what it was made from."""
    copy = os.path.join(folder, "source.FCStd")
    doc.saveCopy(copy)
    revision = {"document": doc.Label, "file": os.path.basename(doc.FileName) if doc.FileName else "",
                "sha256": _sha256(copy), "bytes": os.path.getsize(copy),
                "saved_file_sha256": _sha256(doc.FileName) if doc.FileName and os.path.isfile(doc.FileName) else None,
                "last_modified": getattr(doc, "LastModifiedDate", "")}
    os.remove(copy)
    return revision


def _writers(assembly, page):
    import Import
    import TechDraw
    import SvenDrawing

    def step(path):
        if App.GuiUp:
            import ImportGui  # keeps the part colours
            ImportGui.export([assembly], path)
        else:
            Import.export([assembly], path)

    def pdf(path):
        import SvenDrawingGui
        SvenDrawingGui.write_pdf(page, path)

    def dxf(path):
        TechDraw.writeDXFPage(page, path)

    def csv(path):
        bom = SvenDrawing.page_bom(page) or SvenDrawing.make_bom(assembly)
        SvenDrawing.write_csv(path, bom)

    return {"step": step, "pdf": pdf, "dxf": dxf, "csv": csv}


def _prepare_page(page):
    import SvenDrawing
    import SvenDrawingGui
    page.Document.recompute()
    SvenDrawingGui.refresh_bom_tables(page.Document)
    if not SvenDrawingGui.wait_for_views(page):
        raise PackageError("drawing views are still being computed")
    SvenDrawing.flush_paint()
    try:
        import FreeCADGui as Gui
        Gui.updateGui()
    except Exception:
        pass


def export_package(assembly, page, folder, name=None, retries=1, fail=None, progress=None):
    """Write the package <folder>/<name>. Returns the manifest.

    fail: optional {kind: number of attempts to fail}, used to test retry and rollback."""
    doc = assembly.Document
    name = name or assembly.Label
    folder = os.path.abspath(folder)
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, name)
    fail = dict(fail or {})
    started = time.monotonic()
    staging = tempfile.mkdtemp(prefix=f".{name}-staging-", dir=folder)
    try:
        manifest = {"package": name, "format": 1,
                    "created": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                    "app": list(App.Version()), "units": "mm",
                    "source": source_revision(doc, staging), "assembly": assembly.Label,
                    "page": page.Label, "outputs": [], "attempts": {}}
        _prepare_page(page)
        writers = _writers(assembly, page)
        files = {"step": f"{name}.step", "pdf": f"{name}.pdf", "dxf": f"{name}.dxf", "csv": f"{name}-bom.csv"}
        for kind in OUTPUTS:
            path = os.path.join(staging, files[kind])
            attempt = 0
            while True:
                attempt += 1
                try:
                    if progress:
                        progress(kind, attempt)
                    if fail.get(kind, 0) >= attempt:
                        raise PackageError(f"{kind}: simulated failure (attempt {attempt})")
                    writers[kind](path)
                    _check_file(path, kind)
                    break
                except Exception as error:
                    if os.path.exists(path):
                        os.remove(path)
                    manifest["attempts"].setdefault(kind, []).append(str(error))
                    if attempt > retries:
                        raise PackageError(f"{kind} failed after {attempt} attempts: {error}") from error
            manifest["attempts"].setdefault(kind, []).append("ok")
            manifest["outputs"].append({"kind": kind, "file": files[kind], "bytes": os.path.getsize(path),
                                        "sha256": _sha256(path)})
        manifest["seconds"] = round(time.monotonic() - started, 2)
        with open(os.path.join(staging, MANIFEST), "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        _commit(staging, target)
        staging = None
        return manifest
    finally:
        if staging and os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)


def _commit(staging, target):
    """Swap the checked staging folder in; the previous package is kept as <name>.previous."""
    previous = target + ".previous"
    if os.path.isdir(target):
        if os.path.isdir(previous):
            shutil.rmtree(previous)
        os.rename(target, previous)
    try:
        os.rename(staging, target)
    except Exception:
        if os.path.isdir(previous) and not os.path.isdir(target):
            os.rename(previous, target)
        raise


def read_manifest(target):
    with open(os.path.join(target, MANIFEST), encoding="utf-8") as stream:
        return json.load(stream)


def verify(target):
    """Re-hash the files of a package against its manifest. Returns the list of mismatches."""
    manifest = read_manifest(target)
    bad = []
    for item in manifest["outputs"]:
        path = os.path.join(target, item["file"])
        if not os.path.isfile(path) or _sha256(path) != item["sha256"]:
            bad.append(item["file"])
    return bad
