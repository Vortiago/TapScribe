"""Average-linkage agglomerative clustering on cosine distance, in numpy.

Turns one identity's window embeddings into Voice labels. Average linkage
because a speaker's windows are a loose blob: single linkage chains two speakers
through one boundary-straddling window, complete linkage splits a speaker whose
loudest and quietest window disagree.

No scipy — it is a dev-only dependency (`pyproject.toml`).
"""

from __future__ import annotations

import numpy as np

#: Windows clustered directly. MEMORY is what binds this: the distance matrix is
#: n² float64, so 4000 windows (~50 min of speech at 1.3 a second) is 128 MB and
#: 8000 would be 512 MB. Time is not the constraint — the nearest-neighbour cache
#: below runs 4000 windows in half a second. Above the cap the run clusters an
#: evenly-spaced sample and assigns the rest to the nearest centroid; speaker
#: blobs are hugely over-sampled at that rate, so the sample carries the same
#: clusters.
MAX_SAMPLED_WINDOWS = 4000


def _agglomerate(dist: np.ndarray, *, threshold: float, max_speakers: int) -> np.ndarray:
    """Merge the closest pair until every pair is past `threshold`, or
    `max_speakers` clusters remain — whichever binds later. Returns one
    arbitrary integer per row. **`dist` is consumed in place.**

    Each row caches its own nearest neighbour, so a merge picks the global
    closest pair in O(alive) instead of re-scanning the whole n×n matrix. That
    is exact rather than approximate because average linkage is REDUCIBLE: the
    merged cluster's distance to a third is a size-weighted mean of its halves',
    so it can never fall below either. A row whose nearest neighbour was neither
    half therefore keeps it, and only the rows that pointed at the two merged
    ones need recomputing. Measured 64x faster at the 4000-window cap, with a
    bit-identical partition.
    """
    n = len(dist)
    d = np.asarray(dist, dtype=np.float64)
    np.fill_diagonal(d, np.inf)
    size = np.ones(n)
    label = np.arange(n)
    live = np.ones(n, dtype=bool)
    nn = np.argmin(d, axis=1)
    nnd = d[np.arange(n), nn]
    alive = n

    while alive > 1:
        # The smallest live row achieving the global minimum, and its first
        # column — the same pair a row-major `argmin` over the whole matrix
        # picks, ties included, which is what keeps the partition identical.
        rows = np.flatnonzero(live)
        i = int(rows[np.argmin(nnd[rows])])
        j = int(nn[i])
        if not np.isfinite(nnd[i]) or (nnd[i] > threshold and alive <= max_speakers):
            break
        # Lance-Williams for average linkage: the merged cluster's distance to
        # every other is the size-weighted mean of its two halves'.
        row = (size[i] * d[i] + size[j] * d[j]) / (size[i] + size[j])
        d[i, :] = d[:, i] = row
        d[i, i] = np.inf
        d[j, :] = d[:, j] = np.inf  # retire j
        size[i] += size[j]
        label[label == j] = i
        live[j] = False
        alive -= 1

        # Row i moved; every other row that pointed at i or j has to look again.
        for r in (i, *np.flatnonzero(live & ((nn == i) | (nn == j))).tolist()):
            nn[r] = int(np.argmin(d[r]))
            nnd[r] = d[r, nn[r]]

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
