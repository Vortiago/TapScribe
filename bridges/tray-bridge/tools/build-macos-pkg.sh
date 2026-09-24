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
#
# The Bundle's package is this recipe too (packaging/macos/build-bundle-pkg.sh
# stages its payload into the .app and delegates here). Both get a postinstall
# that removes the app they supersede; only the bridge-only one refuses to
# install over a Bundle, and which one this is is read off the .app (ADR-0024).
set -euo pipefail

app=${1:?usage: build-macos-pkg.sh <path-to-.app> <version> <output.pkg>}
version=${2:?missing version}
out=${3:?missing output path}

# The app's own bundle id, so an install upgrades in place rather than leaving a
# second receipt behind.
identifier=net.havso.tapscribe.traybridge

test -d "$app"
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# A staging directory holding ONLY the bundle: --analyze infers components from
# everything under --root, and the SDK's output directory also carries loose
# build products. `ditto` rather than `cp`, for the symlinks and exec bits a
# .app is made of.
stage=$(mktemp -d)
component=$(mktemp -t component)
scripts=$(mktemp -d)
work=$(mktemp -d)
trap 'rm -rf "$stage" "$component" "$scripts" "$work"' EXIT
ditto "$app" "$stage/$(basename "$app")"

# The component plist exists for ONE flag. BundleIsRelocatable defaults to true,
# which puts a <relocate> entry in the package's PackageInfo, and that tells
# installer to search the volume for an existing copy of this bundle id and
# write the payload THERE, silently ignoring --install-location. An operator
# with a stray copy in ~/Downloads would have the update land on it while
# /Applications stayed on the old version.
#
# Whether the volume holds a registered copy is a race, so the symptom is
# intermittent. `pkgbuild --component` cannot turn the flag off, which is the
# only reason this goes the longer --root route.
pkgbuild --analyze --root "$stage" "$component" >/dev/null
plutil -replace 0.BundleIsRelocatable -bool NO "$component"

# Both packages carry the postinstall that removes the app this one supersedes;
# the script says why that is root's job and how narrow it keeps it.
cp "$here/macos-pkg-scripts/postinstall" "$scripts/postinstall"
chmod +x "$scripts/postinstall"

component_pkg="$work/TapScribe-component.pkg"
pkgbuild \
  --root "$stage" \
  --component-plist "$component" \
  --install-location /Applications \
  --identifier "$identifier" \
  --version "$version" \
  --scripts "$scripts" \
  "$component_pkg" >/dev/null

# The flag is the point of the file, so its absence from the OUTPUT is what gets
# checked, not its presence in the input we just wrote. An empty <relocate/> is
# the fixed shape; <relocate><bundle …/></relocate> is the broken one.
#
# The expand is its own statement rather than the left half of an `&&`, so an
# expand that could not RUN fails the build instead of skipping the check: joined,
# a broken pkgutil and a clean package look identical and the script still says
# "not relocatable". The same distinction ci.yml's sibling step spells out.
pkgutil --expand "$component_pkg" "$work/expanded"
if grep -q '<relocate>' "$work/expanded/PackageInfo"; then
  echo "$out declares a <relocate> bundle: installer may ignore /Applications" >&2
  exit 1
fi
# Same rule for the postinstall: checked in what was built, not in what was handed
# to pkgbuild. An expanded component keeps its Scripts as a cpio archive.
if ! tar -tzf "$work/expanded/Scripts" | grep -q 'postinstall$'; then
  echo "$out carries no postinstall: an upgrade would leave the old app behind" >&2
  exit 1
fi

# Which package this is, read off what it installs: the Bundle's payload sits in
# Contents/Resources (BundleLayout.MacOSPayload), and the tray's own role probe
# is python/ OR wheel/, so this is too. The Bundle may install over anything —
# over a bridge-only install that is the upgrade — so it ships as built.
resources="$stage/$(basename "$app")/Contents/Resources"
if [ -d "$resources/python" ] || [ -d "$resources/wheel" ]; then
  cp "$component_pkg" "$out"
  echo "built $out ($identifier $version, Bundle, not relocatable)"
  exit 0
fi

# The bridge-only package must never install over a Bundle. Both install
# /Applications/TapScribe.app under one identifier, so installing this one there
# replaces the app and deletes the Bundle's interpreter and wheel: the Recorder
# just disappears from the menu. The dashboard offers this package on every
# install, a Bundle's included, so the refusal lives in the package itself, as a
# Distribution installation-check: JavaScript `installer` runs before it writes
# anything, without root and without a script in the payload. The Distribution
# is SYNTHESIZED and then extended, so the pkg-ref and choice wiring stays
# productbuild's own.
dist="$work/Distribution"
productbuild --synthesize --package "$component_pkg" "$dist" >/dev/null
grep -q '</installer-gui-script>' "$dist"
sed -i '' '/<\/installer-gui-script>/d' "$dist"
cat >> "$dist" <<'XML'
    <installation-check script="refuseToReplaceABundle()"/>
    <script><![CDATA[
function refuseToReplaceABundle() {
    var resources = '/Applications/TapScribe.app/Contents/Resources/';
    if (system.files.fileExistsAtPath(resources + 'python')
            || system.files.fileExistsAtPath(resources + 'wheel')) {
        my.result.type = 'Fatal';
        my.result.title = 'TapScribe is already installed';
        my.result.message = 'This Mac has TapScribe with its Recorder, which already '
            + 'includes the bridge. This bridge-only package would replace it and '
            + 'remove the Recorder.';
        return false;
    }
    return true;
}
]]></script>
</installer-gui-script>
XML
productbuild --distribution "$dist" --package-path "$work" "$out" >/dev/null

pkgutil --expand "$out" "$work/product"
if ! grep -q 'refuseToReplaceABundle' "$work/product/Distribution"; then
  echo "$out lost its installation-check: it would install over a Bundle" >&2
  exit 1
fi

echo "built $out ($identifier $version, bridge-only, not relocatable, refuses a Bundle)"
