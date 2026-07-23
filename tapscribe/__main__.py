"""Entry point: `python -m tapscribe [options]`.

Parses CLI flags, constructs the Recorder, attaches it to
`app.state.recorder`, then hands off to uvicorn. When
`config.AUTO_START_LIVE` is set (opt-in via `--auto-live`; off by
default), the FastAPI app's lifespan auto-starts the WhisperLiveKit
child (and stops it again on shutdown) via the Recorder's LiveChannel.
"""

from __future__ import annotations

import argparse

import uvicorn

from . import config, install_target
from .app import app
from .live import LiveConfig
from .recorder import Recorder


def build_parser() -> argparse.ArgumentParser:
    """Construct the `python -m tapscribe` argument parser.

    Extracted from `main()` as a seam so a fast in-process test can pin
    the actual flag-string wiring (e.g. `--auto-live` / the retained
    deprecated `--no-auto-live`) without booting uvicorn.
    """
    p = argparse.ArgumentParser(
        prog="python -m tapscribe",
        description="TapScribe — local-first transcription recorder + dashboard.",
    )
    p.add_argument("--host", default="localhost", help="Bind address. Use 0.0.0.0 to expose on LAN.")
    p.add_argument("--port", type=int, default=8001)
    p.add_argument(
        "--live-model",
        default="tiny.en",
        help="WhisperLiveKit model name (tiny.en, small.en, large-v3, ...). Changeable from the dashboard.",
    )
    p.add_argument("--live-language", default="en", help="WhisperLiveKit language hint (en, no, auto, ...)")
    p.add_argument(
        "--live-host",
        default=None,
        help="Bind host for the live channel. Defaults to 127.0.0.1 — WhisperLiveKit "
        "is internal (only the recorder's live_relay talks to it), so exposing it "
        "on the LAN serves no purpose and just widens the attack surface.",
    )
    p.add_argument(
        "--live-port",
        type=int,
        default=0,
        help="Bind port for the live channel. 0 (default) = pick a free ephemeral "
        "port at spawn time. WhisperLiveKit is internal — only the recorder talks "
        "to it — so a stable well-known port is rarely useful, and a fixed 8000 "
        "is the most common cause of `EADDRINUSE` after a hard-killed prior run.",
    )
    # WhisperLiveKit streaming knobs. Each one trades latency for accuracy;
    # operators tune them against the per-tap lag reading in the live-channel
    # dashboard panel. Unset = use WLK's own default.
    p.add_argument(
        "--live-min-chunk-size",
        type=float,
        default=None,
        help="Minimum audio chunk size in seconds the live channel decodes at "
        "a time. Higher = more future context per decode = better accuracy + "
        "more lag. Unset = use WhisperLiveKit's default.",
    )
    p.add_argument(
        "--live-buffer-trimming",
        choices=("sentence", "segment"),
        default=None,
        help="When the live channel resets its rolling buffer: on sentence "
        "boundaries (more conservative, better accuracy) or segment "
        "boundaries. Unset = use WhisperLiveKit's default.",
    )
    p.add_argument(
        "--live-buffer-trimming-sec",
        type=float,
        default=None,
        help="Buffer length (s) above which trimming is triggered. Pairs with "
        "--live-buffer-trimming. Unset = use WhisperLiveKit's default.",
    )
    p.add_argument(
        "--live-max-context-tokens",
        type=int,
        default=None,
        help="Max prior-text tokens the live decoder can condition on. Higher "
        "= better continuations + more compute per tick. Unset = use "
        "WhisperLiveKit's default.",
    )
    p.add_argument(
        "--live-gate-min-speech-ms",
        type=int,
        default=None,
        help="Minimum confirmed-speech window (ms) before the TapScribe speech "
        "gate releases audio to the live backend. 0 (default) opens on the "
        "first VAD 'start'; higher values suppress brief noise blips (key "
        "taps, single coughs). Tunable from the dashboard at runtime.",
    )
    p.add_argument(
        "--no-mlx",
        action="store_true",
        help="Disable MLX for BOTH live and batch even on Apple Silicon (back-compat alias for --backend=cpu).",
    )
    p.add_argument(
        "--backend",
        choices=("auto", "mlx", "cuda", "cpu"),
        default="auto",
        help="Default backend preference. `auto` picks MLX on Apple Silicon, CUDA on NVIDIA, "
        "CPU otherwise. The dashboard's backend chip can override per-job.",
    )
    p.add_argument(
        "--auto-live",
        action="store_true",
        help="Auto-start the live channel on boot (default: off).",
    )
    p.add_argument(
        "--no-auto-live",
        action="store_true",
        help="[deprecated — off is the default] Accepted for backwards-compat; no-op.",
    )
    p.add_argument(
        "--no-auth",
        action="store_true",
        help="Disable HTTP Basic auth on the dashboard AND the /tap WebSocket token gate. "
        "Only safe on a trusted single-user localhost.",
    )
    p.add_argument(
        "--rotate-password",
        action="store_true",
        help="Delete the persisted password and generate a new one. Invalidates browser sessions.",
    )
    p.add_argument(
        "--rotate-tap-token",
        action="store_true",
        help="Delete the persisted /tap bearer token and generate a new one. "
        "Bridges with the old token will be refused at the WS upgrade.",
    )
    p.add_argument(
        "--tls",
        action="store_true",
        help="Serve the dashboard + /tap over TLS (https:// and wss://). If --cert/--key "
        "are not supplied, a self-signed pair is generated next to .auth-password.",
    )
    p.add_argument(
        "--cert",
        default=None,
        help="Path to a PEM certificate. Implies --tls. Defaults to .tapscribe-cert.pem.",
    )
    p.add_argument(
        "--key", default=None, help="Path to the PEM private key for --cert. Defaults to .tapscribe-key.pem."
    )
    p.add_argument(
        "--log-json",
        action="store_true",
        help="Emit one JSON line per log record instead of uvicorn's plaintext format. "
        "Useful when piping into journalctl -o json / vector / fluent-bit.",
    )
    p.add_argument(
        "--install-spec",
        default=None,
        help="What pip installs TapScribe from when /setup installs model "
        "backends: omitted (a dev checkout, the default), a path to the "
        "Windows Bundle's shipped .whl, or a pinned 'tapscribe==X.Y.Z'. "
        "The Bundle's Launcher passes its wheel. See ADR-0015.",
    )
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # Validate before anything boots. This value is a CLI string that ends up in
    # a pip argv (via `/setup` → `picker_install_argv`), which CodeQL treats as
    # external input regardless of who launched the process (CLAUDE.md). Failing
    # here beats discovering a Bundle's wheel is missing on the operator's first
    # model install. `parser.error` so a bad value exits 2 on stderr like every
    # other malformed flag, rather than inventing a second failure convention.
    try:
        install_target.resolve_install_spec(args.install_spec)
    except install_target.InstallSpecError as exc:
        parser.error(str(exc))

    # Boot-time constants that affect every transcribe-call route the
    # Recorder hands out. `backend` is the per-Recorder preference;
    # `AUTH_ENABLED` and `AUTO_START_LIVE` are still module-level since
    # they're security/boot toggles that don't change at runtime.
    # --no-mlx is the legacy alias: it folds into --backend by forcing
    # `cpu` over whatever `auto` would have chosen on Apple Silicon.
    backend_pref = args.backend
    if args.no_mlx and backend_pref == "auto":
        backend_pref = "cpu"
    config.AUTH_ENABLED = not args.no_auth
    config.AUTO_START_LIVE = args.auto_live

    live_config_kwargs: dict[str, object] = dict(
        model=args.live_model,
        language=args.live_language,
        host=args.live_host or "127.0.0.1",
        port=args.live_port,
        min_chunk_size=args.live_min_chunk_size,
        buffer_trimming=args.live_buffer_trimming,
        buffer_trimming_sec=args.live_buffer_trimming_sec,
        max_context_tokens=args.live_max_context_tokens,
    )
    if args.live_gate_min_speech_ms is not None:
        live_config_kwargs["gate_min_speech_ms"] = args.live_gate_min_speech_ms
    live_config = LiveConfig(**live_config_kwargs)  # type: ignore[arg-type]

    recorder = Recorder(
        recordings_dir=config.RECORDINGS_DIR,
        config_dir=config.CONFIG_DIR,
        live_config=live_config,
        backend=backend_pref,
        auth_password_file=config.AUTH_PASSWORD_FILE,
        tap_token_file=config.TAP_TOKEN_FILE,
    )

    if args.rotate_password:
        recorder.auth.rotate()
    if args.rotate_tap_token:
        recorder.tap.rotate()

    app.state.recorder = recorder
    # Handed to `picker_install_argv` when /setup installs model backends, so a
    # Bundle installs from the wheel it shipped rather than an absent checkout.
    app.state.install_spec = args.install_spec
    app.state.log_json = bool(args.log_json)

    if args.host == "0.0.0.0":
        print(
            "[tapscribe] WARNING: binding to 0.0.0.0 exposes the recorder to "
            "the LAN. Make sure you trust your network.",
            flush=True,
        )

    from .runtime_probe import available_backends, resolve_backend_preference

    avail = sorted(available_backends())
    try:
        resolved = resolve_backend_preference(backend_pref)
    except RuntimeError as e:
        # Operator picked an unavailable kind explicitly — surface the
        # error before uvicorn starts so they see it instead of catching
        # it on the first transcribe call.
        print(f"[tapscribe] FATAL: {e}", flush=True)
        raise SystemExit(2) from e

    print(
        f"[tapscribe] backend preference={backend_pref} (resolves to {resolved} on this machine). "
        f"Available backends: {avail}. Auto-start live: {config.AUTO_START_LIVE}.",
        flush=True,
    )

    # Auth banner.
    if config.AUTH_ENABLED:
        bar = "=" * 64
        print(bar, flush=True)
        print("[tapscribe] Dashboard auth: HTTP Basic", flush=True)
        print(f"[tapscribe]   user:     {config.AUTH_USER}", flush=True)
        print(f"[tapscribe]   password: {recorder.auth.value}", flush=True)
        print(f"[tapscribe]   stored in: {config.AUTH_PASSWORD_FILE}", flush=True)
        print("[tapscribe] (persists across restarts — your browser stays logged in.", flush=True)
        print("[tapscribe]  Rotate via --rotate-password or delete the file.)", flush=True)
        print("[tapscribe] /tap WebSocket auth: bearer token (Sec-WebSocket-Protocol)", flush=True)
        print(f"[tapscribe]   tap token: {recorder.tap.value}", flush=True)
        print(f"[tapscribe]   stored in: {config.TAP_TOKEN_FILE}", flush=True)
        print("[tapscribe]   (paste into the bridge popup. Rotate via --rotate-tap-token.)", flush=True)
        print(bar, flush=True)
    else:
        print("[tapscribe] WARNING: --no-auth — dashboard AND /tap are UNAUTHENTICATED.", flush=True)
        if args.host == "0.0.0.0":
            print("[tapscribe] WARNING: combined with LAN binding, anyone on the", flush=True)
            print("[tapscribe]  network can view/delete recordings. Re-enable auth.", flush=True)

    # TLS wiring — opt-in via --tls (or implied by --cert/--key). When no
    # cert path is given, generate a self-signed pair next to the secret
    # files; reuse it across restarts so browsers only prompt once.
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None
    use_tls = args.tls or bool(args.cert or args.key)
    if use_tls:
        from pathlib import Path as _Path

        from .tls import ensure_self_signed_cert

        cert_path = _Path(args.cert) if args.cert else config.TLS_CERT_FILE
        key_path = _Path(args.key) if args.key else config.TLS_KEY_FILE
        if args.cert or args.key:
            if not (cert_path.is_file() and key_path.is_file()):
                print(
                    f"[tapscribe] ERROR: --cert/--key set but {cert_path} or {key_path} missing.", flush=True
                )
                raise SystemExit(2)
            pair = type("Pair", (), {"cert_file": cert_path, "key_file": key_path})()
        else:
            pair = ensure_self_signed_cert(cert_path, key_path, host=args.host)
            print(f"[tapscribe] TLS: using self-signed cert at {pair.cert_file}", flush=True)
            print(
                "[tapscribe]      (first browser visit will show a 'not secure' prompt — accept once.)",
                flush=True,
            )
        ssl_certfile = str(pair.cert_file)
        ssl_keyfile = str(pair.key_file)
        print(f"[tapscribe] TLS enabled: https://{args.host}:{args.port}/", flush=True)

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
    )


if __name__ == "__main__":
    main()
