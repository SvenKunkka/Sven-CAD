# SPDX-License-Identifier: LGPL-2.1-or-later
# Shared paths for the Apple Silicon build scripts.

tools="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
export PERSONAL_CAD_SOURCE="$(cd "${PERSONAL_CAD_SOURCE:-$tools/../..}" && pwd -P)"
work_dir="${PERSONAL_CAD_WORK:-$PERSONAL_CAD_SOURCE/.personal-cad-build}"
mkdir -p "$work_dir"
export PERSONAL_CAD_WORK="$(cd "$work_dir" && pwd -P)"
export PERSONAL_CAD_PREFIX="${PERSONAL_CAD_PREFIX:-$PERSONAL_CAD_SOURCE/.pixi/envs/default}"
export PERSONAL_CAD_REFERENCE_APP="${PERSONAL_CAD_REFERENCE_APP:-$PERSONAL_CAD_WORK/reference/FreeCAD.app}"
export CMAKE_BUILD_PARALLEL_LEVEL="${CMAKE_BUILD_PARALLEL_LEVEL:-6}"

downloads="$PERSONAL_CAD_WORK/downloads"
qt_work="$PERSONAL_CAD_WORK/qt"
cmake="$PERSONAL_CAD_PREFIX/bin/cmake"
qt_version=6.11.2
qt_archive="qtbase-everywhere-src-$qt_version.tar.xz"
qt_sha256=5b2e00eccaf5a4d8c14134ffa0ea8dfd0a35ae1ffc7f8d87fa4305a1ed23cf22
vulkan_archive=libvulkan-headers-1.4.357.0-h3feff0a_0.conda
vulkan_sha256=6f9cddb173e772819a5d3589478d94a823436c2b838414bf3a03d10e79ace57c
release=26.3rc2
upstream_commit=9fb16e7f981adb09f7d7f130ce085d68e9825358
dmg="FreeCAD_$release-macOS15-arm64.dmg"
dmg_sha256=8fbac0755f809280226ac63d918205e48df424d31f458ede2f2ca37265640389

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

require_macos() {
    [[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]] ||
        fail "Run this build on macOS with a native arm64 shell."
    [[ "$(sw_vers -productVersion | cut -d. -f1)" -ge 15 ]] ||
        fail "This profile requires macOS 15 or later."
}

check_sha256() {
    [[ "$(shasum -a 256 "$1" | awk '{print $1}')" == "$2" ]] ||
        fail "SHA-256 mismatch: $1"
}
