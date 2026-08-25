"""The two operator-tunable diarization knobs, resolved env > file > default.

Split out of `standalone.py` — which owns the engine that USES them — so
`/api/state` can report the values in force without importing it. That module
pulls numpy, the VAD and the fbank frontend at import time, and the VAD reaches
onnxruntime; dragging any of it onto the ~2 Hz poll path would make a broken
diarization install break the dashboard, which is the opposite of ADR-0021's
"no model means a multi-person tap stays one speaker".

Stdlib + `config` only. Keep it that way.
"""

from __future__ import annotations

from .. import config

#: Cosine distance at which two clusters stop being the same speaker. Measured
#: on the fixtures (`tests/fixtures/diarize/PROVENANCE.md`), not assumed: 0.7 is
#: inside every passing threshold range, and below ~0.5 one speaker starts
#: splitting into several.
DEFAULT_THRESHOLD = 0.7

#: Hard ceiling on Voices per tap — the clustering keeps merging until it is
#: met, whatever the threshold says.
DEFAULT_MAX_SPEAKERS = 8

#: Dashboard-tunable (Settings → Advanced), over `config/diarize-*.txt`.
ENV_THRESHOLD = "TAPSCRIBE_DIARIZE_THRESHOLD"
ENV_MAX_SPEAKERS = "TAPSCRIBE_DIARIZE_MAX_SPEAKERS"


def resolve_threshold() -> float:
    return config.resolve_knob(
        ENV_THRESHOLD,
        config.DIARIZE_THRESHOLD_FILE,
        config._parse_diarize_threshold,
        DEFAULT_THRESHOLD,
    )


def resolve_max_speakers() -> int:
    return config.resolve_knob(
        ENV_MAX_SPEAKERS,
        config.DIARIZE_MAX_SPEAKERS_FILE,
        config._parse_diarize_max_speakers,
        DEFAULT_MAX_SPEAKERS,
    )
