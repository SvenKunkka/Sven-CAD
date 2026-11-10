#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
"""Check the contents of a local Sven CAD bundle without printing file data."""

import argparse
import hashlib
import json
import mmap
import re
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--private-model", type=Path, action="append", default=[])
    args = parser.parse_args()
    root = args.app.resolve(strict=True)
    model_names = [model.name.encode("utf-8") for model in args.private_model]
    model_digests = {hashlib.sha256(model.read_bytes()).hexdigest() for model in args.private_model}
    signing_receipt = root / "Contents/_CodeSignature/CodeResources"
    private_key = re.compile(rb"(?m)^-----BEGIN (?:OPENSSH |RSA |EC |DSA |ENCRYPTED )?PRIVATE KEY-----\r?$")
    build_markers = (str(Path.home()).encode("utf-8") + b"/", b"/Volumes/runner/", b"/home/conda/feedstock_root/")
    findings = {
        "build_path_text": [],
        "bundled_models": [],
        "private_model_matches": [],
        "private_key_markers": [],
        "bytecode_or_local_config": [],
        "invalid_symlinks": [],
    }
    count = 0
    for path in root.rglob("*"):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            target = path.resolve()
            if not target.is_relative_to(root) or not target.exists():
                findings["invalid_symlinks"].append(relative)
            continue
        if not path.is_file():
            continue
        count += 1
        is_cad = path.suffix.lower() in (".stp", ".step", ".fcstd", ".stepz")
        if is_cad:
            findings["bundled_models"].append(relative)
        if path.suffix == ".pyc" or path.name in ("user.cfg", "system.cfg", ".env"):
            findings["bytecode_or_local_config"].append(relative)
        if path.stat().st_size == 0:
            continue
        with path.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            if any(data.find(marker) >= 0 for marker in build_markers):
                findings["build_path_text"].append(relative)
            if private_key.search(data):
                findings["private_key_markers"].append(relative)
            if (
                any(name in path.name.encode("utf-8") or data.find(name) >= 0 for name in model_names)
                or (is_cad and model_digests and hashlib.sha256(data).hexdigest() in model_digests)
            ):
                findings["private_model_matches"].append(relative)

    blocking = ("private_model_matches", "private_key_markers", "bytecode_or_local_config", "invalid_symlinks")
    passed = not any(findings[name] for name in blocking)
    report = {
        "status": "passed" if passed else "failed",
        "files": count,
        "signed_resource_sha256": hashlib.sha256(signing_receipt.read_bytes()).hexdigest(),
        "counts": {name: len(paths) for name, paths in findings.items()},
        "findings": findings,
    }
    with args.output.open("x") as output:
        json.dump(report, output, indent=2)
        output.write("\n")
    print(report["status"], report["counts"])
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
