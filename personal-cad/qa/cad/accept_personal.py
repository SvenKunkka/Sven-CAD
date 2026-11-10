"""Run release acceptance on one unchanged, signed macOS Sven CAD bundle."""

import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import subprocess
import sys
from time import monotonic


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--legacy-source", required=True, type=Path)
    parser.add_argument("--probe", required=True, type=Path)
    parser.add_argument("--expected-display-name", default="Sven CAD")
    parser.add_argument("--expect-recovery-details", action="store_true")
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
    info = plistlib.loads((bundle / "Info.plist").read_bytes())
    identity_files = {
        "signed_resource_sha256": bundle / "_CodeSignature/CodeResources",
        "bundle_info_sha256": bundle / "Info.plist",
        "bundle_launcher_sha256": bundle / "MacOS" / info["CFBundleExecutable"],
    }

    def signed_identity():
        return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in identity_files.items()}

    identity = signed_identity()
    report = {"status": "running", "app": str(app), **identity, "steps": []}
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
        brand = ("--expected-display-name", args.expected_display_name)
        run("native-profile", "verify_native_profile.py", "--sample", sample, *brand)
        run("cad", "run_fixtures.py", "--mode", "personal", "--legacy-source", legacy, *brand)
        run("layout", "layout_startup_qa.py")
        for scenario in ("viewport", "shutdown", "shutdown-direct-qt"):
            run(scenario, "run_fixtures.py", "--mode", "personal", "--scenario", scenario)
        for index in range(1, 4):
            run(f"ax-model-tree-{index}", "run_ax_round.py", "--probe", args.probe.resolve(),
                "--scenario", "ax-model-tree", "--samples", 100, "--interval-ms", 120)
        for profile in ("fresh", "legacy-cad", "legacy-blender", "reselected-cad"):
            run(f"navigation-{profile}", "fusion_navigation_qa.py", "--profile", profile)
        run("workflow", "fusion_workflow_qa.py", "--require-no-main-thread-warnings")
        recovery_details = ("--expect-recovery-details",) if args.expect_recovery_details else ()
        run("recovery", "recovery_qa.py", "--mode", "personal", "--fixture", sample, *recovery_details)
        run("soak", "run_fixtures.py", "--mode", "personal", "--scenario", "soak",
            "--duration", args.soak_seconds, "--timeout", args.soak_seconds + 180)
        subprocess.run(signature, check=True)
        assert signed_identity() == identity, "Signed bundle changed during acceptance"
    except (subprocess.CalledProcessError, AssertionError, OSError) as error:
        report.update(status="failed", error=str(error))
        record()
        raise
    report["status"] = "passed"
    record()
    print(evidence)


if __name__ == "__main__":
    main()
