"""The Recorder context object — owns runtime mutable state.

One instance per Python process. FastAPI routes receive it via
`Depends(get_recorder)`, where `get_recorder` reads from
`request.app.state.recorder`. Tests construct a Recorder against a
tmpdir and override `app.state.recorder` for full isolation.

Boot-time constants (paths, AUTH_ENABLED, USE_MLX defaults, AUTH_USER,
AUTH_EXEMPT_ROUTES, SILENT_RMS_DBFS_FLOOR) stay in `tapscribe.config`.
Only state that changes during the Recorder's lifetime — session
rotation, recording toggle, live channel handle, active WebSockets,
in-flight jobs, the live transcript feed, the auth password — moves
onto the Recorder.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

# ---------------------------------------------------------------------------
# ActiveStreams — /record WebSocket connections currently writing WAVs
# ---------------------------------------------------------------------------


@dataclass
class ActiveStream:
    """One live /record WebSocket. Mutated only via ActiveStreams methods.

    `record` / `live` mirror the per-identity TapSetting that was in
    effect when this WS opened — surfaced here so the dashboard can show
    the operator what's currently happening for this tap without a
    second lookup."""

    conn_id: str
    identity: str
    name: str
    filename: str
    started_at: datetime
    bytes_received: int = 0
    record: bool = True
    live: bool = True
    # Exponentially-decayed peak amplitude of the most recent PCM frames,
    # 0.0–1.0. Drives the dashboard's per-tap volume meter — operators
    # use it to confirm sound is actually coming in from a speaker rather
    # than the WS being open with silence flowing through it. Updated by
    # `TapFanOut.write_frame` each 20 ms; decays toward zero between
    # frames so a single loud frame doesn't pin the meter.
    level: float = 0.0
    # Latest `remaining_time_transcription` (seconds) WlK reported for
    # this tap's relay. None until the first FrontData arrives or when
    # live is off; surfaced in /api/state so the dashboard can render a
    # per-row backlog indicator.
    lag_s: float | None = None
    # WlK's latest in-flight (uncommitted) hypothesis text, before
    # it commits to `lines`. Drives the dashboard's "⟳ <text>" indicator.
    buffer_transcription: str = ""
    # True while TapScribe's SpeechGate is forwarding audio (mid-burst,
    # pre-roll + hangover included). Only meaningful when gate_kind=
    # "tapscribe"; under "backend" we can't see the backend VAD's state
    # so it stays False.
    gate_open: bool = False


_ACTIVE_STREAM_FIELDS = frozenset(f.name for f in fields(ActiveStream))


class ActiveStreams:
    """Encapsulates the active-WS dict + its asyncio.Lock.

    Callers don't acquire the lock manually — they call methods that
    do it for them. This kills the "remember to acquire ACTIVE_LOCK"
    pattern that the pre-refactor /record handler depended on.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, ActiveStream] = {}
        self._lock = asyncio.Lock()

    async def register(self, stream: ActiveStream) -> None:
        async with self._lock:
            self._by_id[stream.conn_id] = stream

    async def remove(self, conn_id: str) -> None:
        async with self._lock:
            self._by_id.pop(conn_id, None)

    async def _apply(self, conn_id: str, **fields) -> None:
        """Set one or more fields on the ActiveStream with `conn_id`.
        No-op when the conn_id is unknown — the WS handler can race
        against close() and call us after the entry's been removed.

        Typo'd field names raise `AttributeError` rather than silently
        setting a phantom attribute on the dataclass."""
        for k in fields:
            if k not in _ACTIVE_STREAM_FIELDS:
                raise AttributeError(f"ActiveStream has no field {k!r}")
        async with self._lock:
            existing = self._by_id.get(conn_id)
            if existing is None:
                return
            for k, v in fields.items():
                setattr(existing, k, v)

    async def update_bytes(
        self,
        conn_id: str,
        bytes_received: int,
        *,
        level: float | None = None,
    ) -> None:
        """`level` is the per-frame volume-meter sample (0.0–1.0). Passed
        alongside the byte count so the dashboard's active-streams panel
        gets both updates from a single lock acquire instead of two.
        Missing `level` (e.g. resume path before the first new frame)
        leaves the previous level untouched."""
        fields: dict[str, Any] = {"bytes_received": bytes_received}
        if level is not None:
            fields["level"] = level
        await self._apply(conn_id, **fields)

    async def update_lag(self, conn_id: str, lag_s: float | None) -> None:
        await self._apply(conn_id, lag_s=lag_s)

    async def update_gate_open(self, conn_id: str, gate_open: bool) -> None:
        await self._apply(conn_id, gate_open=gate_open)

    async def update_buffer_transcription(self, conn_id: str, text: str) -> None:
        # Empty `text` is a real value (text just committed out of the
        # buffer), not a sentinel — clear the dashboard indicator.
        await self._apply(conn_id, buffer_transcription=text)

    async def snapshot(self) -> list[ActiveStream]:
        async with self._lock:
            return [replace(s) for s in self._by_id.values()]


# ---------------------------------------------------------------------------
# TapSettings — per-identity record / live preferences
# ---------------------------------------------------------------------------


@dataclass
class TapSetting:
    """Per-identity preferences for an incoming /tap WebSocket.

    `record` controls whether a WAV is written to disk; `live` controls
    whether PCM is relayed to the WhisperLiveKit child for live
    captioning. Both default to True. Snapshotted at WS open — toggling
    mid-utterance applies to the next /tap WS for this identity, same
    semantics as the global pause toggle."""

    record: bool = True
    live: bool = True


class TapSettings:
    """identity -> TapSetting. In-memory only (lost on restart). Not
    locked: every caller runs on the asyncio event loop and there are
    no `await`s inside any method, so the loop's cooperative scheduling
    serialises access."""

    def __init__(self) -> None:
        self._by_identity: dict[str, TapSetting] = {}

    def get(self, identity: str) -> TapSetting:
        """Return a copy so callers can't mutate the stored entry by
        accident. Unknown identities return the default (both on)."""
        existing = self._by_identity.get(identity)
        if existing is None:
            return TapSetting()
        return replace(existing)

    def set(
        self,
        identity: str,
        *,
        record: bool | None = None,
        live: bool | None = None,
    ) -> TapSetting:
        existing = self._by_identity.get(identity) or TapSetting()
        if record is not None:
            existing.record = bool(record)
        if live is not None:
            existing.live = bool(live)
        self._by_identity[identity] = existing
        return replace(existing)

    def snapshot(self) -> dict[str, TapSetting]:
        return {k: replace(v) for k, v in self._by_identity.items()}


# ---------------------------------------------------------------------------
# JobTracker — one in-flight transcribe/strip/summarize per session at a time
# ---------------------------------------------------------------------------


class SessionBusy(Exception):
    """A heavy job (transcribe / strip / summarize) is already in flight on
    this session — `JobTracker.run` couldn't claim the slot. Lives here, next
    to JobTracker, because it's a *job* concept, not a transcription one: the
    batch orchestrators that claim a slot all raise it (via `run`), none of
    them owns it. The route layer maps it to 409."""


@dataclass
class JobState:
    """State for an in-flight session-scoped job (transcribe, strip,
    summarize, or the end-of-meeting pipeline chaining all three)."""

    session: str
    kind: Literal["transcribe", "strip", "summarize", "pipeline"]
    current: int
    total: int
    started_at: datetime
    status: str = "running"
    current_file: str | None = None
    model: str | None = None
    # Which stage a `kind="pipeline"` job is in ("strip" / "transcribe" /
    # "summarize"); None for single-stage jobs.
    stage: str | None = None


class _JobHandle:
    """The handle `JobTracker.run` yields: a thin per-session progress updater
    so the body reports progress with `await job.update(current=i,
    current_file=name)` instead of re-passing the session id each time."""

    def __init__(self, tracker: JobTracker, session: str) -> None:
        self._tracker = tracker
        self._session = session

    async def update(self, **fields) -> None:
        await self._tracker.update(self._session, **fields)


class JobTracker:
    """The 'one job per session at a time' rule lives here, not in each
    route handler. `claim()` returns False when the session is already
    busy; release/update modify in place. `run()` is the high-level verb the
    batch orchestrators use — it brackets a job with claim+release so callers
    never hand-roll the ritual (or its foreign-claim guard)."""

    def __init__(self) -> None:
        self._by_session: dict[str, JobState] = {}
        self._lock = asyncio.Lock()

    async def claim(self, state: JobState) -> bool:
        async with self._lock:
            if state.session in self._by_session:
                return False
            self._by_session[state.session] = state
            return True

    async def update(self, session: str, **fields) -> None:
        async with self._lock:
            existing = self._by_session.get(session)
            if existing is None:
                return
            for k, v in fields.items():
                if hasattr(existing, k):
                    setattr(existing, k, v)

    async def release(self, session: str) -> None:
        async with self._lock:
            self._by_session.pop(session, None)

    @asynccontextmanager
    async def run(
        self,
        session: str,
        *,
        kind: Literal["transcribe", "strip", "summarize", "pipeline"],
        total: int,
        model: str | None = None,
        status: str = "running",
    ):
        """Hold the session's single job slot for the duration of the block.

        Claims on entry — raising `SessionBusy` when the session is already
        busy, in which case the block never runs and NO release happens, so a
        foreign claim is never touched (the guard is *structural*, not a
        try/finally discipline each caller re-derives). Releases on EVERY exit
        path. Yields a `_JobHandle` for in-loop progress updates.

        This is the one verb the batch orchestrators (transcribe / strip /
        summarize) use to bracket their work; it replaces the hand-rolled
        claim → `if not claimed: raise` → try/finally → release block each used
        to copy."""
        claimed = await self.claim(
            JobState(
                session=session,
                kind=kind,
                current=0,
                total=total,
                started_at=datetime.now(UTC),
                status=status,
                model=model,
            )
        )
        if not claimed:
            raise SessionBusy(f"session {session!r} already has a job in flight")
        try:
            yield _JobHandle(self, session)
        finally:
            await self.release(session)

    def handle(self, session: str) -> _JobHandle:
        """A progress handle for a claim the caller made via `claim()`
        directly — the hand-held twin of the handle `run` yields. The
        end-of-meeting pipeline uses this: it claims in the request path
        (deterministic 409) and updates/releases from its background task."""
        return _JobHandle(self, session)

    def get(self, session: str) -> JobState | None:
        """Read-only access — lock not strictly needed for a single dict
        get, and most callers just want a snapshot for /api/state."""
        return self._by_session.get(session)

    def snapshot(self) -> dict[str, JobState]:
        return dict(self._by_session)


# ---------------------------------------------------------------------------
# LiveTranscripts — bounded deque of settled lines from the Bridge
# ---------------------------------------------------------------------------


class LiveTranscripts:
    """The dashboard's 'live transcripts' panel reads from this. The
    Bridge POSTs settled WhisperLiveKit lines to /api/live-transcript;
    they accumulate here until cleared."""

    def __init__(self, max_entries: int = 200) -> None:
        self._entries: deque[dict] = deque(maxlen=max_entries)

    def append(self, entry: dict) -> None:
        self._entries.append(entry)

    def clear(self) -> None:
        self._entries.clear()

    def snapshot(self) -> list[dict]:
        return list(self._entries)


# ---------------------------------------------------------------------------
# PipelineResults — last end-of-meeting pipeline outcome per session
# ---------------------------------------------------------------------------


@dataclass
class PipelineRecord:
    """The last end-of-meeting pipeline outcome for one session. The job
    snapshot vanishes when the slot releases, so the tap poll endpoint reads
    THIS to answer done-with-summary / failed-at-stage after the run."""

    session: str
    state: str  # "running" | "done" | "failed"
    stage: str | None = None  # the failing stage when state == "failed"
    error: str | None = None
    error_kind: str | None = None  # the domain error's class name
    started_at: datetime | None = None
    finished_at: datetime | None = None


class PipelineResults:
    """session -> last PipelineRecord, in memory only and overwritten on the
    next trigger. A Recorder restart loses it by design — the persisted
    session-summary.json still answers "done" for a re-polling Bridge.

    Methods are sync for the same reason as UtteranceIndex: every caller runs
    on the single asyncio event loop with no await points inside."""

    def __init__(self) -> None:
        self._by_session: dict[str, PipelineRecord] = {}

    def begin(self, session: str) -> None:
        self._by_session[session] = PipelineRecord(
            session=session, state="running", started_at=datetime.now(UTC)
        )

    def finish_done(self, session: str) -> None:
        rec = self._by_session.get(session)
        if rec is not None:
            rec.state = "done"
            rec.finished_at = datetime.now(UTC)

    def finish_failed(self, session: str, *, stage: str, error: str, error_kind: str) -> None:
        rec = self._by_session.get(session)
        if rec is not None:
            rec.state = "failed"
            rec.stage = stage
            rec.error = error
            rec.error_kind = error_kind
            rec.finished_at = datetime.now(UTC)

    def get(self, session: str) -> PipelineRecord | None:
        return self._by_session.get(session)


# ---------------------------------------------------------------------------
# UtteranceIndex — bridge-supplied utterance_id -> WAV record, for resume
# ---------------------------------------------------------------------------


@dataclass
class UtteranceRecord:
    """One bridge utterance. The bridge keeps `utterance_id` stable across
    reconnects within a single unmuted speech segment, so a /tap WS that
    drops mid-utterance and comes back appends to the same WAV instead
    of producing a second file."""

    utterance_id: str
    identity: str
    name: str
    filename: str
    path: Path
    started_at: datetime
    bytes_received: int = 0
    open: bool = False
    last_close: datetime | None = None


class UtteranceIndex:
    """utterance_id -> UtteranceRecord. Resume window: a closed record is
    eligible for resume until it ages past RESUME_WINDOW_SECONDS, at
    which point the bridge is treated as starting a fresh utterance.

    Methods are sync: every caller runs on the single asyncio event loop
    and there are no `await` points inside any method, so the loop's
    cooperative scheduling already serialises access. That also means
    release() still completes even if the WS handler is being cancelled
    mid-cleanup (no `await` to abort on)."""

    RESUME_WINDOW_SECONDS = 60.0

    def __init__(self) -> None:
        self._by_id: dict[str, UtteranceRecord] = {}

    def try_resume(self, utterance_id: str, *, identity: str, session_dir: Path) -> UtteranceRecord | None:
        """Return the existing record marked open=True if resumable, else
        None. Caller is expected to reopen the WAV for append.

        `session_dir` is the resuming tap's snapshotted session folder
        (the global current session, or the detached session a
        /tap?session= reconnect names again). A record whose WAV lives in
        a different folder (Recorder rotated session between the original
        open and the resume attempt, or the reconnect targets another
        session) is dropped from the index and reported as not-resumable —
        appending across a session boundary would put the resumed audio in
        a session nobody is looking at.
        """
        self._prune_expired()
        rec = self._by_id.get(utterance_id)
        if rec is None or rec.open or rec.identity != identity:
            return None
        if rec.path.parent != session_dir:
            # Record belongs to a previous session. Drop it so a fresh
            # registration for the same utterance_id won't collide.
            self._by_id.pop(utterance_id, None)
            return None
        if not rec.path.exists():
            # File was deleted (e.g. operator wiped the session dir).
            self._by_id.pop(utterance_id, None)
            return None
        rec.open = True
        return rec

    def register_new(self, rec: UtteranceRecord) -> None:
        self._prune_expired()
        rec.open = True
        self._by_id[rec.utterance_id] = rec

    def release(self, utterance_id: str, *, bytes_received: int, kept: bool) -> None:
        rec = self._by_id.get(utterance_id)
        if rec is None:
            return
        if not kept:
            self._by_id.pop(utterance_id, None)
            return
        rec.open = False
        rec.bytes_received = bytes_received
        rec.last_close = datetime.now(UTC)

    def snapshot(self) -> dict[str, UtteranceRecord]:
        return dict(self._by_id)

    def _prune_expired(self) -> None:
        cutoff = datetime.now(UTC).timestamp() - self.RESUME_WINDOW_SECONDS
        stale = [
            uid
            for uid, rec in self._by_id.items()
            if not rec.open and rec.last_close is not None and rec.last_close.timestamp() < cutoff
        ]
        for uid in stale:
            self._by_id.pop(uid, None)


# ---------------------------------------------------------------------------
# SecretFile — token-shaped secret persisted to disk (auth password, tap token)
# ---------------------------------------------------------------------------


@dataclass
class SecretFile:
    """A short token-shaped secret persisted to disk. Used by the Recorder
    for both the dashboard Basic-auth password (`recorder.auth`) and the
    /tap WebSocket bearer token (`recorder.tap`). Rotated by deleting the
    file and regenerating; the --rotate-password / --rotate-tap-token
    CLI flags call `rotate()` on the corresponding instance."""

    value: str
    path: Path
    label: str  # shapes the warning printed when the FS is read-only

    @classmethod
    def load_or_create(cls, path: Path, *, label: str) -> SecretFile:
        return cls(value=_read_or_mint_secret(path, label=label), path=path, label=label)

    def rotate(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass
        self.value = _read_or_mint_secret(self.path, label=self.label)


def _read_or_mint_secret(path: Path, *, label: str) -> str:
    """Read a token-shaped secret from `path`, or generate a fresh one and
    persist it (best-effort 0600). `label` only shapes the warning printed
    when the FS is read-only."""
    try:
        if path.is_file():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
    except OSError:
        pass
    val = secrets.token_urlsafe(12)
    try:
        path.write_text(val, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except (OSError, NotImplementedError):
            pass  # Windows / restricted FS — best-effort
    except OSError as e:
        print(f"[tapscribe] WARNING: could not write {path}: {e}", flush=True)
        print(f"[tapscribe]          {label} will rotate on next restart.", flush=True)
    return val


# ---------------------------------------------------------------------------
# Recorder — the running TapScribe instance
# ---------------------------------------------------------------------------


def _utc_session_id() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")


class Recorder:
    """One per Python process. Composes the five sub-components and the
    small flags (session metadata, recording toggle, backend preference).

    `backend` is the operator's preference (`auto` / `mlx` / `cuda` /
    `cpu`); the registry-driven factory in `tapscribe.transcribers`
    resolves it per model. `use_mlx` remains as a back-compat property
    derived from `backend` so older callers / tests don't break — see
    the property below.
    """

    def __init__(
        self,
        *,
        recordings_dir: Path,
        config_dir: Path,
        live_config,  # tapscribe.live.LiveConfig; imported lazily to avoid cycles
        backend: str = "auto",
        auth_password_file: Path,
        tap_token_file: Path | None = None,
        use_mlx: bool | None = None,
    ):
        self.recordings_dir = recordings_dir
        self.config_dir = config_dir
        # Back-compat: tests and earlier callers pass `use_mlx=` directly.
        # If both are supplied, `backend` wins. Translate use_mlx into the
        # equivalent preference so the registry sees a uniform field.
        if use_mlx is not None and backend == "auto":
            backend = "mlx" if use_mlx else "cpu"
        self.backend: str = backend

        # Pre-warm the backend-detection cache. The first call imports
        # torch (~1-3 s on a CUDA box for the cuda probe) and we don't
        # want that latency happening inside a `/api/state` request
        # handler — it would make the dashboard's first poll appear to
        # hang and the e2e tests time out on otherwise-correct DOM
        # assertions.
        from .transcribers.catalog import available_backends

        available_backends()

        # Session lifecycle (simple state — single-writer, no lock needed).
        self.session_start: str = _utc_session_id()
        self.session_dir: Path = recordings_dir / self.session_start

        # Recording toggle (operator pause button).
        self.recording_enabled: bool = True

        # Composed sub-components.
        self.streams = ActiveStreams()
        self.tap_settings = TapSettings()
        self.jobs = JobTracker()
        self.pipelines = PipelineResults()
        self.transcripts = LiveTranscripts(max_entries=200)
        self.utterances = UtteranceIndex()
        self.auth = SecretFile.load_or_create(auth_password_file, label="password")
        # Default the tap-token file next to the auth-password file so the
        # two secrets live together. Tests that built a Recorder before
        # the tap-token landed continue to work without changes.
        if tap_token_file is None:
            tap_token_file = auth_password_file.with_name(".tap-token")
        self.tap = SecretFile.load_or_create(tap_token_file, label="tap token")

        # LiveChannel is constructed here too (imported lazily to break
        # what would otherwise be a circular import via tapscribe.live).
        # LiveChannel still receives a `use_mlx`-style bool because the
        # WhisperLiveKit CLI only has a binary `--backend mlx-whisper`
        # flag — the broader 4-value preference matters only for batch
        # adapters that have CUDA variants. `derived_use_mlx` is True iff
        # the operator's chosen backend resolves to MLX.
        from .live import LiveChannel, WhisperLiveKitChannel

        # `self.live` is typed as the `LiveChannel` Protocol so future
        # adapters slot in without a recorder change; today we always
        # instantiate the WhisperLiveKit-backed implementation.
        self.live: LiveChannel = WhisperLiveKitChannel(config=live_config, use_mlx=self.use_mlx)

    @property
    def use_mlx(self) -> bool:
        """Back-compat shim: True iff the chosen backend is `mlx` or
        `auto` resolving to MLX. Callers that need the full 4-value
        preference should use `self.backend` directly.

        The shim deliberately treats `auto` as "depends on the machine"
        — which lines up with the pre-refactor behaviour (`use_mlx` was
        set to `_detect_use_mlx() and not args.no_mlx`).
        """
        if self.backend == "mlx":
            return True
        if self.backend == "auto":
            try:
                from .transcribers.catalog import resolve_backend_preference

                return resolve_backend_preference("auto") == "mlx"
            except Exception:  # noqa: BLE001 — never let the shim crash a caller
                return False
        return False

    def _mint_unclaimed_session_id(self, *, avoid: str | None = None) -> str:
        """Mint a fresh session id, de-collided with a numeric suffix.
        Ids are second-resolution timestamps, so a same-second mint would
        otherwise alias an id that is already taken — either `avoid` (the
        not-necessarily-materialised current session) or any directory
        already on disk (detached sessions are created eagerly; a rotation
        that re-minted one would silently point the global current session
        at the detached dir and merge two meetings)."""
        base = _utc_session_id()
        candidate = base
        n = 2
        while candidate == avoid or (self.recordings_dir / candidate).exists():
            candidate = f"{base}-{n}"
            n += 1
        return candidate

    def rotate_session(self) -> tuple[str, str]:
        """Rotate to a fresh session ID. Returns (previous, current).
        Existing /record WebSockets keep writing to their original
        session_dir (captured at WS open); only new opens land in the
        new folder. A same-second rotation may re-mint the previous id
        when its folder never materialised (harmless: both point at the
        same lazy dir) but never an id that exists on disk."""
        prev = self.session_start
        self.session_start = self._mint_unclaimed_session_id()
        self.session_dir = self.recordings_dir / self.session_start
        return prev, self.session_start

    def create_detached_session(self) -> tuple[str, Path]:
        """Mint a fresh session directory WITHOUT touching the global
        current session — a detached session a Bridge can direct its
        taps into via /tap?session=<id>. Returns (session_id, session_dir).

        The directory is created eagerly (unlike rotate_session's lazy
        materialisation) because /tap validates ?session= through
        resolve_session_dir, which requires the dir to exist."""
        session_id = self._mint_unclaimed_session_id(avoid=self.session_start)
        session_dir = self.recordings_dir / session_id
        session_dir.mkdir(parents=True)
        return session_id, session_dir

    def toggle_recording(self, enabled: bool | None = None) -> bool:
        if enabled is None:
            self.recording_enabled = not self.recording_enabled
        else:
            self.recording_enabled = bool(enabled)
        return self.recording_enabled
