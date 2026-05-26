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
from datetime import UTC, datetime

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
        # Per-tap SpeechGate (Silero-backed). None when gate_kind=
        # "backend" — PCM then bypasses the gate and the backend's
        # own VAD handles silence. `_gate_open_last` mirrors
        # `_gate.is_open` so write_frame only writes to the
        # ActiveStream on transitions (not at the 50 Hz frame rate).
        self._gate: SpeechGate | None = None
        self._gate_open_last: bool = False
        # Strong refs to the buffer-forward tasks fired from
        # `_on_buffer` so a failure inside them surfaces immediately
        # rather than as a GC-time "Task exception was never
        # retrieved" log. Race-wise we accept latest-wins ordering
        # under heavy load — the field is a cosmetic in-flight hint.
        self._buffer_tasks: set[asyncio.Task] = set()
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
        # Recording first — we persist EVERY incoming frame regardless
        # of the gate's verdict so the WAV is a faithful record of what
        # the bridge sent (the gate is a relay-side filter, not a
        # recording filter). The level meter, by contrast, reflects
        # POST-gate audio so the operator can see the gate's effect in
        # real time: silence between speech reads as dark, confirmed
        # speech lights the bar.
        if self._wf is not None:
            self._wf.writeframes(buf)
            self._bytes_received += len(buf)
        if self._gate is not None:
            frames_to_send = self._gate.feed(buf)
            # Surface gate transitions to the dashboard. Skip the lock
            # acquire when the value hasn't changed — otherwise we'd
            # hit the ActiveStreams mutex 50× per second per /tap.
            current_open = self._gate.is_open
            if current_open != self._gate_open_last:
                self._gate_open_last = current_open
                await self._recorder.streams.update_gate_open(self._conn_id, current_open)
                if not current_open:
                    # Tap just went idle — drop any stale lag immediately so
                    # the dashboard stops showing a backlog for a speaker who
                    # stopped talking, rather than waiting for _on_metrics to
                    # next fire (and it now suppresses while closed anyway).
                    await self._recorder.streams.update_lag(self._conn_id, None)
        else:
            frames_to_send = (buf,)

        # Peak-hold-with-decay on what survived the gate. During pending
        # warm-up or pure silence, frames_to_send is empty → peak=0 →
        # the meter decays to dark within its ~165 ms half-life.
        # max() — not a strict `>` test — so a steady tone whose
        # per-frame peak equals the prior level stays pinned instead
        # of leaking down by `LEVEL_DECAY_PER_FRAME` every frame.
        if frames_to_send:
            peak = max(int16_peak_norm(f) for f in frames_to_send)
        else:
            peak = 0.0
        decayed = self._level * LEVEL_DECAY_PER_FRAME
        self._level = peak if peak > decayed else decayed
        await self._recorder.streams.update_bytes(
            self._conn_id,
            self._bytes_received,
            level=self._level,
        )

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
        started_at = datetime.now(UTC)
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
            await self._attach_relay_and_gate(self._recorder.live.config)

    async def _on_metrics(self, lag_s: float) -> None:
        """Push the relay's latest reported lag to this tap's row so the
        dashboard can render a per-tap backlog indicator.

        Suppressed while the TapScribe gate is closed: WlK keeps emitting
        `remaining_time_transcription` even after we stop feeding it, but
        that value is `wall_clock - last_processed_audio` — it climbs purely
        because time passes during the silence we're gating out, not because
        there's a real decode backlog. Reporting it would show a phantom,
        ever-growing lag for a tap whose speaker has gone quiet. When the
        gate is open (actively forwarding, hangover included) the number is
        genuine. Backend-gate mode (`self._gate is None`) feeds WlK
        continuously, so its lag stays meaningful and is always forwarded."""
        if self._gate is not None and not self._gate.is_open:
            return
        await self._recorder.streams.update_lag(self._conn_id, lag_s)

    def _on_buffer(self, text: str) -> None:
        """Forward the relay's latest `buffer_transcription` to the
        active stream so the dashboard's per-tap in-flight indicator
        can render it. The relay invokes this synchronously from its
        consumer task; we spawn a tracked task so the consumer doesn't
        block on the ActiveStreams lock and any failure surfaces
        instead of being swallowed at GC time."""
        task = asyncio.get_running_loop().create_task(
            self._recorder.streams.update_buffer_transcription(self._conn_id, text)
        )
        self._buffer_tasks.add(task)
        task.add_done_callback(self._buffer_tasks.discard)

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
        if await self._attach_relay_and_gate(cfg):
            print(
                f"[tapscribe] /tap relay reconnected for {self._identity} "
                f"-> {cfg.host}:{cfg.port} (model={cfg.model}, lang={cfg.language})",
                flush=True,
            )

    async def _attach_relay_and_gate(self, cfg) -> bool:
        """Build the WlK relay + (optional) SpeechGate against `cfg`.
        Sets `self._relay` / `self._relay_alive` / `self._gate` on
        success and returns True. Returns False if the relay fails to
        connect; the gate is only built (and only paid for) when the
        relay is actually going to be fed.

        Gate-construction failures (Silero load error, etc.) don't kill
        the tap — we log and fall through with `self._gate = None`, so
        the bridge sees passthrough rather than a dropped /tap WS."""
        candidate = WlKRelay(
            host=cfg.host,
            port=cfg.port,
            language=cfg.language,
            on_settled_line=self._on_settled_line,
            on_metrics=self._on_metrics,
            on_buffer=self._on_buffer,
        )
        if not await candidate.connect():
            return False
        self._relay = candidate
        self._relay_alive = True
        try:
            self._gate = build_gate_for_config(cfg)
        except Exception as e:
            print(
                f"[tapscribe] /tap gate construction failed for "
                f"{self._identity}: {e}; falling back to passthrough",
                flush=True,
            )
            self._gate = None
        return True

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
                "ts": datetime.now(UTC).isoformat(),
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
