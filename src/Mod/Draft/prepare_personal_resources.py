# SPDX-License-Identifier: LGPL-2.1-or-later

import sys
from pathlib import Path
import xml.etree.ElementTree as ET


source_qrc, output_qrc = (Path(path).resolve() for path in sys.argv[1:])
source = source_qrc.parent
output_qrc.parent.mkdir(parents=True, exist_ok=True)
ui_relative = "ui/preferences-dxf.ui"
output_ui = output_qrc.parent / ui_relative
output_ui.parent.mkdir(parents=True, exist_ok=True)

ui = ET.parse(source / ui_relative)
mesh_option = ui.find(".//item/layout[@name='horizontalLayout_Export3D']/..")
assert mesh_option is not None
parent = next(element for element in ui.iter() if mesh_option in element)
parent.remove(mesh_option)
connections = ui.find("connections")
for connection in list(connections):
    if connection.findtext("receiver") == "checkBox_dxfmesh":
        connections.remove(connection)
ui.write(output_ui, encoding="utf-8", xml_declaration=True)

qrc = ET.parse(source_qrc)
for entry in qrc.iter("file"):
    relative = entry.text
    entry.attrib.setdefault("alias", relative)
    entry.text = str(output_ui if relative == ui_relative else source / relative)
qrc.write(output_qrc, encoding="utf-8", xml_declaration=True)
