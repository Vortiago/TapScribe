# Packaging

How TapScribe is packaged for distribution, and templates for running it as a
long-lived service.

## Windows Bundle (`windows/`)

`windows/` builds the **Windows Bundle** — the double-clickable installer that
ships an embedded CPython, the `tapscribe` wheel, and a tray **Launcher**, so an
operator needs no Python and no checkout. "Bundle" and "Launcher" are domain
terms; see [CONTEXT.md](../CONTEXT.md) for what they mean and
[ADR-0015](../docs/adr/0015-windows-bundle-embedded-interpreter.md) for why it's
an embedded interpreter rather than a frozen `.exe`.

Contents:

- `src/TapScribe.Bundle.Core/` — cross-platform launcher logic (unit-tested on
  any OS).
- `src/TapScribe.Bundle.Launcher/` — the WinForms tray app: supervises the
  Recorder in a Job Object, pipes its output to a log, and offers Open
  dashboard / Copy password / Show log / Quit.
- `TapScribe.iss` — the Inno Setup script. Per-user install to
  `%LOCALAPPDATA%\Programs\TapScribe`; operator data lives separately in
  `%USERPROFILE%\TapScribe`.

Built by the `bundle` job in `.github/workflows/release.yml` on a tagged
release and attached to the GitHub Release as
`TapScribe-Setup-win-x64.exe`. It is **not** offered by the dashboard's
"Get a bridge" card — a Bundle is not a Bridge, and you need one to *have* a
dashboard.

The Bundle is currently **unsigned**, so SmartScreen shows a
"Windows protected your PC" warning; click *More info → Run anyway*.

## systemd (Linux)

Assumes the repo lives at `/opt/tapscribe` and a `tapscribe` system user
owns it. Adjust the unit's `User=` / `WorkingDirectory=` to match your
install path before enabling.

```
sudo cp packaging/systemd/tapscribe.service /etc/systemd/system/
sudo systemctl enable --now tapscribe
sudo journalctl -u tapscribe -f
```

The unit shells out to `start.sh`, so any CLI flags (e.g.
`--log-json`, `--no-mlx`) go in that script next to the existing
`python -m tapscribe` invocation. Use `curl http://localhost:8001/healthz`
to verify the service is up before wiring it into your monitoring.

macOS operators typically run `start.sh` directly under launchd or just
in a terminal — no plist template ships here.
