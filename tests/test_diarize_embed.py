"""The ONNX embedder's contract, with a fake session: shapes, centring,
batching, and the bound on how much audio reaches the model in one call.

Whether the vectors SEPARATE two humans is `test_diarize_engine.py`'s question
and needs the real model; these are the things a fake can still get wrong.
"""

from __future__ import annotations

import numpy as np
import pytest

from tapscribe.diarizers.base import DiarizerUnavailable
from tapscribe.diarizers.embed import MAX_WINDOW_FRAMES, CampPlusEmbedder


class FakeSession:
    """Records what it was fed and answers with a vector per batch row."""

    def __init__(self) -> None:
        self.calls: list[np.ndarray] = []

    def run(self, names, feed):
        x = feed["x"]
        self.calls.append(x)
        # First coordinate carries the window's own scale, so a mis-ordered
        # result is visible in the assertions below.
        out = np.zeros((len(x), 512), dtype=np.float32)
        out[:, 0] = x[:, 0, 0]
        out[:, 1] = 1.0
        return [out]


def _window(frames: int, value: float) -> np.ndarray:
    return np.full((frames, 80), value, dtype=np.float32) + np.arange(frames, dtype=np.float32)[:, None]


def test_one_unit_vector_per_window_in_order() -> None:
    session = FakeSession()
    windows = [_window(150, v) for v in (1.0, 2.0, 3.0)]

    vectors = CampPlusEmbedder(session).embed(windows)

    assert vectors.shape == (3, 512)
    np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-6)


def test_windows_are_mean_centred_before_the_model_sees_them() -> None:
    """The 3D-Speaker CAM++ export expects global-mean CMN; without it the
    embedding carries the channel as much as the speaker."""
    session = FakeSession()

    CampPlusEmbedder(session).embed([_window(150, 7.0)])

    fed = session.calls[0]
    np.testing.assert_allclose(fed.mean(axis=1), 0.0, atol=1e-4)


def test_same_length_windows_ride_one_call() -> None:
    """A meeting is thousands of windows; one session.run each is ~40% slower
    than batching them."""
    session = FakeSession()

    CampPlusEmbedder(session).embed([_window(150, float(i)) for i in range(8)])

    assert len(session.calls) == 1
    assert session.calls[0].shape == (8, 150, 80)


def test_a_ragged_tail_window_still_gets_embedded() -> None:
    """The last window of a speech region is short whenever the region is not a
    whole number of hops. Different length -> its own call, never dropped."""
    session = FakeSession()
    windows = [_window(150, 1.0), _window(150, 2.0), _window(90, 3.0)]

    vectors = CampPlusEmbedder(session).embed(windows)

    assert vectors.shape == (3, 512)
    assert [c.shape[1] for c in session.calls] == [150, 90]


def test_batches_stay_bounded() -> None:
    """Ten thousand windows in one tensor is 500 MB of float32."""
    session = FakeSession()

    CampPlusEmbedder(session).embed([_window(150, float(i)) for i in range(70)])

    assert max(len(c) for c in session.calls) <= 32
    assert sum(len(c) for c in session.calls) == 70


def test_no_windows_is_no_vectors() -> None:
    session = FakeSession()

    vectors = CampPlusEmbedder(session).embed([])

    assert vectors.shape == (0, 512)
    assert session.calls == []


def test_a_window_longer_than_the_bound_is_refused() -> None:
    """Speaker embedding is a short-window operation, and a long one is where
    onnxruntime 1.27/1.28 silently miscomputed this model. Bounded by contract,
    not by whoever calls it remembering."""
    session = FakeSession()

    with pytest.raises(ValueError, match="frames"):
        CampPlusEmbedder(session).embed([_window(MAX_WINDOW_FRAMES + 1, 1.0)])


def test_loading_without_the_model_is_unavailable(tmp_path, monkeypatch) -> None:
    """Routes turn this into a 400 telling the operator to run preflight — not
    a 500 from onnxruntime failing to open a path."""
    from tapscribe.diarizers import model as diarize_model

    monkeypatch.delenv(diarize_model.ENV_MODEL, raising=False)
    monkeypatch.setattr(diarize_model, "MODELS_DIR", tmp_path / "nothing")

    with pytest.raises(DiarizerUnavailable, match="model"):
        CampPlusEmbedder.load()
