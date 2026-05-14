"""Entry point: `python -m tapscribe [options]`.

Parses CLI flags, constructs the Recorder, attaches it to
`app.state.recorder`, then hands off to uvicorn. The FastAPI app's
lifespan auto-starts the WhisperLiveKit child (and stops it again on
shutdown) via the Recorder's LiveChannel.
"""

from __future__ import annotations

import argparse

import uvicorn

from . import config
from .app import app
from .live import LiveConfig
from .recorder import Recorder


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
                   help="Don't auto-start the live channel on boot.")
    p.add_argument("--no-auth", action="store_true",
                   help="Disable HTTP Basic auth on the dashboard. Only safe on a trusted single-user localhost.")
    p.add_argument("--rotate-password", action="store_true",
                   help="Delete the persisted password and generate a new one. Invalidates browser sessions.")
    args = p.parse_args()

    # Boot-time constants that affect every transcribe-call route the
    # Recorder hands out. `use_mlx` is per-Recorder; `AUTH_ENABLED` and
    # `AUTO_START_LIVE` are still module-level since they're security/boot
    # toggles that don't change at runtime.
    use_mlx = _detect_use_mlx() and not args.no_mlx
    config.AUTH_ENABLED = not args.no_auth
    config.AUTO_START_LIVE = not args.no_auto_live

    live_config = LiveConfig(
        model=args.live_model,
        language=args.live_language,
        host=args.live_host or args.host,
        port=args.live_port,
    )

    recorder = Recorder(
        recordings_dir=config.RECORDINGS_DIR,
        config_dir=config.CONFIG_DIR,
        live_config=live_config,
        use_mlx=use_mlx,
        auth_password_file=config.AUTH_PASSWORD_FILE,
    )

    if args.rotate_password:
        recorder.auth.rotate()

    app.state.recorder = recorder

    if args.host == "0.0.0.0":
        print("[tapscribe] WARNING: binding to 0.0.0.0 exposes the recorder to "
              "the LAN. Make sure you trust your network.", flush=True)

    print(
        f"[tapscribe] MLX={'on' if use_mlx else 'off'} "
        f"(batch={'mlx-whisper' if use_mlx else 'faster-whisper'}, "
        f"live={'mlx-whisper' if use_mlx else 'faster-whisper'}). "
        f"Auto-start live: {config.AUTO_START_LIVE}.",
        flush=True,
    )

    # Auth banner.
    if config.AUTH_ENABLED:
        bar = "=" * 64
        print(bar, flush=True)
        print("[tapscribe] Dashboard auth: HTTP Basic", flush=True)
        print(f"[tapscribe]   user:     {config.AUTH_USER}", flush=True)
        print(f"[tapscribe]   password: {recorder.auth.password}", flush=True)
        print(f"[tapscribe]   stored in: {config.AUTH_PASSWORD_FILE}", flush=True)
        print("[tapscribe] (persists across restarts — your browser stays logged in.", flush=True)
        print("[tapscribe]  Rotate via --rotate-password or delete the file.)", flush=True)
        print("[tapscribe] /record WebSocket is NOT auth'd; --no-auth disables.", flush=True)
        print(bar, flush=True)
    else:
        print("[tapscribe] WARNING: --no-auth — dashboard is UNAUTHENTICATED.", flush=True)
        if args.host == "0.0.0.0":
            print("[tapscribe] WARNING: combined with LAN binding, anyone on the", flush=True)
            print("[tapscribe]  network can view/delete recordings. Re-enable auth.", flush=True)

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


def _detect_use_mlx() -> bool:
    """True on Apple Silicon with mlx-whisper importable, unless explicitly
    disabled via the SX_NO_MLX=1 env var."""
    import os
    import platform
    if os.environ.get("SX_NO_MLX") == "1":
        return False
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return False
    try:
        import mlx_whisper  # noqa: F401
        return True
    except ImportError:
        return False


if __name__ == "__main__":
    main()
