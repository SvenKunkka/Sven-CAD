"""Run release acceptance on one unchanged, signed macOS PersonalCAD bundle."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from time import monotonic


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--legacy-source", required=True, type=Path)
    parser.add_argument("--probe", required=True, type=Path)
    parser.add_argument("--soak-seconds", type=int, default=7200)
    args = parser.parse_args()
    if args.soak_seconds < 30:
        parser.error("A soak test needs at least 30 seconds")

    code = Path(__file__).resolve().parent
    app = args.app.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True)
    legacy = args.legacy_source.resolve()
    sample = legacy / "keyboard-housing.FCStd"
    signature = ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)]
    bundle = app / "Contents"
    resource_hash = hashlib.sha256((bundle / "_CodeSignature/CodeResources").read_bytes()).hexdigest()
    report = {"status": "running", "app": str(app), "signed_resource_sha256": resource_hash, "steps": []}
    evidence = output / "acceptance.json"

    def record():
        temporary = evidence.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        temporary.replace(evidence)

    def run(name, script, *parameters):
        command = [sys.executable, str(code / script), "--app", str(app),
                   "--output", str(output / name), *map(str, parameters)]
        start = monotonic()
        print(f"{name}: starting", flush=True)
        process = subprocess.run(command, check=False)
        report["steps"].append({"name": name, "exit": process.returncode, "seconds": round(monotonic() - start, 3)})
        record()
        process.check_returncode()

    record()
    try:
        subprocess.run(signature, check=True)
        run("native-profile", "verify_native_profile.py", "--sample", sample)
        run("cad", "run_fixtures.py", "--mode", "personal", "--legacy-source", legacy)
        run("layout", "layout_startup_qa.py")
        for scenario in ("viewport", "shutdown", "shutdown-direct-qt"):
            run(scenario, "run_fixtures.py", "--mode", "personal", "--scenario", scenario)
        for index in range(1, 4):
            run(f"ax-model-tree-{index}", "run_ax_round.py", "--probe", args.probe.resolve(),
                "--scenario", "ax-model-tree", "--samples", 100, "--interval-ms", 120)
        run("recovery", "recovery_qa.py", "--mode", "personal", "--fixture", sample)
        run("soak", "run_fixtures.py", "--mode", "personal", "--scenario", "soak",
            "--duration", args.soak_seconds, "--timeout", args.soak_seconds + 180)
        subprocess.run(signature, check=True)
        assert hashlib.sha256((bundle / "_CodeSignature/CodeResources").read_bytes()).hexdigest() == resource_hash
    except (subprocess.CalledProcessError, AssertionError, OSError) as error:
        report.update(status="failed", error=str(error))
        record()
        raise
    report["status"] = "passed"
    record()
    print(evidence)


if __name__ == "__main__":
    main()
