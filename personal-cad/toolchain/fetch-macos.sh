#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-2.1-or-later
set -euo pipefail
source "$(dirname "$0")/common.sh"
require_macos

mode="${1:-all}"
case "$mode" in
    all|reference|qt) ;;
    *) fail "Usage: $0 [reference|qt]" ;;
esac

mkdir -p "$downloads"
tmp="$(mktemp -d "$PERSONAL_CAD_WORK/.fetch.XXXXXX")"
mounted=false
cleanup() {
    if "$mounted"; then
        hdiutil detach "$tmp/mount" >&2 || return
    fi
    rm -rf "$tmp"
}
trap cleanup EXIT

download() {
    local name="$1" url="$2" hash="$3"
    if [[ ! -f "$downloads/$name" ]]; then
        curl --fail --location --retry 2 --output "$tmp/$name" "$url"
        check_sha256 "$tmp/$name" "$hash"
        mv "$tmp/$name" "$downloads/$name"
    fi
    check_sha256 "$downloads/$name" "$hash"
}

verify_reference() {
    codesign --verify --deep --strict "$1"
    local cdhash
    cdhash="$(codesign --display --verbose=4 "$1" 2>&1 | awk -F= '$1 == "CDHash" {print $2}')"
    [[ "$cdhash" == 5888110de93d4d11a5e6c35fea05d104d1f54cae ]] ||
        fail "The reference app does not match the pinned FreeCAD release: $1"
}

if [[ "$mode" != qt ]]; then
    if [[ ! -e "$PERSONAL_CAD_REFERENCE_APP" ]]; then
        download "$dmg" "https://github.com/FreeCAD/FreeCAD/releases/download/$release/$dmg" "$dmg_sha256"
        mkdir "$tmp/mount"
        hdiutil attach -readonly -nobrowse -mountpoint "$tmp/mount" "$downloads/$dmg" >&2
        mounted=true
        ditto "$tmp/mount/FreeCAD.app" "$tmp/FreeCAD.app"
        verify_reference "$tmp/FreeCAD.app"
        mkdir -p "$(dirname "$PERSONAL_CAD_REFERENCE_APP")"
        mv "$tmp/FreeCAD.app" "$PERSONAL_CAD_REFERENCE_APP"
    else
        verify_reference "$PERSONAL_CAD_REFERENCE_APP"
    fi
fi

if [[ "$mode" != reference ]]; then
    download "$qt_archive" "https://download.qt.io/official_releases/qt/6.11/$qt_version/submodules/$qt_archive" "$qt_sha256"
    download "$vulkan_archive" "https://conda.anaconda.org/conda-forge/osx-arm64/$vulkan_archive" "$vulkan_sha256"
fi
