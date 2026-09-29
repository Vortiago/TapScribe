"""Meta-tests for `.github/workflows/install-matrix.yml`: its `family` axis
and its llama-cpp wheel index.

The workflow's whole purpose is catching `pip install -e ".[<family>]"`
regressions per model family (see the workflow's own header comment). A
family whose extra doesn't exist in `pyproject.toml` doesn't error on
install — pip just installs the base package — so that matrix cell passes
green while testing nothing (#263: this is exactly what happened to the
`canary` family after Canary/NeMo was removed).

No `pyyaml`: the workflow file is parsed with a plain regex rather than a
real YAML parser because `pyyaml` is NOT in the `tests` CI job's install
list (`.github/workflows/ci.yml`'s `Install runtime + test dependencies`
step) — it's only pulled in transitively by the `dev`/bandit extra, which
that job doesn't install. The pyproject.toml lookup reuses conftest's
`atomic_extras`, which already owns "does this extra exist in
`[project.optional-dependencies]`" for the same reason (a picker family
whose extra was silently removed).
"""

from __future__ import annotations

import re
from pathlib import Path

from conftest import atomic_extras

from tapscribe import preflight

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_MATRIX_YML = REPO_ROOT / ".github" / "workflows" / "install-matrix.yml"


def test_install_matrix_families_are_valid_pyproject_extras():
    workflow_text = INSTALL_MATRIX_YML.read_text()
    m = re.search(r"family:\s*\[([^\]]+)\]", workflow_text)
    assert m, "couldn't find the `family:` matrix axis in install-matrix.yml"
    families = [f.strip() for f in m.group(1).split(",")]
    assert families, "install-matrix.yml's family axis is empty"

    for family in families:
        # `atomic_extras` asserts the extra exists in pyproject.toml's
        # optional-dependencies and names it on failure — the exact check a
        # family whose extra was silently removed (#263: `canary`) needs.
        atomic_extras(family)


def test_install_matrix_wheel_index_matches_preflight():
    r"""The workflow restates the prebuilt llama-cpp wheel index that
    `preflight.LLAMA_CPP_WHEEL_INDEX` declares. This test ties every
    `--extra-index-url` in the workflow to that one declaration, so a bumped
    index cannot land in only one of them. The `[^\s)]` stops before the bash
    `)` that closes the array append."""
    workflow_text = INSTALL_MATRIX_YML.read_text()
    urls = re.findall(r"--extra-index-url\s+([^\s)]+)", workflow_text)
    assert urls, "couldn't find an `--extra-index-url` in install-matrix.yml"
    assert set(urls) == {preflight.LLAMA_CPP_WHEEL_INDEX}, urls
