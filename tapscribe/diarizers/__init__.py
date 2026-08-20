"""Speaker diarization — the Diarizer seam and its ONNX engine (ADR-0021)."""

from __future__ import annotations

#: onnxruntime below this miscomputes the speaker-embedding model above ~1000
#: frames: vectors stay unit-norm while a speaker stops resembling herself, so
#: clustering silently degrades. Measured broken on 1.27.0 and 1.28.0, fixed in
#: 1.29.0 (`tests/fixtures/diarize/PROVENANCE.md`).
#:
#: Checked here rather than raised as the core `onnxruntime>=1.17` floor: the
#: VAD shares that dependency, is fed 512-sample windows, and is unaffected —
#: forcing every install onto a current runtime for one consumer's bug would be
#: the wrong trade.
MIN_ONNXRUNTIME = (1, 29)


def onnxruntime_too_old(version: str) -> bool:
    """True when `version` is a runtime known to miscompute the model."""
    try:
        parts = tuple(int(p) for p in version.split(".")[:2])
    except ValueError:
        return False  # unparseable (a dev build): assume usable, the guard test catches it
    return parts < MIN_ONNXRUNTIME
