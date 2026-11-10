"""Run one external accessibility round against a synthetic FreeCAD fixture."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
from time import monotonic, sleep


def wait_for_ready(path, process):
    deadline = monotonic() + 30.0
    while monotonic() < deadline and process.poll() is None:
        if path.is_file():
            return json.loads(path.read_text())
        sleep(0.1)
    raise RuntimeError("FreeCAD did not publish a ready marker")


def run(args):
    fixture_runner = Path(__file__).with_name("run_fixtures.py")
    output = args.output.resolve()
    if output.exists():
        raise ValueError(f"Evidence directory already exists: {output}")

    runtime_seconds = (args.samples * args.interval_ms + 999) // 1000 + 30
    fixture = [
        sys.executable, str(fixture_runner),
        "--app", str(args.app.resolve()),
        "--output", str(output),
        "--scenario", args.scenario,
        "--duration", str(runtime_seconds),
        "--timeout", str(runtime_seconds + 30),
    ]
    if args.cocoa_plugin_path:
        fixture.extend(("--cocoa-plugin-path", str(args.cocoa_plugin_path.resolve())))
    if args.widgets_library_path:
        fixture.extend(("--widgets-library-path", str(args.widgets_library_path.resolve())))

    with subprocess.Popen(fixture) as app_runner:
        try:
            ready = wait_for_ready(output / "ready.json", app_runner)
            label = ready["expected_label"]
            if args.scenario != "ax-model-tree-root" and ready["internal_selection"] != [label]:
                raise RuntimeError("The expected object is not selected inside FreeCAD")
            if args.scenario != "ax-table" and ready["tree_selection"] != [label]:
                raise RuntimeError("The expected item is not selected in the FreeCAD tree")
            if args.cocoa_plugin_path:
                expected = (args.cocoa_plugin_path / "libqcocoa.dylib").resolve()
                if Path(ready["cocoa_plugin"]["path"]) != expected:
                    raise RuntimeError("The FreeCAD process loaded a different Cocoa plugin")
            if args.widgets_library_path:
                expected = (args.widgets_library_path / "libQt6Widgets.6.dylib").resolve()
                if Path(ready["qt_widgets"]["path"]) != expected:
                    raise RuntimeError("The FreeCAD process loaded a different Qt Widgets library")

            probe = [
                str(args.probe.resolve()), "--pid", str(ready["pid"]), "--expect", label,
                "--samples", str(args.samples), "--interval-ms", str(args.interval_ms),
                "--min-valid", str(args.min_valid if args.min_valid is not None else args.samples),
            ]
            if args.scenario == "ax-table":
                probe.extend((
                    "--role", "AXTable", "--identifier", "PersonalCadAxProbeTable",
                    "--window-title", "AX Probe Table",
                ))
            else:
                probe.extend(("--role", "AXOutline", "--identifier", "PersonalCadAxProbeModelTree"))
            with (output / "ax-probe.jsonl").open("wb") as stream:
                with subprocess.Popen(probe, stdout=stream) as reader:
                    # Allow the external reader to resolve the initial native rows
                    # before the fixture begins replacing adjacent rows.
                    sleep(1.0)
                    (output / "start-mutations").touch()
                    probe_exit = reader.wait()
        finally:
            if output.is_dir():
                (output / "stop").touch()
        fixture_exit = app_runner.wait()

    result = {"probe_exit": probe_exit, "fixture_exit": fixture_exit}
    (output / "ax-round.json").write_text(json.dumps(result, indent=2) + "\n")
    return 1 if probe_exit or fixture_exit else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cocoa-plugin-path", type=Path)
    parser.add_argument("--widgets-library-path", type=Path)
    parser.add_argument(
        "--scenario", choices=("ax-table", "ax-model-tree", "ax-model-tree-root"), default="ax-table"
    )
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--min-valid", type=int)
    parser.add_argument("--interval-ms", type=int, default=100)
    args = parser.parse_args()
    if args.samples <= 0 or args.interval_ms <= 0 or args.samples * args.interval_ms < 3000:
        parser.error("Samples must cover at least three seconds of GUI activity")
    if args.min_valid is not None and not 0 < args.min_valid <= args.samples:
        parser.error("--min-valid must be between 1 and --samples")
    if not args.probe.is_file():
        parser.error("--probe must name a built external reader")
    try:
        return run(args)
    except (OSError, RuntimeError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
