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
- **`TapScribe.TrayBridge-osx-arm64.zip`** — the macOS menu-bar app bundle
  (Apple silicon, macOS 14.4+). Zipped with `ditto`, which a `.app`'s symlinks
  and executable bits require. First open needs the download quarantine
  cleared: [bridges/tray-bridge/README.md](bridges/tray-bridge/README.md).
- **`TapScribe-Setup-win-x64.exe`** — the [Windows Bundle](packaging/README.md).
  Not a Bridge, so it is announced by the README and the Release page, not the
  dashboard's "Get a bridge" card.
- **PyPI** — `tapscribe` via Trusted Publishing (`pypi-publish` job). It
  depends only on `build`, so a failed Windows Bundle can't block the upload.
  The publisher is registered on pypi.org against **this workflow filename**
  (`release.yml`) and the **`pypi` environment** — renaming either breaks the
  OIDC exchange.
- **`ghcr.io/vortiago/tapscribe`** — Docker image tagged `:vX.Y.Z` and `:latest`.

Release notes are drafted from PR labels by release-drafter. Every artifact is
unsigned: Windows SmartScreen may warn, and macOS refuses a quarantined `.app`
until the attribute is cleared. The bridge zips and the Bundle carry
**stable, unversioned filenames** so
`https://github.com/Vortiago/TapScribe/releases/latest/download/<asset>` is a
permanent URL — the dashboard's Settings "Get a bridge" card links there.
