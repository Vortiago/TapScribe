# Packaging

Templates for running TapScribe as a long-lived service.

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
