# Releasing TapScribe

A release is a deliberate, human-pushed `vX.Y.Z` tag — never automatic (see
[ADR-0012](docs/adr/0012-bridge-artifacts-on-tagged-releases.md)). Pushing the
tag fires `.github/workflows/release.yml`, which builds and publishes every
artifact.

## Cut a release

1. **Bump the version.** On a clean tree, run:

   ```bash
   python tools/bump_version.py X.Y.Z
   ```

   This stamps the version in lockstep across `pyproject.toml`
   (`[project].version`), `tapscribe/__init__.py` (`__version__`), and
   `bridges/spacialchat-bridge/manifest.json` (`version`). A consistency test
   guards against drift.

2. **Open a PR with that bump and merge it.** Use a valid Conventional-Commit
   title the PR-title gate accepts, e.g.:

   ```
   build(release): vX.Y.Z
   ```

3. **Tag the merged commit and push the tag:**

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

   A human-pushed tag triggers the `push: tags: v*` workflow normally.

## What the release produces

`release.yml` attaches these to the GitHub Release:

- **`dist/*`** — the Python wheel + sdist.
- **`tapscribe-spacialchat-bridge.zip`** — the SpatialChat Chrome extension
  (unzip → `chrome://extensions` → Developer mode → Load unpacked).
- **`TapScribe.TrayBridge-win-x64.zip`** — the self-contained Windows tray exe
  (unsigned; SmartScreen may warn).
- **`TapScribe-Setup-win-x64.exe`** — the [Windows Bundle](packaging/README.md):
  embedded CPython + the wheel + the tray Launcher, per-user install, no Python
  prerequisite (unsigned; SmartScreen may warn). Not a Bridge, so it is
  announced by the README and the Release page rather than the dashboard's
  "Get a bridge" card.
- **PyPI** — `tapscribe` is published via Trusted Publishing from the
  `pypi-publish` job. That job depends only on `build`, so a failed Windows
  Bundle can't block or corrupt the upload. The publisher is registered on
  pypi.org against **this workflow filename** (`release.yml`) and the **`pypi`
  environment** — renaming either breaks the OIDC exchange.
- **`ghcr.io/vortiago/tapscribe`** — a Docker image tagged `:vX.Y.Z` and
  `:latest` on GHCR.

Release notes are drafted from PR labels by release-drafter.

The two bridge zip **filenames are stable and unversioned** so the dashboard's
Settings "Get a bridge" card can link at
`https://github.com/Vortiago/TapScribe/releases/latest/download/<asset>` — a
permanent URL that always resolves to the newest release. Those download links
404 until the first `vX.Y.Z` tag is cut.

PyPI publishing is deferred (it needs a manual PyPI-side Trusted Publishing
claim first).
