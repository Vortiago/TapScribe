"""The runtime floor for diarization.

onnxruntime 1.27.0 and 1.28.0 miscompute the speaker-embedding model above
~1000 frames, silently — unit-norm vectors, a speaker no longer resembling
herself. The guard exists so an operator gets a message instead of bad Voices.
"""

from __future__ import annotations

import pytest

from tapscribe.diarizers import MIN_ONNXRUNTIME, onnxruntime_too_old


@pytest.mark.parametrize("version", ["1.17.0", "1.26.0", "1.27.0", "1.28.0", "1.28.9"])
def test_measured_broken_runtimes_are_rejected(version: str) -> None:
    assert onnxruntime_too_old(version)


@pytest.mark.parametrize("version", ["1.29.0", "1.30.0", "2.0.0"])
def test_the_fixed_runtime_and_later_are_accepted(version: str) -> None:
    assert not onnxruntime_too_old(version)


def test_a_dev_build_of_a_good_version_is_accepted() -> None:
    assert not onnxruntime_too_old("1.29.0.dev20260101+cpu.local")


def test_an_unparseable_version_is_not_rejected() -> None:
    """Fail open on a string we can't read: the upstream-contract coherence
    test is what actually proves a runtime good, not this parse."""
    assert not onnxruntime_too_old("unknown")


def test_the_floor_is_where_the_measurement_put_it() -> None:
    """1.28 was measured broken, so the floor is 1.29 — not a guess."""
    assert MIN_ONNXRUNTIME == (1, 29)
