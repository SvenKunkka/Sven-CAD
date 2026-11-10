#!/usr/bin/env python3
"""Exercise the AX probe against a synthetic AppKit table and CLI failure paths."""

import contextlib
import json
from pathlib import Path
import selectors
import subprocess
import sys
import time


BUILD = Path(__file__).resolve().parent / ".build"
PROBE = BUILD / "ax-probe"
FIXTURE = BUILD / "cocoa-fixture"
SENTINEL = "AX Probe Sentinel"
WINDOW = "Personal CAD AX QA fixture"


def read_line(stream):
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        if not selector.select(timeout=5):
            raise TimeoutError("Timed out waiting for AX test output")
    line = stream.readline()
    if not line:
        raise RuntimeError("AX test output closed before the expected event")
    return line


def assert_result(code, records, expected_code, status, matched):
    summary = records[-1]
    assert code == expected_code, (code, records)
    assert summary["event"] == "summary" and summary["status"] == status, records
    assert summary["pass"] is (code == 0), records
    if matched is not None:
        assert summary["matched"] == matched, records
    print(f"PASS {status}: exit={code}, matched={matched}")


def run_probe(*args):
    result = subprocess.run([PROBE, *args], capture_output=True, check=False, timeout=15)
    return result.returncode, [json.loads(line) for line in result.stdout.splitlines()]


def options(pid, *extra, label=SENTINEL, identifier="AXProbeFixture"):
    return [
        "--pid", str(pid), "--expect", label, "--role", "AXTable",
        "--identifier", identifier, "--window-title", WINDOW, *extra,
    ]


def stop(process):
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=5)


@contextlib.contextmanager
def launch_fixture(*args):
    # An accessory window leaves the active app in place and exits at test completion.
    with subprocess.Popen([FIXTURE, *args], stdout=subprocess.PIPE, bufsize=0) as fixture:
        try:
            assert read_line(fixture.stdout).strip() == b"ready"
            yield fixture
        finally:
            stop(fixture)


def wait_for_ax_registration(pid):
    deadline = time.monotonic() + 5
    while True:
        result = run_probe(*options(pid, "--samples", "1"))
        if result[1][-1]["status"] not in {"no_windows", "query_error"} or time.monotonic() >= deadline:
            return result
        time.sleep(0.1)


def main():
    assert_result(*run_probe("--pid", "-1"), 64, "invalid_arguments", None)
    assert_result(
        *run_probe(*options(2147483647, "--samples", "1", "--min-valid", "2")),
        64, "invalid_arguments", None,
    )
    assert_result(*run_probe(*options(2147483647, "--samples", "1")), 69, "target_exited", 0)
    with launch_fixture() as fixture:
        registration = wait_for_ax_registration(fixture.pid)
        if registration[0] == 77:
            assert_result(*registration, 77, "permission_denied", 0)
            print("SKIP live AX checks: this process has no pre-existing Accessibility permission")
            return 77
        assert registration[0] == 0, registration
        code, records = run_probe("--pid", str(fixture.pid), "--window-title", WINDOW, "--discover")
        assert code == 0 and records[-1]["status"] == "discovered", (code, records)
        assert any(view["identifier"] == "AXProbeFixture" and view["role"] == "AXTable"
                   for view in records[-1]["views"]), records
        print("PASS discovery: selected window exposes the expected AXTable identifier")
        result = run_probe(*options(fixture.pid, "--samples", "3"))
        assert_result(*result, 0, "matched", 3)
        samples = [row for row in result[1] if row["event"] == "sample"]
        assert all(row["matched_label"] == SENTINEL and row["warmed_cells"] == 1 for row in samples)
        assert_result(*run_probe(*options(fixture.pid, "--samples", "1", label="wrong")), 1, "mismatch", 0)
        assert_result(
            *run_probe(*options(fixture.pid, "--samples", "1", identifier="MissingFixture")),
            1, "unsupported", 0,
        )

        args = options(fixture.pid, "--samples", "6", "--min-valid", "1", "--interval-ms", "400")
        with subprocess.Popen([PROBE, *args], stdout=subprocess.PIPE, bufsize=0) as probe:
            try:
                records = [json.loads(read_line(probe.stdout)), json.loads(read_line(probe.stdout))]
                assert records[-1]["status"] == "matched", records
                stop(fixture)
                output, _ = probe.communicate(timeout=5)
                records.extend(json.loads(line) for line in output.splitlines())
                assert_result(probe.returncode, records, 69, "target_exited", 1)
            finally:
                stop(probe)
    with launch_fixture("--empty") as fixture:
        assert wait_for_ax_registration(fixture.pid)[1][-1]["status"] == "empty"
        assert_result(*run_probe(*options(fixture.pid, "--samples", "2")), 1, "empty", 0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
