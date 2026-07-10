"""Thin wrapper that opens an existing recorder-format WAV for true append.

The Python ``wave`` module has no append mode.  This module positions a
``wave.Wave_write`` at the end of the existing data chunk and pre-seeds
its internal counters so ``_patchheader`` writes the correct total
(prior + new) on close — O(1), no full re-read.

The recorder format is fixed (16 kHz / mono / 16-bit PCM), so the WAV
written by ``open_recorder_wav`` always has a standard 44-byte header
(RIFF(12) + fmt(24) + data_tag(8)).
"""

from __future__ import annotations

import wave
from pathlib import Path

from .audio import RECORDER_CHANNELS, RECORDER_SAMPLE_RATE, RECORDER_SAMPLE_WIDTH


def open_recorder_wav_append(path: Path) -> wave.Wave_write:
    """Open *path* (an existing recorder-format WAV) for O(1) append.

    Algorithm

    1. Read prior metadata (header only — zero ``readframes`` calls).
    2. Compute the data offset and prior byte count.
    3. Open the file ``r+b``, seek to the end of existing data.
    4. Construct a ``wave.Wave_write`` on this handle, configure its
       private state so ``_patchheader`` writes the correct total size.
    5. Return the handle.

    Empty-resume edge case:  if the caller never calls ``writeframes``
    before ``close()``, the file is byte-identical — ``_patchheader``
    writes the same values that are already in the file.
    """
    # -- 1. Read prior metadata (header only, zero readframes calls) --
    with wave.open(str(path), "rb") as reader:
        prior_nframes = reader.getnframes()

    # -- 2. Compute data offset and prior byte count --
    prior_data_bytes = prior_nframes * RECORDER_CHANNELS * RECORDER_SAMPLE_WIDTH

    # -- 3. Open r+b and position at end of existing data --
    f = open(path, "r+b")
    f.seek(44 + prior_data_bytes)

    # -- 4. Construct and configure a Wave_write on this handle --
    wf = wave.Wave_write(f)
    wf.setnchannels(RECORDER_CHANNELS)
    wf.setsampwidth(RECORDER_SAMPLE_WIDTH)
    wf.setframerate(RECORDER_SAMPLE_RATE)

    # _headerwritten = True — prevents a duplicate RIFF/fmt/data header
    # from being written at the current file position.
    wf._headerwritten = True  # noqa: SLF001

    # _i_opened_the_file must be the file handle itself (not True/False);
    # Wave_write.close() does `if self._i_opened_the_file:
    # self._i_opened_the_file.close()`.  When constructed with a file
    # object (not a filename), the constructor leaves it as None and
    # close() never closes the handle.
    wf._i_opened_the_file = f  # noqa: SLF001

    # _datawritten seeds the byte counter so _patchheader writes
    # prior + new instead of just new.
    wf._datawritten = prior_data_bytes  # noqa: SLF001

    # _form_length_pos: where the RIFF size lives (offset 4 after writing
    # a standard PCM header).
    wf._form_length_pos = 4  # noqa: SLF001

    # _data_length_pos: where the data chunk size lives (offset 40 after
    # writing a standard PCM header).
    wf._data_length_pos = 40  # noqa: SLF001

    # _datalength stays at its init-fp default of 0 — the
    # _patchheader guard (_datalength != _datawritten) fires correctly.

    # -- 5. Return the configured Wave_write --
    return wf
