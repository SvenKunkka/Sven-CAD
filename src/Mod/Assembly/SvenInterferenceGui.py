# SPDX-License-Identifier: LGPL-2.1-or-later
"""Interference command: check the selection or the visible assembly and locate each result."""

import FreeCAD as App
import FreeCADGui as Gui

COMMAND = "PersonalCAD_Interference"

ZH = {
    "Interference": "干涉检查",
    "Check the selected components, or the whole visible design, for overlaps, contacts and small clearances":
        "检查选中的零部件或整个可见设计的穿透、接触和过小间隙",
    "Scope": "范围",
    "Selected components ({n})": "选中的零部件（{n}）",
    "All visible components": "全部可见零部件",
    "Named state: {name}": "命名状态：{name}",
    "Report clearance below": "报告小于此值的间隙",
    "Include contacts": "包含接触",
    "Compute": "计算",
    "Cancel": "取消",
    "Close": "关闭",
    "Show overlap": "显示重叠体",
    "Component 1": "零部件 1",
    "Component 2": "零部件 2",
    "Result": "结果",
    "Value": "数值",
    "Allowed": "允许",
    "overlap": "穿透",
    "contact": "接触",
    "clearance": "间隙",
    "undetermined": "无法判断",
    "duplicate": "重复件",
    "Yes": "是",
    "{v} mm³": "{v} mm³",
    "{v} mm": "{v} mm",
    "Checking {done} of {total} pairs…": "正在检查 {done}/{total} 对…",
    "{n} solids, {pairs} pairs checked in {s} s: {overlap} overlaps, {contact} contacts, {clearance} small clearances, {undetermined} undetermined.":
        "{n} 个实体，{pairs} 对，用时 {s} 秒：穿透 {overlap}，接触 {contact}，小间隙 {clearance}，无法判断 {undetermined}。",
    "Cancelled after {pairs} of {total} pairs; the results so far are listed.":
        "已在 {pairs}/{total} 对处取消，下面列出已得到的结果。",
    "Nothing to check.": "没有可检查的对象。",
    "Select at least two solids or an assembly that contains them.": "请至少选择两个实体，或包含它们的装配。",
    "not a closed solid": "不是封闭实体",
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


def selected_scope():
    """(root, sub-element path) for each selected component."""
    scope = []
    for selection in Gui.Selection.getSelectionEx("", 0):
        for sub in selection.SubElementNames or ("",):
            entry = (selection.Object, sub)
            if entry not in scope:
                scope.append(entry)
    return scope


class Overlay:
    """A red, see-through copy of an overlap volume drawn in the 3D view, not in the document."""

    def __init__(self):
        self.node = None
        self.view = None

    def show(self, shape):
        self.clear()
        from pivy import coin
        view = Gui.ActiveDocument.ActiveView if Gui.ActiveDocument else None
        if view is None or shape.isNull():
            return False
        reader = coin.SoInput()
        reader.setBuffer(shape.writeInventor())
        body = coin.SoDB.readAll(reader)
        if body is None:
            return False
        node = coin.SoSeparator()
        node.setName("SvenInterferenceOverlay")
        material = coin.SoMaterial()
        material.diffuseColor = (0.9, 0.1, 0.1)
        material.emissiveColor = (0.5, 0.0, 0.0)
        material.transparency = 0.35
        node.addChild(material)
        node.addChild(body)
        view.getSceneGraph().addChild(node)
        self.node, self.view = node, view
        return True

    def clear(self):
        if self.node is not None:
            try:
                self.view.getSceneGraph().removeChild(self.node)
            except Exception:
                pass
        self.node = None
        self.view = None


class InterferenceDialog:
    def __init__(self, doc, scope):
        QtCore, _, QtWidgets = _widgets()
        self.doc = doc
        self.scope = scope
        self.report = None
        self.cancel_requested = False
        self.overlay = Overlay()
        dialog = QtWidgets.QDialog(Gui.getMainWindow())
        dialog.setObjectName("SvenInterferenceDialog")
        dialog.setWindowTitle(tr("Interference"))
        dialog.resize(860, 440)
        dialog.finished.connect(lambda *_: self.overlay.clear())
        self.dialog = dialog
        layout = QtWidgets.QVBoxLayout(dialog)
        form = QtWidgets.QFormLayout()
        self.scope_box = QtWidgets.QComboBox(dialog)
        self.scope_box.setObjectName("SvenInterferenceScope")
        if scope:
            self.scope_box.addItem(tr("Selected components ({n})", n=len(scope)), "selection")
        self.scope_box.addItem(tr("All visible components"), "visible")
        try:
            import SvenReplace
            for state in SvenReplace.states(doc):
                self.scope_box.addItem(tr("Named state: {name}", name=state.Label), "state:" + state.Name)
        except Exception:
            pass
        form.addRow(tr("Scope"), self.scope_box)
        self.limit = QtWidgets.QDoubleSpinBox(dialog)
        self.limit.setObjectName("SvenInterferenceClearance")
        self.limit.setDecimals(3)
        self.limit.setRange(0.0, 100.0)
        self.limit.setSingleStep(0.1)
        self.limit.setSuffix(" mm")
        self.limit.setValue(0.2)
        form.addRow(tr("Report clearance below"), self.limit)
        self.contacts = QtWidgets.QCheckBox(tr("Include contacts"), dialog)
        self.contacts.setObjectName("SvenInterferenceContacts")
        self.contacts.setChecked(True)
        form.addRow("", self.contacts)
        layout.addLayout(form)
        headers = ("Component 1", "Component 2", "Result", "Value", "Allowed")
        self.table = QtWidgets.QTableWidget(0, len(headers), dialog)
        self.table.setObjectName("SvenInterferenceResults")
        self.table.setHorizontalHeaderLabels([tr(h) for h in headers])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self.locate)
        layout.addWidget(self.table, 1)
        self.status = QtWidgets.QLabel("", dialog)
        self.status.setObjectName("SvenInterferenceStatus")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.show_overlap = QtWidgets.QCheckBox(tr("Show overlap"), dialog)
        self.show_overlap.setObjectName("SvenInterferenceShowOverlap")
        self.show_overlap.setChecked(True)
        self.show_overlap.toggled.connect(lambda *_: self.locate())
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.show_overlap)
        buttons.addStretch(1)
        self.compute_button = QtWidgets.QPushButton(tr("Compute"), dialog)
        self.compute_button.setObjectName("SvenInterferenceCompute")
        self.compute_button.setDefault(True)
        self.compute_button.clicked.connect(self.compute)
        self.cancel_button = QtWidgets.QPushButton(tr("Cancel"), dialog)
        self.cancel_button.setObjectName("SvenInterferenceCancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.request_cancel)
        close = QtWidgets.QPushButton(tr("Close"), dialog)
        close.setObjectName("SvenInterferenceClose")
        close.clicked.connect(dialog.reject)
        for button in (self.compute_button, self.cancel_button, close):
            buttons.addWidget(button)
        layout.addLayout(buttons)

    def request_cancel(self):
        self.cancel_requested = True

    def _progress(self, done, total):
        _, _, QtWidgets = _widgets()
        if done == total or done % 4 == 0:
            self.status.setText(tr("Checking {done} of {total} pairs…", done=done, total=total))
        QtWidgets.QApplication.processEvents()

    def _cancel(self):
        _, _, QtWidgets = _widgets()
        QtWidgets.QApplication.processEvents()
        return self.cancel_requested

    def compute(self):
        import SvenInterference
        self.overlay.clear()
        self.cancel_requested = False
        self.compute_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.table.setRowCount(0)
        try:
            data = self.scope_box.currentData() or ""
            use_selection = data == "selection"
            state = self.doc.getObject(data[6:]) if data.startswith("state:") else None
            roots = self.scope if use_selection else (None if state else SvenInterference.default_roots(self.doc))
            self.report = SvenInterference.check(
                roots, doc=self.doc, state=state, visible_only=not use_selection,
                clearance_limit=self.limit.value() or None,
                include_contacts=self.contacts.isChecked(), cancel=self._cancel, progress=self._progress)
        except SvenInterference.InterferenceError as error:
            self.status.setText(tr(str(error)))
            return None
        finally:
            self.compute_button.setEnabled(True)
            self.cancel_button.setEnabled(False)
        self.fill()
        return self.report

    def fill(self):
        _, _, QtWidgets = _widgets()
        report = self.report
        rows = report["results"]
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            kind = tr(row["kind"]) + (f" · {tr('duplicate')}" if row.get("duplicate") else "")
            if row["kind"] == "overlap":
                value = tr("{v} mm³", v=f"{row['volume']:.4g}")
            elif "distance" in row:
                value = tr("{v} mm", v=f"{row['distance']:.4g}")
            else:
                value = ""
            if row.get("reason"):
                value = (value + " " if value else "") + tr(row["reason"])
            cells = (row["a"]["label"], row["b"]["label"], kind, value, tr("Yes") if row["allowed"] else "")
            for column, text in enumerate(cells):
                self.table.setItem(index, column, QtWidgets.QTableWidgetItem(text))
        self.table.resizeColumnsToContents()
        counts = report["counts"]
        text = tr("{n} solids, {pairs} pairs checked in {s} s: {overlap} overlaps, {contact} contacts, "
                  "{clearance} small clearances, {undetermined} undetermined.",
                  n=report["instances"], pairs=report["pairs_checked"], s=report["seconds"], **counts)
        if report["cancelled"]:
            text = tr("Cancelled after {pairs} of {total} pairs; the results so far are listed.",
                      pairs=report["pairs_checked"], total=report["candidate_pairs"]) + " " + text
        self.status.setText(text)

    def current(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows or self.report is None:
            return None
        return self.report["results"][rows[0].row()]

    def locate(self):
        """Select both components of the chosen result and show the overlap volume."""
        import SvenInterference
        self.overlay.clear()
        row = self.current()
        if row is None:
            return
        Gui.Selection.clearSelection()
        for entry in (row["a"], row["b"]):
            root, _, sub = entry["key"].partition(".")
            Gui.Selection.addSelection(self.doc.Name, root, sub)
        if row["kind"] == "overlap" and self.show_overlap.isChecked():
            try:
                self.overlay.show(SvenInterference.overlap_shape(self.doc, row))
            except Exception:
                pass

    def exec(self):
        self.dialog.show()
        return self.dialog


class InterferenceCommand:
    def GetResources(self):
        return {"Pixmap": "Part_CheckGeometry", "MenuText": "Interference",
                "ToolTip": tr("Check the selected components, or the whole visible design, for overlaps, "
                              "contacts and small clearances")}

    def IsActive(self):
        return App.ActiveDocument is not None

    def Activated(self):
        doc = App.ActiveDocument
        global _OPEN
        _OPEN = InterferenceDialog(doc, selected_scope())
        _OPEN.exec()


_OPEN = None


def open_dialog():
    return _OPEN


def register():
    Gui.addCommand(COMMAND, InterferenceCommand())
