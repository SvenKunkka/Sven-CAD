#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-2.1-or-later
set -euo pipefail
source "$(dirname "$0")/common.sh"
require_macos

edition="${1:-personal}"
case "$edition" in
    personal|baseline|configure) ;;
    *) fail "Usage: $0 [personal|baseline|configure]" ;;
esac
[[ "$PERSONAL_CAD_PREFIX" == "$PERSONAL_CAD_SOURCE/.pixi/envs/default" ]] ||
    fail "The full build uses its source checkout's locked Pixi prefix."

pixi="${PERSONAL_CAD_PIXI:-pixi}"
command -v "$pixi" >/dev/null || fail "Install Pixi or set PERSONAL_CAD_PIXI."
[[ -f "$PERSONAL_CAD_SOURCE/pixi.lock" ]] || fail "The FreeCAD Pixi lockfile is missing."
sdk="$(xcrun --sdk macosx --show-sdk-path)"

export PIXI_CACHE_DIR="${PIXI_CACHE_DIR:-$PERSONAL_CAD_WORK/cache}"
export RATTLER_CACHE_DIR="${RATTLER_CACHE_DIR:-$PIXI_CACHE_DIR}"
export CCACHE_DIR="${CCACHE_DIR:-$PERSONAL_CAD_WORK/ccache}"
export PYTHONDONTWRITEBYTECODE=1

manifest="$PERSONAL_CAD_SOURCE/pixi.toml"
pixi_args=(--manifest-path "$manifest" --locked)
"$pixi" install "${pixi_args[@]}"

qt_opengl="$PERSONAL_CAD_PREFIX/lib/cmake/Qt6/FindWrapOpenGL.cmake"
if [[ -f "$qt_opengl" ]] && grep -q 'find_library(WrapOpenGL_AGL' "$qt_opengl"; then
    # Same AGL workaround as the upstream macOS bundle, limited to this Pixi environment.
    sed -i.bak -e '/find_library(WrapOpenGL_AGL/,/target_link_libraries.*__opengl_agl_fw_path/d' "$qt_opengl"
fi

source_dir="$PERSONAL_CAD_SOURCE"
preset=personal-macos-release
build_dir="$PERSONAL_CAD_WORK/personal-release"
if [[ "$edition" == baseline ]]; then
    source_dir="$PERSONAL_CAD_WORK/upstream"
    preset=conda-macos-release
    build_dir="$PERSONAL_CAD_WORK/baseline-a"
    if [[ ! -e "$source_dir" ]]; then
        git clone --branch "$release" --depth 1 https://github.com/FreeCAD/FreeCAD.git "$source_dir"
    fi
    [[ "$(git -C "$source_dir" rev-parse HEAD)" == "$upstream_commit" ]] &&
        git -C "$source_dir" diff --quiet "$upstream_commit" -- ||
        fail "The baseline requires the clean upstream commit $upstream_commit."
fi
git -C "$source_dir" submodule update --init --recursive
submodules="$(git -C "$source_dir" submodule status --recursive)"
if grep -Eq '^[+-U]' <<< "$submodules"; then
    fail "A submodule is missing or differs from its recorded commit."
fi

export PERSONAL_CAD_CONFIG_PRESET="$preset" PERSONAL_CAD_CONFIG_SOURCE="$source_dir"
export PERSONAL_CAD_CONFIG_BUILD="$build_dir" PERSONAL_CAD_CONFIG_SDK="$sdk"
"$pixi" run "${pixi_args[@]}" --no-install -x /bin/bash -c '
    set -euo pipefail
    export CXXFLAGS="${CXXFLAGS:-} -D_LIBCPP_DISABLE_AVAILABILITY"
    cmake --preset "$PERSONAL_CAD_CONFIG_PRESET" -S "$PERSONAL_CAD_CONFIG_SOURCE" -B "$PERSONAL_CAD_CONFIG_BUILD" \
        -DCMAKE_OSX_ARCHITECTURES=arm64 -DCMAKE_OSX_DEPLOYMENT_TARGET=15.0 \
        -DCMAKE_OSX_SYSROOT="$PERSONAL_CAD_CONFIG_SDK" -DCONDA_BUILD_SYSROOT="$PERSONAL_CAD_CONFIG_SDK" \
        -DFREECAD_3DCONNEXION_SUPPORT=None -DBUILD_DYNAMIC_LINK_PYTHON=OFF \
        -DFREECAD_QUICKLOOK_MODERN_SUPPORT=OFF -DFREECAD_QUICKLOOK_LEGACY_SUPPORT=OFF \
        -DCMAKE_INCLUDE_PATH="$CONDA_PREFIX/include" -DCMAKE_LIBRARY_PATH="$CONDA_PREFIX/lib" \
        -DCMAKE_INSTALL_PREFIX="$PERSONAL_CAD_CONFIG_BUILD/stage" -DCMAKE_INSTALL_LIBDIR=lib \
        -DRELATIVE_RPATH=ON -DINSTALL_TO_SITEPACKAGES=OFF \
        -DPython_EXECUTABLE="$CONDA_PREFIX/bin/python" \
        -DPython3_EXECUTABLE="$CONDA_PREFIX/bin/python"
'
[[ "$edition" != configure ]] || exit 0

"$pixi" run "${pixi_args[@]}" --no-install cmake --build "$build_dir"
"$pixi" run "${pixi_args[@]}" --no-install cmake --install "$build_dir"
bash "$tools/fetch-macos.sh" reference
bash "$tools/build-qt.sh"

receipt="$qt_work/build-receipt.json"
if [[ "$edition" == baseline ]]; then
    /usr/bin/python3 "$tools/stage-macos.py" baseline-a
    /usr/bin/python3 "$tools/stage-macos.py" baseline-b --qt-build-receipt "$receipt"
else
    launcher="$PERSONAL_CAD_WORK/personal-launcher"
    "$cmake" -G Ninja -S "$source_dir/package/bundle/osx/launcher" -B "$launcher" \
        -DCMAKE_MAKE_PROGRAM="$PERSONAL_CAD_PREFIX/bin/ninja" \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_OSX_ARCHITECTURES=arm64 \
        -DCMAKE_OSX_DEPLOYMENT_TARGET=15.0 -DCMAKE_OSX_SYSROOT="$sdk"
    "$cmake" --build "$launcher" --target PersonalCAD
    /usr/bin/python3 "$tools/stage-macos.py" personal --qt-build-receipt "$receipt"
fi
