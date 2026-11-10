#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p .build
xcrun clang -arch arm64 -fobjc-arc -O2 -Wall -Wextra -Werror \
    -framework Foundation -framework ApplicationServices ax-probe.m -o .build/ax-probe
xcrun clang -arch arm64 -fobjc-arc -O2 -Wall -Wextra -Werror \
    -framework AppKit cocoa-fixture.m -o .build/cocoa-fixture
