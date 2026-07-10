"""Thin wrapper that opens an existing recorder-format WAV for true append.

The Python ``wave`` module has no append mode.  This module locates the
existing ``data`` chunk, positions a ``wave.Wave_write`` at the end of it,
and pre-seeds the writer's private counters so ``close()`` patches the
RIFF/``data`` size fields to the new total (prior + new) — O(1), no re-read
of the prior samples.

The recorder format is fixed (16 kHz / mono / 16-bit PCM), so a WAV written
by ``open_recorder_wav`` always has the canonical 44-byte header (RIFF(12) +
fmt(24) + ``data`` tag(8)).  We DERIVE the data offset from the file rather
than trust that layout blindly: the stdlib ``Wave_write._patchheader`` we
drive hardcodes the 36-byte standard prefix, so a WAV carrying an extra
``LIST``/``fact`` chunk before ``data`` could not be size-patched correctly.
``open_recorder_wav_append`` rejects any such non-canonical file loudly
instead of seeking mid-header and silently corrupting it.
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path

from .audio import RECORDER_CHANNELS, RECORDER_SAMPLE_WIDTH, _configure_recorder_format

# The canonical recorder header (RIFF 12 + fmt 24 + "data"+size 8) puts the
# first sample at byte 44; the RIFF and data size fields live at bytes 4 and
# 40. open_recorder_wav emits exactly this, and _patchheader assumes it.
_CANONICAL_DATA_OFFSET = 44
_FRAME_BYTES = RECORDER_CHANNELS * RECORDER_SAMPLE_WIDTH


def _locate_data_chunk(path: Path) -> tuple[int, int]:
    """Walk the RIFF chunk headers and return ``(data_offset, data_bytes)`` for
    the ``data`` chunk — the byte offset of the first sample and the length its
    header claims. Reads only 8-byte chunk headers, never the samples, so a
    resume stays O(1) rather than O(prior bytes)."""
    with open(path, "rb") as f:
        riff = f.read(12)
        if len(riff) < 12 or riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
            raise ValueError(f"{path.name}: not a RIFF/WAVE file")
        while True:
            header = f.read(8)
            if len(header) < 8:
                raise ValueError(f"{path.name}: no data chunk found")
            chunk_id = header[:4]
            (chunk_size,) = struct.unpack("<I", header[4:8])
            if chunk_id == b"data":
                return f.tell(), chunk_size
            # RIFF chunks are word-aligned: skip the payload and any pad byte.
            f.seek(chunk_size + (chunk_size & 1), 1)


def open_recorder_wav_append(path: Path) -> wave.Wave_write:
    """Open *path* (an existing recorder-format WAV) for O(1) append.

    Locate the ``data`` chunk by walking the header, seek to the end of the
    existing samples, and hand back a ``wave.Wave_write`` whose private
    counters are pre-seeded so ``close()`` patches the RIFF/``data`` sizes to
    prior+new. Reading nothing off disk beyond the chunk headers keeps a
    reconnect from stalling the event loop on a large prior utterance.

    Raises ``ValueError`` if the file is not a canonical recorder WAV (``data``
    at byte 44): ``Wave_write._patchheader`` hardcodes the 36-byte standard
    prefix, so a non-standard header could not be size-patched correctly and
    must fail loudly rather than corrupt the file.

    Empty-resume edge case: if the caller never calls ``writeframes`` before
    ``close()``, ``_patchheader`` rewrites the identical size fields already on
    disk, so the file is byte-identical.
    """
    data_offset, header_data_bytes = _locate_data_chunk(path)
    if data_offset != _CANONICAL_DATA_OFFSET:
        raise ValueError(
            f"{path.name}: data chunk at byte {data_offset}, expected "
            f"{_CANONICAL_DATA_OFFSET} (canonical recorder header) — append "
            "only supports recorder-format WAVs, not files with extra chunks"
        )

    # Trust the SMALLER of the header's claim and the bytes actually on disk:
    # a prior WAV truncated after close then appends at real EOF with a size
    # field that matches, instead of seeking past EOF and injecting a silent
    # gap. For a well-formed prior (data is the last chunk) these are equal.
    on_disk_bytes = path.stat().st_size - data_offset
    prior_data_bytes = min(header_data_bytes, on_disk_bytes)
    prior_nframes = prior_data_bytes // _FRAME_BYTES

    f = open(path, "r+b")
    try:
        f.seek(data_offset + prior_data_bytes)
        wf = wave.Wave_write(f)
        _configure_recorder_format(wf)
    except BaseException:
        # Wave_write got a file object (not a filename), so it leaves
        # _i_opened_the_file=None and won't close f itself on a later failure;
        # close it here so a raise in the ctor/setters can't leak the handle
        # (an unclosed-file ResourceWarning is an error under filterwarnings).
        f.close()
        raise

    # Bend the fresh Wave_write into append mode. Mark the header as already
    # written (no second RIFF/fmt/data header at the seek position); own the
    # handle so close() closes it; seed the byte + frame counters so a resume
    # reports and patches prior+new; point the size-field patches at the
    # canonical offsets. _datalength stays at its init default (0) so the
    # _patchheader guard (_datalength != _datawritten) fires on close.
    wf._headerwritten = True  # noqa: SLF001
    wf._i_opened_the_file = f  # noqa: SLF001
    wf._datawritten = prior_data_bytes  # noqa: SLF001
    wf._nframeswritten = prior_nframes  # noqa: SLF001 — getnframes()/tell() report prior+new
    wf._form_length_pos = 4  # noqa: SLF001 — RIFF size, fixed by the RIFF spec
    wf._data_length_pos = data_offset - 4  # noqa: SLF001 — data-size field precedes the samples
    return wf
