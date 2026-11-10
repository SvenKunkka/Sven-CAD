#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
"""Prepare the personal app's Info.plist before bundle signing."""

import argparse
import plistlib
from pathlib import Path


APP_ID = "local.personalcad.PersonalCAD"
APP_NAME = "Sven CAD"
FCSTD_ID = "org.freecad.fcstd"


def personalize(info):
    info["CFBundleIdentifier"] = APP_ID
    info["CFBundleName"] = APP_NAME
    info["CFBundleDisplayName"] = APP_NAME
    info["CFBundleDocumentTypes"] = [{
        "CFBundleTypeName": "FreeCAD Document",
        "CFBundleTypeRole": "Editor",
        "CFBundleTypeIconFile": "freecad-doc.icns",
        "LSHandlerRank": "Alternate",
        "LSItemContentTypes": [FCSTD_ID],
    }]
    declarations = info.pop("UTExportedTypeDeclarations", [])
    fcstd = next(
        (item for item in declarations if item["UTTypeIdentifier"] == FCSTD_ID),
        None,
    )
    if fcstd is None:
        raise ValueError("Expected the upstream FreeCAD FCStd type declaration.")
    info["UTImportedTypeDeclarations"] = [fcstd]
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    with args.source.open("rb") as source:
        info = plistlib.load(source)
    info = personalize(info)
    with args.destination.open("xb") as destination:
        plistlib.dump(info, destination)


if __name__ == "__main__":
    main()
