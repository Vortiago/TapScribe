"""End-to-end tests for the full TapScribe pipeline.

These tests boot a real uvicorn server, run real /tap WebSocket bridges
that stream WAV-derived PCM frames, and verify every UI-visible piece of
state (active streams, live feed, on-disk WAVs, session transcript).

A real whisperlivekit-server is faked via the same FakeWlkThread the
unit tests use; the recorder relays /tap bytes to it and we drive
settled lines into the live feed by pushing through the fake.

A FakeTranscriber stands in for faster-whisper / mlx-whisper for the
default test path so the suite stays portable. A second test gated by
@pytest.mark.real_audio exercises a real model when fixtures + an actual
whisper backend are available.
"""
