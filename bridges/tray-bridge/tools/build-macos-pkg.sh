#!/bin/bash
# Build the macOS tray Bridge's installer package.
#
#   build-macos-pkg.sh <path-to-.app> <version> <output.pkg>
#
# One owner for the recipe, because two callers need it and they must agree:
# release.yml BUILDS the shipped package with it, and ci.yml's "Prove
# installer-written payload is not quarantined" step ASSERTS against it. Two
# copies of the flags would let the gate drift into testing something nobody
# downloads, and the flags are not obvious enough to keep in sync by eye.
#
# Why a package at all rather than only the zip: the bundle is ad-hoc signed
# (the macOS SDK signs it, and arm64 will not execute one with no signature),
# and Gatekeeper reads a signature it cannot validate as tampering, so a
# QUARANTINED ad-hoc bundle is reported as damaged with only Move to Trash.
# `installer` writes payload outside the path that applies quarantine, so the
# app a package drops in /Applications opens with no Terminal step. ADR-0012
# carries the decision.
set -euo pipefail

app=${1:?usage: build-macos-pkg.sh <path-to-.app> <version> <output.pkg>}
version=${2:?missing version}
out=${3:?missing output path}

# The app's own bundle id, so an install upgrades in place rather than leaving a
# second receipt behind.
identifier=net.havso.tapscribe.traybridge

test -d "$app"

# A staging directory holding ONLY the bundle: --analyze infers components from
# everything under --root, and the SDK's output directory also carries loose
# build products. `ditto` rather than `cp`, for the symlinks and exec bits a
# .app is made of.
stage=$(mktemp -d)
component=$(mktemp -t component)
trap 'rm -rf "$stage" "$component"' EXIT
ditto "$app" "$stage/$(basename "$app")"

# The component plist exists for ONE flag. BundleIsRelocatable defaults to true,
# which puts a <relocate> entry in the package's PackageInfo, and that tells
# installer to search the volume for an existing copy of this bundle id and
# write the payload THERE, silently ignoring --install-location. An operator
# with a stray copy in ~/Downloads would have the update land on it while
# /Applications stayed on the old version.
#
# It is not a hypothetical: it broke CI's own check, and only intermittently,
# because whether the freshly built bundle in the workspace has been registered
# yet is a race. `pkgbuild --component` cannot turn it off, which is the only
# reason this goes the longer --root route.
pkgbuild --analyze --root "$stage" "$component" >/dev/null
plutil -replace 0.BundleIsRelocatable -bool NO "$component"

pkgbuild \
  --root "$stage" \
  --component-plist "$component" \
  --install-location /Applications \
  --identifier "$identifier" \
  --version "$version" \
  "$out" >/dev/null

# The flag is the point of the file, so its absence from the OUTPUT is what gets
# checked, not its presence in the input we just wrote. An empty <relocate/> is
# the fixed shape; <relocate><bundle …/></relocate> is the broken one.
if pkgutil --expand "$out" "$stage/expanded" 2>/dev/null &&
  grep -q '<relocate>' "$stage/expanded/PackageInfo"; then
  echo "$out declares a <relocate> bundle: installer may ignore /Applications" >&2
  exit 1
fi

echo "built $out ($identifier $version, not relocatable)"
