"""End-to-end coverage for the install picker's skip-when-unchanged logic.

The unit suite in `tests/test_install_picker.py` mocks `run_install`, so it
proves the *decision* logic but never exercises a real `pip install`, a real
on-disk stamp, or the real `importlib.find_spec` presence probe wired
together. This test closes that gap: it stands up a throwaway venv and a
trivial dependency-free package, then drives the real `install_picker.main`
in a *fresh subprocess per run* — exactly how repeated `start.sh` launches
hit it — and asserts the observable outcome (`pip` ran vs. was skipped) for
each scenario:

  1. clean slate (nothing installed, no stamp) → real install, stamp written
  2. unchanged selection + pyproject + package present → skip
  3. pyproject.toml edited (dependency bump) → real install again
  4. unchanged again → skip (proves the stamp was refreshed in step 3)
  5. package uninstalled out-of-band but stamp still current → real install
     (proves the find_spec guard won't skip a vanished package)

Marked `real_pip` and self-skips when a trivial editable install can't be
built (e.g. no outbound network for the PEP 517 build backend), so it never
turns into a false CI failure.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.real_pip

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Runs ONE install_picker.main() against a redirected catalog/paths. Only the
# paths and the (no-extras) catalog are swapped so the install is a trivial,
# dependency-free package; run_install -> real pip, the stamp read/write,
# pyproject_fingerprint, and package_is_installed -> real importlib.find_spec
# all execute for real.
_DRIVER = """\
import os, sys, pathlib
# The picker lives in the package now (ADR-0015). This driver runs inside a
# throwaway venv where tapscribe is NOT installed, so point sys.path at the
# source tree — importable without installing because tapscribe/__init__.py
# has no imports and the picker is stdlib-only.
sys.path.insert(0, os.environ["IP_SRC"])
from tapscribe import install_picker as ip

ip.REPO_ROOT = pathlib.Path(os.environ["IP_REPO"])
ip.STATE_FILE = pathlib.Path(os.environ["IP_STATE"])
ip.STAMP_FILE = pathlib.Path(os.environ["IP_STAMP"])
ip.FAMILIES = (
    ip.FamilyDef(
        key="demo", label="Demo", description="d", size_hint="s",
        backends=(ip.BackendDef(key=ip.BACKEND_CPU, label="CPU", extras=()),),
        default_selected=True,
    ),
)
ip.detect_caps = lambda **k: ip.MachineCaps(os_name="Linux", arch="x86_64", mlx=False, cuda=False)
# Still the REAL probe (importlib.metadata), just aimed at the distribution
# we actually install in the sandbox instead of `tapscribe`.
_real = ip.package_is_installed
ip.package_is_installed = lambda: _real(os.environ["IP_PKG"])

raise SystemExit(ip.main(["--non-interactive"]))
"""

_PYPROJECT = """\
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "tapscribe-skip-demo"
version = "0.1.0"
requires-python = ">=3.8"
# fingerprint-marker
"""


def _venv_python(venv: Path) -> Path:
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


@pytest.fixture
def sandbox(tmp_path):
    pkg_dir = tmp_path / "repo" / "demo_pkg"
    pkg_dir.mkdir(parents=True)
    pyproject = tmp_path / "repo" / "pyproject.toml"
    pyproject.write_text(_PYPROJECT)
    (pkg_dir / "__init__.py").write_text("x = 1\n")

    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)

    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER)

    py = _venv_python(venv)
    stamp = venv / ".tapscribe-install-stamp.json"
    env = {
        **os.environ,
        "IP_SRC": str(REPO_ROOT),
        "IP_REPO": str(tmp_path / "repo"),
        "IP_STATE": str(tmp_path / "state.json"),
        "IP_STAMP": str(stamp),
        # The DISTRIBUTION name, not the import name: package_is_installed now
        # asks importlib.metadata, because find_spec stopped being able to
        # answer "is it installed" once the picker moved into the package.
        "IP_PKG": "tapscribe-skip-demo",
    }

    def run() -> tuple[int, str]:
        proc = subprocess.run(
            [str(py), str(driver)],
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        return proc.returncode, proc.stdout + proc.stderr

    def uninstall() -> None:
        subprocess.run(
            [str(py), "-m", "pip", "uninstall", "-y", "tapscribe-skip-demo"],
            capture_output=True,
            text=True,
            check=True,
        )

    return types.SimpleNamespace(run=run, uninstall=uninstall, pyproject=pyproject, stamp=stamp)


def _ran_pip(output: str) -> bool:
    return "Running:" in output and "skipping pip" not in output


def _skipped_pip(output: str) -> bool:
    return "skipping pip" in output and "Running:" not in output


def test_skip_install_lifecycle_with_real_pip(sandbox):
    # 1. Clean slate → a real `pip install -e .` runs and a stamp is written.
    #    A non-zero rc here means this environment can't build a trivial
    #    editable package (no network for the build backend) — skip rather
    #    than fail, mirroring the repo's other prerequisite-gated e2e tests.
    rc, out = sandbox.run()
    if rc != 0:
        pytest.skip(f"real editable install unavailable here; skipping.\n{out[-800:]}")
    assert _ran_pip(out), out
    assert sandbox.stamp.exists()

    # 2. Nothing changed and the package is present → pip is skipped.
    rc, out = sandbox.run()
    assert rc == 0, out
    assert _skipped_pip(out), out

    # 3. A real edit to pyproject.toml (dependency bump) changes the
    #    fingerprint → pip runs again.
    sandbox.pyproject.write_text(_PYPROJECT.replace("# fingerprint-marker", "dependencies = []"))
    rc, out = sandbox.run()
    assert rc == 0, out
    assert _ran_pip(out), out

    # 4. Unchanged again → skipped, proving step 3 refreshed the stamp.
    rc, out = sandbox.run()
    assert rc == 0, out
    assert _skipped_pip(out), out

    # 5. Package removed out-of-band while the stamp stays current → the
    #    find_spec guard must force a reinstall, not trust the stale stamp.
    sandbox.uninstall()
    rc, out = sandbox.run()
    assert rc == 0, out
    assert _ran_pip(out), out
