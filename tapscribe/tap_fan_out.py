"""TapFanOut — the per-`/tap`-WS lifecycle object.

One TapFanOut owns one Bridge utterance's worth of audio fan-out: the
WAV write, the UtteranceIndex bookkeeping, the ActiveStream row, and
(when do_live + the LiveChannel is running) the WlKRelay. The `/tap`
route just reads the WS, calls `write_frame(buf)` per PCM frame, and
relies on the async context manager to clean up on exit.

See ADR-0002 for the "Recorder fans the audio out internally" decision
this object embodies, and CONTEXT.md for the Bridge / Utterance / Drain
vocabulary used throughout.
"""

from __future__ import annotations

import asyncio
import time
import wave
from contextlib import suppress
from datetime import datetime, timezone

from .audio import int16_peak_norm, open_recorder_wav
from .live_relay import WlKRelay
from .recorder import ActiveStream, Recorder, UtteranceRecord
from .speech_gate import SpeechGate, build_gate_for_config
from .text import build_recorder_wav_name, clean_meta_tokens, safe_name

# Per-frame decay factor for the volume-meter peak hold. Frames are 20 ms
# (640 bytes @ 16 kHz mono int16); 0.92 per frame gives a ~165 ms half-life
# — long enough that the meter doesn't flicker between syllables, short
# enough that it falls back to silence within a few hundred ms of speech
# ending. Tuned visually rather than from first principles.
LEVEL_DECAY_PER_FRAME: float = 0.92

# Minimum seconds between in-fan-out relay reconnect attempts. The relay
# can die mid-utterance for two reasons we want to recover from
# transparently — without forcing the bridge to drop and re-open /tap:
#   1. WhisperLiveKit child crashed.
#   2. Operator clicked Apply (restart) on the dashboard to swap the
#      model / language; the recorder stopped the old child and started
#      a new one (possibly with a different config).
# At 20 ms frames that's 50 candidate reconnect points per second per
# stream — without backoff we'd hammer a still-starting WlK. One second
# leaves a small audio gap during restart but is responsive enough that
# the operator sees captions resume within ~1 cycle past WlK's ready
# time. Lowered to ~0 in tests to keep the suite quick.
RELAY_RECONNECT_BACKOFF_S: float = 1.0


class TapFanOut:
    """One open `/tap` WebSocket worth of fan-out state. Built by
    `TapFanOut.open(...)`; used as an async context manager."""

    def __init__(
        self,
        recorder: Recorder,
        *,
        identity: str,
        name: str,
        utterance_id: str,
        do_record: bool,
        do_live: bool,
    ) -> None:
        self._recorder = recorder
        self._identity = identity
        self._name = name
        self._utterance_id = utterance_id
        self._do_record = do_record
        self._do_live = do_live
        self._wf: wave.Wave_write | None = None
        self._record: UtteranceRecord | None = None
        self._conn_id: str = ""
        self._bytes_received: int = 0
        self._relay: WlKRelay | None = None
        self._relay_alive: bool = False
        # Per-tap SpeechGate (Silero-backed). Built in `_open` from the
        # recorder's current LiveConfig; None when gate_kind="backend"
        # (in which case PCM goes straight to the relay and the
        # backend's own VAD does the gating). Tests override
        # `tapscribe.tap_fan_out.build_gate_for_config` to inject a
        # deterministic fake VAD without loading Silero.
        self._gate: SpeechGate | None = None
        # Mirror of `self._gate.is_open` from the last frame, so
        # write_frame can detect transitions and only push to the
        # ActiveStream when the value actually changed (avoids a lock
        # acquire on every 20 ms frame).
        self._gate_open_last: bool = False
        # Backoff bookkeeping for transparent relay reconnection across
        # WhisperLiveKit restarts (model swap, child crash). The task
        # handle lets _close cancel an in-flight attempt cleanly; the
        # monotonic timestamp + attempt counter implement the backoff.
        # `_relay_reconnect_attempts` is also read by tests as a way to
        # verify the backoff actually coalesces bursts of frames into a
        # single connect attempt.
        self._relay_reconnect_task: asyncio.Task | None = None
        self._relay_last_attempt_at: float = 0.0
        self._relay_reconnect_attempts: int = 0
        # Peak-hold for the dashboard's per-tap volume meter, decayed
        # per frame (see LEVEL_DECAY_PER_FRAME). Mirrored onto the
        # ActiveStream row via update_bytes so /api/state surfaces it.
        self._level: float = 0.0

    @classmethod
    async def open(
        cls,
        recorder: Recorder,
        *,
        identity: str,
        name: str,
        utterance_id: str,
        do_record: bool,
        do_live: bool,
    ) -> TapFanOut:
        self = cls(
            recorder,
            identity=identity,
            name=name,
            utterance_id=utterance_id,
            do_record=do_record,
            do_live=do_live,
        )
        await self._open()
        return self

    async def __aenter__(self) -> TapFanOut:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._close()

    async def write_frame(self, buf: bytes) -> None:
        # Update the volume-meter peak BEFORE the WAV / relay sends so a
        # single update_bytes call carries both the new byte count and
        # the fresh level — one lock acquire instead of two. We update
        # the meter regardless of `do_record` because the dashboard
        # should still show a live "audio coming in" indicator for taps
        # that the operator chose not to persist to disk.
        peak = int16_peak_norm(buf)
        # Peak-hold-with-decay: keep the louder of (this frame's peak,
        # the previous level decayed by one frame). max() — not a
        # strict `>` test — so a steady tone whose per-frame peak
        # equals the prior level stays pinned at that level instead
        # of leaking down by `LEVEL_DECAY_PER_FRAME` every frame.
        decayed = self._level * LEVEL_DECAY_PER_FRAME
        self._level = peak if peak > decayed else decayed
        if self._wf is not None:
            self._wf.writeframes(buf)
            self._bytes_received += len(buf)
        await self._recorder.streams.update_bytes(
            self._conn_id,
            self._bytes_received,
            level=self._level,
        )
        # TapScribe-side speech gate (Silero VAD + pre-roll). When the
        # gate is present (gate_kind="tapscribe") it filters silence
        # before bytes reach WlK — recovering leading consonants via
        # pre-roll, sparing the shared model from idle-time decoder
        # work. When absent (gate_kind="backend"), bytes flow straight
        # through and the backend's own VAD does the gating.
        if self._gate is not None:
            frames_to_send = self._gate.feed(buf)
            # Surface gate transitions to the dashboard. Skip the lock
            # acquire when the value hasn't changed — otherwise we'd
            # hit the ActiveStreams mutex 50× per second per /tap.
            current_open = self._gate.is_open
            if current_open != self._gate_open_last:
                self._gate_open_last = current_open
                await self._recorder.streams.update_gate_open(self._conn_id, current_open)
        else:
            frames_to_send = (buf,)

        if not frames_to_send:
            return

        # Best-effort relay with transparent reconnect across WhisperLiveKit
        # restarts. If the relay is alive, forward the frame(s). If it
        # died (operator clicked Apply (restart) on the dashboard, or the
        # WlK child crashed) but LiveChannel is back up, schedule a rebuild
        # so frames start flowing again without the bridge having to
        # drop and re-open /tap. Recording to disk continues unaffected
        # regardless — per ADR-0002 graceful degradation.
        for frame in frames_to_send:
            if self._relay_alive:
                assert self._relay is not None
                if not await self._relay.send(frame):
                    self._relay_alive = False
                    break
            elif self._do_live and self._recorder.live.running():
                self._maybe_schedule_relay_reconnect()
                break

    # ------------------------------------------------------------------
    # Lifecycle internals
    # ------------------------------------------------------------------

    async def _open(self) -> None:
        started_at = datetime.now(timezone.utc)
        fname = "(record off)"

        if self._do_record:
            resumed = self._recorder.utterances.try_resume(
                self._utterance_id,
                identity=self._identity,
                session_dir=self._recorder.session_dir,
            )
            if resumed is not None:
                # Bridge reconnected within the resume window with the
                # same utterance_id. Append to the existing WAV; preserve
                # bytes_received and started_at so the merged file looks
                # like one continuous utterance. `wave` has no append
                # mode, so we read the existing frames and rewrite them
                # through the canonical header so the resulting file
                # stays structurally valid before AND after the resumed
                # segment is appended.
                if self._name and self._name != resumed.name:
                    resumed.name = self._name
                with wave.open(str(resumed.path), "rb") as r:
                    existing = r.readframes(r.getnframes())
                wf = open_recorder_wav(resumed.path)
                if existing:
                    wf.writeframes(existing)
                self._wf = wf
                self._record = resumed
                self._bytes_received = resumed.bytes_received
                started_at = resumed.started_at
                fname = resumed.filename
                print(
                    f"[tapscribe] /tap resume -> {fname} (prior {self._bytes_received} bytes)",
                    flush=True,
                )
            else:
                # `safe_name` + `[:10]` cap the identity slug; the helper
                # applies its own `safe_name` and empty-string fallbacks so
                # we don't repeat them here.
                short_id = safe_name(self._identity)[:10]
                fname = build_recorder_wav_name(started_at, self._name or "", short_id)
                session_dir = self._recorder.session_dir
                session_dir.mkdir(parents=True, exist_ok=True)
                fpath = session_dir / fname
                record = UtteranceRecord(
                    utterance_id=self._utterance_id,
                    identity=self._identity,
                    name=self._name,
                    filename=fname,
                    path=fpath,
                    started_at=started_at,
                )
                self._recorder.utterances.register_new(record)
                self._wf = open_recorder_wav(fpath)
                self._record = record
                print(f"[tapscribe] /tap open -> {fname}", flush=True)
        else:
            print(f"[tapscribe] /tap open (record off) for {self._identity}", flush=True)

        self._conn_id = self._utterance_id[:8] + "-" + (safe_name(self._identity)[:10] or "unknown")
        await self._recorder.streams.register(
            ActiveStream(
                conn_id=self._conn_id,
                identity=self._identity,
                name=self._name,
                filename=fname,
                started_at=started_at,
                # On resume, self._bytes_received already carries the prior
                # utterance's byte count — register it that way so the
                # dashboard's counter doesn't visibly drop to zero between
                # the WS reopen and the first new frame's update_bytes call.
                # Fresh (non-resumed) utterances have it at the default 0.
                bytes_received=self._bytes_received,
                record=self._do_record,
                live=self._do_live,
            )
        )

        if self._do_live and self._recorder.live.running():
            candidate = WlKRelay(
                host=self._recorder.live.config.host,
                port=self._recorder.live.config.port,
                language=self._recorder.live.config.language,
                on_settled_line=self._on_settled_line,
                on_metrics=self._on_metrics,
                on_buffer=self._on_buffer,
            )
            if await candidate.connect():
                self._relay = candidate
                self._relay_alive = True
            # Build the TapScribe-side gate once we know live is going
            # to be wired. build_gate_for_config returns None when
            # gate_kind="backend", which the write_frame fast-path
            # checks for before invoking. Gate failures (Silero load
            # error, etc.) shouldn't kill the tap — log and fall back
            # to passthrough so the bridge doesn't see a dropped /tap.
            try:
                self._gate = build_gate_for_config(self._recorder.live.config)
            except Exception as e:
                print(
                    f"[tapscribe] /tap gate construction failed for "
                    f"{self._identity}: {e}; falling back to passthrough",
                    flush=True,
                )
                self._gate = None

    async def _on_metrics(self, lag_s: float) -> None:
        """Push the relay's latest reported lag to this tap's row so the
        dashboard can render a per-tap backlog indicator."""
        await self._recorder.streams.update_lag(self._conn_id, lag_s)

    def _on_buffer(self, text: str) -> None:
        """Forward the relay's latest `buffer_transcription` to the
        active stream so the dashboard's per-tap in-flight indicator
        can render it. Sync (not awaitable) because the relay invokes
        it from inside its consumer task; we hop onto the loop via
        `create_task` to avoid coupling consumer latency to the
        ActiveStreams lock acquisition."""
        loop = asyncio.get_event_loop()
        loop.create_task(self._recorder.streams.update_buffer_transcription(self._conn_id, text))

    def _maybe_schedule_relay_reconnect(self) -> None:
        """Kick off a background relay reconnect if none is pending and
        we're outside the backoff window. Synchronous (no await) so
        write_frame keeps moving — the actual connect happens in a task
        so a slow / unreachable WlK can't stall the frame stream."""
        if self._relay_reconnect_task is not None and not self._relay_reconnect_task.done():
            return
        now = time.monotonic()
        if now - self._relay_last_attempt_at < RELAY_RECONNECT_BACKOFF_S:
            return
        self._relay_last_attempt_at = now
        self._relay_reconnect_attempts += 1
        self._relay_reconnect_task = asyncio.create_task(self._reconnect_relay())

    async def _reconnect_relay(self) -> None:
        """Rebuild the WlK relay using the recorder's CURRENT live
        config — so a model / language / port change applied via the
        dashboard takes effect for already-open /tap WebSockets too.

        The stale relay is closed first; failures there are swallowed
        because the connection is already known-dead. A connect failure
        on the new attempt just leaves _relay_alive=False; the backoff
        guard in _maybe_schedule_relay_reconnect rate-limits retries
        until WlK comes up."""
        stale = self._relay
        self._relay = None
        if stale is not None:
            with suppress(Exception):
                await stale.close()
        cfg = self._recorder.live.config
        candidate = WlKRelay(
            host=cfg.host,
            port=cfg.port,
            language=cfg.language,
            on_settled_line=self._on_settled_line,
            on_metrics=self._on_metrics,
            on_buffer=self._on_buffer,
        )
        if await candidate.connect():
            self._relay = candidate
            self._relay_alive = True
            # The operator may have flipped gate_kind via Apply (restart),
            # so rebuild the gate too. Keeps the active /tap honest with
            # the recorder's current LiveConfig instead of remembering
            # the gate it was opened with.
            try:
                self._gate = build_gate_for_config(cfg)
            except Exception as e:
                print(
                    f"[tapscribe] /tap gate rebuild failed for "
                    f"{self._identity}: {e}; falling back to passthrough",
                    flush=True,
                )
                self._gate = None
            print(
                f"[tapscribe] /tap relay reconnected for {self._identity} "
                f"-> {cfg.host}:{cfg.port} (model={cfg.model}, lang={cfg.language})",
                flush=True,
            )

    def _on_settled_line(self, text: str) -> None:
        """Settled-line consumer for the WlKRelay. Cleans Whisper
        meta-tokens (e.g. `<|nospeech|>`), drops letterless residues,
        then appends to LiveTranscripts attributed to this fan-out's
        identity/name and current session."""
        cleaned = clean_meta_tokens(text)
        if not cleaned or not any(c.isalpha() for c in cleaned):
            return
        self._recorder.transcripts.append(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "identity": self._identity,
                "name": self._name,
                "text": cleaned,
                "session": self._recorder.session_start,
            }
        )

    async def _close(self) -> None:
        # Sync cleanup first: these must run even if the surrounding task
        # is being cancelled (TestClient does that on WS exit). Async
        # awaits below may raise CancelledError and skip remaining work.
        if self._wf is not None and self._record is not None:
            self._wf.close()
            kept = self._bytes_received > 0
            if not kept:
                with suppress(OSError):
                    self._record.path.unlink()
                print(
                    f"[tapscribe] /tap closed (empty), removed {self._record.filename}",
                    flush=True,
                )
            else:
                dur = self._bytes_received / 32000.0
                print(
                    f"[tapscribe] /tap closed, wrote {self._bytes_received} bytes "
                    f"({dur:.2f}s) to {self._record.filename}",
                    flush=True,
                )
            self._recorder.utterances.release(
                self._utterance_id,
                bytes_received=self._bytes_received,
                kept=kept,
            )
        else:
            print(f"[tapscribe] /tap closed (record off) for {self._identity}", flush=True)
        # Cancel any in-flight reconnect attempt before we close the
        # current relay — otherwise the task could land a fresh relay
        # right after we tried to close it, leaving a stray WS to the
        # WlK child after the fan-out has gone away.
        if self._relay_reconnect_task is not None and not self._relay_reconnect_task.done():
            self._relay_reconnect_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._relay_reconnect_task
        self._relay_reconnect_task = None
        if self._relay is not None:
            await self._relay.close()  # drains tail captions per Q2
        await self._recorder.streams.remove(self._conn_id)
