# SPDX-License-Identifier: LGPL-2.1-or-later
"""Drawing commands for thread callouts, BOM tables and the PDF, DXF and CSV export."""

import os
import time

import FreeCAD as App
import FreeCADGui as Gui


ZH = {
    "Thread Callouts": "螺纹标注",
    "Label every thread and threaded hole in the selected views from the model":
        "按模型给所选视图中的螺纹和螺纹孔加标注",
    "BOM Table": "BOM 表",
    "Place the assembly BOM on the drawing page, with fasteners grouped by part number":
        "把装配 BOM 放到图纸上，紧固件按料号汇总",
    "Export Drawing": "导出图纸",
    "Write the page as PDF and DXF, and its BOM as CSV": "把图纸导出为 PDF 和 DXF，BOM 导出为 CSV",
    "Select one or more drawing views.": "请选择一个或多个视图。",
    "No threads or threaded holes were found in the selected views.": "所选视图中没有螺纹或螺纹孔。",
    "Every thread in the selected views already has a callout.": "所选视图中的螺纹都已有标注。",
    "Select a drawing page or one of its views.": "请选择一张图纸或其中的视图。",
    "Add the parts to one assembly, or select the assembly to list.": "请把零件放进一个装配，或选中要列出的装配。",
    "Drawing exported": "图纸已导出",
    "Export failed": "导出失败",
    "Open the drawing page before exporting it.": "请先打开图纸页再导出。",
    "The views are still being computed. Try again in a moment.": "视图仍在计算，请稍后再试。",
    "PDF (*.pdf)": "PDF (*.pdf)",
}


def chinese():
    try:
        return Gui.getLocale() == "Chinese (Simplified)"
    except Exception:
        return False


def tr(text, **values):
    if chinese():
        text = ZH.get(text, text)
    return text.format(**values) if values else text


def _widgets():
    from PySide import QtCore, QtWidgets
    return QtCore, QtWidgets


def _message(title, text, warning=True, name=None):
    _, QtWidgets = _widgets()
    box = QtWidgets.QMessageBox(Gui.getMainWindow())
    box.setIcon(QtWidgets.QMessageBox.Icon.Warning if warning else QtWidgets.QMessageBox.Icon.Information)
    box.setWindowTitle(title)
    box.setText(text)
    if name:
        box.setObjectName(name)
    box.exec()


def _selected():
    return [s.Object for s in Gui.Selection.getSelectionEx("", 0)]


def selected_page(doc):
    for obj in _selected():
        if obj.isDerivedFrom("TechDraw::DrawPage"):
            return obj
        if obj.isDerivedFrom("TechDraw::DrawView"):
            page = obj.findParentPage()
            if page is not None:
                return page
    pages = [o for o in doc.Objects if o.isDerivedFrom("TechDraw::DrawPage")]
    return pages[0] if len(pages) == 1 else None


def selected_assembly(doc):
    for obj in _selected():
        if obj.TypeId == "Assembly::AssemblyObject":
            return obj
    assemblies = [o for o in doc.Objects if o.TypeId == "Assembly::AssemblyObject"]
    return assemblies[0] if len(assemblies) == 1 else None


def wait_for_views(page, timeout=120.0):
    """Process events until every view on the page has finished hidden line removal."""
    QtCore, QtWidgets = _widgets()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        busy = [v for v in page.getAllViews()
                if v.isDerivedFrom("TechDraw::DrawViewPart") and v.waitingForResult()]
        if not busy:
            return True
        QtWidgets.QApplication.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 50)
        time.sleep(0.02)
    return False


def write_pdf(page, path):
    """Print the page as PDF, opening it first when it has never been shown."""
    import TechDrawGui
    try:
        TechDrawGui.exportPageAsPdf(page, path)
        return
    except RuntimeError:
        # A page that was never opened has no template on its scene yet.
        page.ViewObject.show()
        Gui.updateGui()
        wait_for_views(page)
    try:
        TechDrawGui.exportPageAsPdf(page, path)
    except RuntimeError as error:
        import SvenDrawing
        raise SvenDrawing.DrawingError(tr("Open the drawing page before exporting it.")) from error


def export_drawing(page, pdf_path):
    """Write <name>.pdf, <name>.dxf and, when the page shows a BOM, <name>.csv. Returns the paths."""
    import TechDraw
    import TechDrawGui
    import SvenDrawing

    base = os.path.splitext(pdf_path)[0]
    page.Document.recompute()
    refresh_bom_tables(page.Document)
    if not wait_for_views(page):
        raise SvenDrawing.DrawingError(tr("The views are still being computed. Try again in a moment."))
    page.Document.recompute()
    wait_for_views(page)
    written = []
    write_pdf(page, base + ".pdf")
    written.append(base + ".pdf")
    TechDraw.writeDXFPage(page, base + ".dxf")
    written.append(base + ".dxf")
    bom = SvenDrawing.page_bom(page)
    if bom is not None:
        written.append(SvenDrawing.write_csv(base + ".csv", bom))
    return written


class ThreadCalloutsCommand:
    def GetResources(self):
        return {"Pixmap": "actions/TechDraw_Annotation",
                "MenuText": tr("Thread Callouts"),
                "ToolTip": tr("Label every thread and threaded hole in the selected views from the model")}

    def IsActive(self):
        return App.ActiveDocument is not None

    def Activated(self):
        import SvenDrawing
        doc = App.ActiveDocument
        title = tr("Thread Callouts")
        views = [o for o in _selected() if o.isDerivedFrom("TechDraw::DrawViewPart")]
        if not views:
            _message(title, tr("Select one or more drawing views."))
            return
        found = sum(len(SvenDrawing.view_threads(v)) for v in views)
        if not found:
            _message(title, tr("No threads or threaded holes were found in the selected views."))
            return
        for page in {v.findParentPage() for v in views if v.findParentPage() is not None}:
            wait_for_views(page)
        doc.openTransaction(title)
        try:
            created = []
            for view in views:
                created.extend(SvenDrawing.add_callouts(view))
            doc.recompute()
        except Exception as error:
            doc.abortTransaction()
            _message(title, str(error))
            return
        doc.commitTransaction()
        if not created:
            _message(title, tr("Every thread in the selected views already has a callout."), warning=False)


class DrawingBomCommand:
    def GetResources(self):
        return {"Pixmap": "Assembly_BillOfMaterials",
                "MenuText": tr("BOM Table"),
                "ToolTip": tr("Place the assembly BOM on the drawing page, with fasteners grouped by part number")}

    def IsActive(self):
        return App.ActiveDocument is not None

    def Activated(self):
        import SvenDrawing
        doc = App.ActiveDocument
        title = tr("BOM Table")
        page = selected_page(doc)
        if page is None:
            _message(title, tr("Select a drawing page or one of its views."))
            return
        assembly = selected_assembly(doc)
        if assembly is None:
            _message(title, tr("Add the parts to one assembly, or select the assembly to list."))
            return
        doc.openTransaction(title)
        try:
            bom = SvenDrawing.make_bom(assembly)
            SvenDrawing.place_bom(page, bom)
            doc.recompute()
        except Exception as error:
            doc.abortTransaction()
            _message(title, str(error))
            return
        doc.commitTransaction()


class ExportDrawingCommand:
    def GetResources(self):
        return {"Pixmap": "actions/TechDraw_ExportPageDXF",
                "MenuText": tr("Export Drawing"),
                "ToolTip": tr("Write the page as PDF and DXF, and its BOM as CSV")}

    def IsActive(self):
        return App.ActiveDocument is not None

    def Activated(self):
        _, QtWidgets = _widgets()
        doc = App.ActiveDocument
        title = tr("Export Drawing")
        page = selected_page(doc)
        if page is None:
            _message(title, tr("Select a drawing page or one of its views."))
            return
        folder = os.path.dirname(doc.FileName) if doc.FileName else os.path.expanduser("~")
        dialog = QtWidgets.QFileDialog(Gui.getMainWindow(), title,
                                       os.path.join(folder, page.Label + ".pdf"), tr("PDF (*.pdf)"))
        dialog.setObjectName("SvenDrawingExportDialog")
        dialog.setOption(QtWidgets.QFileDialog.Option.DontUseNativeDialog, True)
        dialog.setAcceptMode(QtWidgets.QFileDialog.AcceptMode.AcceptSave)
        dialog.setDefaultSuffix("pdf")
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted or not dialog.selectedFiles():
            return
        try:
            written = export_drawing(page, dialog.selectedFiles()[0])
        except Exception as error:
            _message(tr("Export failed"), str(error), name="SvenDrawingExportResult")
            return
        _message(tr("Drawing exported"), "\n".join(written), warning=False, name="SvenDrawingExportResult")


def refresh_bom_tables(doc):
    """Add or remove BOM cells after the BOM gained or lost rows. Returns the refreshed pages."""
    import SvenDrawing
    pages = [p for p in doc.Objects if p.isDerivedFrom("TechDraw::DrawPage") and SvenDrawing.bom_needs_layout(p)]
    if not pages:
        return []
    own = not doc.HasPendingTransaction
    if own:
        doc.openTransaction(tr("BOM Table"))
    try:
        for page in pages:
            SvenDrawing.place_bom(page, SvenDrawing.page_bom(page))
        doc.recompute()
    except Exception as error:
        if own:
            doc.abortTransaction()
        App.Console.PrintWarning(f"BOM table refresh failed: {error}\n")
        return []
    if own:
        doc.commitTransaction()
    return pages


class BomTableObserver:
    """Keeps printed BOM tables in step with the BOM when a recompute adds or removes rows.

    New cells cannot be created during a recompute, so the refresh runs right after it."""

    def __init__(self):
        self.pending = set()

    def slotRecomputedDocument(self, doc):
        import SvenDrawing
        if doc.Name in self.pending or doc.Restoring or doc.Transacting:
            return
        if not any(p.isDerivedFrom("TechDraw::DrawPage") and SvenDrawing.bom_needs_layout(p) for p in doc.Objects):
            return
        from PySide import QtCore
        self.pending.add(doc.Name)
        QtCore.QTimer.singleShot(0, lambda name=doc.Name: self.refresh(name))

    def refresh(self, name):
        self.pending.discard(name)
        doc = App.listDocuments().get(name)
        if doc is not None and not doc.Transacting:
            refresh_bom_tables(doc)


_observer = None


def register():
    global _observer
    if _observer is None:
        _observer = BomTableObserver()
        App.addDocumentObserver(_observer)
    Gui.addCommand("PersonalCAD_ThreadCallouts", ThreadCalloutsCommand())
    Gui.addCommand("PersonalCAD_DrawingBom", DrawingBomCommand())
    Gui.addCommand("PersonalCAD_ExportDrawing", ExportDrawingCommand())
