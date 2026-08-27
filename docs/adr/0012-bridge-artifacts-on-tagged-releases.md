---
status: accepted
date: 2026-07-15
---

# Bridge artifacts are built by CI and attached to tag-driven GitHub Releases

Pushing a `vX.Y.Z` tag fires `.github/workflows/release.yml`, which builds the
wheel + sdist, the SpatialChat extension zip, the Windows tray zip, the macOS
tray package and bundle, and a GHCR image, then attaches the release assets
under **stable, unversioned filenames**:

- `tapscribe-spacialchat-bridge.zip`
- `TapScribe.TrayBridge-win-x64.zip`
- `TapScribe.TrayBridge-osx-arm64.pkg`
- `TapScribe.TrayBridge-osx-arm64.zip`

The dashboard's Settings "Get a bridge" card links to
`https://github.com/{GITHUB_REPO}/releases/latest/download/<asset>`.
`releases/latest/download/...` always resolves to the newest non-prerelease,
non-draft Release, and the unversioned asset names make each URL permanent
across versions. The repo slug lives in ONE Python constant
(`config.GITHUB_REPO`); `GET /api/bridges` composes the URLs from the static
`bridges_catalog.BRIDGE_ARTIFACTS` table so the UI stays dumb and the endpoint
is unit-testable. The browser downloads straight from GitHub — no server-side
`FileResponse` proxy or offline cache.

**The version bump is a deliberate manual step.** A release is cut by running
`tools/bump_version.py X.Y.Z` (stamps `pyproject.toml`,
`tapscribe/__init__.py`, and the extension `manifest.json` in lockstep,
guarded by a consistency test), merging that bump as a normal PR, then pushing
a `vX.Y.Z` tag. A human-pushed tag triggers the `push: tags: v*` workflow
normally — the `GITHUB_TOKEN`-created-refs-don't-trigger limitation only
affects bot-created tags.

## First-release UX

The `latest/download` URLs 404 until the first tag is cut. The card renders
the download links unconditionally plus a hint line ("available after the
first tagged release") rather than probing: a server-side probe adds a network
dependency, needs caching, and breaks airgapped servers. A client-side
click-time check against the CORS-enabled
`api.github.com/.../releases/latest` remains an option.

Rejected: **release-please / auto-versioning** — dependency-minimalism; the
tag-driven `release.yml` + `release-drafter.yml` autolabeler already work, and
release-please adds a bot, a release-PR dance, and the bot-created-tag trigger
problem, all to replace a deliberate `git tag` that is a feature, not a chore.
**A server-side artifact proxy / offline cache** — bundles binaries into the
app, cache-invalidation logic, and a larger attack surface, for a public-repo
download the browser can fetch directly.

## Consequences

- Obtaining a Bridge is a dashboard click (Settings → Get a bridge), not a
  repo clone + manual build.
- The asset filenames are a HARD contract shared by the release build and the
  dashboard catalog — renaming one without the other breaks the download
  links.
- Cutting a release is a documented ritual (see `RELEASING.md`), never
  automatic.
- Neither tray build is signed, and each OS objects in its own way, so the card
  documents both. Windows shows SmartScreen, a click-through. macOS is why the
  Mac artifact the card offers is a `.pkg` while Windows ships a zip: the bundle
  carries only an ad-hoc signature, Gatekeeper reads a signature it cannot
  validate as tampering, and a quarantined copy is therefore reported as DAMAGED
  with Move to Trash as the only offer. Right-click -> Open does not bypass that
  (it bypasses the milder unidentified-developer prompt, which needs a real
  signature), leaving `xattr` as the only route out of a zip. An unsigned PACKAGE
  gets the milder treatment, and `installer` writes payload outside the path that
  applies quarantine, so the installed app opens straight away. Apple documents
  none of that, so `ci.yml` asserts it on a real runner. The zip stays published
  for anyone who wants the bundle without an installer, and it still needs the
  command. Notarisation would remove the one remaining block and needs a paid
  Apple Developer account; that, code signing and auto-update stay out of scope
  for v1.
