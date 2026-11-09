# SPDX-License-Identifier: LGPL-2.1-or-later

import csv
import io

import FreeCAD as App
import FreeCADGui as Gui
from PySide import QtCore, QtWidgets


translate = App.Qt.translate


class BomTableModel(QtCore.QAbstractTableModel):
    def __init__(self, columns, entries, parent):
        super().__init__(parent)
        self.columns = columns
        self.entries = entries

    def rowCount(self, parent=QtCore.QModelIndex()):
        if parent.isValid() or not self.columns:
            return 0
        return len(self.entries) // len(self.columns)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.columns)

    def data(self, index, role=QtCore.Qt.ItemDataRole.DisplayRole):
        if index.isValid() and role == QtCore.Qt.ItemDataRole.DisplayRole:
            return self.entries[index.row() * len(self.columns) + index.column()]
        return None

    def headerData(self, section, orientation, role=QtCore.Qt.ItemDataRole.DisplayRole):
        if (
            orientation == QtCore.Qt.Orientation.Horizontal
            and role == QtCore.Qt.ItemDataRole.DisplayRole
        ):
            return self.columns[section]
        return super().headerData(section, orientation, role)


def showPreview(bom):
    dialog = QtWidgets.QDialog(Gui.getMainWindow())
    dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
    dialog.setWindowTitle(bom.Label)
    layout = QtWidgets.QVBoxLayout(dialog)
    table = QtWidgets.QTableView(dialog)
    table.setModel(BomTableModel(bom.columnsNames, bom.BomRows, table))
    table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
    table.horizontalHeader().setStretchLastSection(True)
    layout.addWidget(table)
    dialog.resize(720, 400)
    dialog.show()


def writeCsv(filename, columns, entries):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    if columns:
        writer.writerow(columns)
        for start in range(0, len(entries), len(columns)):
            writer.writerow(entries[start : start + len(columns)])

    contents = stream.getvalue().encode("utf-8")
    output = QtCore.QSaveFile(filename)
    if not output.open(QtCore.QIODevice.OpenModeFlag.WriteOnly):
        raise OSError(output.errorString())
    if output.write(contents) != len(contents) or not output.commit():
        raise OSError(output.errorString())


def exportCsv(bom, parent):
    dialog = QtWidgets.QFileDialog(
        parent,
        translate("Assembly", "Export Bill of Materials"),
        "",
        translate("Assembly", "CSV files (*.csv)"),
    )
    dialog.setAcceptMode(QtWidgets.QFileDialog.AcceptMode.AcceptSave)
    dialog.setDefaultSuffix("csv")
    if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
        return
    try:
        writeCsv(dialog.selectedFiles()[0], bom.columnsNames, bom.BomRows)
    except OSError as error:
        QtWidgets.QMessageBox.warning(parent, translate("Assembly", "Export failed"), str(error))
