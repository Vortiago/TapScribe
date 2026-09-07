"""RED contract for the install-target seam (ADR-0015).

TapScribe now has THREE install topologies, not one:

  * **checkout** — a dev clone. `pip install -e ".[extras]"`, what
    `build_pip_argv` has always done.
  * **Bundle** — the Windows installer. A wheel sits next to the embedded
    interpreter; extras resolve from ITS metadata, so the shipped installer's
    version can never drift from the running server's.
  * **PyPI** — a plain `pip install tapscribe` in someone's venv.

`install_target` is the one place that decides between them. The topology
arrives as an explicit `--install-spec` argument (never a sniffed environment),
so every consumer — the picker, preflight, `/setup` — asks the same function
and a misconfigured Bundle fails loudly instead of silently reverting to
checkout behaviour.

The spec is external input by CodeQL's reckoning (CLAUDE.md: "CLI parsers flow
into many places"), and it flows straight into a pip argv. So it's validated
against an allowlist of three shapes at the boundary, exactly like
`setup_install.validate_selection` does for family/backend keys.
"""

from __future__ import annotations

import sys

import pytest

from tapscribe.install_target import (
    CHECKOUT_SPEC,
    InstallSpecError,
    pip_install_argv,
    resolve_install_spec,
)

# --- resolve_install_spec: the allowlist -----------------------------------


def test_absent_spec_resolves_to_the_checkout():
    """No --install-spec is the dev path. Devs launch on Windows without the
    installer, so this default must stay byte-identical to today's behaviour."""
    assert resolve_install_spec(None) == CHECKOUT_SPEC


def test_existing_wheel_resolves_to_its_absolute_path(tmp_path):
    wheel = tmp_path / "tapscribe-1.1.0-py3-none-any.whl"
    wheel.write_bytes(b"")
    assert resolve_install_spec(str(wheel)) == str(wheel.resolve())


def test_relative_wheel_path_is_absolutised(tmp_path, monkeypatch):
    """The tray may pass a path relative to the install dir; pip runs with
    a different cwd, so the seam resolves it once, here."""
    wheel = tmp_path / "tapscribe-1.1.0-py3-none-any.whl"
    wheel.write_bytes(b"")
    monkeypatch.chdir(tmp_path)
    assert resolve_install_spec("tapscribe-1.1.0-py3-none-any.whl") == str(wheel.resolve())


def test_pinned_pypi_requirement_is_allowed():
    assert resolve_install_spec("tapscribe==1.1.0") == "tapscribe==1.1.0"


def test_missing_wheel_raises_rather_than_reaching_pip(tmp_path):
    """A Bundle whose wheel didn't ship must fail with something an operator can
    act on — not hand pip a path it will report as an unfindable requirement."""
    with pytest.raises(InstallSpecError, match="does not exist"):
        resolve_install_spec(str(tmp_path / "absent.whl"))


@pytest.mark.parametrize(
    "hostile",
    [
        "requests",  # not TapScribe — would install an unrelated package
        "tapscribe",  # unpinned: a Bundle must never float to a newer release
        "tapscribe==1.1.0 --index-url https://evil.example",  # argv smuggling
        "/etc/passwd",  # not a wheel
        "tapscribe-1.1.0.tar.gz",  # sdist: builds from source, not our topology
        "",
    ],
)
def test_unrecognised_specs_are_rejected(hostile):
    with pytest.raises(InstallSpecError):
        resolve_install_spec(hostile)


# --- pip_install_argv: the argv itself -------------------------------------


def test_checkout_argv_is_editable_and_unchanged():
    argv = pip_install_argv(["whisper-live", "whisper-cpu"], python="py")
    assert argv == ["py", "-m", "pip", "install", "-e", ".[whisper-live,whisper-cpu]"]


def test_wheel_argv_is_not_editable(tmp_path):
    """`-e` against a wheel is meaningless — the Bundle installs it normally."""
    wheel = tmp_path / "tapscribe-1.1.0-py3-none-any.whl"
    wheel.write_bytes(b"")
    argv = pip_install_argv(["whisper-cpu"], install_spec=str(wheel), python="py")
    assert "-e" not in argv
    assert argv == ["py", "-m", "pip", "install", f"{wheel.resolve()}[whisper-cpu]"]


def test_no_extras_omits_the_bracket_group(tmp_path):
    wheel = tmp_path / "tapscribe-1.1.0-py3-none-any.whl"
    wheel.write_bytes(b"")
    assert pip_install_argv([], install_spec=str(wheel), python="py")[-1] == str(wheel.resolve())
    assert pip_install_argv([], python="py")[-1] == CHECKOUT_SPEC


def test_python_defaults_to_the_running_interpreter():
    """Keeps the install inside whichever venv is executing — the bundled one in
    a Bundle, the dev's .venv in a checkout."""
    assert pip_install_argv([])[0] == sys.executable


def test_pinned_pypi_argv_puts_extras_before_the_version(tmp_path):
    """PEP 508 requires `name[extras]==version`, not `name==version[extras]`.

    Getting it backwards produces a requirement pip refuses to parse at all
    ("Expected end or semicolon (after version specifier)"), so the PyPI
    topology — which `resolve_install_spec` accepts and all three `--install-spec`
    help strings advertise — failed on the FIRST model install and on every
    preflight `[summarize]` step. The `.` and `.whl` shapes are unaffected: pip
    routes paths through a different parser that strips extras itself.
    """
    from packaging.requirements import Requirement

    argv = pip_install_argv(["whisper-live", "whisper-cpu"], install_spec="tapscribe==1.1.0", python="py")
    requirement = argv[-1]
    assert requirement == "tapscribe[whisper-live,whisper-cpu]==1.1.0"
    # Independent source of truth: the real PEP 508 parser must accept it.
    parsed = Requirement(requirement)
    assert parsed.name == "tapscribe"
    assert parsed.extras == {"whisper-live", "whisper-cpu"}
    assert str(parsed.specifier) == "==1.1.0"


def test_pinned_pypi_argv_without_extras_is_the_bare_pin():
    assert pip_install_argv([], install_spec="tapscribe==1.1.0", python="py")[-1] == "tapscribe==1.1.0"
