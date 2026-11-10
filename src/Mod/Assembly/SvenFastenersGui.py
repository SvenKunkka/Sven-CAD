# SPDX-License-Identifier: LGPL-2.1-or-later
"""Commands to search, insert, replace and count common fasteners."""

import FreeCAD as App
import FreeCADGui as Gui

INSERT = "PersonalCAD_InsertFastener"
REPLACE = "PersonalCAD_ReplaceFastener"
SUMMARY = "PersonalCAD_FastenerSummary"

ZH = {
    "Fastener": "紧固件",
    "Insert Fastener": "插入紧固件",
    "Search common screws, nuts and washers by size, length or part number and insert linked instances":
        "按规格、长度或料号检索常用螺钉、螺母和垫圈，并插入链接实例",
    "Replace Fastener": "替换紧固件",
    "Replace only the selected fastener instances with another catalog item":
        "只把选中的紧固件实例替换为另一个目录项",
    "Fastener Summary": "紧固件汇总",
    "Count fastener instances by part number": "按料号汇总紧固件实例数量",
    "Size, length, part number or type": "规格、长度、料号或类型",
    "All": "全部", "Screw": "螺钉", "Nut": "螺母", "Washer": "垫圈",
    "Part number": "料号", "Type": "类型", "Size": "规格", "Length": "长度", "Standard": "标准",
    "Description": "说明", "Quantity": "数量", "Company part number": "公司料号", "Source": "来源",
    "Search": "搜索", "Kind": "类别",
    "Reverse direction": "反向放置",
    "Placement": "位置",
    "At the origin, spaced along X": "放在原点，沿 X 方向排开",
    "{n} selected hole edge(s), one instance each": "已选 {n} 个孔边，每个孔边放一个实例",
    "{n} selected instance(s) will change; other instances stay unchanged.":
        "将修改选中的 {n} 个实例，其他实例保持不变。",
    "Choose a catalog item.": "请选择一个目录项。",
    "Source: {source} · retrieved {date} · catalog {version}": "来源：{source} · 读取于 {date} · 目录 {version}",
    "Total instances: {n}": "实例总数：{n}",
    "Select circular hole edges, or clear the selection to insert at the origin.":
        "请选择圆形孔边，或清空选择后放在原点。",
    "Select circular hole edges.": "请选择圆形孔边。",
    "The hole edge must lie on a planar face.": "孔边必须位于平面上。",
    "Select fastener instances to replace.": "请选择要替换的紧固件实例。",
    "Select fastener instances from one document.": "请选择同一文档中的紧固件实例。",
    "The document has no fastener instances.": "文档中还没有紧固件实例。",
    "Close": "关闭",
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
    from PySide import QtCore, QtGui, QtWidgets
    return QtCore, QtGui, QtWidgets


def _warn(title, message):
    _, _, QtWidgets = _widgets()
    QtWidgets.QMessageBox.warning(Gui.getMainWindow(), title, message)


def _selected_elements():
    """Return (owner shape in global coordinates, element name) for each selected element."""
    import Part
    elements = []
    plain = 0
    for selection in Gui.Selection.getSelectionEx("", 0):
        names = selection.SubElementNames or ("",)
        for sub in names:
            element = sub.rsplit(".", 1)[-1] if sub else ""
            if not element:
                plain += 1
                continue
            path = sub[: len(sub) - len(element)]
            shape = Part.getShape(selection.Object, path, needSubElement=False, transform=True)
            elements.append((shape, element))
    return elements, plain


def _selected_instances():
    import SvenFasteners
    found = []
    other = 0
    for selection in Gui.Selection.getSelectionEx("", 0):
        for sub in selection.SubElementNames or ("",):
            chain = selection.Object.getSubObjectList(sub)
            link = next((obj for obj in chain if SvenFasteners.is_instance(obj)), None)
            if link is None:
                other += 1
            elif link not in found:
                found.append(link)
    return found, other


def _parent():
    try:
        import UtilsAssembly
        return UtilsAssembly.activePartOrAssembly()
    except Exception:
        return None


class FastenerDialog:
    def __init__(self, mode, *, edges=0, kind=None, count=0):
        QtCore, QtGui, QtWidgets = _widgets()
        import SvenFasteners
        self.module = SvenFasteners
        self.mode = mode
        dialog = QtWidgets.QDialog(Gui.getMainWindow())
        dialog.setObjectName("SvenFastenerDialog")
        dialog.setWindowTitle(tr("Insert Fastener" if mode == "insert" else "Replace Fastener"))
        dialog.resize(640, 460)
        layout = QtWidgets.QVBoxLayout(dialog)
        row = QtWidgets.QHBoxLayout()
        self.search = QtWidgets.QLineEdit(dialog)
        self.search.setObjectName("SvenFastenerSearch")
        self.search.setPlaceholderText(tr("Size, length, part number or type"))
        self.search.setClearButtonEnabled(True)
        self.kind = QtWidgets.QComboBox(dialog)
        self.kind.setObjectName("SvenFastenerKind")
        for key, label in ((None, "All"), ("screw", "Screw"), ("nut", "Nut"), ("washer", "Washer")):
            self.kind.addItem(tr(label), key)
        if kind:
            self.kind.setCurrentIndex(self.kind.findData(kind))
        row.addWidget(self.search, 1)
        row.addWidget(self.kind)
        layout.addLayout(row)
        self.table = QtWidgets.QTableWidget(0, 5, dialog)
        self.table.setObjectName("SvenFastenerResults")
        self.table.setHorizontalHeaderLabels([tr(t) for t in ("Part number", "Type", "Size", "Length", "Standard")])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        self.source = QtWidgets.QLabel(dialog)
        self.source.setObjectName("SvenFastenerSource")
        self.source.setWordWrap(True)
        layout.addWidget(self.source)
        form = QtWidgets.QFormLayout()
        self.quantity = None
        self.flip = None
        if mode == "insert":
            if edges:
                info = QtWidgets.QLabel(tr("{n} selected hole edge(s), one instance each", n=edges), dialog)
                info.setObjectName("SvenFastenerPlacementInfo")
                form.addRow(tr("Placement"), info)
            else:
                self.quantity = QtWidgets.QSpinBox(dialog)
                self.quantity.setObjectName("SvenFastenerQuantity")
                self.quantity.setRange(1, 200)
                form.addRow(tr("Quantity"), self.quantity)
                info = QtWidgets.QLabel(tr("At the origin, spaced along X"), dialog)
                info.setObjectName("SvenFastenerPlacementInfo")
                form.addRow(tr("Placement"), info)
            self.flip = QtWidgets.QCheckBox(tr("Reverse direction"), dialog)
            self.flip.setObjectName("SvenFastenerFlip")
            form.addRow(self.flip)
        else:
            info = QtWidgets.QLabel(
                tr("{n} selected instance(s) will change; other instances stay unchanged.", n=count), dialog)
            info.setObjectName("SvenFastenerPlacementInfo")
            info.setWordWrap(True)
            form.addRow(info)
        layout.addLayout(form)
        self.validation = QtWidgets.QLabel(dialog)
        self.validation.setObjectName("SvenFastenerValidation")
        layout.addWidget(self.validation)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel, dialog)
        self.ok = buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(self._accept)
        layout.addWidget(buttons)
        self.dialog = dialog
        self.items = []
        self.search.textChanged.connect(self.refresh)
        self.kind.currentIndexChanged.connect(self.refresh)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.refresh()
        self.search.setFocus()

    def refresh(self):
        QtCore, _, QtWidgets = _widgets()
        self.items = self.module.search(self.search.text(), self.kind.currentData())
        self.table.setRowCount(len(self.items))
        kinds = {"screw": "Screw", "nut": "Nut", "washer": "Washer"}
        for row, item in enumerate(self.items):
            values = (item["part_number"], tr(kinds[item["kind"]]), item["size"],
                      f"{item['length']:g} mm" if item["length"] else "—", item["standard"])
            for column, value in enumerate(values):
                self.table.setItem(row, column, QtWidgets.QTableWidgetItem(value))
        if len(self.items) == 1:
            self.table.selectRow(0)
        self._selection_changed()

    def selected_item(self):
        rows = self.table.selectionModel().selectedRows()
        return self.items[rows[0].row()] if rows else None

    def _selection_changed(self):
        item = self.selected_item()
        self.ok.setEnabled(item is not None)
        if item is None:
            self.source.setText("")
            return
        self.validation.setText("")
        source = item["source"]
        self.source.setText(
            f"{item['description']}\n"
            + tr("Source: {source} · retrieved {date} · catalog {version}",
                 source=source["publisher"], date=source["retrieved"], version=item["catalog_version"])
            + f"\n{source['url']}")

    def _accept(self):
        if self.selected_item() is None:
            self.validation.setText(tr("Choose a catalog item."))
            return
        self.dialog.accept()

    def exec(self):
        return self.dialog.exec()


def _run_transaction(doc, name, work):
    doc.openTransaction(name)
    try:
        result = work()
        doc.recompute()
        doc.commitTransaction()
        return result
    except Exception:
        doc.abortTransaction()
        raise


def _hide_source(source):
    view = getattr(source, "ViewObject", None)
    if view is not None:
        view.Visibility = False
    group = source.getParentGroup()
    if group is not None and getattr(group, "ViewObject", None) is not None:
        group.ViewObject.Visibility = False


class InsertFastenerCommand:
    def GetResources(self):
        return {"Pixmap": "Link", "MenuText": "Fastener",
                "ToolTip": tr("Search common screws, nuts and washers by size, length or part number and insert linked instances")}

    def IsActive(self):
        return App.ActiveDocument is not None and not Gui.Control.activeDialog()

    def Activated(self):
        import SvenFasteners
        doc = App.ActiveDocument
        title = tr("Insert Fastener")
        elements, plain = _selected_elements()
        if plain or (elements and not all(name.startswith("Edge") for _, name in elements)):
            _warn(title, tr("Select circular hole edges, or clear the selection to insert at the origin."))
            return
        try:
            global_placements = [SvenFasteners.edge_placement(shape, name) for shape, name in elements]
        except SvenFasteners.FastenerError as error:
            _warn(title, tr(str(error)))
            return
        form = FastenerDialog("insert", edges=len(global_placements))
        if not form.exec() or App.ActiveDocument != doc:
            return
        item = form.selected_item()
        parent = _parent()
        if global_placements:
            inverse = parent.getGlobalPlacement().inverse() if parent is not None else App.Placement()
            placements = [inverse.multiply(p) for p in global_placements]
        else:
            placements = SvenFasteners.default_placements(item, form.quantity.value())
        if form.flip.isChecked():
            turn = App.Placement(App.Vector(), App.Rotation(App.Vector(1, 0, 0), 180))
            placements = [p.multiply(turn) for p in placements]

        def work():
            links, source, created = SvenFasteners.insert(doc, item, placements, parent)
            if created:
                _hide_source(source)
            return links

        try:
            links = _run_transaction(doc, "Insert Fastener", work)
        except SvenFasteners.FastenerError as error:
            _warn(title, tr(str(error)))
            return
        Gui.Selection.clearSelection()
        for link in links:
            Gui.Selection.addSelection(link)


class ReplaceFastenerCommand:
    def GetResources(self):
        return {"Pixmap": "LinkReplace", "MenuText": "Replace Fastener",
                "ToolTip": tr("Replace only the selected fastener instances with another catalog item")}

    def IsActive(self):
        return App.ActiveDocument is not None and not Gui.Control.activeDialog()

    def Activated(self):
        import SvenFasteners
        doc = App.ActiveDocument
        title = tr("Replace Fastener")
        links, other = _selected_instances()
        if not links or other or any(link.Document != doc for link in links):
            _warn(title, tr("Select fastener instances to replace."))
            return
        kinds = {SvenFasteners.source_of(link).FastenerKind for link in links}
        form = FastenerDialog("replace", kind=kinds.pop() if len(kinds) == 1 else None, count=len(links))
        if not form.exec() or App.ActiveDocument != doc:
            return
        item = form.selected_item()

        def work():
            source, created = SvenFasteners.replace(links, item)
            if created:
                _hide_source(source)

        try:
            _run_transaction(doc, "Replace Fastener", work)
        except SvenFasteners.FastenerError as error:
            _warn(title, tr(str(error)))


class FastenerSummaryCommand:
    def GetResources(self):
        return {"Pixmap": "LinkSelectAll", "MenuText": "Fastener Summary",
                "ToolTip": tr("Count fastener instances by part number")}

    def IsActive(self):
        return App.ActiveDocument is not None

    def Activated(self):
        import SvenFasteners
        _, _, QtWidgets = _widgets()
        rows = SvenFasteners.summary(App.ActiveDocument)
        dialog = QtWidgets.QDialog(Gui.getMainWindow())
        dialog.setObjectName("SvenFastenerSummary")
        dialog.setWindowTitle(tr("Fastener Summary"))
        dialog.resize(720, 360)
        layout = QtWidgets.QVBoxLayout(dialog)
        headers = ("Part number", "Description", "Quantity", "Company part number", "Source")
        table = QtWidgets.QTableWidget(len(rows), len(headers), dialog)
        table.setObjectName("SvenFastenerSummaryTable")
        table.setHorizontalHeaderLabels([tr(h) for h in headers])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        for index, row in enumerate(rows):
            for column, value in enumerate((row["part_number"], row["description"], str(row["count"]),
                                            row["company_part_number"] or "—", row["source"])):
                table.setItem(index, column, QtWidgets.QTableWidgetItem(value))
        table.resizeColumnsToContents()
        layout.addWidget(table, 1)
        total = QtWidgets.QLabel(
            tr("Total instances: {n}", n=sum(r["count"] for r in rows)) if rows
            else tr("The document has no fastener instances."), dialog)
        total.setObjectName("SvenFastenerSummaryTotal")
        layout.addWidget(total)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close, dialog)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()


def register():
    Gui.addCommand(INSERT, InsertFastenerCommand())
    Gui.addCommand(REPLACE, ReplaceFastenerCommand())
    Gui.addCommand(SUMMARY, FastenerSummaryCommand())
