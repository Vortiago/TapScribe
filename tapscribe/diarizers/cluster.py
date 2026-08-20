"""Average-linkage agglomerative clustering on cosine distance, in numpy.

Turns one identity's window embeddings into Voice labels. Average linkage
because a speaker's windows are a loose blob: single linkage chains two speakers
through one boundary-straddling window, complete linkage splits a speaker whose
loudest and quietest window disagree.

No scipy — it is a dev-only dependency (`pyproject.toml`).
"""

from __future__ import annotations

import numpy as np

#: Windows clustered directly. The merge loop scans an n×n matrix once per
#: merge: 3000 windows (~40 min of speech) is 12 s, 8000 would be ~4 min and
#: 256 MB. Above this the run clusters an evenly-spaced sample and assigns the
#: rest to the nearest centroid — speaker blobs are hugely over-sampled at 1.3
#: windows a second, so the sample carries the same clusters.
MAX_SAMPLED_WINDOWS = 4000


def _agglomerate(dist: np.ndarray, *, threshold: float, max_speakers: int) -> np.ndarray:
    """Merge the closest pair until every pair is past `threshold`, or
    `max_speakers` clusters remain — whichever binds later. Returns one
    arbitrary integer per row."""
    n = len(dist)
    d = np.array(dist, dtype=np.float64)
    np.fill_diagonal(d, np.inf)
    size = np.ones(n)
    label = np.arange(n)
    alive = n

    while alive > 1:
        i, j = divmod(int(np.argmin(d)), n)
        if not np.isfinite(d[i, j]) or (d[i, j] > threshold and alive <= max_speakers):
            break
        # Lance-Williams for average linkage: the merged cluster's distance to
        # every other is the size-weighted mean of its two halves'.
        row = (size[i] * d[i] + size[j] * d[j]) / (size[i] + size[j])
        d[i, :] = d[:, i] = row
        d[i, i] = np.inf
        d[j, :] = d[:, j] = np.inf  # retire j
        size[i] += size[j]
        label[label == j] = i
        alive -= 1

    return label


def _by_first_appearance(label: np.ndarray) -> np.ndarray:
    """Renumber to 0, 1, 2 … in the order each cluster first speaks, so Voice A
    is whoever spoke first rather than whichever row the merge loop kept."""
    order: dict[int, int] = {}
    out = np.empty(len(label), dtype=int)
    for k, lab in enumerate(label.tolist()):
        out[k] = order.setdefault(lab, len(order))
    return out


def cluster_voices(
    vectors: np.ndarray,
    *,
    threshold: float,
    max_speakers: int,
    sample_cap: int = MAX_SAMPLED_WINDOWS,
) -> np.ndarray:
    """`(n, dim)` L2-normalised embeddings, in time order → one Voice index per
    row. `threshold` is a cosine DISTANCE: a larger one merges more, so it errs
    towards too few Voices.
    """
    n = len(vectors)
    if n == 0:
        return np.zeros(0, dtype=int)
    if n <= sample_cap:
        return _by_first_appearance(
            _agglomerate(1.0 - vectors @ vectors.T, threshold=threshold, max_speakers=max_speakers)
        )

    # Strictly increasing for n > cap, so the sample is exactly `sample_cap` rows.
    idx = np.linspace(0, n - 1, sample_cap).astype(int)
    sample = vectors[idx]
    sample_labels = _agglomerate(1.0 - sample @ sample.T, threshold=threshold, max_speakers=max_speakers)
    centroids = np.stack([sample[sample_labels == lab].mean(axis=0) for lab in np.unique(sample_labels)])
    centroids /= np.linalg.norm(centroids, axis=1, keepdims=True)
    return _by_first_appearance(np.argmax(vectors @ centroids.T, axis=1))
