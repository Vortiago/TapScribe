"""Re-derives `reference.npz` from the real upstream, so the committed oracle
cannot rot unnoticed.

`test_diarize_fbank.py` checks the port against the archive everywhere; this
checks the archive itself, and runs only where `kaldi-native-fbank` is installed
— the `upstream-contract` CI lane. Same split as `tapscribe/vad/`, whose
`silero-port` lane exists for exactly this reason.

The embedding half additionally needs the model, which is fetched rather than
vendored; point `TAPSCRIBE_DIARIZE_MODEL` at it or those tests skip.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from tapscribe.diarizers.fbank import fbank
from tests.fixtures.diarize import load_reference, read_fixture_wav

knf = pytest.importorskip("kaldi_native_fbank", reason="upstream kaldi-native-fbank not installed")

FIXTURES = ["armstrong-en", "marlene-nb", "solen-da"]
_MODEL = os.environ.get("TAPSCRIBE_DIARIZE_MODEL", "")


def _upstream_fbank(samples: np.ndarray) -> np.ndarray:
    opts = knf.FbankOptions()
    opts.frame_opts.dither = 0.0
    opts.frame_opts.samp_freq = 16000
    opts.frame_opts.snip_edges = False
    opts.mel_opts.num_bins = 80
    f = knf.OnlineFbank(opts)
    f.accept_waveform(16000, samples.tolist())
    f.input_finished()
    if f.num_frames_ready == 0:
        return np.zeros((0, 80), dtype=np.float32)
    return np.stack([f.get_frame(i) for i in range(f.num_frames_ready)]).astype(np.float32)


@pytest.mark.parametrize("name", FIXTURES)
def test_the_committed_reference_still_matches_upstream(name: str) -> None:
    """The archive is the oracle everywhere else, so nothing else can catch it
    drifting from the implementation it was derived from."""
    ref = load_reference()
    want = _upstream_fbank(read_fixture_wav(name))

    np.testing.assert_allclose(ref[f"fbank__{name}"], want[:100], rtol=0, atol=1e-5)
    np.testing.assert_allclose(ref[f"fbank_tail__{name}"], want[-100:], rtol=0, atol=1e-5)


@pytest.mark.parametrize("name", FIXTURES)
def test_the_port_matches_upstream_over_the_whole_clip(name: str) -> None:
    """The committed slices cover 200 of ~1400 frames; here the whole clip is
    compared, so a divergence in the middle has somewhere to fail."""
    samples = read_fixture_wav(name)

    np.testing.assert_allclose(fbank(samples), _upstream_fbank(samples), rtol=0, atol=2e-3)


@pytest.mark.parametrize("n", [1, 79, 80, 120, 139, 161, 400, 401])
def test_the_port_matches_upstream_on_short_inputs(n: int) -> None:
    """Under ~140 samples a frame overhangs both edges and Kaldi's reflection
    repeats — a single reflection silently diverges in exactly that window."""
    samples = (np.random.default_rng(7).standard_normal(n) * 0.05).astype(np.float32)

    got, want = fbank(samples), _upstream_fbank(samples)

    assert got.shape == want.shape
    if got.size:
        np.testing.assert_allclose(got, want, rtol=0, atol=2e-3)


@pytest.mark.skipif(not _MODEL, reason="set TAPSCRIBE_DIARIZE_MODEL to the embedding model")
def test_the_runtime_embeds_long_input_coherently() -> None:
    """onnxruntime 1.27.0 miscomputes this model above ~1000 frames, silently:
    the vectors stay unit-norm while a speaker stops resembling herself. Nothing
    else turns that into a failure (PROVENANCE.md has the measurements)."""
    ort = pytest.importorskip("onnxruntime")
    session = ort.InferenceSession(_MODEL, providers=["CPUExecutionProvider"])

    def embed(feats: np.ndarray) -> np.ndarray:
        centred = feats - feats.mean(axis=0, keepdims=True)
        vec = session.run(["embedding"], {"x": centred[None, :, :]})[0][0].astype(np.float64)
        return vec / np.linalg.norm(vec)

    mine = fbank(read_fixture_wav("marlene-nb"))
    other = fbank(read_fixture_wav("solen-da"))
    short, long = embed(mine[:600]), embed(mine[:1500])

    assert float(np.dot(short, long)) > 0.85, "same speaker stopped resembling herself"
    assert float(np.dot(long, embed(other[:1500]))) < 0.30, "speakers stopped separating"


@pytest.mark.skipif(not Path(_MODEL or "/nonexistent").exists(), reason="model not present")
@pytest.mark.parametrize("name", FIXTURES)
def test_the_committed_embedding_still_matches(name: str) -> None:
    ort = pytest.importorskip("onnxruntime")
    session = ort.InferenceSession(_MODEL, providers=["CPUExecutionProvider"])
    feats = fbank(read_fixture_wav(name))
    centred = feats - feats.mean(axis=0, keepdims=True)
    vec = session.run(["embedding"], {"x": centred[None, :, :]})[0][0].astype(np.float64)

    got = vec / np.linalg.norm(vec)

    assert float(np.dot(got, load_reference()[f"emb__{name}"].astype(np.float64))) > 0.999
