"""Live-transcription channel — protocol, shared base, WhisperLiveKit engine.

The Recorder holds one `LiveChannel` (the Protocol). Two concrete
implementations ship today, both inheriting the shared transition surface
from `LiveChannelBase` (ADR-0014):

  * `WhisperLiveKitChannel` (this module) — encapsulates the
    subprocess.Popen handle, the threading lock guarding spawn/kill, the
    log-pump tail, and the human-readable state dict the dashboard
    displays.
  * `MoonshineLiveChannel` (`moonshine_live.py`) — no subprocess at all:
    an in-process `/asr` WebSocket server on its own thread + loop.

A third engine (e.g. a `ParakeetLiveChannel` wrapping `parakeet-mlx` in a
rolling-chunk pseudo-streaming loop) is a one-file addition with no
Recorder change: inherit `LiveChannelBase` for the shared surface, or
implement the Protocol directly. The swap-and-restart orchestration lives
in `live_control.plan_live` / `apply_live`, not in the channels.

`build_live_cmd` is the pure argv builder for WhisperLiveKit (testable
as data); the class wires the surrounding orchestration (find the exe,
download NB-Whisper weights, spawn, drain stdout, update INFO).

`LiveSnapshot` is one tick's read of a channel for `/api/state` — here
rather than in `state_view` because reading a channel safely is this
module's business: the `info` copy, the `TailLog` tail bounded by
`LOG_PREVIEW_LINES`, and `supports_native_vad`'s safe-in-absence default.
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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, get_args, runtime_checkable

from .nb_whisper import download_nb_whisper_ct2_dir
from .speech_gate import effective_gate_config
from .text import read_config

# Decimals the dashboard mirrors the gate speech threshold at. Two live uses:
# `_mirror_gate_info` renders the threshold at this precision for /api/state,
# and `apply_gate_knobs` round-compares a re-POSTed value at the SAME precision
# so a >2-decimal config (e.g. 0.567, shown as "0.57") is not quantized down to
# the display-rounded re-submit — the #238 display-precision guarantee.
GATE_THRESHOLD_DECIMALS = 2

# The valid `gate_kind` values — which layer runs speech gating (see
# `LiveConfig.gate_kind` for what each means). `GateKind` is the ONE
# authority: the runtime allowlist tuple is derived from the Literal via
# `get_args`, so the type checker's view and every membership check
# (`_transition_replacements` for both channels' `begin_transition`, and
# `/api/live/start`'s boundary check in `tapscribe.app`) can't drift.
GateKind = Literal["tapscribe", "backend"]
GATE_KINDS: tuple[str, ...] = get_args(GateKind)


def gate_kind_error(got: object) -> str:
    """The single wording for a `gate_kind` outside `GATE_KINDS` — used by
    `_transition_replacements`' ValueError and imported by
    `tapscribe.app`'s `/api/live/start` 400, so the HTTP boundary and the
    channels' own validation can't drift apart."""
    return f"gate_kind must be {' or '.join(repr(k) for k in GATE_KINDS)}, got {got!r}"


class TailLog(deque):
    """A bounded log deque whose mutation AND iteration are serialised by
    one internal lock — the `log` attribute type both live channels use.

    Why: the channel's pump thread appends log lines from a plain
    `threading.Thread` while `/api/state` (`LiveSnapshot.capture`, which
    tails one snapshot) and `/api/live/log` (`list(log)`) iterate the same
    deque on the event loop. A CPython deque iterator raises
    `RuntimeError("deque mutated during iteration")` when an append lands
    between iterator creation and consumption, 500ing the poll.

    Marshalling appends onto the event loop
    (`loop.call_soon_threadsafe(log.append, line)`) is structurally
    awkward here: `start()` runs on a `asyncio.to_thread` worker (or the
    pre-loop boot path), so there is no running loop to capture at the
    writer's spawn point. Locking inside the log type instead makes the
    guarantee structural: `__iter__` snapshots under the lock, so EVERY
    reader that iterates (`list()`, `islice()`, a `for` loop — including
    tests appending from foreign threads) is safe with no bespoke copies
    or locking at any call site. Readers pay one O(`LOG_TAIL_LINES`) copy.

    Only the operations the channels use are overridden (`append`,
    `clear`, `__iter__`); give any other mutator the same lock treatment
    before calling it from a second thread.
    """

    def __init__(self, maxlen: int | None = None) -> None:
        super().__init__(maxlen=maxlen)
        self._lock = threading.Lock()

    def append(self, item) -> None:
        with self._lock:
            super().append(item)

    def clear(self) -> None:
        with self._lock:
            super().clear()

    def __iter__(self):
        with self._lock:
            snapshot = list(super().__iter__())
        return iter(snapshot)


#: How many lines a channel's `TailLog` keeps — the bound `/api/live/log` serves
#: in full. Named rather than repeated as a literal in each channel's `__init__`,
#: so it is one thing the preview below can be a preview OF.
LOG_TAIL_LINES = 200

#: How many log lines `/api/state` ships per poll tick. The dashboard's log
#: dialog fetches the whole `LOG_TAIL_LINES` on demand via `/api/live/log`, so
#: the poll needs only the live-channel card's inline preview. Declared beside
#: the bound so the two cannot drift into "preview larger than the log". Must
#: stay > 0: a slice of `[-0:]` is the WHOLE list, so a "0 means off" reading
#: would ship every retained line per tick rather than none.
LOG_PREVIEW_LINES = 30


@dataclass(frozen=True)
class LiveSnapshot:
    """One tick's read of a `LiveChannel`, as plain data.

    `/api/state` renders the live-channel card from three attributes the channel
    mutates from its own pump thread, and the payload is assembled on a worker
    thread (`state_view.build_state_blob`). Handing that worker the CHANNEL would
    mean serialising a dict and a deque a `threading.Thread` is appending to;
    handing it this means every read already happened, once, on the event loop.

    It lives here rather than in `state_view` because reading a channel safely is
    this module's business — the `info` copy, the `TailLog` tail, and
    `supports_native_vad`'s safe-in-absence default. The route's previous
    hand-marshalling proved the point by going stale: its "islice walks straight
    to the tail without copying the deque" comment was written against a bare
    deque, and `TailLog.__iter__` copies the whole thing under its lock.

    `live_feed` is deliberately NOT here: those settled lines come from the
    Recorder's `LiveTranscripts`, not from the channel, so a snapshot carrying
    them would have two owners and could not be captured in one call.

    Frozen for the declaration, not for deep immutability: `info` and `log` are
    ordinary mutable containers (and `frozen=True` synthesises an `__hash__` that
    raises on them), so this is a record of one instant, not a cache key.
    """

    info: dict[str, str]
    log: list[str]
    supports_native_vad: bool

    @classmethod
    def capture(cls, channel: Any) -> LiveSnapshot:
        """Read `channel` once, for one poll tick.

        The tail comes off ONE snapshot and is capped by a slice, not by an index
        computed from a separately-read `len()`: `TailLog` serialises each read
        individually, so a pair of them could disagree while the pump thread
        appends. (Atomicity is the log type's to give — a channel whose `log` is a
        bare `deque` rather than a `TailLog` is exposed to
        `RuntimeError: deque mutated during iteration` here exactly as it was
        before.)

        `supports_native_vad` is read with `getattr`, not as the plain attribute
        the `LiveChannel` Protocol declares, for the same reason
        `speech_gate.effective_gate_config` does: this runs on the ~2 Hz poll, and
        a channel that never declared the flag must degrade to "no native VAD"
        rather than 500 the dashboard. `LiveChannelBase` deliberately leaves the
        declaration to each subclass (its safe default is the opposite of
        Whisper's), so a non-declaring channel is a shape the codebase permits.
        `live_control.plan_live` reads it strictly instead — it genuinely requires
        a conforming channel.
        """
        return cls(
            info=dict(channel.info),
            log=list(channel.log)[-LOG_PREVIEW_LINES:],
            supports_native_vad=bool(getattr(channel, "supports_native_vad", False)),
        )


def resolve_live_init_prompt() -> str | None:
    """Read the live-channel init prompt (config/live-prompt.txt) and
    coerce the empty case to None so build_live_cmd omits --init-prompt.

    Wraps the read in a tiny helper so the substitution is testable
    without spinning up a subprocess."""
    return read_config("live-prompt") or None


@runtime_checkable
class LiveChannel(Protocol):
    """The interface every live-transcription engine satisfies.

    Concrete implementations: `WhisperLiveKitChannel` (this module) and
    `MoonshineLiveChannel` (`moonshine_live.py`), both inheriting the
    shared transition machinery from `LiveChannelBase`. The Recorder
    consumes this Protocol (not a concrete class) so a future
    `ParakeetLiveChannel` slots in as a drop-in — inheriting the base for
    the shared surface, or implementing the Protocol directly.

    Attributes the dashboard reads via `/api/state`:
      * `info`  — dict mirrored into the response payload
      * `log`   — bounded deque of recent log lines
      * `use_mlx` — whether the engine is configured to use MLX (the
                    dashboard ribbon's "mlx available" hint)
      * `config` — LiveConfig (model, language, etc.) — replaced
                   wholesale via `begin_transition` / `start`
    """

    info: dict[str, str]
    log: deque[str]
    use_mlx: bool
    config: LiveConfig
    # True when the backend has its own native VAD that can be enabled/
    # disabled. WhisperLiveKit does (via --vac / --no-vac), so True.
    # A future Parakeet live channel that doesn't run its own native
    # VAD will set this False — the dashboard then greys out the
    # "backend" option for gate_kind, since picking it would be a no-op.
    supports_native_vad: bool

    def running(self) -> bool: ...

    def matches(
        self,
        *,
        model: str | None,
        language: str | None,
        gate_kind: str | None,
        conf: bool | None,
        # Recorder-side gate knobs (#224): accepted-but-ignored by every
        # implementation — a differing knob never forces a restart. Declared
        # here so the Protocol signature matches what callers actually pass
        # (`@runtime_checkable` only checks method names, not signatures).
        gate_speech_threshold: float | None = None,
        gate_hangover_ms: int | None = None,
        gate_pre_roll_ms: int | None = None,
        gate_min_speech_ms: int | None = None,
    ) -> bool: ...

    def begin_transition(
        self,
        *,
        model: str | None = None,
        language: str | None = None,
        gate_kind: str | None = None,
        conf: bool | None = None,
        gate_speech_threshold: float | None = None,
        gate_hangover_ms: int | None = None,
        gate_pre_roll_ms: int | None = None,
        gate_min_speech_ms: int | None = None,
    ) -> None: ...

    def apply_gate_knobs(
        self,
        *,
        gate_speech_threshold: float | None = None,
        gate_hangover_ms: int | None = None,
        gate_pre_roll_ms: int | None = None,
        gate_min_speech_ms: int | None = None,
    ) -> None: ...

    def start(self, *, model: str | None = None, language: str | None = None) -> tuple[bool, str]: ...

    def stop(self, *, timeout: float = 5.0) -> tuple[bool, str]: ...


@dataclass(frozen=True)
class LiveConfig:
    """Immutable configuration for one whisperlivekit-server invocation.

    Mutation of the live channel's config goes through replacing the
    whole value, not poking at fields — the LiveChannel.config attribute
    is swapped wholesale by `start()` when called with overrides.

    The streaming-knob fields below default to `None`: when unset we
    omit the corresponding WLK flag and let WLK's own default apply.
    Operators trade latency for accuracy by setting them — the per-tap
    `lag_s` reported by the relay tells them when a setting has pushed
    the machine past keep-up.
    """

    model: str
    language: str
    host: str
    port: int
    # Which layer runs speech gating.
    #   "tapscribe" → TapScribe's own SpeechGate (Silero) sits in front
    #                 of the relay; WlK runs with --no-vac. Recovers
    #                 leading consonants via pre-roll and is backend-
    #                 agnostic (a future Parakeet live channel plugs
    #                 into the same gate). Default.
    #   "backend"  → defer to the backend's native VAD (--vac on for WlK).
    #                 No pre-roll, no leading-word recovery — kept as an
    #                 escape hatch for A/B comparison and for backends
    #                 whose native VAD is good enough.
    # Runtime validation checks membership against GATE_KINDS, which is
    # derived from this same GateKind Literal — one authority, no drift.
    gate_kind: GateKind = "tapscribe"
    # Operator-tunable thresholds for the TapScribe gate. Consumed by
    # SpeechGate (NOT by build_live_cmd / WlK).
    gate_speech_threshold: float = 0.5  # Silero speech probability gate
    gate_hangover_ms: int = 400  # post-speech silence before close
    gate_pre_roll_ms: int = 300  # ring buffer flushed on open
    # Minimum confirmed-speech window (ms) before the gate emits any
    # audio. 0 = open instantly on the VAD's first "start" event.
    # Higher values suppress brief noise blips (key taps, single
    # coughs, brief bumps) that Silero would otherwise flag as a
    # one-frame "start". The gate buffers candidate frames during the
    # warm-up; if VAD says "end" before this threshold is reached, the
    # candidate is discarded silently. Silero's `VADIterator` has no
    # equivalent knob — this filter lives entirely in SpeechGate.
    gate_min_speech_ms: int = 0
    confidence_validation: bool = True
    # Forwarded to whisperlivekit-server when set — see build_live_cmd.
    min_chunk_size: float | None = None
    buffer_trimming: str | None = None  # "sentence" | "segment"
    buffer_trimming_sec: float | None = None
    max_context_tokens: int | None = None


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
    # gate_kind=="tapscribe" means our own SpeechGate is gating PCM
    # before it reaches WlK, so WlK's native VAC must be off. The
    # operator-tunable gate knobs (gate_speech_threshold, gate_*_ms)
    # are consumed by SpeechGate on the TapScribe side — they do NOT
    # appear in WlK's argv.
    if config.gate_kind == "tapscribe":
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
        # Pin the backend explicitly so it's a pure function of (model, MLX)
        # and OS-independent. Without an explicit --backend on the non-MLX
        # path, WhisperLiveKit falls back to its OWN default (now
        # SimulStreaming), which both mismatches the `info["backend"]`
        # status label below and diverges from the batch path's
        # faster-whisper. MLX boxes get mlx-whisper; everywhere else
        # (Windows AND Linux/CUDA) gets faster-whisper.
        if use_mlx:
            cmd.extend(["--backend", "mlx-whisper"])
        else:
            cmd.extend(["--backend", "faster-whisper"])

    if init_prompt:
        cmd.extend(["--init-prompt", init_prompt])

    # Streaming knobs: only emit a flag when the operator set the field, so
    # WLK's own defaults apply otherwise. Pairs (e.g. trimming strategy +
    # threshold) are independently optional — WLK accepts the strategy
    # alone and falls back to its default threshold.
    #
    # Flag names match WhisperLiveKit's CLI verbatim — note the mixed
    # dash/underscore convention (`--min-chunk-size` vs `--buffer_trimming`).
    # That's how WLK ships them; don't normalize or the child rejects argv.
    if config.min_chunk_size is not None:
        cmd.extend(["--min-chunk-size", str(config.min_chunk_size)])
    if config.buffer_trimming is not None:
        cmd.extend(["--buffer_trimming", config.buffer_trimming])
    if config.buffer_trimming_sec is not None:
        cmd.extend(["--buffer_trimming_sec", str(config.buffer_trimming_sec)])
    if config.max_context_tokens is not None:
        cmd.extend(["--max-context-tokens", str(config.max_context_tokens)])

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


def parse_accelerator_line(ln: str) -> str | None:
    """Extract the child's accelerator self-report from a WhisperLiveKit
    startup-banner line — `  Accelerator: CUDA (NVIDIA ...)` or
    `  Accelerator:  CPU only` (the banner goes to stderr, which the
    spawn merges into stdout, so the log pump sees it). Returns None for
    every other line.

    This is the ground truth the pump uses to overwrite the parent's
    seeded device PREDICTION (`_seed_device_label`): WlK exposes no
    --device flag — its faster-whisper backend hands device="auto" to
    CTranslate2 inside the child — so only the child knows what it
    actually resolved."""
    s = ln.strip()
    if not s.startswith("Accelerator:"):
        return None
    return s[len("Accelerator:") :].strip() or None


def _seed_device_label(use_mlx: bool) -> str:
    """The parent's prediction of the device the child will pick, shown
    until the child's `Accelerator:` banner is observed by the log pump.
    The non-MLX label comes from the same `available_backends()` probe
    the batch chips use; the child shares the venv, so its torch-CUDA
    visibility matches. "(auto)" flags that TapScribe isn't pinning the
    device — CTranslate2 resolves it inside the child."""
    if use_mlx:
        return "Apple Silicon GPU"
    # Late import: the probe imports torch on first call (cached after).
    from .runtime_probe import available_backends

    return "CUDA (auto)" if "cuda" in available_backends() else "CPU"


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
        # gate_kind = "tapscribe" | "backend" — which layer runs speech
        # gating. Surfaced in /api/state so the dashboard's gate-kind
        # selector reflects the active config.
        "gate_kind": "",
        "gate_speech_threshold": "",
        "gate_hangover_ms": "",
        "gate_pre_roll_ms": "",
        "gate_min_speech_ms": "",
        "confidence_validation": "",  # "on" / "off"
    }


def _gate_knob_replacements(
    *,
    gate_speech_threshold: float | None,
    gate_hangover_ms: int | None,
    gate_pre_roll_ms: int | None,
    gate_min_speech_ms: int | None,
) -> dict[str, Any]:
    """Coerce the four supplied (non-None) SpeechGate knobs into a
    `dataclasses.replace` kwargs dict for `LiveConfig`. Shared by
    `begin_transition` (restart path) and `apply_gate_knobs` (no-restart path)
    so the per-knob coercions stay in lockstep across both."""
    replacements: dict[str, Any] = {}
    if gate_speech_threshold is not None:
        replacements["gate_speech_threshold"] = float(gate_speech_threshold)
    if gate_hangover_ms is not None:
        replacements["gate_hangover_ms"] = int(gate_hangover_ms)
    if gate_pre_roll_ms is not None:
        replacements["gate_pre_roll_ms"] = int(gate_pre_roll_ms)
    if gate_min_speech_ms is not None:
        replacements["gate_min_speech_ms"] = int(gate_min_speech_ms)
    return replacements


def _changed_gate_knobs(
    config: LiveConfig,
    *,
    gate_speech_threshold: float | None,
    gate_hangover_ms: int | None,
    gate_pre_roll_ms: int | None,
    gate_min_speech_ms: int | None,
) -> dict[str, Any]:
    """The `_gate_knob_replacements` coercions filtered down to knobs whose
    value actually DIFFERS from `config` — the no-restart `apply_gate_knobs`
    diff both channels share. The dashboard pre-fills + re-POSTs every gate
    value on each Apply, so a no-op re-submit must not churn the frozen
    dataclass/info — and, for the threshold, must not quantize a >2-decimal
    stored value (0.567) down to the display-rounded re-POST (0.57). The
    threshold is compared at the dashboard's display precision
    (GATE_THRESHOLD_DECIMALS), the ints exactly — the #238 precision
    guarantee."""
    replacements = _gate_knob_replacements(
        gate_speech_threshold=gate_speech_threshold,
        gate_hangover_ms=gate_hangover_ms,
        gate_pre_roll_ms=gate_pre_roll_ms,
        gate_min_speech_ms=gate_min_speech_ms,
    )
    changed: dict[str, Any] = {}
    for field, value in replacements.items():
        current = getattr(config, field)
        if field == "gate_speech_threshold":
            if round(value, GATE_THRESHOLD_DECIMALS) == round(current, GATE_THRESHOLD_DECIMALS):
                continue
        elif value == current:
            continue
        changed[field] = value
    return changed


def _transition_replacements(
    *,
    gate_kind: str | None,
    effective_gate_kind: str,
    conf: bool | None,
    gate_speech_threshold: float | None,
    gate_hangover_ms: int | None,
    gate_pre_roll_ms: int | None,
    gate_min_speech_ms: int | None,
) -> dict[str, Any]:
    """The `dataclasses.replace` kwargs a `begin_transition` writes into
    `LiveConfig` — the gate knobs (applied unconditionally: the child
    respawns regardless, so no diff-guard, unlike `_changed_gate_knobs`)
    plus the validated `gate_kind` and the bool-coerced
    `confidence_validation`. Shared by both channels' `begin_transition`
    so the allowlist check and coercions stay in lockstep. Raises
    `ValueError` on a gate_kind outside GATE_KINDS.

    A `gate_kind` equal to `effective_gate_kind` is NOT emitted: it's the
    value `info` reports and the dashboard echoes back, i.e. no change at
    all (see `LiveChannelBase.begin_transition`). Validation runs first, so
    a bogus kind still raises."""
    replacements = _gate_knob_replacements(
        gate_speech_threshold=gate_speech_threshold,
        gate_hangover_ms=gate_hangover_ms,
        gate_pre_roll_ms=gate_pre_roll_ms,
        gate_min_speech_ms=gate_min_speech_ms,
    )
    if gate_kind is not None:
        if gate_kind not in GATE_KINDS:
            raise ValueError(gate_kind_error(gate_kind))
        if gate_kind != effective_gate_kind:
            replacements["gate_kind"] = gate_kind
    if conf is not None:
        replacements["confidence_validation"] = bool(conf)
    return replacements


class LiveChannelBase:
    """Shared implementation for every concrete `LiveChannel`.

    Owns the parts identical across engines: the frozen-`config` swap
    logic (`matches` / `apply_gate_knobs` / `begin_transition`) and the
    `info` gate-mirror. The engine-specific lifecycle — construction and
    `running` / `start` / `stop` — stays in the subclass. Two capability
    flags express the only real divergences between engines:

      * `fixed_language` — the engine transcribes exactly one language
        regardless of the requested one (Moonshine: `"en"`; `None` =
        multilingual). A language change never forces a restart
        (`matches`), and `info` reports the fixed language
        (`begin_transition`).
      * `supports_confidence_validation` — the engine has a confidence-
        validation knob (WhisperLiveKit's `--confidence-validation`). When
        False (Moonshine): `info` reports `confidence_validation` as ""
        ("not applicable") rather than a misleading on/off (keeping the
        `/api/state` payload byte-identical to the pre-base engine), AND
        `matches` ignores a `conf` change so it never forces a restart the
        engine wouldn't honour.

    `supports_native_vad` is deliberately NOT declared here: its
    safe-in-absence default is `False` (build a gate) — the OPPOSITE of
    Whisper's `True` — so it stays a required per-subclass declaration (and a
    `LiveChannel` Protocol field, since `plan_live` reads it externally).

    Subclasses MUST set `config`, `info`, and `log` in their own
    `__init__` before any gate mirror runs (see `_mirror_gate_info`).
    """

    # Divergence hooks — overridden per engine (see the class docstring).
    fixed_language: str | None = None
    supports_confidence_validation: bool = True

    # Set by the subclass constructor; declared here for readers/type-checkers.
    config: LiveConfig
    info: dict[str, str]
    log: deque[str]

    def running(self) -> bool:  # pragma: no cover - subclass owns the lifecycle
        raise NotImplementedError

    def start(  # pragma: no cover - subclass owns the lifecycle
        self, *, model: str | None = None, language: str | None = None
    ) -> tuple[bool, str]:
        raise NotImplementedError

    def stop(self, *, timeout: float = 5.0) -> tuple[bool, str]:  # pragma: no cover
        raise NotImplementedError

    def _effective_gate_kind(self) -> str:
        """The gate_kind actually in force — `config.gate_kind` coerced through
        the same `effective_gate_config` seam TapRelay builds tap gates from. The
        ONE definition `info` reports, `matches` compares against, and
        `begin_transition` treats as "not a change"."""
        return effective_gate_config(self, self.config).gate_kind

    def _mirror_gate_info(self) -> None:
        """Push the current `config`'s gate + confidence fields into `info`.
        Called from the subclass seed (boot) and `start()` (after a
        config-swap) so the dashboard never reads a stale value.

        `gate_kind` is derived through the same `effective_gate_config`
        seam TapRelay builds tap gates from, so a no-native-VAD engine
        can't report "backend" while the tap actually runs the tapscribe
        gate. `confidence_validation` is mirrored only when the engine
        has that knob (`supports_confidence_validation`)."""
        self.info["gate_kind"] = self._effective_gate_kind()
        self.info["gate_speech_threshold"] = (
            f"{self.config.gate_speech_threshold:.{GATE_THRESHOLD_DECIMALS}f}"
        )
        self.info["gate_hangover_ms"] = str(self.config.gate_hangover_ms)
        self.info["gate_pre_roll_ms"] = str(self.config.gate_pre_roll_ms)
        self.info["gate_min_speech_ms"] = str(self.config.gate_min_speech_ms)
        if self.supports_confidence_validation:
            self.info["confidence_validation"] = "on" if self.config.confidence_validation else "off"

    def apply_gate_knobs(
        self,
        *,
        gate_speech_threshold: float | None = None,
        gate_hangover_ms: int | None = None,
        gate_pre_roll_ms: int | None = None,
        gate_min_speech_ms: int | None = None,
    ) -> None:
        """Apply Recorder-side gate-knob changes to config without announcing a
        (re)start. Replaces only the non-None gate knobs on `self.config` and
        mirrors the updated gate info into `info`, leaving `info["state"]`,
        `info["last_error"]`, `info["model"]`, and `info["language"]` untouched.
        Used on the no-restart path in `live_control.apply_live`. The changed-
        only diff (and its #238 display-precision compare) lives in
        `_changed_gate_knobs`."""
        changed = _changed_gate_knobs(
            self.config,
            gate_speech_threshold=gate_speech_threshold,
            gate_hangover_ms=gate_hangover_ms,
            gate_pre_roll_ms=gate_pre_roll_ms,
            gate_min_speech_ms=gate_min_speech_ms,
        )
        if changed:
            self.config = replace(self.config, **changed)
            self._mirror_gate_info()

    def matches(
        self,
        *,
        model: str | None,
        language: str | None,
        gate_kind: str | None,
        conf: bool | None,
        gate_speech_threshold: float | None = None,
        gate_hangover_ms: int | None = None,
        gate_pre_roll_ms: int | None = None,
        gate_min_speech_ms: int | None = None,
    ) -> bool:
        """True when a running engine already satisfies the requested config.

        Compares only the engine-side config — model / language / gate_kind /
        conf: an explicitly-supplied value that differs returns False (the
        caller then restarts). `model`/`language` treat "" as "no override";
        `gate_kind`/`conf` compare only when non-None. An engine with a
        `fixed_language` (Moonshine) skips the `language` clause, and an engine
        without `supports_confidence_validation` (Moonshine) skips the `conf`
        clause — in both cases the requested value can't change what the engine
        does, so it never forces a restart (PR #334 finding #9).

        `gate_kind` is compared against the EFFECTIVE value
        (`effective_gate_config`) — the one `info` reports and the dashboard
        therefore echoes back on every Apply — not the raw `config.gate_kind`.
        On a no-native-VAD engine carrying the operator's "backend" those two
        differ, and comparing the raw value made the echo look like a change:
        a knob-only tweak forced a full engine restart. Pinned by
        `test_reported_gate_kind_echoed_back_is_a_no_op_not_a_config_rewrite`.

        The four `gate_*` kwargs are Recorder-side per #224 (they configure the
        per-tap SpeechGate, NOT the engine) and are accepted-but-IGNORED here: a
        differing gate knob does NOT force a restart — it is applied via
        `apply_gate_knobs` on the no-restart path. Pinned by
        `test_matches_ignores_gate_knob_differences`."""
        if not self.running():
            return False
        if model and model != self.config.model:
            return False
        if self.fixed_language is None and language and language != self.config.language:
            return False
        if gate_kind is not None and gate_kind != self._effective_gate_kind():
            return False
        if (
            self.supports_confidence_validation
            and conf is not None
            and conf != self.config.confidence_validation
        ):
            return False
        return True

    def begin_transition(
        self,
        *,
        model: str | None = None,
        language: str | None = None,
        gate_kind: str | None = None,
        conf: bool | None = None,
        gate_speech_threshold: float | None = None,
        gate_hangover_ms: int | None = None,
        gate_pre_roll_ms: int | None = None,
        gate_min_speech_ms: int | None = None,
    ) -> None:
        """Announce that a (re)start with the supplied overrides is about to
        happen. Replaces `config` for the supplied knobs so the next `start()`
        picks them up, and flips `info` to `state="starting"` with the new
        model/language reflected — so dashboards polling /api/state during the
        stop→start window don't see the previous selection. `start()`
        overwrites `state` again on success; this method makes the transition
        itself observable. An engine with a `fixed_language` always reports
        that language regardless of the requested one (Moonshine `"en"`, PR
        #334 finding #3 — `config.language` is carried verbatim, never
        rewritten here).

        A `gate_kind` equal to the EFFECTIVE one is likewise carried, not
        written: the dashboard echoes back the coerced value `info` reports,
        so on a no-native-VAD engine holding the operator's "backend" an
        untouched selector would otherwise overwrite that choice with
        "tapscribe" — permanently, since the config is what survives the swap
        back to WhisperLiveKit. Only a genuinely different value (one the
        request is actually asking to CHANGE) rewrites config."""
        replacements = _transition_replacements(
            gate_kind=gate_kind,
            effective_gate_kind=self._effective_gate_kind(),
            conf=conf,
            gate_speech_threshold=gate_speech_threshold,
            gate_hangover_ms=gate_hangover_ms,
            gate_pre_roll_ms=gate_pre_roll_ms,
            gate_min_speech_ms=gate_min_speech_ms,
        )
        if replacements:
            self.config = replace(self.config, **replacements)
        self.info["state"] = "starting"
        self.info["last_error"] = ""
        if model is not None:
            self.info["model"] = model
        if self.fixed_language is not None:
            self.info["language"] = self.fixed_language
        elif language is not None:
            self.info["language"] = language


class WhisperLiveKitChannel(LiveChannelBase):
    """Owns one supervised whisperlivekit-server child process.
    Concrete `LiveChannel` (Protocol) implementation backing the existing
    `whisperlivekit-server` integration.

    `info` is a dict mirrored into `/api/state` so the dashboard can
    render the live-channel panel. `log` is a 200-entry deque of the
    child's stdout tail. `config` holds the current LiveConfig; replaced
    wholesale via `start(model=..., language=...)`.
    """

    def __init__(self, *, config: LiveConfig, use_mlx: bool):
        self.config = config
        self.use_mlx = use_mlx
        # Remember whether the operator asked for an ephemeral port (port=0)
        # at construction time. After the first spawn we mutate config.port
        # to the actually-picked number — but on the NEXT start() (e.g. the
        # dashboard's stop-then-start "Apply model" flow) we want a FRESH
        # ephemeral port, not to reuse the prior one which is now sitting
        # in TIME_WAIT for ~60s. Without this flag, restarts would hit the
        # exact bug ephemeral defaults were meant to fix.
        self._ephemeral_port = config.port == 0
        self.info: dict[str, str] = _initial_info()
        # TailLog, not a bare deque: the pump thread appends while the
        # /api/state and /api/live/log routes iterate on the event loop.
        self.log: deque[str] = TailLog(maxlen=LOG_TAIL_LINES)
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        # Seed info with the boot-time config so the dashboard renders
        # something useful before the child has started.
        self.info["model"] = config.model
        self.info["language"] = config.language
        self.info["host"] = config.host
        self.info["port"] = str(config.port)
        self.info["backend"] = "mlx-whisper" if use_mlx else "faster-whisper"
        self.info["device"] = _seed_device_label(use_mlx)
        # Seed gate info too so the dashboard's selector / sliders show
        # the right values before the first start().
        self._mirror_gate_info()

    supports_native_vad: bool = True  # --vac / --no-vac flag exists

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, *, model: str | None = None, language: str | None = None) -> tuple[bool, str]:
        """Spawn whisperlivekit-server with the current (optionally overridden)
        config. Returns (ok, message). Does NOT stop a running process first —
        callers wanting 'apply' semantics call stop() then start()."""
        # Pre-resolve NB-Whisper weights BEFORE taking the spawn lock. Real
        # I/O (an HF fetch, several hundred MB on a cold cache) so it stays
        # out of the pure build_live_cmd — and out of the critical section:
        # `stop()` takes this SAME lock as its first statement, and its
        # `timeout` bounds `proc.wait()`, NOT lock acquisition. Holding the
        # lock across the download therefore wedged every shutdown for the
        # whole fetch — the ASGI lifespan calls `live.stop(timeout=3.0)`
        # inline on the event loop, so Ctrl-C/SIGTERM hung until it finished
        # (and `POST /api/live/stop` hung identically). A lock guarding
        # spawn/kill must not span unbounded network I/O.
        target_model = model if model is not None else self.config.model
        ct2_dir: Path | None = None
        if is_nb_whisper(target_model):
            try:
                ct2_dir = download_nb_whisper_ct2_dir(target_model)
            except Exception as e:
                msg = f"failed to fetch nb-whisper ct2 weights: {e}"
                with self._lock:
                    # Only stamp the failure when nothing is running: a
                    # spurious concurrent start() against a healthy child
                    # must not flip the dashboard to "error" behind its back
                    # (same ownership rule `stop()`/`_pump_logs` apply).
                    if not self.running():
                        self._proc = None
                        self.info["state"] = "error"
                        self.info["last_error"] = msg
                return False, msg

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

            if self.config.model != target_model:
                # A concurrent start() rewrote config.model while the fetch
                # above ran unlocked, so `ct2_dir` may belong to the wrong
                # model (and build_live_cmd would raise on a missing one).
                # Bail rather than spawn a mismatched child; the caller's
                # next Apply resolves weights for the settled model.
                msg = (
                    f"live model changed from {target_model!r} to {self.config.model!r} "
                    "while resolving weights — retry"
                )
                self.info["state"] = "error"
                self.info["last_error"] = msg
                return False, msg

            exe = self._find_exe()
            if exe is None:
                msg = "whisperlivekit-server not found on PATH or in the recorder's venv"
                self.info["state"] = "error"
                self.info["last_error"] = msg
                print(f"[tapscribe] {msg}", flush=True)
                return False, msg

            # Ephemeral mode: pick a NEW free port on every start, not just
            # the first one. WLK is internal — only `live_relay` connects
            # to it inside the recorder — so the port has no external
            # consumer that would care about it being stable. Re-picking
            # avoids the dashboard "Apply model" restart hitting TIME_WAIT
            # on the previous spawn's port.
            if self._ephemeral_port:
                picked = _pick_ephemeral_port(self.config.host)
                self.config = replace(self.config, port=picked)

            port_err = _probe_port_free(self.config.host, self.config.port)
            if port_err is not None:
                self.info["state"] = "error"
                self.info["last_error"] = port_err
                print(f"[tapscribe] cannot start whisperlivekit-server: {port_err}", flush=True)
                return False, port_err

            cmd = build_live_cmd(
                exe,
                self.config,
                use_mlx=self.use_mlx,
                nb_whisper_ct2_dir=ct2_dir,
                init_prompt=resolve_live_init_prompt(),
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
            self.info["device"] = _seed_device_label(self.use_mlx)
            self._mirror_gate_info()
            self.info["state"] = "starting"
            self.info["last_error"] = ""
            self.info["pid"] = str(self._proc.pid)
            self.info["started_at"] = datetime.now(UTC).isoformat()
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
                    # ProcessLookupError: the group already exited (child died
                    # between the poll() above and this signal) — nothing to
                    # terminate. PermissionError: the pgid was reused by a
                    # process we may not signal (macOS can also raise it for
                    # zombies). Either way the escalation ladder below still
                    # runs: wait() reaps a dead child, SIGKILL covers a live
                    # one, so nothing is lost by continuing.
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
                        # Same as the SIGTERM branch: the group vanished (it
                        # finally honoured SIGTERM) or the pgid isn't ours to
                        # signal. The wait() below still reaps/observes the
                        # child; suppressing here loses nothing.
                        pass
                else:
                    proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    # SIGKILLed but not reaped within 2s — the child is stuck
                    # in uninterruptible kernel state (rare). Report "stopped"
                    # anyway: SIGKILL cannot be refused, and the pump thread's
                    # blocking proc.wait() reaps the corpse whenever the
                    # kernel releases it. What's lost: the dashboard may say
                    # "stopped" a moment before the process table agrees.
                    pass
        except Exception as e:
            return False, f"stop failed: {e}"

        with self._lock:
            if self._proc is not proc:
                # A concurrent start() replaced the child while we waited for
                # the old one to die — info now describes the NEW child
                # ("starting", its pid). Stamping "stopped"/"" here would
                # clobber that fresh state; same ownership guard _pump_logs
                # applies to its exit-path info writes. Our proc IS dead, so
                # still report success.
                return True, "stopped"
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
                    # Both info writes below carry the same ownership guard
                    # the `finally` and `stop()` apply: a superseded child's
                    # stdout is block-buffered, so its banner can still be
                    # READ after a fresh start() replaced it — promoting on
                    # it would show the live channel up ~30s before the new
                    # child has bound anything. The log tail above is
                    # unguarded on purpose: it is the child's own history,
                    # not the channel's current state.
                    #
                    # The child's own accelerator report beats the seeded
                    # prediction — same observe-the-child pattern as the
                    # 'running' promotion below.
                    device = parse_accelerator_line(ln)
                    if device is not None and self._proc is proc:
                        self.info["device"] = device
                    if not promoted:
                        low = ln.lower()
                        if "uvicorn running" in low or "application startup complete" in low:
                            if self._proc is proc:
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
