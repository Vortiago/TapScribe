"""Entry point: `python -m tapscribe [options]`.

Parses CLI flags, mirrors them into the module-level config + live state,
prints the auth banner, then hands off to uvicorn. The FastAPI app's
lifespan handles auto-starting the WhisperLiveKit child (and stopping it
again on shutdown).
"""

from __future__ import annotations

import argparse

import uvicorn

from . import auth, config, live
from .app import app


def main() -> None:
    p = argparse.ArgumentParser(
        prog="python -m tapscribe",
        description="TapScribe — local-first transcription recorder + dashboard.",
    )
    p.add_argument("--host", default="localhost", help="Bind address. Use 0.0.0.0 to expose on LAN.")
    p.add_argument("--port", type=int, default=8001)
    p.add_argument("--live-model", default="tiny.en",
                   help="WhisperLiveKit model name (tiny.en, small.en, large-v3, ...). Changeable from the dashboard.")
    p.add_argument("--live-language", default="en",
                   help="WhisperLiveKit language hint (en, no, auto, ...)")
    p.add_argument("--live-host", default=None,
                   help="Bind host for the live channel; defaults to --host.")
    p.add_argument("--live-port", type=int, default=8000)
    p.add_argument("--no-mlx", action="store_true",
                   help="Disable MLX for BOTH live and batch even on Apple Silicon (fall back to faster-whisper / CPU).")
    p.add_argument("--no-auto-live", action="store_true",
                   help="Don't auto-start the live channel on boot. Start it from the dashboard or via POST /api/live/start.")
    p.add_argument("--no-auth", action="store_true",
                   help="Disable HTTP Basic auth on the dashboard. Only safe on a trusted single-user localhost.")
    p.add_argument("--rotate-password", action="store_true",
                   help="Delete the persisted password and generate a new one. Invalidates any browser sessions.")
    args = p.parse_args()

    if args.no_mlx:
        config.USE_MLX = False
    if args.no_auth:
        config.AUTH_ENABLED = False
    if args.rotate_password:
        try:
            config.AUTH_PASSWORD_FILE.unlink(missing_ok=True)
        except OSError as e:
            print(f"[tapscribe] could not delete {config.AUTH_PASSWORD_FILE}: {e}", flush=True)
        auth.set_password(auth.load_or_create_password())

    config.AUTO_START_LIVE = not args.no_auto_live

    live.LIVE_CONFIG["model"] = args.live_model
    live.LIVE_CONFIG["language"] = args.live_language
    live.LIVE_CONFIG["host"] = args.live_host or args.host
    live.LIVE_CONFIG["port"] = args.live_port

    # Mirror static fields into LIVE_INFO so the dashboard shows them even
    # before the child is up. Dynamic fields (state/pid/started_at) get
    # populated by start_live_proc / _pump_live_logs.
    live.LIVE_INFO["model"] = str(live.LIVE_CONFIG["model"])
    live.LIVE_INFO["language"] = str(live.LIVE_CONFIG["language"])
    live.LIVE_INFO["host"] = str(live.LIVE_CONFIG["host"])
    live.LIVE_INFO["port"] = str(live.LIVE_CONFIG["port"])
    live.LIVE_INFO["backend"] = "mlx-whisper" if config.USE_MLX else "faster-whisper"
    live.LIVE_INFO["device"] = "Apple Silicon GPU" if config.USE_MLX else "CPU"

    if args.host == "0.0.0.0":
        print("[tapscribe] WARNING: binding to 0.0.0.0 exposes the recorder to "
              "the LAN. Make sure you trust your network.", flush=True)

    print(
        f"[tapscribe] MLX={'on' if config.USE_MLX else 'off'} "
        f"(batch={'mlx-whisper' if config.USE_MLX else 'faster-whisper'}, "
        f"live={'mlx-whisper' if config.USE_MLX else 'faster-whisper'}). "
        f"Auto-start live: {config.AUTO_START_LIVE}.",
        flush=True,
    )

    # Auth banner. Printed prominently and last so it's the bottom of the
    # boot output. Password persists across restarts; rotate via
    # --rotate-password or delete the file.
    if config.AUTH_ENABLED:
        bar = "=" * 64
        print(bar, flush=True)
        print("[tapscribe] Dashboard auth: HTTP Basic", flush=True)
        print(f"[tapscribe]   user:     {config.AUTH_USER}", flush=True)
        print(f"[tapscribe]   password: {auth.AUTH_PASS}", flush=True)
        print(f"[tapscribe]   stored in: {config.AUTH_PASSWORD_FILE}", flush=True)
        print("[tapscribe] (persists across restarts — your browser stays logged", flush=True)
        print("[tapscribe]  in. Rotate via --rotate-password or delete the file.)", flush=True)
        print("[tapscribe] /record WebSocket is NOT auth'd; --no-auth disables.", flush=True)
        print(bar, flush=True)
    else:
        print("[tapscribe] WARNING: --no-auth — dashboard is UNAUTHENTICATED.", flush=True)
        if args.host == "0.0.0.0":
            print("[tapscribe] WARNING: combined with LAN binding, anyone on the", flush=True)
            print("[tapscribe]  network can view/delete recordings. Re-enable auth.", flush=True)

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
