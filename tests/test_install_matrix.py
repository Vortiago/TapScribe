"""Meta-test for `.github/workflows/install-matrix.yml`'s `family` axis.

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
that job doesn't install. `pyproject.toml` is parsed with stdlib `tomllib`,
the same approach `tests/test_install_picker.py` uses.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_MATRIX_YML = REPO_ROOT / ".github" / "workflows" / "install-matrix.yml"


def test_install_matrix_families_are_valid_pyproject_extras():
    workflow_text = INSTALL_MATRIX_YML.read_text()
    m = re.search(r"family:\s*\[([^\]]+)\]", workflow_text)
    assert m, "couldn't find the `family:` matrix axis in install-matrix.yml"
    families = [f.strip() for f in m.group(1).split(",")]
    assert families, "install-matrix.yml's family axis is empty"

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    extras = pyproject["project"]["optional-dependencies"]

    missing = [f for f in families if f not in extras]
    assert not missing, (
        f"install-matrix.yml lists families with no matching pyproject.toml extra: {missing} — "
        "that matrix cell's `pip install` silently installs nothing and the job passes vacuously"
    )
