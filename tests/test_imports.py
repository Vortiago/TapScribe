"""Sanity check: every top-level module imports without pulling in a heavy
optional backend at module load. If someone adds an unguarded `import torch`
or `import faster_whisper` at the top of a file, this test fails on CI
(which doesn't install those deps)."""

from __future__ import annotations

import importlib


def test_lightweight_modules_import():
    for name in [
        "tapscribe",
        "tapscribe.config",
        "tapscribe.text",
        "tapscribe.hallucinations",
        "tapscribe.audio",
        "tapscribe.strip_silence",
        "tapscribe.nb_whisper",
    ]:
        importlib.import_module(name)
