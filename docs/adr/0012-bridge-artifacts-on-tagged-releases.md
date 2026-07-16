---
status: accepted
date: 2026-07-15
---

# Bridge artifacts are built by CI and attached to tag-driven GitHub Releases

TapScribe ships two user-facing Bridges (the SpatialChat Chrome MV3 extension
and the Windows tray app) alongside the Python dashboard, but historically had
no automated build/publish path and no way for an operator to obtain a Bridge
from the dashboard — a user had to clone the repo and either "Load unpacked" the
extension or `dotnet publish` the tray app themselves.

This ADR records how Bridges are now distributed and why the mechanism is
deliberately minimal.

## Decision

**Bridges are built by CI and attached as assets to tag-driven GitHub
Releases.** Pushing a `vX.Y.Z` tag fires `.github/workflows/release.yml`, which
builds the wheel + sdist, the SpatialChat extension zip, the Windows tray exe
zip, and a GHCR image, then attaches the release assets under **stable,
unversioned filenames**:

- `tapscribe-spacialchat-bridge.zip`
- `TapScribe.TrayBridge-win-x64.zip`

The dashboard's Settings "Get a bridge" card links to
`https://github.com/{GITHUB_REPO}/releases/latest/download/<asset>`.
`releases/latest/download/...` always resolves to the newest non-prerelease,
non-draft Release, and the unversioned asset names make each URL permanent
across versions. The repo slug lives in ONE Python constant
(`config.GITHUB_REPO`); `GET /api/bridges` composes the URLs from the static
`bridges_catalog.BRIDGE_ARTIFACTS` table so the UI stays dumb and the endpoint
is unit-testable. The browser downloads straight from GitHub — there is no
server-side `FileResponse` proxy or offline cache.

**The version bump is a deliberate manual step.** A release is cut by running
`tools/bump_version.py X.Y.Z` (which stamps `pyproject.toml`,
`tapscribe/__init__.py`, and the extension `manifest.json` in lockstep, guarded
by a consistency test), merging that bump as a normal PR, then pushing a
`vX.Y.Z` tag. A human-pushed tag triggers the `push: tags: v*` workflow normally
— the `GITHUB_TOKEN`-created-refs-don't-trigger limitation only affects
bot-created tags, so the manual flow sidesteps it entirely.

## First-release UX

The `latest/download` URLs 404 until the first `vX.Y.Z` tag is cut. The card
therefore renders the download links unconditionally plus a hint line
("available after the first tagged release") rather than probing the GitHub API.
A server-side probe was rejected: it adds a network dependency, needs caching,
and breaks airgapped servers. A future client-side click-time check against the
CORS-enabled `api.github.com/.../releases/latest` remains an option.

## Considered alternatives

**release-please / auto-versioning — rejected.** The guiding constraint is
dependency-minimalism: add third-party tooling only with a good reason. The repo
already has a working tag-driven `release.yml` and a `release-drafter.yml`
autolabeler, so extending that path costs zero new third-party actions.
release-please would add a bot, a release-PR dance, and the bot-created-tag
trigger problem — all to replace a deliberate `git tag` we consider a feature,
not a chore. Releases staying a conscious, human-pushed tag is the point.

**A server-side artifact proxy / offline cache — rejected.** It would let the
dashboard serve Bridges without GitHub, but at the cost of bundling binaries
into the app, cache-invalidation logic, and a larger attack surface, for a
public-repo download that the browser can fetch directly.

## Consequences

- Obtaining a Bridge is a dashboard click (Settings → Get a bridge), not a repo
  clone + manual build.
- The asset filenames are a HARD contract shared by the release build and the
  dashboard catalog — renaming one without the other breaks the download links.
- Cutting a release is a documented ritual (see `RELEASING.md`), never
  automatic.
- The tray exe is unsigned; the card documents the SmartScreen warning. Code
  signing / installer / auto-update stay out of scope.
