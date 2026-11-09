"""Identify the native images that the GUI process has actually loaded."""

import ctypes
import hashlib
import os
from pathlib import Path


def cocoa_plugin():
    return loaded_image("libqcocoa.dylib")


def qt_widgets():
    return loaded_image("libQt6Widgets.")


def freecad_app():
    return loaded_image("libFreeCADApp.dylib")


def loaded_image(name):
    dyld = ctypes.CDLL(None)
    count = dyld._dyld_image_count
    count.argtypes = []
    count.restype = ctypes.c_uint32
    image_name = dyld._dyld_get_image_name
    image_name.argtypes = [ctypes.c_uint32]
    image_name.restype = ctypes.c_char_p
    for index in range(count()):
        raw = image_name(index)
        if not raw:
            continue
        path = Path(os.fsdecode(raw)).resolve()
        if path.name == name or (path.name.startswith(name) and path.suffix == ".dylib"):
            return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    raise RuntimeError(f"Native image {name} is not loaded")
