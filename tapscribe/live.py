"""WhisperLiveKit child-process management — `LiveChannel` class.

The Recorder holds one `LiveChannel` instance. The class encapsulates
the subprocess.Popen handle, the threading lock guarding spawn/kill,
the log-pump tail, and the human-readable state dict the dashboard
displays. `build_live_cmd` is the pure argv builder (testable as data);
the class wires the surrounding orchestration (find the exe, download
NB-Whisper weights, spawn, drain stdout, update INFO).
"""

from __future__ import annotations

import errno
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .nb_whisper import download_nb_whisper_ct2_dir
from .text import read_prompt


@dataclass(frozen=True)
class LiveConfig:
    """Immutable configuration for one whisperlivekit-server invocation.

    Mutation of the live channel's config goes through replacing the
    whole value, not poking at fields — the LiveChannel.config attribute
    is swapped wholesale by `start()` when called with overrides.
    """

    model: str
    language: str
    host: str
    port: int
    vac: bool = True
    confidence_validation: bool = True


def is_nb_whisper(model: str) -> bool:
    """The nb-whisper-* family needs a different CLI shape — see
    build_live_cmd. Kept as a tiny predicate so the routing rule is
    grep-findable."""
    return model.startswith("nb-whisper-")


def build_live_cmd(
    exe: str,
    config: LiveConfig,
    *,
    use_mlx: bool,
    nb_whisper_ct2_dir: Path | None = None,
    init_prompt: str | None = None,
) -> list[str]:
    """Build the whisperlivekit-server argv for the given config.

    Pure — no subprocess spawn, no HuggingFace download, no log pump.
    Callers handle that orchestration; this function exists so the CLI
    surface is testable as data.

    NB-Whisper routing: WhisperLiveKit's `--model` flag only accepts
    names from its built-in table. NB-Whisper isn't there, so the
    escape hatch is `--model-path <local-ct2-dir>`. The caller must
    supply `nb_whisper_ct2_dir` for those models (download it first
    via `tapscribe.nb_whisper.download_nb_whisper_ct2_dir`); we raise
    `ValueError` rather than silently dropping `--model-path`.
    """
    cmd: list[str] = [
        exe,
        "--lan",
        config.language,
        "--host",
        config.host,
        "--port",
        str(config.port),
        "--pcm-input",
    ]
    if not config.vac:
        cmd.append("--no-vac")
    if config.confidence_validation:
        cmd.append("--confidence-validation")

    if is_nb_whisper(config.model):
        if nb_whisper_ct2_dir is None:
            raise ValueError(
                f"build_live_cmd: nb-whisper model {config.model!r} requires "
                "nb_whisper_ct2_dir to be supplied. Call "
                "tapscribe.nb_whisper.download_nb_whisper_ct2_dir first."
            )
        cmd.extend(["--model-path", str(nb_whisper_ct2_dir)])
        cmd.extend(["--backend-policy", "localagreement"])
        # Intentionally NO --backend mlx-whisper, regardless of use_mlx:
        # NB-Whisper has no public MLX weights, and forcing the flag
        # re-triggers WhisperLiveKit's strict compatibility check that
        # `--model-path` exists specifically to sidestep.
    else:
        cmd.extend(["--model", config.model])
        if use_mlx:
            cmd.extend(["--backend", "mlx-whisper"])

    if init_prompt:
        cmd.extend(["--init-prompt", init_prompt])

    return cmd


def _probe_port_free(host: str, port: int) -> str | None:
    """Bind a throwaway socket to (host, port). Return None if the port
    is free, else a human-readable diagnostic.

    Called as a fail-fast preflight before spawning whisperlivekit-server.
    Without this, an occupied port surfaces 10-30s later as a cryptic
    `[wlk] ERROR: [Errno 48] ...` after the child finally tries to bind —
    by which time bridges have already opened /tap WS connections that
    can't be transcribed live. The usual culprit is a leftover
    whisperlivekit-server from a previous crash or SIGKILL (the
    recorder's lifespan cleanup only runs on graceful shutdown).
    """
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return None
    except OSError as e:
        if e.errno in (errno.EADDRINUSE, errno.EACCES):
            if os.name == "nt":
                find_cmd = f"`netstat -ano | findstr :{port}` then `taskkill /PID <pid> /F`"
            else:
                # `lsof -i :PORT` (no LISTEN filter) catches both listeners
                # AND TIME_WAIT sockets — the latter are invisible to
                # `lsof | grep LISTEN` but still block a fresh bind for up
                # to ~60s on macOS after a previous process exited.
                find_cmd = f"`lsof -i :{port}` (shows listeners AND TIME_WAIT)"
            return (
                f"port {port} on {host} is already in use. Likely causes: "
                f"(a) a leftover whisperlivekit-server from a previous crash — find with {find_cmd} "
                f"and kill it; (b) a recently-killed process is still in TIME_WAIT — wait ~60s "
                f"and retry; (c) set SX_PORT_WLK to a free port (e.g. 8010) and restart."
            )
        return f"unable to probe live-channel port {port} on {host}: {e}"
    finally:
        s.close()


def _pick_ephemeral_port(host: str) -> int:
    """Ask the kernel for a free TCP port on `host` and immediately
    release it, returning the number. Used by LiveChannel when the
    configured port is 0 (the default) — WhisperLiveKit is an internal
    detail of the recorder (only `live_relay` connects to it; bridges
    talk to /tap on the recorder, not to WLK), so a stable well-known
    port has no value and just causes EADDRINUSE collisions with stale
    sockets from prior runs.

    Race: between this socket closing and whisperlivekit-server's
    uvicorn binding (~10-30s while the model loads), another process
    could in theory grab the port. In practice the kernel avoids
    recently-used ephemeral ports for new allocations, so the window is
    very small; if it does happen, _probe_port_free's diagnostic
    surfaces it immediately. Caller can also pin a port explicitly via
    SX_PORT_WLK / --live-port.
    """
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    try:
        s.bind((host, 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _is_console_worthy(ln: str) -> bool:
    """True for lines the operator should see in the recorder's stdout
    (warnings, errors, tracebacks). Everything else is kept in the
    in-memory deque only — the dashboard log dialog reads it from there
    via /api/live/log."""
    stripped = ln.lstrip()
    if stripped.startswith(("WARNING:", "ERROR:", "CRITICAL:", "Traceback")):
        return True
    return ":WARNING:" in ln or ":ERROR:" in ln or ":CRITICAL:" in ln


# ---------------------------------------------------------------------------
# LiveChannel — owns the whisperlivekit-server child + its supervision
# ---------------------------------------------------------------------------


def _initial_info() -> dict[str, str]:
    return {
        "model": "",
        "backend": "",  # "mlx-whisper" or "faster-whisper" or ""
        "device": "",  # human-readable
        "language": "",
        "host": "",
        "port": "",
        "state": "stopped",  # stopped | starting | running | error
        "last_error": "",
        "pid": "",
        "started_at": "",
        "vac": "",  # "on" / "off"
        "confidence_validation": "",  # "on" / "off"
    }


class LiveChannel:
    """Owns one supervised whisperlivekit-server child process.

    `info` is a dict mirrored into `/api/state` so the dashboard can
    render the live-channel panel. `log` is a 200-entry deque of the
    child's stdout tail. `config` holds the current LiveConfig; replaced
    wholesale via `start(model=..., language=...)`.
    """

    def __init__(self, *, config: LiveConfig, use_mlx: bool):
        self.config = config
        self.use_mlx = use_mlx
        self.info: dict[str, str] = _initial_info()
        self.log: deque[str] = deque(maxlen=200)
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        # Seed info with the boot-time config so the dashboard renders
        # something useful before the child has started.
        self.info["model"] = config.model
        self.info["language"] = config.language
        self.info["host"] = config.host
        self.info["port"] = str(config.port)
        self.info["backend"] = "mlx-whisper" if use_mlx else "faster-whisper"
        self.info["device"] = "Apple Silicon GPU" if use_mlx else "CPU"

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def matches(self, *, model: str | None, language: str | None, vac, conf) -> bool:
        """True when a running child already satisfies the requested config.
        `vac` and `conf` are None when the caller doesn't want to change
        them; they only require a restart when explicitly supplied."""
        return (
            self.running()
            and (not model or model == self.config.model)
            and (not language or language == self.config.language)
            and vac is None
            and conf is None
        )

    def begin_transition(
        self,
        *,
        model: str | None = None,
        language: str | None = None,
        vac: bool | None = None,
        conf: bool | None = None,
    ) -> None:
        """Announce that a (re)start with the supplied overrides is about
        to happen. Replaces `config` for vac/conf so the next `start()`
        spawns with the new values, and flips `info` to `state="starting"`
        with the new model/language reflected — so dashboards polling
        /api/state during the stop→start window don't see the previous
        selection. `start()` will overwrite `state` again on success;
        this method ensures the transition itself is observable."""
        if vac is not None or conf is not None:
            self.config = replace(
                self.config,
                vac=bool(vac) if vac is not None else self.config.vac,
                confidence_validation=bool(conf) if conf is not None else self.config.confidence_validation,
            )
        self.info["state"] = "starting"
        self.info["last_error"] = ""
        if model is not None:
            self.info["model"] = model
        if language is not None:
            self.info["language"] = language

    def start(self, *, model: str | None = None, language: str | None = None) -> tuple[bool, str]:
        """Spawn whisperlivekit-server with the current (optionally overridden)
        config. Returns (ok, message). Does NOT stop a running process first —
        callers wanting 'apply' semantics call stop() then start()."""
        with self._lock:
            if self.running():
                return False, "already running"

            # Update config with overrides (LiveConfig is frozen — replace).
            if model is not None or language is not None:
                self.config = replace(
                    self.config,
                    model=model if model is not None else self.config.model,
                    language=language if language is not None else self.config.language,
                )

            exe = self._find_exe()
            if exe is None:
                msg = "whisperlivekit-server not found on PATH or in the recorder's venv"
                self.info["state"] = "error"
                self.info["last_error"] = msg
                print(f"[tapscribe] {msg}", flush=True)
                return False, msg

            # port=0 means "pick an ephemeral port now". WLK is internal —
            # only `live_relay` connects to it inside the recorder — so a
            # stable port has no external consumer. Allocating fresh avoids
            # the most common breakage: port 8000 left in TIME_WAIT (or
            # held by a leftover WLK) after a hard kill.
            if self.config.port == 0:
                picked = _pick_ephemeral_port(self.config.host)
                self.config = replace(self.config, port=picked)

            port_err = _probe_port_free(self.config.host, self.config.port)
            if port_err is not None:
                self.info["state"] = "error"
                self.info["last_error"] = port_err
                print(f"[tapscribe] cannot start whisperlivekit-server: {port_err}", flush=True)
                return False, port_err

            # Pre-resolve NB-Whisper weights if needed. Real I/O (HF fetch)
            # so it stays out of the pure build_live_cmd.
            ct2_dir: Path | None = None
            if is_nb_whisper(self.config.model):
                try:
                    ct2_dir = download_nb_whisper_ct2_dir(self.config.model)
                except Exception as e:
                    msg = f"failed to fetch nb-whisper ct2 weights: {e}"
                    self.info["state"] = "error"
                    self.info["last_error"] = msg
                    self._proc = None
                    return False, msg

            cmd = build_live_cmd(
                exe,
                self.config,
                use_mlx=self.use_mlx,
                nb_whisper_ct2_dir=ct2_dir,
                init_prompt=read_prompt() or None,
            )

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
                    f"[tapscribe] starting whisperlivekit-server: model={self.config.model} "
                    f"lang={self.config.language} mlx={self.use_mlx}",
                    flush=True,
                )
                self._proc = subprocess.Popen(cmd, **popen_kwargs)
            except Exception as e:
                self._proc = None
                self.info["state"] = "error"
                self.info["last_error"] = f"spawn failed: {e}"
                return False, self.info["last_error"]

            self.info["model"] = self.config.model
            self.info["language"] = self.config.language
            self.info["host"] = self.config.host
            self.info["port"] = str(self.config.port)
            self.info["backend"] = "mlx-whisper" if self.use_mlx else "faster-whisper"
            self.info["device"] = "Apple Silicon GPU" if self.use_mlx else "CPU"
            self.info["vac"] = "on" if self.config.vac else "off"
            self.info["confidence_validation"] = "on" if self.config.confidence_validation else "off"
            self.info["state"] = "starting"
            self.info["last_error"] = ""
            self.info["pid"] = str(self._proc.pid)
            self.info["started_at"] = datetime.now(timezone.utc).isoformat()
            self.log.clear()

            threading.Thread(target=self._pump_logs, args=(self._proc,), daemon=True).start()
            return True, f"started pid {self._proc.pid}"

    def stop(self, *, timeout: float = 5.0) -> tuple[bool, str]:
        """Terminate the live child (if any). Idempotent."""
        with self._lock:
            proc = self._proc
            if proc is None:
                self.info["state"] = "stopped"
                return True, "not running"
            if proc.poll() is not None:
                self._proc = None
                self.info["state"] = "stopped"
                self.info["pid"] = ""
                return True, "already exited"

        # Released the lock while we wait — pump thread will see the exit.
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

        with self._lock:
            if self._proc is proc:
                self._proc = None
        self.info["state"] = "stopped"
        self.info["pid"] = ""
        return True, "stopped"

    @staticmethod
    def _find_exe() -> str | None:
        """Find whisperlivekit-server on PATH, falling back to the
        current venv's Scripts/bin directory."""
        exe = shutil.which("whisperlivekit-server")
        if exe:
            return exe
        # Fallback: if the recorder is itself running inside a venv,
        # look for whisperlivekit-server in that venv's bin/Scripts
        # directly. This works without `source .venv/bin/activate` and
        # matters when the recorder is run as `path/to/venv/python -m
        # tapscribe` (e.g. a service that doesn't activate).
        venv_bin = Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin")
        for cand in ("whisperlivekit-server.exe", "whisperlivekit-server"):
            p = venv_bin / cand
            if p.is_file():
                return str(p)
        return None

    def _pump_logs(self, proc: subprocess.Popen) -> None:
        """Drain the child's stdout into `log` (tail) and the recorder's
        own stdout (prefixed). Promote 'starting' → 'running' on the
        uvicorn-startup signal. On exit, mark 'stopped' or 'error'.

        Filters out whisperlivekit's audio_processor heartbeat
        ('internal_buffer=…s | lag=…s |') — it fires several times a
        second per stream, has no timestamp, and drowns the console.

        Only WARNING/ERROR/Traceback lines are forwarded to the recorder's
        stdout; everything else stays in the 200-line deque, exposed via
        GET /api/live/log and the dashboard's log dialog. Spawn/stop
        breadcrumbs that the operator actually needs in the console are
        printed by `start()`/`stop()` directly.
        """
        promoted = False
        try:
            if proc.stdout is not None:
                for line in proc.stdout:
                    ln = line.rstrip("\n")
                    if "whisperlivekit.audio_processor:internal_buffer=" in ln:
                        continue
                    self.log.append(ln)
                    if _is_console_worthy(ln):
                        print(f"[wlk] {ln}", flush=True)
                    if not promoted:
                        low = ln.lower()
                        if "uvicorn running" in low or "application startup complete" in low:
                            self.info["state"] = "running"
                            promoted = True
        finally:
            rc = proc.wait()
            # Only update INFO if this proc is still the active one; a fresh
            # start() may already have replaced it.
            if self._proc is proc:
                if rc == 0 or rc is None:
                    self.info["state"] = "stopped"
                else:
                    self.info["state"] = "error"
                    tail = list(self.log)[-5:]
                    self.info["last_error"] = (" | ".join(tail))[:500] or f"exited with code {rc}"
                self.info["pid"] = ""
