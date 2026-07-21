"""Every workflow `run:` block must be valid shell for EVERY matrix lane.

A `run:` block is a template. `${{ matrix.foo }}` for a key an `include` entry
omits renders to the EMPTY STRING, and the result still has to be a valid
script. That is not obvious from reading the YAML, and it is not covered by
`yaml.safe_load` (the YAML is fine — the shell it produces is not).

It bit for real: an install step gained

    if [ -n "${{ matrix.pre_install }}" ]; then
      ${{ matrix.pre_install }}
    fi

which renders on any lane WITHOUT `pre_install` to `if [ -n "" ]; then` with an
empty body — a bash syntax error. The step died before running a command, so
three lanes that had nothing to do with the change went red simultaneously,
and nothing local caught it because the repo's test suite never renders a
workflow.

This walks every job, expands each matrix lane, and runs `bash -n` on the
result. It is deliberately a SYNTAX check only: it cannot tell you the commands
are correct, just that the shell will get as far as running them.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML needed to parse the workflows")

_WORKFLOWS = sorted((Path(__file__).resolve().parent.parent / ".github" / "workflows").glob("*.yml"))

#: `${{ matrix.x || 'fallback' }}` — take the fallback, which is what Actions
#: does when the key is absent.
_FALLBACK_RE = re.compile(r"\$\{\{\s*matrix\.\w+\s*\|\|\s*'([^']*)'\s*\}\}")
#: Any other `${{ … }}`. Rendered to a placeholder rather than "" so a step that
#: legitimately interpolates into a word (`pip install ${{ env.PKG }}`) doesn't
#: become a syntax error for the wrong reason. The empty-body hazard this test
#: exists for survives, because an expression alone on its own line still
#: collapses when the KEY IS ABSENT — handled explicitly below.
_EXPR_RE = re.compile(r"\$\{\{[^}]*\}\}")


#: Shells `bash -n` can check. Matched EXACTLY, not by substring — `"sh" in
#: "pwsh"` is True, which silently pulled every PowerShell step into the check
#: and reported three false failures the first time this ran.
_POSIX_SHELLS = frozenset({"bash", "sh"})


def _is_posix_shell(shell: str, job: dict) -> bool:
    """Whether this step runs under a shell `bash -n` can parse.

    An explicit `shell:` wins. Otherwise GitHub's DEFAULT depends on the
    runner: bash on Linux/macOS, but `pwsh` on Windows — so a step with no
    `shell:` in a `windows-*` job is PowerShell and must be skipped.
    """
    if shell:
        return shell.split()[0] in _POSIX_SHELLS
    return "windows" not in str(job.get("runs-on", "")).lower()


def _lanes(job: dict) -> list[dict]:
    matrix = job.get("strategy", {}).get("matrix")
    if not isinstance(matrix, dict):
        return [{}]
    lanes = [dict(e) for e in matrix.get("include", []) if isinstance(e, dict)]
    return lanes or [{}]


def _render(script: str, lane: dict) -> str:
    for key, value in lane.items():
        script = script.replace(f"${{{{ matrix.{key} }}}}", str(value))
    # Absent keys first: Actions renders them empty, which is the whole hazard.
    script = _FALLBACK_RE.sub(r"\1", script)
    script = re.sub(r"\$\{\{\s*matrix\.\w+\s*\}\}", "", script)
    return _EXPR_RE.sub("X", script)


def _run_steps() -> list[tuple[str, str, str, str]]:
    out: list[tuple[str, str, str, str]] = []
    for wf in _WORKFLOWS:
        doc = yaml.safe_load(wf.read_text(encoding="utf-8")) or {}
        for job_name, job in (doc.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            # PowerShell / cmd steps aren't bash; skip rather than mis-report.
            job_shell = str(job.get("defaults", {}).get("run", {}).get("shell", ""))
            for step in job.get("steps") or []:
                script = step.get("run") if isinstance(step, dict) else None
                if not script:
                    continue
                shell = str(step.get("shell", job_shell)).strip()
                if not _is_posix_shell(shell, job):
                    continue
                for lane in _lanes(job):
                    label = lane.get("lane") or lane.get("os") or "default"
                    out.append(
                        (
                            f"{wf.name}:{job_name}",
                            str(step.get("name", "?")),
                            str(label),
                            _render(script, lane),
                        )
                    )
    return out


_STEPS = _run_steps()


def test_there_are_run_blocks_to_check():
    """Floor: a rename of .github/workflows or a parse change must not turn this
    file into a silent no-op that reports green having checked nothing."""
    assert len(_STEPS) > 20, f"expected many run blocks, found {len(_STEPS)}"


@pytest.mark.parametrize(
    ("where", "step", "lane", "script"),
    _STEPS,
    ids=[f"{w}|{s}|{lane}"[:90] for w, s, lane, _ in _STEPS],
)
def test_run_block_is_valid_shell_for_every_matrix_lane(where, step, lane, script):
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        result = subprocess.run(["bash", "-n", path], capture_output=True, text=True)  # nosec B603,B607
    finally:
        Path(path).unlink(missing_ok=True)

    assert result.returncode == 0, (
        f"{where} step {step!r} renders invalid shell for lane {lane!r}:\n"
        f"{result.stderr.strip()}\n--- rendered ---\n{script}"
    )
