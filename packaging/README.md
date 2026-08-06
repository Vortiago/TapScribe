# Packaging

## Windows Bundle (`windows/`)

Builds the **Windows Bundle** — the double-clickable installer shipping an
embedded CPython, the `tapscribe` wheel, and a tray **Launcher**, so an
operator needs no Python and no checkout. [CONTEXT.md](../CONTEXT.md) defines
the terms;
[ADR-0015](../docs/adr/0015-windows-bundle-embedded-interpreter.md) owns why
it embeds an interpreter rather than freezing an `.exe`.

- `src/TapScribe.Bundle.Core/` — cross-platform launcher logic, unit-tested
  on any OS (`tests/`).
- `src/TapScribe.Bundle.Launcher/` — the WinForms tray app: supervises the
  Recorder in a Job Object, pipes its output to a log, offers Open dashboard /
  Copy password / Show log / Quit.
- `TapScribe.iss` — Inno Setup: per-user install to
  `%LOCALAPPDATA%\Programs\TapScribe`; operator data lives separately in
  `%USERPROFILE%\TapScribe`.

Built by the `bundle` job in `.github/workflows/release.yml` on a tagged
release and attached as `TapScribe-Setup-win-x64.exe`. Not offered by the
dashboard's "Get a bridge" card — a Bundle is not a Bridge. Unsigned, so
SmartScreen warns: click *More info → Run anyway*.

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
