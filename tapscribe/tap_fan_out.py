"""TapFanOut — the per-`/tap`-WS lifecycle object.

One TapFanOut owns one Bridge utterance's worth of audio fan-out: the
WAV write, the UtteranceIndex bookkeeping, the ActiveStream row (level
meter + gate-open transition), and the live leg — which it holds as one
`TapRelay` (the WlK relay + gate + reconnect machinery; see
`tap_relay.py`). The `/tap` route just reads the WS, calls
`write_frame(buf)` per PCM frame, and relies on the async context
manager to clean up on exit.

See ADR-0002 for the "Recorder fans the audio out internally" decision
this object embodies, and CONTEXT.md for the Bridge / Utterance / Drain
vocabulary used throughout.
"""

from __future__ import annotations

import asyncio
import wave
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from . import roster
from .audio import int16_peak_norm, open_recorder_wav
from .recorder import ActiveStream, Recorder, UtteranceRecord
from .tap_relay import RelayHandlers, TapRelay
from .text import build_recorder_wav_name, clean_meta_tokens, safe_name
from .wav_append import open_recorder_wav_append

# Per-frame decay factor for the volume-meter peak hold. Frames are 20 ms
# (640 bytes @ 16 kHz mono int16); 0.92 per frame gives a ~165 ms half-life
# — long enough that the meter doesn't flicker between syllables, short
# enough that it falls back to silence within a few hundred ms of speech
# ending. Tuned visually rather than from first principles.
LEVEL_DECAY_PER_FRAME: float = 0.92

# Reserved identity used by bridges to verify the /tap tap-secret works.
# A probe tap must leave no durable state (no roster occurrence → no
# auto-bound Person in people.json).
PROBE_IDENTITY = "__probe__"


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
        session: str | None = None,
        session_dir: Path | None = None,
        tap_relay: TapRelay | None = None,
    ) -> None:
        self._recorder = recorder
        self._identity = identity
        self._name = name
        self._utterance_id = utterance_id
        # A fresh per-connection token. Two /tap WSes can briefly share one
        # utterance_id (a reconnect firing before the old WS is seen closed),
        # so the UtteranceIndex record and the ActiveStream row are keyed by
        # this owner — not by utterance_id alone — to keep concurrent taps
        # from clobbering each other's state.
        self._owner: str = uuid4().hex
        self._do_record = do_record
        self._do_live = do_live
        # Session affiliation — snapshotted at construction (WS open), like
        # the do_record/do_live prefs: a rotation never re-homes an open
        # tap, and a detached tap (?session=) stays in its own session for
        # both the WAV and the live-feed attribution. None → the recorder's
        # current session at open time.
        self._session: str = session if session is not None else recorder.session_start
        self._session_dir: Path = session_dir if session_dir is not None else recorder.session_dir
        self._wf: wave.Wave_write | None = None
        self._record: UtteranceRecord | None = None
        self._conn_id: str = ""
        self._bytes_received: int = 0
        # The live leg — WlK relay + per-tap SpeechGate + transparent
        # reconnect-with-backoff — lives entirely behind TapRelay (built
        # in `_open`, or injected here for tests). `_gate_open_last`
        # tracks the gate-open state TapRelay reports through `feed` so
        # write_frame pushes an ActiveStream gate-open update only on a
        # transition, not at the 50 Hz frame rate.
        self._tap_relay: TapRelay | None = tap_relay
        self._gate_open_last: bool = False
        # Strong refs to the buffer-forward tasks fired from
        # `_on_buffer` so a failure inside them surfaces immediately
        # rather than as a GC-time "Task exception was never
        # retrieved" log. Race-wise we accept latest-wins ordering
        # under heavy load — the field is a cosmetic in-flight hint.
        self._buffer_tasks: set[asyncio.Task] = set()
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
        session: str | None = None,
        session_dir: Path | None = None,
        tap_relay: TapRelay | None = None,
    ) -> TapFanOut:
        self = cls(
            recorder,
            identity=identity,
            name=name,
            utterance_id=utterance_id,
            do_record=do_record,
            do_live=do_live,
            session=session,
            session_dir=session_dir,
            tap_relay=tap_relay,
        )
        try:
            await self._open()
        except BaseException:
            # Unwind partial state, then re-raise the original failure. Suppress
            # only ordinary Exceptions from the cleanup so a _close error can't
            # mask the original — a fresh CancelledError (BaseException, not
            # Exception) still propagates rather than being silently dropped.
            with suppress(Exception):
                await self._close()
            raise
        return self

    async def __aenter__(self) -> TapFanOut:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._close()

    @property
    def relay(self) -> TapRelay:
        """This tap's live leg. Its `reconnect_attempts` / `connected`
        read-surface is what reconnect tests assert on — formerly the
        private relay fields on this object. Set in `_open`."""
        assert self._tap_relay is not None
        return self._tap_relay

    async def write_frame(self, buf: bytes) -> None:
        # Recording first — we persist EVERY incoming frame regardless
        # of the gate's verdict so the WAV is a faithful record of what
        # the bridge sent (the gate is a relay-side filter, not a
        # recording filter).
        if self._wf is not None:
            self._wf.writeframes(buf)
            self._bytes_received += len(buf)

        # The live leg: feed the frame through the gate to the relay,
        # reconnecting transparently if WlK restarted. TapRelay hands back
        # the POST-gate frames + gate-open state so the meter and the
        # ActiveStream row reflect the gate's effect in real time —
        # silence between speech reads as dark, confirmed speech lights
        # the bar. Recording to disk is unaffected if the relay is dead
        # (ADR-0002 graceful degradation).
        assert self._tap_relay is not None  # built in _open
        fed = await self._tap_relay.feed(buf)

        # Surface gate transitions to the dashboard. Skip the lock acquire
        # when the value hasn't changed — otherwise we'd hit the
        # ActiveStreams mutex 50× per second per /tap.
        if fed.gate_open != self._gate_open_last:
            self._gate_open_last = fed.gate_open
            await self._recorder.streams.update_gate_open(self._conn_id, fed.gate_open)

        # Peak-hold-with-decay on what survived the gate. During pending
        # warm-up or pure silence, fed.frames is empty → peak=0 → the
        # meter decays to dark within its ~165 ms half-life. max() — not a
        # strict `>` test — so a steady tone whose per-frame peak equals
        # the prior level stays pinned instead of leaking down by
        # `LEVEL_DECAY_PER_FRAME` every frame.
        peak = max(int16_peak_norm(f) for f in fed.frames) if fed.frames else 0.0
        decayed = self._level * LEVEL_DECAY_PER_FRAME
        self._level = peak if peak > decayed else decayed
        await self._recorder.streams.update_bytes(
            self._conn_id,
            self._bytes_received,
            level=self._level,
        )

    # ------------------------------------------------------------------
    # Lifecycle internals
    # ------------------------------------------------------------------

    async def _open(self) -> None:
        started_at = datetime.now(UTC)
        fname = "(record off)"

        # A reserved __probe__ tap only proves the tap-secret works; it must
        # leave NO durable and NO live-visible state. That means skipping THREE
        # things below — the WAV open (here), the roster occurrence, and the
        # ActiveStream registration — not just the roster. Skipping the WAV
        # (rather than relying on the empty-WAV-unlink at close) decouples the
        # "no auto-bound Person" guarantee from the probe sending zero audio: a
        # misbehaving probe that DID send audio would otherwise keep a non-empty
        # WAV whose slug the recorded-slug backfill (name_resolution F1) turns
        # into a 'probe' Person. Skipping the ActiveStream keeps __probe__ out of
        # /api/state's live_identities, which attach_people would auto-bind AND
        # persist as a blank Person on the ~0.5s poll even with zero audio. We
        # still build + open the relay so the context manager and write_frame
        # stay valid (a probe that sends frames is fed through an inert relay).
        is_probe = self._identity == PROBE_IDENTITY

        if self._do_record and not is_probe:
            resumed = self._recorder.utterances.try_resume(
                self._utterance_id,
                identity=self._identity,
                session_dir=self._session_dir,
                owner=self._owner,
            )
            if resumed is not None:
                # Bridge reconnected within the resume window with the
                # same utterance_id. Append to the existing WAV; preserve
                # bytes_received and started_at so the merged file looks
                # like one continuous utterance. open_recorder_wav_append
                # seeks to the end of the existing data and patches the
                # RIFF/data sizes on close — O(1), no re-read of the prior
                # frames (see wav_append.py).
                if self._name and self._name != resumed.name:
                    resumed.name = self._name
                self._wf = open_recorder_wav_append(resumed.path)
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
                session_dir = self._session_dir
                session_dir.mkdir(parents=True, exist_ok=True)
                fpath = session_dir / fname
                record = UtteranceRecord(
                    utterance_id=self._utterance_id,
                    identity=self._identity,
                    name=self._name,
                    filename=fname,
                    path=fpath,
                    started_at=started_at,
                    owner=self._owner,
                )
                indexed = self._recorder.utterances.register_new(record)
                if not indexed:
                    # Another live tap already holds this utterance_id open (a
                    # duplicate-id overlap). We keep recording to our own WAV
                    # but stay out of the resume index so neither tap corrupts
                    # the other's record.
                    print(
                        f"[tapscribe] /tap open -> {fname} "
                        f"(utterance_id {self._utterance_id[:8]} already live; not resumable)",
                        flush=True,
                    )
                else:
                    print(f"[tapscribe] /tap open -> {fname}", flush=True)
                self._wf = open_recorder_wav(fpath)
                self._record = record
        else:
            print(f"[tapscribe] /tap open (record off) for {self._identity}", flush=True)

        # Roster the occurrence (ADR-0009): record this FULL identity's presence
        # so the People Registry can recover it (the WAV filename only carries
        # the lossy `safe_name(identity)[:10]` slug). Synchronous, no await → the
        # read-modify-write is atomic under the event loop against concurrent
        # taps. Guarded so a record-off live tap doesn't materialise an empty
        # session folder just for a roster; a recording tap already created it.
        # `not is_probe` is the People-pollution guard (see the _open preamble):
        # a probe roster occurrence auto-binds a durable 'probe' Person.
        if not is_probe and (self._do_record or self._session_dir.exists()):
            roster.record_occurrence(
                self._session_dir,
                identity=self._identity,
                name=self._name,
                recorded=self._do_record,
                wav=fname if self._do_record else None,
            )

        # Include the per-connection owner token so two taps sharing one
        # utterance_id (+ identity) get DISTINCT ActiveStream rows — otherwise
        # one tap's close would remove the other's row and freeze its counters.
        self._conn_id = (
            self._utterance_id[:8]
            + "-"
            + (safe_name(self._identity)[:10] or "unknown")
            + "-"
            + self._owner[:6]
        )
        # A probe registers NO ActiveStream: /api/state builds live_identities
        # from the ActiveStreams snapshot, and attach_people auto-binds + PERSISTS
        # a blank Person for every live identity — so a registered __probe__ would
        # materialise a durable probe Person on the next dashboard poll, zero audio
        # or not. (update_*/remove no-op on an unknown conn_id, so write_frame and
        # _close stay safe without a row.)
        if not is_probe:
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
                    session=self._session,
                )
            )

        # Build the live leg (relay + gate + reconnect) and let it attach
        # if this is a live tap and the channel is up. A record-only or
        # live-down tap holds an inert TapRelay, so write_frame can call
        # feed() unconditionally with no do_live branch of its own.
        if self._tap_relay is None:
            self._tap_relay = TapRelay(
                self._recorder.live,
                do_live=self._do_live,
                handlers=RelayHandlers(
                    on_settled_line=self._on_settled_line,
                    on_metrics=self._on_metrics,
                    on_buffer=self._on_buffer,
                ),
                label=self._identity,
            )
        await self._tap_relay.open()

    async def _on_metrics(self, lag_s: float) -> None:
        """Push the relay's latest reported lag to this tap's row so the
        dashboard can render a per-tap backlog indicator."""
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

    def _on_settled_line(self, text: str) -> None:
        """Settled-line consumer for the WlKRelay. Cleans Whisper
        meta-tokens (e.g. `<|nospeech|>`), drops letterless residues,
        then appends to LiveTranscripts attributed to this fan-out's
        identity/name and snapshotted session."""
        cleaned = clean_meta_tokens(text)
        if not cleaned or not any(c.isalpha() for c in cleaned):
            return
        self._recorder.transcripts.append(
            {
                "ts": datetime.now(UTC).isoformat(),
                "identity": self._identity,
                "name": self._name,
                "text": cleaned,
                "session": self._session,
            }
        )

    async def _close(self) -> None:
        # Sync cleanup first: these must run even if the surrounding task
        # is being cancelled (TestClient does that on WS exit). Async
        # awaits below may raise CancelledError and skip remaining work.
        kept = self._bytes_received > 0
        # Finalize the WAV handle only if it was actually opened.
        if self._wf is not None and self._record is not None:
            self._wf.close()
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
        else:
            print(f"[tapscribe] /tap closed (record off) for {self._identity}", flush=True)
        # Release the UtteranceIndex record whenever it was indexed —
        # register_new / try_resume mark it open=True BEFORE self._wf/self._record
        # are assigned, so coupling this to the WAV-finalize guard above would
        # strand the record open=True forever if the WAV open itself raised
        # (disk-full fresh open, or the resume reopen — both #196 failure points).
        # release() is a no-op when this connection never indexed the id (absent,
        # or held by another owner), so it stays safe on record-off / probe / lost-
        # overlap paths that never registered our own record.
        self._recorder.utterances.release(
            self._utterance_id,
            owner=self._owner,
            bytes_received=self._bytes_received,
            kept=kept,
        )
        # Tear down the live leg: TapRelay cancels any in-flight reconnect
        # (so it can't land a fresh relay right after teardown, leaking a
        # WS to the WlK child) and then closes the relay, which drains
        # tail captions. The ActiveStream row removal runs unconditionally in
        # the `finally` — the row must go even if the relay is None (open unwound
        # before it was built) or its teardown raises/cancels; remove() no-ops on
        # an unregistered conn_id.
        try:
            if self._tap_relay is not None:
                await self._tap_relay.close()
        finally:
            await self._recorder.streams.remove(self._conn_id)
