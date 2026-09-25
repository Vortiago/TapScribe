---
status: accepted (extends ADR-0015, ADR-0020)
date: 2026-08-28
---

# The macOS Bundle keeps its interpreter outside the signed .app

macOS gets a [Bundle](../../CONTEXT.md#bundle): a `.pkg` installing
`TapScribe.app` to `/Applications`, carrying an embedded CPython
(python-build-standalone, `osx-arm64`) and the wheel as **read-only payload**.
On first launch with the [host role](../../CONTEXT.md#host-role), the tray copies
the interpreter and wheel to `~/Library/Application Support/TapScribe/runtime/`
and every pip run — preflight's repairs, `/setup`'s model backends — targets the
copy. The `.app` is never written to.

The copy is **version-stamped and atomic**, which is three decisions, not one:

- It lands in `runtime/<version>.partial/` and is renamed to `runtime/<version>/`
  only once complete. A quit or crash partway through 300 MB otherwise leaves a
  `runtime/` that exists but holds a broken interpreter, and a "copy on first
  launch" check never fires again.
- The stamp is the `.app`'s version, so **an upgrade re-copies**. Without it,
  installing 1.4 over a runtime copied from 1.3 leaves the installer saying 1.4
  while the Recorder serves 1.3 — precisely the drift ADR-0015's one-wheel rule
  exists to prevent, and one `ResolveWheel` cannot catch, because the stale
  runtime holds exactly one (wrong) wheel.
- A re-copy starts from the shipped interpreter, so the model backends `/setup`
  pip-installed into the old one are gone. The tray says so and offers to
  re-run the install picker; the previous `runtime/<old>` is kept until the new
  one is complete, then deleted.

Repair-by-reinstall has to be spelled out for the same reason: `BundleLayout`'s
error text tells operators to reinstall, and on macOS reinstalling replaces the
pristine `.app`, not the broken copy. The tray therefore treats a failed
integrity check on `runtime/<version>/` as "delete and re-copy", and reinstalling
does fix it — via the next launch, not via the installer.

Two facts force it. ADR-0015: `/setup` pip-installs into the embedded
interpreter at runtime, which is why the Windows Bundle puts it under
`%LOCALAPPDATA%\Programs\TapScribe` rather than `Program Files`. And on macOS
writing inside an `.app` invalidates its code signature — the failure mode
ADR-0020 and `build-macos-pkg.sh` already document at length (ad-hoc signed,
arm64 refuses unsigned, Gatekeeper reads an unvalidatable signature as tampering
and offers only Move to Trash).

**Data root: `~/Library/Application Support/TapScribe`**, what
`TAPSCRIBE_BASE_DIR` points at. `Bridge.MacOS/TrayStores` already puts the
tray's own settings there, so settings, `recordings/`, `config/`,
`.auth-password`, `.tap-token` and the runtime share one folder: one thing to
back up, one to delete, and a bridge-only operator who later installs the Bundle
keeps their settings. It is hidden in Finder, so the tray offers **Reveal
recordings in Finder**. Not `~/Documents`, `~/Desktop` or `~/Downloads`, which
are TCC-protected.

Signing stays **ad-hoc**, per ADR-0020. Developer ID + notarization would remove
the whole Gatekeeper class and make the zipped `.app` viable again; it costs an
Apple membership and CI secrets, and is deferred rather than rejected.

## Considered options

**Run the interpreter in place inside the `.app`.** Rejected: the first
`/setup` pip run breaks the signature.

**A `pkg` postinstall script writing into the installing user's home.**
Rejected: `installer` runs it as root with `$HOME` at `/var/root`, so it must
resolve the console user by hand — root-shaped, fiddly, and untestable on the
Linux CI leg that covers the rest of `Bundle.Core`. The objection is to reaching
into a user's home, not to a root script as such: see the one postinstall both
packages do carry, under Consequences.

**Fetch python-build-standalone on first run**, the way
`tapscribe/diarizers/model.py` fetches CAM++ with a verified sha256. Precedent
exists and it halves the artifact, but it costs an offline install, which is
most of a Bundle's point.

## Consequences

The runtime exists twice on disk — once read-only in the `.app`, once writable
in the data root. Order-of 300 MB rather than 150 MB, and an uninstall that
deletes only the `.app` leaves the runtime behind with the operator's data,
which is the correct side to err on.

Both packages install `/Applications/TapScribe.app` under one identifier, which
is what lets a Bundle upgrade a bridge-only install in place. It cuts the other
way too, so the packages carry two rules of their own, both in
`bridges/tray-bridge/tools/build-macos-pkg.sh` and both proven on CI's macOS
leg:

- **The bridge-only package refuses to install over a Bundle.** Replacing the
  app there deletes the Bundle's `python/` and `wheel/`, and the Recorder just
  disappears from the menu. The dashboard offers that package on every install,
  so the refusal is in the package: a Distribution `installation-check` that
  `installer` runs before it writes anything, keyed on the same `python/` OR
  `wheel/` probe the tray uses for its own role.
- **Both packages carry a postinstall that removes the app they supersede**,
  `/Applications/TapScribe.TrayBridge.MacOS.app`. `installer` wrote that bundle,
  so it is owned by root, and the tray's first-launch removal
  (`LegacyAppBundle`), which runs as the operator, cannot delete it. The script
  touches that one path only, only when the bundle there is ours by identifier,
  never through a symlink, and never fails the install. It writes nothing and
  reads no `$HOME`, so it is not the postinstall rejected above.
