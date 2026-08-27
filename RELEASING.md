# Releasing TapScribe

A release is a deliberate, human-pushed `vX.Y.Z` tag — never automatic
([ADR-0012](docs/adr/0012-bridge-artifacts-on-tagged-releases.md)). The tag
fires `.github/workflows/release.yml`, which builds and publishes everything.

## Cut a release

1. **Bump the version** on a clean tree:

   ```bash
   python tools/bump_version.py X.Y.Z
   ```

   Stamps `pyproject.toml`, `tapscribe/__init__.py`, and
   `bridges/spacialchat-bridge/manifest.json` in lock-step
   (`tests/test_version_consistency.py` guards drift).

2. **Open a PR with the bump and merge it.** Conventional-Commit title, e.g.
   `build(release): vX.Y.Z`.

3. **Tag the merged commit and push the tag:**

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

## What the release produces

- **`dist/*`** — Python wheel + sdist, attached to the GitHub Release.
- **`tapscribe-spacialchat-bridge.zip`** — the SpatialChat Chrome extension
  (unzip → `chrome://extensions` → Developer mode → Load unpacked).
- **`TapScribe.TrayBridge-win-x64.zip`** — the self-contained Windows tray exe.
- **`TapScribe.TrayBridge-osx-arm64.pkg`**: the macOS menu-bar app (Apple
  silicon, macOS 14.4+), and the one the dashboard card offers. An unsigned
  package, because `installer` writes payload without the download quarantine
  that makes our ad-hoc-signed bundle read as damaged:
  [bridges/tray-bridge/README.md](bridges/tray-bridge/README.md).
- **`TapScribe.TrayBridge-osx-arm64.zip`**: the same bundle, for anyone who
  wants it without an installer. Zipped with `ditto`, which a `.app`'s symlinks
  and executable bits require. First open needs the quarantine cleared by hand.
- **`TapScribe-Setup-win-x64.exe`** — the [Windows Bundle](packaging/README.md).
  Not a Bridge, so it is announced by the README and the Release page, not the
  dashboard's "Get a bridge" card.
- **PyPI** — `tapscribe` via Trusted Publishing (`pypi-publish` job). It
  depends only on `build`, so a failed Windows Bundle can't block the upload.
  The publisher is registered on pypi.org against **this workflow filename**
  (`release.yml`) and the **`pypi` environment** — renaming either breaks the
  OIDC exchange.
- **`ghcr.io/vortiago/tapscribe`** — Docker image tagged `:vX.Y.Z` and `:latest`.

Release notes are drafted from PR labels by release-drafter. Nothing carries a
Developer ID: Windows SmartScreen may warn, and the Mac bundle's ad-hoc
signature makes a quarantined `.app` read as damaged until the attribute is
cleared, which is why the card offers the `.pkg` (ADR-0012). The bridge zips
and the Bundle carry **stable, unversioned filenames** so
`https://github.com/Vortiago/TapScribe/releases/latest/download/<asset>` is a
permanent URL — the dashboard's Settings "Get a bridge" card links there.
