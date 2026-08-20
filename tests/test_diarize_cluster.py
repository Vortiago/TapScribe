"""Agglomerative clustering over speaker embeddings — the step that decides how
many Voices a tap holds.

Synthetic vectors, no model: the numbers here are about the algorithm, not about
whether CAM++ separates two humans (`test_diarize_engine.py` is that).

The dangerous direction is the FALSE SPLIT: one human clustered as two Voices is
a row the operator maps twice, and half their words end up under someone else's
name. A missed split leaves a tap under-attributed, which reads as "diarization
didn't help", so the tests below weight the split direction accordingly.
"""

from __future__ import annotations

import numpy as np
import pytest

from tapscribe.diarizers.cluster import cluster_voices


def _blobs(counts: list[int], *, spread: float = 0.03, seed: int = 7) -> np.ndarray:
    """One tight unit-norm blob per count, interleaved in the order given."""
    rng = np.random.default_rng(seed)
    centres = rng.standard_normal((len(counts), 512))
    centres /= np.linalg.norm(centres, axis=1, keepdims=True)
    rows = [centres[i] + spread * rng.standard_normal(512) for i, n in enumerate(counts) for _ in range(n)]
    out = np.stack(rows)
    return out / np.linalg.norm(out, axis=1, keepdims=True)


def test_two_separated_groups_become_two_voices() -> None:
    labels = cluster_voices(_blobs([6, 6]), threshold=0.7, max_speakers=8)

    assert sorted(set(labels.tolist())) == [0, 1]
    assert labels[:6].tolist() == [0] * 6
    assert labels[6:].tolist() == [1] * 6


def test_one_speaker_is_never_split() -> None:
    """The direction that puts a stranger's name on someone's words."""
    labels = cluster_voices(_blobs([12]), threshold=0.7, max_speakers=8)

    assert set(labels.tolist()) == {0}


def test_labels_are_numbered_by_first_appearance() -> None:
    """Voice A is whoever spoke first — stable across runs, and the order the
    operator reads down the mapping list."""
    labels = cluster_voices(_blobs([2, 5, 3]), threshold=0.7, max_speakers=8)

    assert labels.tolist() == [0, 0, 1, 1, 1, 1, 1, 2, 2, 2]


def test_one_window_between_two_speakers_does_not_chain_them() -> None:
    """A window straddling a turn change embeds as a blend of both speakers.
    Under single linkage that one row chains the two blobs into one Voice; under
    average linkage it joins whichever blob it is nearer and the split holds."""
    blobs = _blobs([6, 6])
    bridge = blobs[0] + blobs[6]
    vectors = np.vstack([blobs[:6], bridge / np.linalg.norm(bridge), blobs[6:]])

    labels = cluster_voices(vectors, threshold=0.7, max_speakers=8)

    assert len(set(labels.tolist())) == 2, "the boundary window chained two speakers into one Voice"


def test_max_speakers_forces_merges_past_the_threshold() -> None:
    """A hard cap, not a hint: the operator asked for at most two Voices."""
    labels = cluster_voices(_blobs([4, 4, 4]), threshold=0.7, max_speakers=2)

    assert len(set(labels.tolist())) == 2


def test_no_windows_is_no_voices() -> None:
    """Silence, or a tap the VAD found no speech in."""
    labels = cluster_voices(np.zeros((0, 512)), threshold=0.7, max_speakers=8)

    assert labels.tolist() == []


def test_a_single_window_is_one_voice() -> None:
    labels = cluster_voices(_blobs([1]), threshold=0.7, max_speakers=8)

    assert labels.tolist() == [0]


def test_above_the_cap_every_window_still_gets_a_voice() -> None:
    """The distance matrix is O(n²) and the merge loop walks it once per merge,
    so an all-day tap has to cluster a bounded sample and assign the rest."""
    vectors = _blobs([40, 40])

    labels = cluster_voices(vectors, threshold=0.7, max_speakers=8, sample_cap=10)

    assert len(labels) == len(vectors), "a window went unlabelled"
    assert sorted(set(labels.tolist())) == [0, 1]
    assert labels[:40].tolist() == [0] * 40
    assert labels[40:].tolist() == [1] * 40


def test_the_sample_cap_bounds_the_distance_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins that the cap is what the clustering actually runs on — asserting
    only the labels would pass with the cap ignored."""
    seen: list[int] = []
    real = cluster_voices.__globals__["_agglomerate"]

    def spy(dist, **kwargs):
        seen.append(len(dist))
        return real(dist, **kwargs)

    monkeypatch.setitem(cluster_voices.__globals__, "_agglomerate", spy)
    cluster_voices(_blobs([40, 40]), threshold=0.7, max_speakers=8, sample_cap=10)

    assert seen == [10]
