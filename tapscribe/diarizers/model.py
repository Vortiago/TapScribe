"""Where the speaker-embedding model lives, and how it gets there.

Fetched at bring-up rather than vendored: 30 MB of weights with a published
sha256 is better provenance than a committed blob, because the fetcher VERIFIES
the digest. `tapscribe.preflight` probes `model_present` and runs this module as
its repair step (`python -m tapscribe.diarizers.model`), so a Bundle — which has
no `start.ps1` to inherit a shell step from — gets it too (ADR-0015).

Stdlib only, for the same reason preflight is: this runs against a venv that may
hold nothing but pip.
"""

from __future__ import annotations

import hashlib
import os
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import config

#: 3D-Speaker CAM++, 512-d, exported by sherpa-onnx (Apache-2.0). Provenance and
#: the separation measurements that picked it: `tests/fixtures/diarize/`.
MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-recongition-models/3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"
)
MODEL_SHA256 = "357a834f702b80161e5b981182c038e18553c1f2ca752ed6cec2052365d4129b"
MODEL_FILENAME = "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"

#: Operator escape hatch to a model they supply, like the summarizer's
#: `TAPSCRIBE_SUMMARIZE_*_MODEL`. Operator-controlled, so the shipped digest
#: does not apply to it.
ENV_MODEL = "TAPSCRIBE_DIARIZE_MODEL"

#: Under BASE_DIR, not the package: a pip-installed site-packages is routinely
#: read-only and is wiped on upgrade.
MODELS_DIR: Path = config.BASE_DIR / "models"

_CHUNK = 1 << 20


class ModelFetchError(RuntimeError):
    """The download did not produce the model — wrong digest, or transport."""


def model_path() -> Path:
    return Path(os.environ.get(ENV_MODEL) or (MODELS_DIR / MODEL_FILENAME))


def model_present() -> bool:
    """A file whose digest matches. Size alone would pass a truncated download,
    which loads as a broken graph much later and blames the engine."""
    path = model_path()
    if not path.is_file():
        return False
    if os.environ.get(ENV_MODEL):
        return True
    return _digest(path) == MODEL_SHA256


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def fetch(*, open_url: Callable[[str], Any] = urllib.request.urlopen) -> Path:
    """Download to a part-file, verify, then rename into place."""
    path = model_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    h = hashlib.sha256()
    try:
        with open_url(MODEL_URL) as body, part.open("wb") as out:
            while chunk := body.read(_CHUNK):
                h.update(chunk)
                out.write(chunk)
        if h.hexdigest() != MODEL_SHA256:
            raise ModelFetchError(f"sha256 mismatch for {MODEL_URL}: got {h.hexdigest()}")
    except ModelFetchError:
        part.unlink(missing_ok=True)
        raise
    except OSError as exc:
        part.unlink(missing_ok=True)
        raise ModelFetchError(f"could not fetch {MODEL_URL}: {exc}") from exc
    os.replace(part, path)
    return path


def main(*, open_url: Callable[[str], Any] = urllib.request.urlopen) -> int:
    """`python -m tapscribe.diarizers.model` — preflight's repair step."""
    if model_present():
        print(f"[diarize-model] already present: {model_path()}", flush=True)
        return 0
    print(f"[diarize-model] fetching {MODEL_URL}", flush=True)
    try:
        path = fetch(open_url=open_url)
    except ModelFetchError as exc:
        print(f"[diarize-model] {exc}", file=sys.stderr, flush=True)
        return 1
    print(f"[diarize-model] wrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
