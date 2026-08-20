"""The numpy fbank must match Kaldi's, bit-for-bit enough to feed the model.

The speaker-embedding model was trained on Kaldi-compatible 80-bin fbank. Get
the window, preemphasis, mel edges or the framing subtly wrong and it still
returns 512 plausible-looking numbers — every mocked test passes and the
clustering quietly degrades. So the frontend is pinned against a reference
computed once with `kaldi-native-fbank`, the same frontend sherpa-onnx feeds
these models with (`tests/fixtures/diarize/PROVENANCE.md`).

`tests/test_diarize_fbank_upstream.py` re-derives the reference live in the
`upstream-contract` CI lane; this file is the check that runs everywhere.
"""

from __future__ import annotations

import numpy as np
import pytest

from tapscribe.diarizers.fbank import fbank
from tests.fixtures.diarize import load_reference, read_fixture_wav

FIXTURES = ["armstrong-en", "marlene-nb", "solen-da"]


@pytest.fixture(scope="module")
def reference():
    return load_reference()


@pytest.mark.parametrize("name", FIXTURES)
def test_fbank_matches_the_kaldi_reference(name: str, reference) -> None:
    want = reference[f"fbank__{name}"]

    got = fbank(read_fixture_wav(name))[: len(want)]

    np.testing.assert_allclose(got, want, rtol=0, atol=2e-3)


def test_fbank_frame_count_follows_the_ten_millisecond_hop() -> None:
    """`snip_edges=False`: one frame per hop, centred, edges mirrored — NOT the
    shorter `snip_edges=True` count, which would silently shift every frame."""
    samples = read_fixture_wav("marlene-nb")

    got = fbank(samples)

    assert got.shape == (round(len(samples) / 160), 80)


def test_fbank_of_silence_is_finite() -> None:
    """A digital-silence frame has zero energy; the log floor has to hold or the
    model is fed -inf and returns NaNs."""
    got = fbank(np.zeros(16000, dtype=np.float32))

    assert np.isfinite(got).all()
