# SPDX-License-Identifier: LGPL-2.1-or-later
"""Replace-component and named-state commands for assemblies."""

import FreeCAD as App
import FreeCADGui as Gui

import SvenReplace as R

REPLACE_COMMAND = "PersonalCAD_ReplaceComponent"
STATES_COMMAND = "PersonalCAD_NamedStates"
SHEET_COMMAND = "PersonalCAD_AssemblySheet"
PACKAGE_COMMAND = "PersonalCAD_ExportPackage"

ZH = {
    "Replace Component": "替换零部件",
    "Replace the selected component instances with another version and keep joints, states and exploded views":
        "把选中的零部件实例替换为另一个版本，保留联接、命名状态和爆炸图",
    "Named States": "命名状态",
    "Save and restore which parts are hidden and which exploded view is shown":
        "保存和恢复隐藏了哪些零件、显示哪个爆炸图",
    "Instances": "实例",
    "New version": "新版本",
    "Import STEP…": "导入 STEP…",
    "Keep position": "保持位置",
    "Analyze": "分析",
    "Replace": "替换",
    "Close": "关闭",
    "Status": "状态",
    "Old part": "旧零件",
    "New part": "新零件",
    "Offset": "偏差",
    "(none)": "（无）",
    "same": "相同",
    "changed": "已修改",
    "renamed": "已改名",
    "ambiguous": "有歧义",
    "missing": "缺失",
    "added": "新增",
    "Part number: {old} → {new}": "料号：{old} → {new}",
    "Pose: {source}, shift {shift} mm, rotation {angle}°, residual {residual} mm":
        "位姿：{source}，平移 {shift} mm，旋转 {angle}°，残差 {residual} mm",
    "{n} references: {mapped} mapped, {approx} approximate, {bad} unresolved, {dropped} dropped":
        "{n} 处引用：{mapped} 处对应，{approx} 处近似，{bad} 处无法对应，{dropped} 处删除",
    "Leave unresolved references unchanged": "无法对应的引用保持原样",
    "Choose a part for every ambiguous row first.": "请先为每个有歧义的行选择零件。",
    "Replaced {n} instances.": "已替换 {n} 个实例。",
    "Not replaced: {why}": "未替换：{why}",
    "Select component instances (links) of one component.": "请选择同一零部件的实例（链接）。",
    "No other version in the document. Import one first.": "文档中没有其他版本，请先导入。",
    "State name": "状态名称",
    "Exploded view": "爆炸图",
    "Save current": "保存当前",
    "Apply": "应用",
    "Delete": "删除",
    "Hidden: {n}": "隐藏：{n}",
    "Applied {name}: {n} hidden.": "已应用 {name}：隐藏 {n} 项。",
    "Assembly Drawing": "装配出图",
    "Create a drawing with three views, a section and the exploded view of a named state":
        "按命名状态生成三视图、剖视和爆炸图",
    "State": "状态",
    "(as shown)": "（当前显示）",
    "Section view": "剖视图",
    "Exploded view of the state": "状态的爆炸图",
    "Create": "生成",
    "Created {page}: {views} views, scale {scale}.": "已生成 {page}：{views} 个视图，比例 {scale}。",
    "Export Package": "一键出包",
    "Write coloured STEP, PDF, DXF, BOM CSV and a manifest of this revision; the last good package stays until all succeed":
        "导出本修订的有色 STEP、PDF、DXF、BOM CSV 和来源清单；全部成功前保留上一次有效成果",
    "Create an assembly drawing first.": "请先生成装配图纸。",
    "Package written": "已出包",
    "Package failed; the previous package is unchanged": "出包失败，上一次的成果未改动",
    "Saved {name}: {n} hidden.": "已保存 {name}：隐藏 {n} 项。",
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


def _qt():
    from PySide import QtCore, QtWidgets
    return QtCore, QtWidgets


def selected_instances():
    links = []
    for sel in Gui.Selection.getSelectionEx("", 0):
        for sub in sel.SubElementNames or ("",):
            obj = sel.Object.getSubObject(sub, retType=1) if sub else sel.Object
            # walk up from a picked face to the link that holds it
            chain = sel.Object.getSubObjectList(sub) if sub else [sel.Object]
            link = next((o for o in reversed(chain) if o.TypeId == "App::Link"), None)
            if link is None and obj is not None and obj.TypeId == "App::Link":
                link = obj
            if link is not None and link not in links:
                links.append(link)
    return links


def candidate_versions(doc, current):
    """Components that could replace the current one: top-level parts and solids not in an assembly."""
    out = []
    for obj in doc.Objects:
        if obj == current or obj.TypeId == "App::Link" or obj.isDerivedFrom("Assembly::AssemblyObject"):
            continue
        if not (obj.isDerivedFrom("App::Part") or obj.isDerivedFrom("Part::Feature")):
            continue
        parents = [p for p in obj.InList if p.isDerivedFrom("App::Part") or p.isDerivedFrom("PartDesign::Body")]
        if parents or getattr(obj, "FastenerKind", None):
            continue
        out.append(obj)
    return out


class ReplaceDialog:
    def __init__(self, doc, links):
        QtCore, QtWidgets = _qt()
        self.doc = doc
        self.links = links
        self.result = None
        self.choices = {}
        self.manual = {}
        self.last = None
        dialog = QtWidgets.QDialog(Gui.getMainWindow())
        dialog.setObjectName("SvenReplaceDialog")
        dialog.setWindowTitle(tr("Replace Component"))
        dialog.resize(900, 520)
        self.dialog = dialog
        layout = QtWidgets.QVBoxLayout(dialog)
        form = QtWidgets.QFormLayout()
        self.instances = QtWidgets.QLabel(", ".join(l.Label for l in links), dialog)
        self.instances.setObjectName("SvenReplaceInstances")
        form.addRow(tr("Instances"), self.instances)
        row = QtWidgets.QHBoxLayout()
        self.version = QtWidgets.QComboBox(dialog)
        self.version.setObjectName("SvenReplaceVersion")
        row.addWidget(self.version, 1)
        self.import_button = QtWidgets.QPushButton(tr("Import STEP…"), dialog)
        self.import_button.setObjectName("SvenReplaceImport")
        self.import_button.clicked.connect(self.import_step)
        row.addWidget(self.import_button)
        form.addRow(tr("New version"), row)
        self.keep = QtWidgets.QCheckBox(tr("Keep position"), dialog)
        self.keep.setObjectName("SvenReplaceKeep")
        self.keep.setChecked(True)
        form.addRow("", self.keep)
        layout.addLayout(form)
        self.summary = QtWidgets.QLabel("", dialog)
        self.summary.setObjectName("SvenReplaceSummary")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        headers = ("Status", "Old part", "New part", "Offset")
        self.table = QtWidgets.QTableWidget(0, len(headers), dialog)
        self.table.setObjectName("SvenReplaceTable")
        self.table.setHorizontalHeaderLabels([tr(h) for h in headers])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)
        self.allow = QtWidgets.QCheckBox(tr("Leave unresolved references unchanged"), dialog)
        self.allow.setObjectName("SvenReplaceAllowUnresolved")
        layout.addWidget(self.allow)
        self.status = QtWidgets.QLabel("", dialog)
        self.status.setObjectName("SvenReplaceStatus")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        for name, text, slot in (("SvenReplaceAnalyze", "Analyze", self.analyze),
                                 ("SvenReplaceApply", "Replace", self.apply),
                                 ("SvenReplaceClose", "Close", dialog.close)):
            button = QtWidgets.QPushButton(tr(text), dialog)
            button.setObjectName(name)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.version.currentIndexChanged.connect(self.version_changed)
        self.fill_versions()

    def version_changed(self, *_):
        self.manual = {}
        self.choices = {}
        self.analyze()

    def fill_versions(self, select=None):
        self.version.blockSignals(True)
        self.version.clear()
        current = self.links[0].LinkedObject
        for obj in candidate_versions(self.doc, current):
            self.version.addItem(f"{obj.Label} ({obj.Name})", obj.Name)
        if select is not None:
            index = self.version.findData(select)
            if index >= 0:
                self.version.setCurrentIndex(index)
        self.version.blockSignals(False)
        self.manual = {}
        self.choices = {}
        if self.version.count():
            self.analyze()
        else:
            self.status.setText(tr("No other version in the document. Import one first."))

    def import_step(self):
        _, QtWidgets = _qt()
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self.dialog, tr("Import STEP…"), "",
                                                        "STEP (*.step *.stp *.STEP *.STP)")
        if path:
            self.load_step(path)

    def load_step(self, path):
        """Import a STEP file into the document as a new version and select it."""
        import Import
        before = set(o.Name for o in self.doc.Objects)
        Import.insert(str(path), self.doc.Name)
        self.doc.recompute()
        added = [o for o in self.doc.Objects if o.Name not in before]
        roots = [o for o in added if not any(p in added for p in o.InList)]
        root = roots[0] if roots else None
        for obj in roots:
            obj.Visibility = False
        self.fill_versions(root.Name if root else None)
        return root

    def new_root(self):
        name = self.version.currentData()
        return self.doc.getObject(name) if name else None

    def resolutions(self):
        """Manual picks so far; they stay when the row is no longer ambiguous."""
        for path, combo in self.choices.items():
            data = combo.currentData()
            if data is not None:
                self.manual[path] = data if data != "" else None
        return dict(self.manual)

    def analyze(self):
        QtCore, QtWidgets = _qt()
        new = self.new_root()
        if new is None:
            return None
        self.result = R.match(self.links[0].LinkedObject, new, self.resolutions())
        result = self.result
        old_choices = {p: c.currentData() for p, c in self.choices.items()}
        self.choices = {}
        self.table.setRowCount(len(result["entries"]))
        for row, entry in enumerate(result["entries"]):
            status = tr(entry["status"]) + (" ✋" if entry["note"] == "manual" else "")
            self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(status))
            old_text = entry["old_label"] + (f"  [{entry['old_part_number']}]" if entry["old_part_number"] else "")
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(old_text))
            if entry["status"] in (R.AMBIGUOUS, R.MISSING) and entry["old"] is not None:
                combo = QtWidgets.QComboBox(self.table)
                combo.setObjectName("SvenReplaceChoice")
                combo.addItem("—", None)
                for path in entry["candidates"]:
                    leaf = result["_new"][path]
                    combo.addItem(leaf.label, path)
                combo.addItem(tr("(none)"), "")
                previous = old_choices.get(entry["old"])
                if previous is not None:
                    combo.setCurrentIndex(max(0, combo.findData(previous)))
                self.table.setCellWidget(row, 2, combo)
                self.choices[entry["old"]] = combo
            else:
                new_text = entry["new_label"] + (f"  [{entry['new_part_number']}]"
                                                 if entry["new_part_number"] else "")
                self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(new_text))
            offset = entry["offset"]
            self.table.setItem(row, 3, QtWidgets.QTableWidgetItem("" if offset is None else f"{offset:.4f}"))
        self.table.resizeColumnsToContents()
        pose = result["pose"]
        plan = R.plan_references(self.doc, self.links, result) if result["settled"] else []
        self.last_plan = plan
        lines = [tr("Part number: {old} → {new}", old=result["old_part_number"] or "—",
                    new=result["new_part_number"] or "—"),
                 tr("Pose: {source}, shift {shift} mm, rotation {angle}°, residual {residual} mm",
                    source=pose["source"], shift=", ".join(f"{v:.3f}" for v in pose["shift"]),
                    angle=f"{pose['angle_deg']:.2f}", residual=f"{pose['residual']:.2g}")]
        if plan:
            lines.append(tr("{n} references: {mapped} mapped, {approx} approximate, {bad} unresolved, {dropped} dropped",
                            n=len(plan), mapped=sum(p["state"] == R.MAPPED for p in plan),
                            approx=sum(p["state"] == R.APPROXIMATE for p in plan),
                            bad=sum(p["state"] == R.UNRESOLVED for p in plan),
                            dropped=sum(p["state"] == R.DROPPED for p in plan)))
            for p in plan:
                if p["state"] == R.UNRESOLVED:
                    lines.append(f"  {p['label']}.{p['property']}: {p['sub']}")
        self.summary.setText("\n".join(lines))
        self.status.setText("" if result["settled"] else tr("Choose a part for every ambiguous row first."))
        return result

    def apply(self):
        result = self.analyze()
        if result is None:
            return None
        if not result["settled"]:
            return None
        try:
            report = R.replace(self.doc, self.links, self.new_root(), result, keep_position=self.keep.isChecked(),
                               allow_unresolved=self.allow.isChecked())
        except R.ReplaceError as exc:
            self.status.setText(tr("Not replaced: {why}", why=str(exc)))
            return None
        self.last = report
        if report.get("refused"):
            self.status.setText(tr("Not replaced: {why}", why=report["refused"]))
        else:
            self.status.setText(tr("Replaced {n} instances.", n=len(report["replaced"])))
            Gui.updateGui()
        return report

    def exec(self):
        self.dialog.show()
        return self.dialog


class StatesDialog:
    def __init__(self, doc, assembly):
        QtCore, QtWidgets = _qt()
        self.doc = doc
        self.assembly = assembly
        dialog = QtWidgets.QDialog(Gui.getMainWindow())
        dialog.setObjectName("SvenStatesDialog")
        dialog.setWindowTitle(tr("Named States"))
        dialog.resize(520, 380)
        self.dialog = dialog
        layout = QtWidgets.QVBoxLayout(dialog)
        self.list = QtWidgets.QListWidget(dialog)
        self.list.setObjectName("SvenStatesList")
        layout.addWidget(self.list, 1)
        form = QtWidgets.QFormLayout()
        self.name = QtWidgets.QLineEdit(dialog)
        self.name.setObjectName("SvenStatesName")
        form.addRow(tr("State name"), self.name)
        self.view = QtWidgets.QComboBox(dialog)
        self.view.setObjectName("SvenStatesView")
        self.view.addItem(tr("(none)"), "")
        for obj in self.exploded_views():
            self.view.addItem(obj.Label, obj.Name)
        form.addRow(tr("Exploded view"), self.view)
        layout.addLayout(form)
        self.status = QtWidgets.QLabel("", dialog)
        self.status.setObjectName("SvenStatesStatus")
        layout.addWidget(self.status)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        for name, text, slot in (("SvenStatesSave", "Save current", self.save),
                                 ("SvenStatesApply", "Apply", self.apply),
                                 ("SvenStatesDelete", "Delete", self.delete),
                                 ("SvenStatesClose", "Close", dialog.close)):
            button = QtWidgets.QPushButton(tr(text), dialog)
            button.setObjectName(name)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.refresh()

    def exploded_views(self):
        out = []
        for group in self.assembly.Group:
            if group.TypeId == "Assembly::ViewGroup":
                out += [o for o in group.Group if hasattr(o, "Proxy") and hasattr(o.Proxy, "applyMoves")]
        return out

    def refresh(self):
        _, QtWidgets = _qt()
        self.list.clear()
        for state in R.states(self.doc):
            if state.SvenAssembly == self.assembly:
                item = QtWidgets.QListWidgetItem(f"{state.Label}  —  {tr('Hidden: {n}', n=len(R.state_hidden(state)))}")
                item.setData(256, state.Name)
                self.list.addItem(item)

    def current(self):
        item = self.list.currentItem()
        return self.doc.getObject(item.data(256)) if item else None

    def save(self, name=None):
        name = name or self.name.text().strip() or f"State {len(R.states(self.doc)) + 1}"
        view = self.doc.getObject(self.view.currentData()) if self.view.currentData() else None
        state = R.capture(self.assembly, name, view)
        self.refresh()
        self.status.setText(tr("Saved {name}: {n} hidden.", name=name, n=len(R.state_hidden(state))))
        return state

    def apply(self, state=None):
        state = state or self.current()
        if state is None:
            return None
        self.doc.openTransaction("Apply named state")
        hidden = R.apply_state(state)
        self.doc.commitTransaction()
        Gui.updateGui()
        self.status.setText(tr("Applied {name}: {n} hidden.", name=state.Label, n=len(hidden)))
        return hidden

    def delete(self):
        state = self.current()
        if state is not None:
            self.doc.openTransaction("Delete named state")
            self.doc.removeObject(state.Name)
            self.doc.commitTransaction()
            self.refresh()

    def exec(self):
        self.dialog.show()
        return self.dialog


class SheetDialog:
    def __init__(self, doc, assembly):
        _, QtWidgets = _qt()
        self.doc = doc
        self.assembly = assembly
        self.made = None
        dialog = QtWidgets.QDialog(Gui.getMainWindow())
        dialog.setObjectName("SvenSheetDialog")
        dialog.setWindowTitle(tr("Assembly Drawing"))
        self.dialog = dialog
        layout = QtWidgets.QVBoxLayout(dialog)
        form = QtWidgets.QFormLayout()
        self.state = QtWidgets.QComboBox(dialog)
        self.state.setObjectName("SvenSheetState")
        self.state.addItem(tr("(as shown)"), "")
        for state in R.states(doc):
            if state.SvenAssembly == assembly:
                self.state.addItem(state.Label, state.Name)
        form.addRow(tr("State"), self.state)
        self.section = QtWidgets.QCheckBox(tr("Section view"), dialog)
        self.section.setObjectName("SvenSheetSection")
        self.section.setChecked(True)
        form.addRow("", self.section)
        self.exploded = QtWidgets.QCheckBox(tr("Exploded view of the state"), dialog)
        self.exploded.setObjectName("SvenSheetExploded")
        self.exploded.setChecked(True)
        form.addRow("", self.exploded)
        layout.addLayout(form)
        self.status = QtWidgets.QLabel("", dialog)
        self.status.setObjectName("SvenSheetStatus")
        layout.addWidget(self.status)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        for name, text, slot in (("SvenSheetCreate", "Create", self.create), ("SvenSheetClose", "Close", dialog.close)):
            button = QtWidgets.QPushButton(tr(text), dialog)
            button.setObjectName(name)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        layout.addLayout(buttons)

    def create(self):
        import SvenSheets
        name = self.state.currentData()
        state = self.doc.getObject(name) if name else None
        made = SvenSheets.make_sheet(self.assembly, state, section=self.section.isChecked(),
                                     exploded=self.exploded.isChecked())
        self.made = made
        count = len(made["views"]) + ("section" in made) + ("exploded" in made)
        self.status.setText(tr("Created {page}: {views} views, scale {scale}.", page=made["page"].Label,
                               views=count, scale=SvenSheets.scale_text(made["scale"])))
        return made

    def exec(self):
        self.dialog.show()
        return self.dialog


def _assembly(doc):
    try:
        import UtilsAssembly
        active = UtilsAssembly.activeAssembly()
    except Exception:
        active = None
    if active is not None:
        return active
    return next((o for o in doc.Objects if o.isDerivedFrom("Assembly::AssemblyObject")), None)


class ReplaceCommand:
    def GetResources(self):
        return {"Pixmap": "Assembly_InsertLink", "MenuText": "Replace Component",
                "ToolTip": tr("Replace the selected component instances with another version and keep joints, "
                              "states and exploded views")}

    def IsActive(self):
        return App.ActiveDocument is not None

    def Activated(self):
        global _OPEN
        links = selected_instances()
        if not links or len({l.LinkedObject for l in links}) != 1:
            _, QtWidgets = _qt()
            QtWidgets.QMessageBox.information(Gui.getMainWindow(), tr("Replace Component"),
                                              tr("Select component instances (links) of one component."))
            return
        _OPEN = ReplaceDialog(App.ActiveDocument, links)
        _OPEN.exec()


class StatesCommand:
    def GetResources(self):
        return {"Pixmap": "Std_ToggleVisibility", "MenuText": "Named States",
                "ToolTip": tr("Save and restore which parts are hidden and which exploded view is shown")}

    def IsActive(self):
        return App.ActiveDocument is not None and _assembly(App.ActiveDocument) is not None

    def Activated(self):
        global _OPEN
        _OPEN = StatesDialog(App.ActiveDocument, _assembly(App.ActiveDocument))
        _OPEN.exec()


def package_page(doc, assembly):
    pages = [o for o in doc.Objects if o.isDerivedFrom("TechDraw::DrawPage")]
    sheets = [p for p in pages if any(getattr(v, "Source", None) and
                                      getattr(v.Source[0], "SvenAssembly", None) == assembly
                                      for v in p.Views if hasattr(v, "Source"))]
    return (sheets or pages or [None])[0]


class PackageCommand:
    def GetResources(self):
        return {"Pixmap": "Std_Export", "MenuText": "Export Package",
                "ToolTip": tr("Write coloured STEP, PDF, DXF, BOM CSV and a manifest of this revision; "
                              "the last good package stays until all succeed")}

    def IsActive(self):
        return App.ActiveDocument is not None and _assembly(App.ActiveDocument) is not None

    def Activated(self):
        import os
        import SvenPackage
        _, QtWidgets = _qt()
        doc = App.ActiveDocument
        assembly = _assembly(doc)
        page = package_page(doc, assembly)
        title = tr("Export Package")
        if page is None:
            QtWidgets.QMessageBox.information(Gui.getMainWindow(), title, tr("Create an assembly drawing first."))
            return
        start = os.path.dirname(doc.FileName) if doc.FileName else os.path.expanduser("~")
        folder = QtWidgets.QFileDialog.getExistingDirectory(Gui.getMainWindow(), title, start)
        if not folder:
            return
        try:
            manifest = SvenPackage.export_package(assembly, page, folder)
        except Exception as error:
            QtWidgets.QMessageBox.warning(Gui.getMainWindow(), tr("Package failed; the previous package is unchanged"),
                                          str(error))
            return
        QtWidgets.QMessageBox.information(Gui.getMainWindow(), tr("Package written"),
                                          "\n".join(o["file"] for o in manifest["outputs"]))


class SheetCommand:
    def GetResources(self):
        return {"Pixmap": "TechDraw_PageDefault", "MenuText": "Assembly Drawing",
                "ToolTip": tr("Create a drawing with three views, a section and the exploded view of a named state")}

    def IsActive(self):
        return App.ActiveDocument is not None and _assembly(App.ActiveDocument) is not None

    def Activated(self):
        global _OPEN
        _OPEN = SheetDialog(App.ActiveDocument, _assembly(App.ActiveDocument))
        _OPEN.exec()


_OPEN = None


def open_dialog():
    return _OPEN


def register():
    Gui.addCommand(REPLACE_COMMAND, ReplaceCommand())
    Gui.addCommand(STATES_COMMAND, StatesCommand())
    Gui.addCommand(SHEET_COMMAND, SheetCommand())
    Gui.addCommand(PACKAGE_COMMAND, PackageCommand())
