#!/bin/bash
# Build the macOS BUNDLE's installer package (ADR-0024).
#
#   build-bundle-pkg.sh <path-to-.app> <python-dir> <wheel> <version> <output.pkg>
#
# The Bundle is the same tray as the bridge-only artifact — one tray per OS
# (ADR-0022) — with a read-only payload laid inside it. So this stages the
# payload and then DELEGATES to bridges/tray-bridge/tools/build-macos-pkg.sh
# rather than restating the packaging recipe: the --analyze →
# BundleIsRelocatable NO → --component-plist sequence and the <relocate>
# self-check have one owner, and this is not it. Two copies of those flags is
# exactly the drift that owner exists to prevent, and it is why ci.yml's
# quarantine proof can keep exercising the bridge-only package and still be
# proving the recipe this one ships.
#
# The payload goes in Contents/Resources/ because that is where
# BundleLayout.MacOSPayload looks. Three things have to agree on that path —
# the layout, the tray's role probe, and this script — and the assertions
# below are this script's half of the agreement.
#
# The .app is COPIED first and never mutated: the same bundle is published as
# the bridge-only zip, and that one must not grow 300 MB of interpreter.
set -euo pipefail

app=${1:?usage: build-bundle-pkg.sh <path-to-.app> <python-dir> <wheel> <version> <output.pkg>}
python_dir=${2:?missing python directory}
wheel=${3:?missing wheel}
version=${4:?missing version}
out=${5:?missing output path}

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo=$(cd "$here/../.." && pwd)

test -d "$app"
test -d "$python_dir"
test -f "$wheel"

staged=$(mktemp -d)
trap 'rm -rf "$staged"' EXIT
bundle="$staged/$(basename "$app")"

# ditto rather than cp -R, for the symlinks and exec bits a .app and a
# python-build-standalone tree are both made of.
ditto "$app" "$bundle"

resources="$bundle/Contents/Resources"
mkdir -p "$resources"
ditto "$python_dir" "$resources/python"
mkdir -p "$resources/wheel"
ditto "$wheel" "$resources/wheel/$(basename "$wheel")"

# Asserted rather than assumed, the way release.yml already asserts the Windows
# Bundle's two interpreters. A payload that landed one directory off reads at
# runtime as "no host payload" — a Bundle that silently installs as a
# bridge-only tray, with the operator's Recorder simply absent from the menu.
test -x "$resources/python/bin/python3"
# Exactly one wheel: BundleLayout.ResolveWheel THROWS on two, and it is right to
# — a Bundle whose installer version diverges from the wheel it installs is the
# drift ADR-0015's one-wheel rule exists to prevent.
test "$(find "$resources/wheel" -maxdepth 1 -name '*.whl' | wc -l | tr -d ' ')" = 1

# Signing LAST, over the payload: the SDK ad-hoc signed the .app at build time,
# and writing into a signed bundle invalidates that signature — which is the
# very failure ADR-0024 exists to keep off the operator's machine, so it must
# not be shipped into one either. Re-signed here, the seal covers what we added.
codesign --force --deep --sign - "$bundle"
codesign -dv "$bundle" 2>&1 | grep -q 'Signature=adhoc'

"$repo/bridges/tray-bridge/tools/build-macos-pkg.sh" "$bundle" "$version" "$out"
