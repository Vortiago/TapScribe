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
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# ActiveStreams — /record WebSocket connections currently writing WAVs
# ---------------------------------------------------------------------------

@dataclass
class ActiveStream:
    """One live /record WebSocket. Mutated only via ActiveStreams methods."""
    conn_id: str
    identity: str
    name: str
    filename: str
    started_at: datetime
    bytes_received: int = 0


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

    async def update_bytes(self, conn_id: str, bytes_received: int) -> None:
        """No-op when the conn_id is unknown — the WS handler can race
        against close and call us after the entry's been removed."""
        async with self._lock:
            existing = self._by_id.get(conn_id)
            if existing is not None:
                existing.bytes_received = bytes_received

    async def snapshot(self) -> list[ActiveStream]:
        async with self._lock:
            return [replace(s) for s in self._by_id.values()]


# ---------------------------------------------------------------------------
# JobTracker — one in-flight transcribe/strip per session at a time
# ---------------------------------------------------------------------------

@dataclass
class JobState:
    """State for an in-flight session-scoped job (transcribe or strip)."""
    session: str
    kind: Literal["transcribe", "strip"]
    current: int
    total: int
    started_at: datetime
    status: str = "running"
    current_file: str | None = None
    model: str | None = None


class JobTracker:
    """The 'one job per session at a time' rule lives here, not in each
    route handler. `claim()` returns False when the session is already
    busy; release/update modify in place."""

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
# AuthState — the dashboard's HTTP Basic password
# ---------------------------------------------------------------------------

@dataclass
class AuthState:
    """Random per first-run password persisted to disk. Rotated via
    `rotate()` (the --rotate-password CLI flag invokes that)."""
    password: str
    password_file: Path

    @classmethod
    def load_or_create(cls, password_file: Path) -> AuthState:
        try:
            if password_file.is_file():
                existing = password_file.read_text(encoding="utf-8").strip()
                if existing:
                    return cls(password=existing, password_file=password_file)
        except OSError:
            pass
        pw = secrets.token_urlsafe(12)
        try:
            password_file.write_text(pw, encoding="utf-8")
            try:
                os.chmod(password_file, 0o600)
            except (OSError, NotImplementedError):
                pass  # Windows / restricted FS — best-effort
        except OSError as e:
            print(f"[tapscribe] WARNING: could not write {password_file}: {e}", flush=True)
            print("[tapscribe]          password will rotate on next restart.", flush=True)
        return cls(password=pw, password_file=password_file)

    def rotate(self) -> None:
        try:
            self.password_file.unlink(missing_ok=True)
        except OSError:
            pass
        new = self.__class__.load_or_create(self.password_file)
        self.password = new.password


# ---------------------------------------------------------------------------
# Recorder — the running TapScribe instance
# ---------------------------------------------------------------------------

def _utc_session_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


class Recorder:
    """One per Python process. Composes the five sub-components and the
    small flags (session metadata, recording toggle, use_mlx)."""

    def __init__(
        self,
        *,
        recordings_dir: Path,
        config_dir: Path,
        live_config,  # tapscribe.live.LiveConfig; imported lazily to avoid cycles
        use_mlx: bool,
        auth_password_file: Path,
    ):
        self.recordings_dir = recordings_dir
        self.config_dir = config_dir
        self.use_mlx = use_mlx

        # Session lifecycle (simple state — single-writer, no lock needed).
        self.session_start: str = _utc_session_id()
        self.session_dir: Path = recordings_dir / self.session_start

        # Recording toggle (operator pause button).
        self.recording_enabled: bool = True

        # Composed sub-components.
        self.streams = ActiveStreams()
        self.jobs = JobTracker()
        self.transcripts = LiveTranscripts(max_entries=200)
        self.auth = AuthState.load_or_create(auth_password_file)

        # LiveChannel is constructed here too (imported lazily to break
        # what would otherwise be a circular import via tapscribe.live).
        from .live import LiveChannel
        self.live = LiveChannel(config=live_config, use_mlx=use_mlx)

    def rotate_session(self) -> tuple[str, str]:
        """Rotate to a fresh session ID. Returns (previous, current).
        Existing /record WebSockets keep writing to their original
        session_dir (captured at WS open); only new opens land in the
        new folder."""
        prev = self.session_start
        self.session_start = _utc_session_id()
        self.session_dir = self.recordings_dir / self.session_start
        return prev, self.session_start

    def toggle_recording(self, enabled: bool | None = None) -> bool:
        if enabled is None:
            self.recording_enabled = not self.recording_enabled
        else:
            self.recording_enabled = bool(enabled)
        return self.recording_enabled
