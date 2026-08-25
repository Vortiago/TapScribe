"""CAM++ speaker embeddings, one L2-normalised vector per fbank window.

The model is 3D-Speaker's `campplus_sv_en_voxceleb_16k` ONNX export: input
`x [N, T, 80]` of mean-centred log-mel fbank, output a 512-d embedding whose
cosine distance separates speakers (`tests/fixtures/diarize/PROVENANCE.md` has
the measurements that picked it).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import numpy as np

from . import model as diarize_model
from .base import DiarizerUnavailable

#: Longest window handed to the model in one call. Speaker embedding is a
#: short-window operation — the engine uses 1.5 s — and a long call is where
#: onnxruntime 1.27/1.28 silently miscomputed this model.
MAX_WINDOW_FRAMES = 600

#: Rows per `session.run`. Batching is ~40% faster than one call per window;
#: unbounded, a meeting's worth of windows would be one 500 MB tensor.
BATCH_WINDOWS = 32

DIM = 512


class CampPlusEmbedder:
    """Wraps one onnxruntime session. Not thread-safe by convention — construct
    one per diarization run, like every other consumer in the repo."""

    engine = "campplus"

    def __init__(self, session) -> None:
        self._session = session

    @classmethod
    def load(cls, path: Path | None = None) -> CampPlusEmbedder:
        """Open the fetched model. `DiarizerUnavailable` when it isn't there or
        onnxruntime is missing — both are install problems the operator fixes by
        re-running preflight, not engine failures."""
        path = path or diarize_model.model_path()
        if not path.is_file():
            raise DiarizerUnavailable(
                f"the speaker-embedding model is not at {path} — run "
                "`python -m tapscribe.diarizers.model` to fetch it."
            )
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - core dep, broken venv only
            raise DiarizerUnavailable(
                "onnxruntime is a core dependency but isn't importable — reinstall TapScribe."
            ) from exc
        try:
            return cls(ort.InferenceSession(str(path), providers=["CPUExecutionProvider"]))
        except Exception as exc:
            # A file that exists but is not this graph: a truncated download the
            # digest probe would have caught, or an operator `TAPSCRIBE_DIARIZE_
            # MODEL` the digest deliberately does not apply to. Still an install
            # problem, so it takes the same 400 and names the same repair.
            raise DiarizerUnavailable(
                f"the speaker-embedding model at {path} could not be opened ({exc}) — delete it and "
                "run `python -m tapscribe.diarizers.model` to re-fetch."
            ) from exc

    def embed(self, windows: Sequence[np.ndarray]) -> np.ndarray:
        """`(n, 512)` unit vectors for `(frames, 80)` fbank windows, in order."""
        vectors = np.zeros((len(windows), DIM), dtype=np.float64)
        for length, positions in _by_length(windows):
            if length > MAX_WINDOW_FRAMES:
                raise ValueError(f"window of {length} frames exceeds the {MAX_WINDOW_FRAMES}-frame bound")
            for chunk in range(0, len(positions), BATCH_WINDOWS):
                at = positions[chunk : chunk + BATCH_WINDOWS]
                batch = np.stack([windows[i] for i in at]).astype(np.float32)
                batch -= batch.mean(axis=1, keepdims=True)  # global-mean CMN, per window
                vectors[at] = self._session.run(["embedding"], {"x": batch})[0]
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.where(norms > 0, norms, 1.0)


def _by_length(windows: Sequence[np.ndarray]) -> list[tuple[int, list[int]]]:
    """Group window positions by frame count, so equal-length ones stack into
    one batch. A speech region's last window is short whenever the region isn't
    a whole number of hops, so ragged input is the norm, not the exception."""
    groups: dict[int, list[int]] = {}
    for i, window in enumerate(windows):
        groups.setdefault(len(window), []).append(i)
    return sorted(groups.items(), reverse=True)
