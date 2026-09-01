# Packaging

## The Bundle's .NET assemblies (`bundle/`)

The **host role** (ADR-0022): boot, supervise and reap a co-located Recorder, and
be the way in to it. There is no executable here — the role is carried by the
[Tray Bridge](../bridges/tray-bridge/), which references these. One tray per OS,
two roles, and the SHELL is where they meet: neither core references the other,
so a [Bundle](../CONTEXT.md#bundle) stays not-a-Bridge.

- `src/TapScribe.Bundle.Core/` — cross-platform (`net10.0`), and unit-tested on
  the ubuntu CI leg: `BundleLayout` (paths, the one-wheel rule, and
  `HostPayloadPresent` — the role probe), `RecorderCommand` (argv + environment),
  `RecorderSupervisor` (the two children, and whether a Recorder that died means
  "someone else holds the port" or "this install is broken"), `HostController`
  (what the menu says), `LogRotation` + `RotatingLogWriter`, `PasswordFile`.
- `src/TapScribe.Bundle.Windows/` — one type, `JobObject`, and the Win32 P/Invoke
  it needs, behind `IProcessReaper`. `net10.0` rather than `net10.0-windows` so it
  still COMPILES on the Linux leg; its tests are `[RequiresWindows]` and name the
  capability they skip.

Why the split is here and not in `windows/`: the assemblies are cross-platform by
design, and ADR-0024 adds a macOS reaper beside the Windows one.

```
dotnet test packaging/bundle/tests/TapScribe.Bundle.Core.Tests -c Release   # anywhere
dotnet build packaging/bundle/TapScribe.Bundle.slnx -c Release              # Windows
```

## Windows installer (`windows/`)

`TapScribe.iss` — Inno Setup: per-user install to
`%LOCALAPPDATA%\Programs\TapScribe`, with operator data separately in
`%USERPROFILE%\TapScribe` so uninstalling never deletes recordings.
[ADR-0015](../docs/adr/0015-windows-bundle-embedded-interpreter.md) owns why the
Bundle embeds an interpreter rather than freezing an `.exe`.

It stages three things, all produced by the `bundle` job in
`.github/workflows/release.yml`:

| staged | what |
|---|---|
| `staging/python/` | embedded CPython with the core deps already installed |
| `staging/wheel/` | the one `tapscribe-X.Y.Z-*.whl` this Bundle installs from |
| `staging/tray/` | the tray, **taken from the bridge-only artifact**, not rebuilt |

That last row is ADR-0022's decision made concrete: the Bundle ships the same
tray the `TapScribe.TrayBridge-win-x64.zip` does, and the host role switches on
the payload above being on disk beside it. Building it twice is where the two
copies would drift.

`AppMutex` carries the tray's single-instance name **and** the retired Launcher's,
so an installer upgrading over a running old Launcher can still ask it to close.

Attached to a tagged release as `TapScribe-Setup-win-x64.exe`. Not offered by the
dashboard's "Get a bridge" card — a Bundle is not a Bridge, and you need a Bundle
to have a dashboard. Unsigned, so SmartScreen warns: *More info → Run anyway*.

## systemd (Linux)

The unit assumes the repo at `/opt/tapscribe`, owned by a `tapscribe` system
user — adjust `User=` / `WorkingDirectory=` first.

```
sudo cp packaging/systemd/tapscribe.service /etc/systemd/system/
sudo systemctl enable --now tapscribe
sudo journalctl -u tapscribe -f
```

The unit shells out to `start.sh`, so CLI flags (`--log-json`, `--no-mlx`) go
there. Verify with `curl http://localhost:8001/healthz`. No launchd plist
ships; macOS operators run `start.sh` directly.
