"""WhisperLiveKit child-process management.

The recorder owns a single `whisperlivekit-server` child process and exposes
start / stop / restart controls to the dashboard. State is kept in module-
level dicts so /api/state can render it without any extra IPC.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config
from .models import download_nb_whisper_ct2_dir
from .text import read_prompt

# ---------------------------------------------------------------------------
# Live-channel info (mirrored into the dashboard via /api/state)
# ---------------------------------------------------------------------------

LIVE_INFO: dict[str, str] = {
    "model": "",
    "backend": "",       # "mlx-whisper" or "faster-whisper" or ""
    "device": "",        # human-readable
    "language": "",
    "host": "",
    "port": "",
    "state": "stopped",  # stopped | starting | running | error
    "last_error": "",
    "pid": "",
    "started_at": "",
    "vac": "",           # "on" / "off"
    "confidence_validation": "",  # "on" / "off"
}

# Mutable config; updated by --live-* CLI args at boot and by /api/live/start
# payloads at runtime. _start_live_proc() reads from here when building argv.
#
# Quality knobs:
#   vac=True (default)
#     Silero-VAD gates Whisper at the audio layer — inference only runs
#     while speech is detected, eliminating "[BLANK_AUDIO]" / "Thanks for
#     watching" hallucinations Whisper otherwise emits on silence.
#     WhisperLiveKit defaults VAC to ON; the only relevant CLI flag is
#     --no-vac.
#   confidence_validation=True
#     Whisper commits a token only when its avg_logprob clears a confidence
#     bar; uncertain tokens are held back instead of flickering on screen.
#     Reduces visual noise and near-silence hallucinations VAC missed.
LIVE_CONFIG: dict[str, Any] = {
    "model": "tiny.en",
    "language": "en",
    "host": "localhost",
    "port": 8000,
    "vac": True,
    "confidence_validation": True,
}

# Held while spawning/killing the child so two concurrent /api/live/* calls
# can't race. A regular threading lock is sufficient.
LIVE_LOCK = threading.Lock()
LIVE_PROC: subprocess.Popen | None = None
LIVE_LOG: deque[str] = deque(maxlen=200)


def live_running() -> bool:
    return LIVE_PROC is not None and LIVE_PROC.poll() is None


def start_live_proc(
    model: str | None = None,
    language: str | None = None,
) -> tuple[bool, str]:
    """Spawn whisperlivekit-server with the current (optionally overridden)
    config. Returns (ok, message). Does NOT stop a running process first —
    /api/live/start handles 'apply' as stop+start."""
    global LIVE_PROC
    with LIVE_LOCK:
        if live_running():
            return False, "already running"

        if model:
            LIVE_CONFIG["model"] = model
        if language:
            LIVE_CONFIG["language"] = language

        exe = shutil.which("whisperlivekit-server")
        if not exe:
            # Fallback: if the recorder is itself running inside a venv,
            # look for whisperlivekit-server in that venv's bin/Scripts
            # directly. This works without `source .venv/bin/activate` and
            # matters when the recorder is run as `path/to/venv/python -m
            # tapscribe` (e.g. a service that doesn't activate).
            import sys as _sys
            venv_bin = Path(_sys.prefix) / ("Scripts" if os.name == "nt" else "bin")
            for cand in ("whisperlivekit-server.exe", "whisperlivekit-server"):
                p = venv_bin / cand
                if p.is_file():
                    exe = str(p)
                    break
        if not exe:
            msg = "whisperlivekit-server not found on PATH or in the recorder's venv"
            LIVE_INFO["state"] = "error"
            LIVE_INFO["last_error"] = msg
            print(f"[tapscribe] {msg}", flush=True)
            return False, msg

        # WhisperLiveKit's `--model` arg only accepts names it has in its
        # built-in mapping table (tiny.en / large-v3 / etc.) and errors out
        # for unknown names. NB-Whisper isn't in that table.
        #
        # The escape hatch is `--model-path <hf-repo>` — WhisperLiveKit
        # calls snapshot_download() and then inspects which backends are
        # usable against the actual downloaded files (compatible_whisper_mlx
        # / compatible_faster_whisper). We also drop the explicit
        # --backend mlx-whisper for that path: forcing it would re-trigger
        # the strict compatibility check we're trying to sidestep.
        live_model = str(LIVE_CONFIG["model"])
        is_nb_whisper = live_model.startswith("nb-whisper-")

        cmd = [
            exe,
            "--lan", str(LIVE_CONFIG["language"]),
            "--host", str(LIVE_CONFIG["host"]),
            "--port", str(LIVE_CONFIG["port"]),
            "--pcm-input",
        ]
        if not LIVE_CONFIG.get("vac"):
            cmd.append("--no-vac")
        if LIVE_CONFIG.get("confidence_validation"):
            cmd.append("--confidence-validation")
        if is_nb_whisper:
            try:
                ct2_dir = download_nb_whisper_ct2_dir(live_model)
            except Exception as e:
                msg = f"failed to fetch nb-whisper ct2 weights: {e}"
                LIVE_INFO["state"] = "error"
                LIVE_INFO["last_error"] = msg
                LIVE_PROC = None
                return False, msg
            cmd.extend(["--model-path", str(ct2_dir)])
            cmd.extend(["--backend-policy", "localagreement"])
        else:
            cmd.extend(["--model", live_model])
            if config.USE_MLX:
                cmd.extend(["--backend", "mlx-whisper"])

        prompt = read_prompt()
        if prompt:
            cmd.extend(["--init-prompt", prompt])

        popen_kwargs: dict[str, Any] = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            # On Windows the default text-mode codec is cp1252; non-ASCII
            # output from WhisperLiveKit (e.g. a Norwegian transcript
            # snippet echoed back) would raise UnicodeDecodeError and kill
            # the pump thread. errors="replace" makes that tolerable.
            errors="replace",
        )
        # On POSIX, give the child its own process group so we can SIGTERM
        # the whole tree (uvicorn → ASR worker) cleanly.
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True

        try:
            print(
                f"[tapscribe] starting whisperlivekit-server: model={LIVE_CONFIG['model']} "
                f"lang={LIVE_CONFIG['language']} mlx={config.USE_MLX}",
                flush=True,
            )
            LIVE_PROC = subprocess.Popen(cmd, **popen_kwargs)
        except Exception as e:
            LIVE_PROC = None
            LIVE_INFO["state"] = "error"
            LIVE_INFO["last_error"] = f"spawn failed: {e}"
            return False, LIVE_INFO["last_error"]

        LIVE_INFO["model"] = str(LIVE_CONFIG["model"])
        LIVE_INFO["language"] = str(LIVE_CONFIG["language"])
        LIVE_INFO["host"] = str(LIVE_CONFIG["host"])
        LIVE_INFO["port"] = str(LIVE_CONFIG["port"])
        LIVE_INFO["backend"] = "mlx-whisper" if config.USE_MLX else "faster-whisper"
        LIVE_INFO["device"] = "Apple Silicon GPU" if config.USE_MLX else "CPU"
        LIVE_INFO["vac"] = "on" if LIVE_CONFIG.get("vac") else "off"
        LIVE_INFO["confidence_validation"] = "on" if LIVE_CONFIG.get("confidence_validation") else "off"
        LIVE_INFO["state"] = "starting"
        LIVE_INFO["last_error"] = ""
        LIVE_INFO["pid"] = str(LIVE_PROC.pid)
        LIVE_INFO["started_at"] = datetime.now(timezone.utc).isoformat()
        LIVE_LOG.clear()

        threading.Thread(target=_pump_live_logs, args=(LIVE_PROC,), daemon=True).start()
        return True, f"started pid {LIVE_PROC.pid}"


def _pump_live_logs(proc: subprocess.Popen) -> None:
    """Drain the child's stdout into LIVE_LOG (tail) and the recorder's
    stdout (prefixed). Promote 'starting' → 'running' on uvicorn-startup
    signal. On exit, mark 'stopped' or 'error' and capture a tail of the
    last lines."""
    promoted = False
    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                ln = line.rstrip("\n")
                LIVE_LOG.append(ln)
                print(f"[wlk] {ln}", flush=True)
                if not promoted:
                    low = ln.lower()
                    if "uvicorn running" in low or "application startup complete" in low:
                        LIVE_INFO["state"] = "running"
                        promoted = True
    finally:
        rc = proc.wait()
        # Only update INFO if this proc is still the active one; a fresh
        # start_live_proc() may already have replaced it.
        if LIVE_PROC is proc:
            if rc == 0 or rc is None:
                LIVE_INFO["state"] = "stopped"
            else:
                LIVE_INFO["state"] = "error"
                tail = list(LIVE_LOG)[-5:]
                LIVE_INFO["last_error"] = (" | ".join(tail))[:500] or f"exited with code {rc}"
            LIVE_INFO["pid"] = ""


def stop_live_proc(timeout: float = 5.0) -> tuple[bool, str]:
    """Terminate the live child (if any). Idempotent."""
    global LIVE_PROC
    with LIVE_LOCK:
        proc = LIVE_PROC
        if proc is None:
            LIVE_INFO["state"] = "stopped"
            return True, "not running"
        if proc.poll() is not None:
            LIVE_PROC = None
            LIVE_INFO["state"] = "stopped"
            LIVE_INFO["pid"] = ""
            return True, "already exited"

    # Released the lock while we wait — log pump will see exit.
    try:
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        else:
            proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            else:
                proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
    except Exception as e:
        return False, f"stop failed: {e}"

    with LIVE_LOCK:
        if LIVE_PROC is proc:
            LIVE_PROC = None
    LIVE_INFO["state"] = "stopped"
    LIVE_INFO["pid"] = ""
    return True, "stopped"
