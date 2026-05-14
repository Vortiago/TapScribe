"""Shared pytest fixtures.

The package is imported only after we redirect the recording / config dirs
to a per-session tmpdir — otherwise importing tapscribe.config from a CI
runner would create `recordings/` next to the repo and pollute the
worktree.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the in-tree package importable when pytest is invoked from the repo
# root without an editable install (CI's most common shape).
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def tmp_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the package's CONFIG_DIR + the three config files at a tmpdir.

    Tests that want to exercise prompt/hotwords/hallucinations reads write
    files into this dir directly.
    """
    from tapscribe import config

    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "PROMPT_FILE", cfg / "prompt.txt")
    monkeypatch.setattr(config, "HOTWORDS_FILE", cfg / "hotwords.txt")
    monkeypatch.setattr(config, "HALLUCINATIONS_FILE", cfg / "hallucinations.txt")
    return cfg
