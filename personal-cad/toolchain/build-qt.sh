#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-2.1-or-later
set -euo pipefail
source "$(dirname "$0")/common.sh"
require_macos
mode="${1:-build}"
case "$mode" in
    build|configure|test) ;;
    *) fail "Usage: $0 [configure|test]" ;;
esac
[[ -x "$cmake" ]] || fail "Install the repository's locked Pixi environment first."
bash "$tools/fetch-macos.sh" qt

mkdir -p "$qt_work"
patches=(
    QTBUG-149612.patch
    QT-AX-nested-tree-selection.patch
    QT-AX-tree-selection-after-mutations.patch
    QT-AX-retain-tree-selection-on-layout.patch
)
tmp="$(mktemp -d "$qt_work/.sources.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT
printf '%s\n' "$qt_sha256" > "$tmp/source-sha256"
for file in "${patches[@]}"; do
    (cd "$tools/qt-cocoa" && shasum -a 256 "$file") >> "$tmp/source-sha256"
done
qt_source="$qt_work/qtbase-source"
if [[ ! -e "$qt_source" ]]; then
    tar -xf "$downloads/$qt_archive" -C "$tmp"
    source_tree="$tmp/qtbase-everywhere-src-$qt_version"
    for file in "${patches[@]}"; do
        (cd "$source_tree" && patch -f -F 0 -p1 -i "$tools/qt-cocoa/$file")
    done
    mv "$tmp/source-sha256" "$source_tree/.personal-cad-source-sha256"
    mv "$source_tree" "$qt_source"
else
    cmp -s "$tmp/source-sha256" "$qt_source/.personal-cad-source-sha256" ||
        fail "Qt source inputs changed. Use a fresh PERSONAL_CAD_WORK directory."
fi

headers="$qt_work/vulkan-headers"
if [[ ! -e "$headers" ]]; then
    mkdir "$tmp/vulkan-headers"
    unzip -p "$downloads/$vulkan_archive" 'pkg-*.tar.zst' |
        "$PERSONAL_CAD_PREFIX/bin/zstd" -dc |
        tar -xf - -C "$tmp/vulkan-headers"
    mv "$tmp/vulkan-headers" "$headers"
fi
[[ -f "$headers/include/vulkan/vulkan.h" ]] || fail "Missing pinned Vulkan headers."

args=(
    -G Ninja -DCMAKE_MAKE_PROGRAM="$PERSONAL_CAD_PREFIX/bin/ninja"
    -DCMAKE_C_COMPILER=/usr/bin/cc -DCMAKE_CXX_COMPILER=/usr/bin/c++
    -DCMAKE_OBJCXX_COMPILER=/usr/bin/c++
    -DCMAKE_OSX_ARCHITECTURES=arm64 -DCMAKE_OSX_DEPLOYMENT_TARGET=11.0
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="$PERSONAL_CAD_PREFIX"
    -DCMAKE_IGNORE_PREFIX_PATH="/opt/homebrew;/usr/local/homebrew"
    -DVulkan_INCLUDE_DIR="$headers/include"
    -DVulkan_LIBRARY="$PERSONAL_CAD_PREFIX/lib/libvulkan.dylib"
    -DQTBASE_SOURCE="$qt_source"
)
plugin="$qt_work/plugin-B-reference"
widgets="$qt_work/widgets-C3-build"
"$cmake" -S "$tools/qt-cocoa/plugin-project" -B "$plugin" "${args[@]}"
"$cmake" -S "$tools/qt-cocoa/widgets-C-project" -B "$widgets" "${args[@]}" \
    -DCMAKE_UNITY_BUILD=ON -DCMAKE_UNITY_BUILD_BATCH_SIZE=8
[[ "$mode" != configure ]] || exit 0
"$cmake" --build "$plugin" --target QCocoaIntegrationPlugin
"$cmake" --build "$widgets" --target Widgets

# The receipt binds the local outputs to this tree. It does not claim byte-for-byte
# equality across SDK or compiler revisions.
/usr/bin/python3 - "$tools/stage-macos.py" "$qt_work" <<'PY'
import json
import runpy
import sys
from pathlib import Path

stage = runpy.run_path(sys.argv[1])
root = Path(sys.argv[2])
outputs = (stage["COCOA_BINARY"], stage["WIDGETS_BINARY"])
record = {
    "source": stage["qt_source_identity"](),
    "outputs": {str(path): stage["sha256"](root / path) for path in outputs},
}
temporary = root / "build-receipt.json.tmp"
temporary.write_text(json.dumps(record, indent=2) + "\n")
temporary.replace(root / "build-receipt.json")
PY

if [[ "$mode" == test ]]; then
    tests="$qt_work/ax-regression"
    "$cmake" -S "$tools/qt-cocoa/test-project" -B "$tests" "${args[@]}" \
        -DQTBASE_TEST_SOURCE="$qt_source"
    "$cmake" --build "$tests" --target tst_qaccessibilitymac
fi
