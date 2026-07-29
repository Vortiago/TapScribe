"""Unit contract for `LiveSnapshot` (#365): one tick's read of a live channel.

`GET /api/state` renders the dashboard's live-channel card from three attributes
the channel mutates from its own pump thread — `info`, `log`, and
`supports_native_vad` — and the payload is assembled on a worker thread
(`state_view.build_state_blob`). The route used to reach for all three by hand;
that hand-marshalling went stale, because it lived too far from the type that
changed underneath it: the "islice walks straight to the tail without copying
the deque" comment was written against a bare `deque`, but `TailLog.__iter__`
copies the whole thing under its lock.

So reading a channel for a poll tick belongs to the channel's own module, and
what is pinned here is the three rules the route can no longer get wrong: how
much log a poll ships, that the cap survives a concurrent append, and that
`info` is a copy rather than the live dict.
"""

from __future__ import annotations

from tapscribe.live import LOG_PREVIEW_LINES, LiveSnapshot, TailLog


class _Channel:
    """The duck-typed shape `LiveSnapshot.capture` reads. Not a `LiveChannel`:
    `capture` deliberately does not go through the Protocol, which is the engine
    LIFECYCLE seam (running / start / stop), so a test double needs only the
    three display attributes."""

    def __init__(self, *, info=None, log=None, **extra):
        self.info = info if info is not None else {"state": "stopped"}
        self.log = log if log is not None else TailLog(maxlen=200)
        for name, value in extra.items():
            setattr(self, name, value)


def test_capture_ships_a_log_preview_not_the_whole_tail():
    """The ~2 Hz poll carries a PREVIEW; the dashboard's log dialog fetches the
    whole 200-line tail on demand via /api/live/log. Shipping the tail on every
    tick would put 200 lines of engine chatter in a body the ETag hashes, and
    re-hash all of it whenever the engine logged anything. The preview is the
    NEWEST lines, still oldest-first — the card reads top to bottom."""
    channel = _Channel(supports_native_vad=True)
    for i in range(200):
        channel.log.append(f"line {i}")

    snapshot = LiveSnapshot.capture(channel)

    assert len(snapshot.log) == LOG_PREVIEW_LINES
    assert snapshot.log[-1] == "line 199"
    assert snapshot.log[0] == f"line {200 - LOG_PREVIEW_LINES}"


def test_capture_ships_the_whole_log_when_it_is_shorter_than_the_preview():
    """A freshly-started channel has logged less than a preview's worth. The cap
    is a ceiling, not a demand: the card shows what there is."""
    channel = _Channel(supports_native_vad=True)
    channel.log.append("spawning whisperlivekit-server")

    assert LiveSnapshot.capture(channel).log == ["spawning whisperlivekit-server"]


def test_capture_caps_the_preview_even_when_the_log_grows_mid_read():
    """The cap comes off ONE snapshot, sliced — not an index computed from a
    separately-read `len()`. `TailLog` serialises each read individually, not a
    pair of them, so the pump thread appending between a `len()` and the
    iteration widened the window: the route's old
    `islice(log, len(log) - 30, None)` served 31 lines on a log that grew in
    between. One read cannot disagree with itself."""

    class GrowsWhenMeasured(TailLog):
        """A log that appends a line as a side effect of being measured — the
        pump-thread race, made deterministic."""

        def __len__(self) -> int:
            before = super().__len__()
            self.append("a line the pump thread landed mid-read")
            return before

    channel = _Channel(log=GrowsWhenMeasured(maxlen=200), supports_native_vad=True)
    for i in range(LOG_PREVIEW_LINES + 5):
        channel.log.append(f"line {i}")

    assert len(LiveSnapshot.capture(channel).log) == LOG_PREVIEW_LINES


def test_capture_copies_info_so_a_later_mutation_cannot_reach_the_payload():
    """`info` is a live dict the channel rewrites from its own thread (the state
    transitions and `_mirror_gate_info`). The payload serialises on a WORKER
    thread, so the snapshot must be a copy: handing over the dict itself would
    let a mid-flight state change land in a body that already reported the old
    state, and the ETag would then cover bytes nobody assembled."""
    channel = _Channel(info={"state": "running", "model": "tiny.en"}, supports_native_vad=True)

    snapshot = LiveSnapshot.capture(channel)
    channel.info["state"] = "stopping"

    assert snapshot.info == {"state": "running", "model": "tiny.en"}


def test_capture_defaults_supports_native_vad_to_false_when_undeclared():
    """Safe-in-absence, the same direction `speech_gate.effective_gate_config`
    takes: a channel that does not declare the flag reports "no native VAD", so
    the dashboard offers a TapScribe gate rather than a `backend` option the
    engine would silently ignore. A live case, not paranoia —
    `LiveChannelBase` deliberately leaves the flag to each subclass because its
    safe default is the OPPOSITE of Whisper's."""
    assert LiveSnapshot.capture(_Channel()).supports_native_vad is False


def test_capture_coerces_supports_native_vad_to_a_real_bool():
    """The flag is serialised straight into the payload, and the dashboard tests
    it with `=== true`. A channel declaring a truthy non-bool would ship `1`,
    which is JSON the frontend reads as false."""
    snapshot = LiveSnapshot.capture(_Channel(supports_native_vad=1))

    assert snapshot.supports_native_vad is True
