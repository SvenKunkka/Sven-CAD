#!/usr/bin/env python3
"""Stage a fresh local macOS app from the locked runtime and a CMake install receipt."""

import argparse
import ctypes
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path


TOOLS = Path(__file__).resolve().parent
CANDIDATE = Path(os.environ.get("PERSONAL_CAD_SOURCE", TOOLS.parents[1])).expanduser().resolve()
WORK = Path(os.environ.get("PERSONAL_CAD_WORK", CANDIDATE / ".personal-cad-build")).expanduser().resolve()
PREFIX = Path(os.environ.get("PERSONAL_CAD_PREFIX", CANDIDATE / ".pixi/envs/default")).expanduser().resolve()
REFERENCE = Path(os.environ.get(
    "PERSONAL_CAD_REFERENCE_APP", WORK / "reference/FreeCAD.app"
)).expanduser().resolve() / "Contents"
QT_REFERENCE = (WORK / "qt").resolve()
PLUGIN_PATH = Path("lib/qt6/plugins/platforms/libqcocoa.dylib")
WIDGETS_PATH = Path("lib/libQt6Widgets.6.11.2.dylib")
CORE_LIBRARIES = tuple(Path(f"lib/libFreeCAD{name}.dylib") for name in ("App", "Base", "Gui"))
STOCK_SHA256 = "01718a3e37874a9ac5a7533a89bc74ca1393851dfc4dbaff48ad79784c3dc2c1"
PATCH_SHA256 = "c8376b2232bdca96d656334bc289e6ed9fe95550b0e78aacfb09997c3722cd7f"
WIDGETS_SHA256 = "fef99db5282336439632491867b2ba8d40c6ef9791378d23293c2fc7c14cd15d"
QTBASE_SHA256 = "5b2e00eccaf5a4d8c14134ffa0ea8dfd0a35ae1ffc7f8d87fa4305a1ed23cf22"
QT_PATCHES = (
    "QTBUG-149612.patch",
    "QT-AX-nested-tree-selection.patch",
    "QT-AX-tree-selection-after-mutations.patch",
    "QT-AX-retain-tree-selection-on-layout.patch",
)
COCOA_BINARY = Path("plugin-B-reference/plugins/platforms/libqcocoa.dylib")
WIDGETS_BINARY = Path("widgets-C3-build") / WIDGETS_PATH
OMITTED_PACKAGES = {"calculix", "gmsh", "smesh", "ifcopenshell", "opencamlib", "pcl"}
OMITTED_MODULES = {
    "fem", "robot", "cam", "path", "bim", "points", "inspection",
    "reverseengineering", "mesh", "meshpart", "flatmesh", "jtreader", "openscad",
    "spreadsheet", "material",
}
RUNTIME_ROOTS = {"bin", "etc", "fonts", "lib", "libexec", "share", "ssl",
                 "PySide6", "shiboken6", "__clobbers__"}
LOAD_COMMANDS = {
    "LC_LOAD_DYLIB", "LC_LOAD_WEAK_DYLIB", "LC_REEXPORT_DYLIB",
    "LC_LOAD_UPWARD_DYLIB", "LC_LAZY_LOAD_DYLIB",
}
MACHO_MAGIC = {
    b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
}
CLONE_FILE = getattr(ctypes.CDLL(None), "clonefile", None)
if CLONE_FILE is not None:
    CLONE_FILE.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int)
    CLONE_FILE.restype = ctypes.c_int


def run(*args):
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE).stdout


def safe_relative(path):
    normalized = Path(os.path.normpath(path))
    if normalized.is_absolute() or normalized.parts[:1] == ("..",):
        raise ValueError(f"Path escapes the runtime root: {path}")
    return normalized


def runtime_files(personal):
    records = [json.loads(path.read_text()) for path in (PREFIX / "conda-meta").glob("*.json")]
    if not records:
        raise FileNotFoundError(f"No locked dependency receipts in {PREFIX}")
    omit = OMITTED_PACKAGES if personal else set()
    owners = {}
    for package in records:
        if package["name"] in omit:
            continue
        dependencies = {item.split()[0] for item in package.get("depends", [])}
        if dependencies & omit:
            raise ValueError(f"{package['name']} depends on excluded packages {dependencies & omit}")
        for name in package.get("files", []):
            path = safe_relative(name)
            owners.setdefault(path, set()).add(package["name"])

    paths = set()
    for path in owners:
        source = PREFIX / path
        reference = REFERENCE / "Resources" / path
        if (
            path.parts[0] not in RUNTIME_ROOTS
            or path.suffix in {".o", ".a", ".pyc"}
            or path.name.startswith("libLTO.")
            or "menuinst" in path.parts
            or (personal and path.parts[2:5] == ("site-packages", "tornado", "test"))
        ):
            continue
        if path.parent.name == "PySide6" and path.name.startswith("Qt") and path.suffix == ".so":
            module = path.name.split(".")[0].removeprefix("Qt")
            if not (PREFIX / "lib" / f"libQt6{module}.6.dylib").is_file():
                continue
        if (reference.is_file() or reference.is_symlink()) and (source.is_file() or source.is_symlink()):
            paths.add(path)

    # The official pack copies the python executable. Preserve its source symlink
    # and include its recorded target in the staged runtime.
    pending = list(paths)
    while pending:
        path = pending.pop()
        source = PREFIX / path
        if source.is_symlink():
            target = safe_relative(path.parent / os.readlink(source))
            if target not in paths and target in owners:
                paths.add(target)
                pending.append(target)
    for relative in (PLUGIN_PATH, Path("bin/python"), Path("lib/libQt6Core.6.dylib")):
        if relative not in paths:
            raise FileNotFoundError(f"Required runtime file absent from selection: {relative}")
    selected_owners = Counter(owner for path in paths for owner in owners[path])
    return sorted(paths), dict(sorted(selected_owners.items()))


def sha256(path):
    with path.open("rb") as stream:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def qt_source_identity():
    return {
        "qtbase_sha256": QTBASE_SHA256,
        "patches": {patch: sha256(TOOLS / "qt-cocoa" / patch) for patch in QT_PATCHES},
    }


def qt_output_digests(receipt):
    if receipt is None:
        return PATCH_SHA256, WIDGETS_SHA256
    record = json.loads(receipt.read_text())
    if record["source"] != qt_source_identity():
        raise ValueError(f"Qt build receipt does not match the pinned source and patches: {receipt}")
    digests = tuple(record["outputs"][str(binary)] for binary in (COCOA_BINARY, WIDGETS_BINARY))
    if not all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in digests):
        raise ValueError(f"Invalid binary digest in Qt build receipt: {receipt}")
    return digests


def install_receipt(personal):
    build = WORK / ("personal-release" if personal else "baseline-a")
    prefix = build / "stage"
    receipt = build / "install_manifest.txt"
    paths = sorted({Path(name).relative_to(prefix) for name in receipt.read_text().splitlines() if name})
    if not paths or not all((prefix / path).is_file() or (prefix / path).is_symlink() for path in paths):
        raise ValueError(f"Incomplete staged source install: {receipt}")
    paths = [path for path in paths if not (
        path.parts[0] == "include" or path.parts[:2] in {("lib", "cmake"), ("lib", "pkgconfig")}
        or path.suffix in {".a", ".o", ".pyc"}
    )]
    if personal:
        removed_examples = {"BIMExample.FCStd", "FEMExample.FCStd", "ArchDetail.FCStd"}
        paths = [
            path for path in paths
            if not (path.parent == Path("share/examples") and path.name in removed_examples)
        ]
        for path in paths:
            parts = [part.lower() for part in path.parts]
            if any(parent == "mod" and child in OMITTED_MODULES for parent, child in zip(parts, parts[1:])) or parts[0] in {"plugins", "library"}:
                raise ValueError(f"Excluded module or extension appears in the install receipt: {path}")
            if path.parent == Path("lib") and re.fullmatch(
                r"(?:lib)?(?:Mesh(?:Part|Gui|PartGui)?|Fem(?:Gui)?|Spreadsheet(?:Gui)?|"
                r"Material[s]?|MatGui|Robot(?:Gui)?|Points(?:Gui)?|Inspection(?:Gui)?|"
                r"ReverseEngineering(?:Gui)?|Path(?:App|Gui|Simulator)|CAMSimulator|FlatMesh|JtReader|OpenSCAD)\.(?:so|dylib)",
                path.name, flags=re.IGNORECASE,
            ):
                raise ValueError(f"Excluded native module appears in the install receipt: {path}")
    return prefix, paths, hashlib.sha256(receipt.read_bytes()).hexdigest()


def copy_file(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if CLONE_FILE is not None and not source.is_symlink():
        if CLONE_FILE(os.fsencode(source), os.fsencode(destination), 0) == 0:
            return
    shutil.copy2(source, destination, follow_symlinks=False)


def is_macho(path):
    if path.is_symlink() or path.suffix == ".class":
        return False
    with path.open("rb") as stream:
        return stream.read(4) in MACHO_MAGIC


def macho_commands(paths):
    result = {path: {"rpaths": [], "loads": [], "ids": []} for path in paths}
    for start in range(0, len(paths), 100):
        batch = paths[start:start + 100]
        by_name = {str(path): path for path in batch}
        current = None
        command = None
        for line in run("/usr/bin/otool", "-arch", "arm64", "-l", *batch).splitlines():
            if line.endswith(":") and not line.startswith((" ", "\t")):
                name = line.removesuffix(":").removesuffix(" (architecture arm64)")
                current = result.get(by_name.get(name))
                command = None
            elif current is not None:
                field = line.strip()
                if field.startswith("cmd "):
                    command = field.removeprefix("cmd ")
                elif command == "LC_RPATH" and field.startswith("path "):
                    current["rpaths"].append(field[5:].rsplit(" (offset ", 1)[0])
                elif command in LOAD_COMMANDS and field.startswith("name "):
                    current["loads"].append(field[5:].rsplit(" (offset ", 1)[0])
                elif command == "LC_ID_DYLIB" and field.startswith("name "):
                    current["ids"].append(field[5:].rsplit(" (offset ", 1)[0])
        if not all(any(result[path].values()) for path in batch):
            empty = [path for path in batch if not any(result[path].values())]
            raise ValueError(f"No arm64 load commands read for {empty}")
    return result


def loader_relative(path, destination):
    return "@loader_path/" + os.path.relpath(destination, path.parent)


def relocate_native_files(app, installed_prefix):
    resources = app / "Contents/Resources"
    library = resources / "lib"
    installed_prefixes = {PREFIX, installed_prefix, QT_REFERENCE / "reference-prefix"}
    binaries = sorted(path for path in app.rglob("*") if path.is_file() and is_macho(path))
    commands = macho_commands(binaries)
    altered = 0
    for path, entries in commands.items():
        options = []
        for rpath in dict.fromkeys(entries["rpaths"]):
            if Path(rpath).is_absolute() and any(Path(rpath).is_relative_to(p) for p in installed_prefixes):
                relative = next(Path(rpath).relative_to(p) for p in installed_prefixes if Path(rpath).is_relative_to(p))
                new = loader_relative(path, resources / relative)
                options.extend(("-delete_rpath", rpath))
                if new not in entries["rpaths"] and not (relative == Path("lib") and "@loader_path/" in entries["rpaths"] and path.parent == library):
                    options.extend(("-add_rpath", new))
        for old in dict.fromkeys(entries["loads"]):
            origin = next((p for p in installed_prefixes if Path(old).is_relative_to(p)), None)
            if origin:
                options.extend(("-change", old, loader_relative(path, resources / Path(old).relative_to(origin))))
        for old in entries["ids"]:
            if any(Path(old).is_relative_to(p) for p in installed_prefixes):
                options.extend(("-id", "@rpath/" + path.name))
        if options:
            run("/usr/bin/install_name_tool", *options, path)
            run("/usr/bin/codesign", "--force", "--sign", "-", path)
            altered += 1
    if altered:
        commands = macho_commands(binaries)
    failures = []
    for path, entries in commands.items():
        for rpath in entries["rpaths"]:
            if Path(rpath).is_absolute() and not rpath.startswith(("/usr/lib", "/System/Library/")):
                failures.append(f"{path.relative_to(app)} RPATH {rpath}")
        roots = [library, path.parent]
        for rpath in entries["rpaths"]:
            if rpath == "@loader_path" or rpath.startswith("@loader_path/"):
                roots.append(path.parent / rpath.removeprefix("@loader_path").lstrip("/"))
            elif rpath == "@executable_path" or rpath.startswith("@executable_path/"):
                roots.append(resources / "bin" / rpath.removeprefix("@executable_path").lstrip("/"))
        for name in entries["loads"]:
            if name.startswith(("/usr/lib/", "/System/Library/")):
                continue
            if name.startswith("@rpath/"):
                candidates = [root / name.removeprefix("@rpath/") for root in roots]
            elif name.startswith("@loader_path/"):
                candidates = [path.parent / name.removeprefix("@loader_path/")]
            elif name.startswith("@executable_path/"):
                candidates = [resources / "bin" / name.removeprefix("@executable_path/")]
            else:
                candidates = []
            if not any(candidate.is_file() and candidate.resolve().is_relative_to(app) for candidate in candidates):
                failures.append(f"{path.relative_to(app)} -> {name}")
    if failures:
        raise ValueError("Unresolved or external native references:\n" + "\n".join(failures))
    return {"native_images": len(binaries), "relocated_images": altered}



def relocate_runtime_config(resources):
    fonts = resources / "etc/fonts/fonts.conf"
    if fonts.is_file():
        checkout_cache = f"<cachedir>{PREFIX}/var/cache/fontconfig</cachedir>"
        fonts.write_text(fonts.read_text().replace(checkout_cache, ""))
    for name in ("pip", "pyside6-rcc"):
        script = resources / "bin" / name
        if not script.is_file() or script.is_symlink():
            continue
        source = script.read_text()
        interpreter = f'"{PREFIX}/bin/python3.13"'
        if interpreter in source:
            script.write_text(source.replace(interpreter, '"$(dirname "$0")/python"', 1))


def runtime_smoke(app, edition):
    resources = app / "Contents/Resources"
    plugins = resources / "lib/qt6/plugins"
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("DYLD_", "PYTHON", "QT_", "FREECAD_"))
    }
    env.update({
        "PYTHONHOME": str(resources),
        "PYTHONPATH": str(resources),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "QT_PLUGIN_PATH": str(plugins),
        "QT_QPA_PLATFORM_PLUGIN_PATH": str(plugins / "platforms"),
        "FONTCONFIG_FILE": str(resources / "etc/fonts/fonts.conf"),
        "FONTCONFIG_PATH": str(resources / "etc/fonts"),
        "SSL_CERT_FILE": str(resources / "ssl/cacert.pem"),
    })
    with tempfile.TemporaryDirectory(prefix=".bundle-smoke-", dir=app.parent) as scratch:
        profile = Path(scratch)
        env["XDG_CACHE_HOME"] = str(profile / "cache")
        for name in ("home", "data", "temp", "cache"):
            (profile / name).mkdir()
            env["FREECAD_USER_" + name.upper()] = str(profile / name)
        probe = """
import ctypes
import json
import os
import sys
from pathlib import Path
from PySide6.QtCore import qVersion
from PySide6.QtWidgets import QWidget

root = Path(sys.argv[1]).resolve()
required = [root / name for name in sys.argv[2:]]
ctypes.CDLL(str(required[0]))  # Cocoa
ctypes.CDLL(str(required[-1]))  # FreeCADGui and its App/Base dependencies

dyld = ctypes.CDLL(None)
count = dyld._dyld_image_count
count.restype = ctypes.c_uint32
image_name = dyld._dyld_get_image_name
image_name.argtypes = [ctypes.c_uint32]
image_name.restype = ctypes.c_char_p
images = {Path(os.fsdecode(image_name(i))).resolve() for i in range(count())}
for library in required:
    matching = {image for image in images if image.name == library.name}
    assert matching == {library.resolve()}, (library, matching)
print(json.dumps({"qt": qVersion(), "loaded": sys.argv[2:]}))
"""
        qt_probe = json.loads(subprocess.check_output(
            [resources / "bin/python", "-c", probe, resources, PLUGIN_PATH, WIDGETS_PATH, *CORE_LIBRARIES],
            env=env, text=True, stderr=subprocess.STDOUT, timeout=60,
        ).splitlines()[-1])
        if qt_probe["qt"] != "6.11.2":
            raise ValueError(f"Unexpected bundled Qt version: {qt_probe['qt']}")

        cmd = resources / "bin/FreeCADCmd"
        version = subprocess.check_output(
            [cmd, "--safe-mode", "--version"],
            env=env, text=True, stderr=subprocess.STDOUT, timeout=60,
        ).strip()
        identity = "PersonalCAD" if edition == "personal" else "FreeCAD"
        if identity not in version:
            raise ValueError(f"Unexpected executable identity: {version}")
        probe = (
            "import sys, Part; from pathlib import Path; from pivy import coin; "
            f"root = Path({str(resources)!r}).resolve(); "
            "assert Path(sys.prefix).resolve() == root; "
            "assert all(Path(module.__file__).resolve().is_relative_to(root) for module in (Part, coin)); "
            "print('BUNDLE_COIN_OK', coin.SoDB.getVersion())"
        )
        if edition == "personal":
            probe = "import sys; assert sys.dont_write_bytecode; " + probe
        coin = subprocess.check_output(
            [cmd, "--safe-mode", "--console", probe],
            input="", env=env, text=True, stderr=subprocess.STDOUT, timeout=60,
        )
        marker = next((line for line in coin.splitlines() if line.startswith("BUNDLE_COIN_OK ")), None)
        if marker is None:
            raise ValueError(f"Bundled Coin could not be loaded: {coin}")
    return {
        "qt_dlopen_version": qt_probe["qt"],
        "staged_native_images_loaded": qt_probe["loaded"],
        "cli_version": version,
        "coin_version": marker.split(maxsplit=1)[1],
    }


def prepare_contents(app, edition, paths, installed_prefix, app_files, qt_digests):
    personal = edition == "personal"
    contents = app / "Contents"
    resources = contents / "Resources"
    if not set(CORE_LIBRARIES).issubset(app_files):
        raise ValueError("The source install receipt does not include the FreeCAD App, Base and Gui libraries.")
    source_core = {
        str(path): hashlib.sha256((installed_prefix / path).read_bytes()).hexdigest()
        for path in CORE_LIBRARIES
    }
    if personal:
        for path in CORE_LIBRARIES:
            stock = PREFIX / path
            if stock.is_file() and hashlib.sha256(stock.read_bytes()).hexdigest() == source_core[str(path)]:
                raise ValueError(f"Candidate source installed an original FreeCAD library: {path}")
    source_images = []
    for path in paths:
        copy_file(PREFIX / path, resources / path)
    for path in app_files:
        root = contents if path.parts[0] in {"PlugIns", "Library"} else resources
        destination = root / path
        if destination.exists() or destination.is_symlink():
            raise ValueError(f"Source install overlaps a locked runtime file: {path}")
        copy_file(installed_prefix / path, destination)
        if is_macho(destination):
            source_images.append(destination)

    for icon in (REFERENCE / "Resources").glob("freecad*.icns"):
        copy_file(icon, resources / icon.name)
    info_path = contents / "Info.plist"
    original_info = REFERENCE / "Info.plist"
    with original_info.open("rb") as stream:
        info = plistlib.load(stream)
    launcher_name = info["CFBundleExecutable"]
    if personal:
        run("/usr/bin/python3", CANDIDATE / "package/bundle/osx/personalize_info_plist.py",
            original_info, info_path)
        info = plistlib.loads(info_path.read_bytes())
        version_header = (installed_prefix.parent / "src/Build/Version.h").read_text()
        version_values = dict(re.findall(r'^#define\s+(FC\w+)\s+"([^"]*)"', version_header, re.M))
        version = ".".join(version_values[f"FCVersion{part}"] for part in ("Major", "Minor", "Point"))
        info["CFBundleShortVersionString"] = version
        info["CFBundleVersion"] = version
        info["SvenCADSourceRevision"] = version_values["FCRepositoryHash"]
        if info["SvenCADSourceRevision"] != run("git", "-C", CANDIDATE, "rev-parse", "HEAD").strip():
            raise ValueError("The built source revision does not match the checkout.")
        info_path.write_bytes(plistlib.dumps(info))
        launcher = WORK / "personal-launcher/PersonalCAD"
    else:
        copy_file(original_info, info_path)
        launcher = REFERENCE / "MacOS" / launcher_name
    copy_file(launcher, contents / "MacOS" / launcher_name)

    original_plugin = resources / PLUGIN_PATH
    plugin_input = (
        PREFIX / PLUGIN_PATH if edition == "baseline-a"
        else QT_REFERENCE / COCOA_BINARY
    )
    plugin_digest = hashlib.sha256(plugin_input.read_bytes()).hexdigest()
    cocoa_digest, widgets_digest = qt_digests
    expected_digest = STOCK_SHA256 if edition == "baseline-a" else cocoa_digest
    if plugin_digest != expected_digest:
        raise ValueError(f"The approved Cocoa plugin digest does not match: {plugin_input}")
    if edition != "baseline-a":
        copy_file(plugin_input, original_plugin)
    widgets = resources / WIDGETS_PATH
    if personal:
        widgets_input = QT_REFERENCE / WIDGETS_BINARY
        if sha256(widgets_input) != widgets_digest:
            raise ValueError(f"The approved Qt Widgets digest does not match: {widgets_input}")
        copy_file(widgets_input, widgets)

    for entry in sorted(resources.rglob("*")):
        if not entry.is_symlink():
            continue
        if not entry.exists():
            entry.unlink()  # The official pack also drops dangling runtime links.
        elif not entry.resolve().is_relative_to(app):
            raise ValueError(f"Staged symlink refers outside the app: {entry}")
    for required in ("bin/FreeCAD", "bin/FreeCADCmd", "bin/python", str(PLUGIN_PATH), "ssl/cacert.pem"):
        if not (resources / required).is_file():
            raise FileNotFoundError(f"Staged runtime is missing {required}")

    evidence = relocate_native_files(app, installed_prefix)
    evidence["source_core_libraries_sha256"] = source_core
    for image in source_images:
        valid = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--strict", image],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
        if not valid:
            run("/usr/bin/codesign", "--force", "--sign", "-", image)
    relocate_runtime_config(resources)
    for graphviz_config in (REFERENCE / "Resources/lib/graphviz").glob("config*"):
        plugin_names = re.findall(r"^([^\s#]+\.dylib)\s", graphviz_config.read_text(), flags=re.M)
        if not plugin_names or not all((resources / "lib/graphviz" / name).is_file() for name in plugin_names):
            raise ValueError(f"Staged Graphviz libraries differ from the official runtime: {graphviz_config}")
        copy_file(graphviz_config, resources / "lib/graphviz" / graphviz_config.name)
    run("/usr/bin/codesign", "--verify", "--strict", original_plugin)
    run("/usr/bin/codesign", "--verify", "--strict", widgets)
    evidence["staged_widgets_sha256"] = hashlib.sha256(widgets.read_bytes()).hexdigest()
    if personal and evidence["staged_widgets_sha256"] != widgets_digest:
        raise ValueError("The approved Qt Widgets binary changed during staging.")
    evidence["staged_widgets_uuid"] = run("/usr/bin/dwarfdump", "--uuid", widgets).split()[1]
    if (contents / "PlugIns").is_dir():
        for extension in (contents / "PlugIns").glob("*.appex"):
            run("/usr/bin/codesign", "--force", "--sign", "-", extension)
    if not personal:
        evidence.update(runtime_smoke(app, edition))
        # The stock embedded interpreter writes caches in Resources during imports.
        for bytecode in resources.rglob("*.pyc"):
            bytecode.unlink()
    run("/usr/bin/codesign", "--force", "--sign", "-", app)
    if personal:
        evidence.update(runtime_smoke(app, edition))
        if any(resources.rglob("*.pyc")):
            raise ValueError("Embedded Python wrote bytecode into the signed app.")
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", app)
    evidence["staged_cocoa_sha256"] = hashlib.sha256(original_plugin.read_bytes()).hexdigest()
    if edition == "baseline-a" and evidence["staged_cocoa_sha256"] != STOCK_SHA256:
        raise ValueError("The stock Cocoa plugin changed during staging.")
    evidence["input_cocoa_sha256"] = plugin_digest
    evidence["staged_cocoa_uuid"] = run("/usr/bin/dwarfdump", "--uuid", original_plugin).split()[1]
    if evidence["staged_cocoa_uuid"] != run("/usr/bin/dwarfdump", "--uuid", plugin_input).split()[1]:
        raise ValueError("The Cocoa plugin UUID changed during staging.")
    evidence["staged_core_libraries_sha256"] = {
        str(path): hashlib.sha256((resources / path).read_bytes()).hexdigest()
        for path in CORE_LIBRARIES
    }
    evidence["executable_sha256"] = {
        name: hashlib.sha256((resources / "bin" / name).read_bytes()).hexdigest()
        for name in ("FreeCAD", "FreeCADCmd")
    }
    info = plistlib.loads(info_path.read_bytes())
    evidence["bundle_identifier"] = info["CFBundleIdentifier"]
    if personal:
        evidence["source_revision"] = info["SvenCADSourceRevision"]
        evidence["bundle_version"] = info["CFBundleShortVersionString"]
        evidence["bundle_display_name"] = info["CFBundleDisplayName"]
        evidence["bundle_code_resources_sha256"] = sha256(contents / "_CodeSignature/CodeResources")
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("edition", choices=("baseline-a", "baseline-b", "personal"))
    parser.add_argument("--plan", action="store_true", help="Show locked runtime selection without copying.")
    parser.add_argument("--output", type=Path, default=WORK / "artifacts")
    parser.add_argument("--qt-build-receipt", type=Path,
                        help="Use verified file hashes from the local patched Qt build.")
    args = parser.parse_args()

    personal = args.edition == "personal"
    paths, selected = runtime_files(personal)
    evidence = {
        "edition": args.edition,
        "runtime_files": len(paths),
        "runtime_package_owners": selected,
        "excluded_runtime_packages": sorted(OMITTED_PACKAGES) if personal else [],
    }
    if args.plan:
        print(json.dumps(evidence, indent=2))
        return

    if personal and run("git", "-C", CANDIDATE, "status", "--porcelain").strip():
        raise ValueError("Build and stage the personal app from a clean checkout.")
    installed, app_files, receipt_hash = install_receipt(personal)
    qt_digests = qt_output_digests(args.qt_build_receipt)
    evidence["source_install_files"] = len(app_files)
    evidence["source_install_receipt_sha256"] = receipt_hash
    dest = (args.output / args.edition / ("Sven CAD.app" if personal else "FreeCAD.app")).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise FileExistsError(f"Staging destination already exists: {dest}")
    with tempfile.TemporaryDirectory(prefix=".staging-", dir=dest.parent) as scratch:
        app = Path(scratch) / dest.name
        evidence.update(prepare_contents(app, args.edition, paths, installed, app_files, qt_digests))
        app.rename(dest)
    evidence["bundle_path"] = str(dest)
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        if error.stdout:
            print(error.stdout, file=sys.stderr)
        raise
