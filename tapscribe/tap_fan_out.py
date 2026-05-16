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

import wave
from contextlib import suppress
from datetime import datetime, timezone
from uuid import uuid4

from .audio import open_recorder_wav
from .live_relay import WlKRelay
from .recorder import ActiveStream, Recorder, UtteranceRecord
from .text import clean_meta_tokens, safe_name


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
        if self._wf is not None:
            self._wf.writeframes(buf)
            self._bytes_received += len(buf)
            await self._recorder.streams.update_bytes(self._conn_id, self._bytes_received)
        # Best-effort relay. If the relay reports closed/dead we stop
        # trying for the rest of this fan-out — recording continues
        # unaffected. Per ADR-0002 graceful degradation.
        if self._relay_alive:
            assert self._relay is not None
            if not await self._relay.send(buf):
                self._relay_alive = False

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
                started_iso = started_at.strftime("%Y-%m-%dT%H-%M-%SZ")
                short_id = safe_name(self._identity)[:10] or "unknown"
                name_slug = safe_name(self._name) or "anon"
                # Filename uses a fresh local uuid for uniqueness; the bridge's
                # utterance_id lives in the index, not in the path. Two distinct
                # utterances sharing an utterance_id (e.g. an expired-and-restarted
                # one) must not collide on disk.
                fname = f"{started_iso}_{name_slug}_{short_id}_{uuid4().hex[:8]}.wav"
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
                bytes_received=0,
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
            )
            if await candidate.connect():
                self._relay = candidate
                self._relay_alive = True

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
        if self._relay is not None:
            await self._relay.close()  # drains tail captions per Q2
        await self._recorder.streams.remove(self._conn_id)
