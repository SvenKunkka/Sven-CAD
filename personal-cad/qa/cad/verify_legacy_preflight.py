#!/usr/bin/env python3
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
from zipfile import ZipFile


def check(probe, path, expected_messages=()):
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = subprocess.run([probe, path], capture_output=True, text=True, timeout=15)
    print(f"{path.name}: exit={result.returncode}, SHA256={before}", flush=True)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert result.returncode == (2 if expected_messages else 0), result.stderr
    for message in expected_messages:
        assert message in result.stderr, result.stderr
    if expected_messages:
        print(result.stderr, flush=True)


def main():
    cad = Path(__file__).resolve().parent
    repo = cad.parents[2]
    parser = argparse.ArgumentParser(description="Run the native FCStd preflight on synthetic files.")
    parser.add_argument("--repo", type=Path, default=repo)
    parser.add_argument("--build", type=Path)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--baseline-lib", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    args = parser.parse_args()
    candidate = args.repo.resolve()
    build = args.build.resolve() if args.build else candidate / "build/personal-release"
    commands = json.loads((build / "compile_commands.json").read_text())
    entry = next(item for item in commands if item["file"].endswith("/src/App/Document.cpp"))
    argv = shlex.split(entry["command"])
    out = argv.index("-o")
    del argv[out:out + 2]
    argv.remove("-c")
    argv.remove(entry["file"])
    prefix = args.prefix.resolve()
    libraries = args.baseline_lib.resolve()
    fixtures = args.fixtures.resolve()

    with tempfile.TemporaryDirectory(prefix="legacy-preflight-") as temporary:
        work = Path(temporary)
        probe = work / "legacy-preflight"
        argv.extend([
            str(cad / "legacy_preflight.cpp"),
            str(candidate / "src/App/private/PersonalDocumentCompatibility.cpp"),
            "-o", str(probe), f"-L{libraries}", f"-L{prefix}/lib",
            "-lFreeCADBase", "-lxerces-c", "-Wl,-needed-lpython3.13",
            f"-Wl,-rpath,{libraries}", f"-Wl,-rpath,{prefix}/lib",
        ])
        subprocess.run(argv, cwd=entry["directory"], check=True)
        for name in ("keyboard-housing", "keyboard-assembly", "keyboard-step-roundtrip", "legacy-link-source"):
            check(probe, fixtures / (name + ".FCStd"))

        check(probe, fixtures / "legacy-mixed.FCStd", (
            "LegacyParameters", "Spreadsheet::Sheet", "ParametricLegacyBox", "has expressions",
            "LegacyMesh", "Mesh::Feature", "legacy-link-source.FCStd",
        ))

        synthetic = {
            "legacy-spreadsheet-view": (
                '<Objects><Object type="TechDraw::DrawViewSpreadsheetPython" name="OldView"/></Objects>',
                "OldView", "TechDraw::DrawViewSpreadsheetPython",
            ),
            "legacy-spreadsheet-bom": (
                '<ObjectData><Object name="LegacyBOM"><Properties><Property name="cells" '
                'type="Spreadsheet::PropertySheet"/></Properties></Object></ObjectData>',
                "LegacyBOM", "Spreadsheet::PropertySheet",
            ),
            "legacy-motion-proxy": (
                '<ObjectData><Object name="MovingParts"><Properties><Property name="Proxy">'
                '<Python module="CommandCreateSimulation" class="Simulation"/></Property>'
                '</Properties></Object></ObjectData>',
                "MovingParts", "CommandCreateSimulation",
            ),
        }
        for name, (markup, *messages) in synthetic.items():
            path = work / (name + ".FCStd")
            with ZipFile(path, "w") as archive:
                archive.writestr("Document.xml", '<Document SchemaVersion="4">' + markup + '</Document>')
            check(probe, path, messages)
    print("PASS")


if __name__ == "__main__":
    main()
