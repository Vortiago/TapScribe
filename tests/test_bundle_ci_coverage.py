"""Every Bundle project must be built by CI, and every Bundle test project run.

The sibling rule to `test_tray_bridge_ci_coverage.py`, and it needs its own file
because the Bundle's answer is different. The tray Bridge solution is built by no
job at all (it holds a `net10.0-macos` project, so each OS names its own project
paths); the Bundle solution IS buildable end to end on Windows, and `bundle-build`
builds the whole `.slnx`. So "is it compiled" is answered by construction here.

What is NOT answered by construction is whether its TESTS run. `dotnet build` of a
solution compiles a test project without running a single test, and the two
`dotnet test` steps name individual projects. A third test project added to
`packaging/bundle/tests/` therefore compiles green, reports nothing, and reads as
coverage — the same shape as the `[RequiresWindows]` skip that let this branch's
CI stay red for five commits, one level up.

Both halves are pinned: that the solution build still names the solution (lose
that and "built by definition" quietly stops being true), and that every test
project is named on a `dotnet test` line.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BUNDLE = REPO_ROOT / "packaging" / "bundle"
SOLUTION = BUNDLE / "TapScribe.Bundle.slnx"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _live_lines() -> list[str]:
    """CI's `run:` text with commented lines dropped — a step commented out must
    stop counting as coverage, which is the drift worth catching."""
    return [
        line
        for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]


def _bundle_test_projects() -> set[Path]:
    """Every test project on disk under the Bundle tree, repo-relative.

    Read from the FILESYSTEM rather than from the solution: a project missing from
    the solution is a different (and louder) problem, and one added to neither is
    exactly the case that must not slip through.
    """
    found = {p.relative_to(REPO_ROOT) for p in (BUNDLE / "tests").rglob("*.csproj")}
    assert found, "no test projects found under packaging/bundle/tests/"
    return found


def test_the_bundle_solution_build_still_names_the_solution() -> None:
    # What makes every project in the solution compiled-by-definition. If this step
    # ever names individual projects instead, the coverage question below has to be
    # asked about building too, not only about testing.
    assert any("dotnet build" in line and "TapScribe.Bundle.slnx" in line for line in _live_lines()), (
        "no CI step builds packaging/bundle/TapScribe.Bundle.slnx"
    )
    assert SOLUTION.exists(), SOLUTION


def test_every_bundle_test_project_is_run_by_some_ci_job() -> None:
    tested = {
        Path(path)
        for line in _live_lines()
        if "dotnet test" in line
        for path in re.findall(r"packaging/bundle/\S+\.csproj", line)
    }
    unrun = sorted(str(p) for p in _bundle_test_projects() - tested)
    assert not unrun, (
        "these Bundle test projects are compiled by the solution build but no CI "
        "step RUNS them, so they report as coverage while asserting nothing. Add a "
        f"`dotnet test` step to the job for their platform in ci.yml: {unrun}"
    )


def test_every_ci_named_bundle_project_exists() -> None:
    # The other direction: a renamed or moved project leaves a `dotnet test` step
    # pointing at nothing, which fails loudly on the affected OS only — and the
    # affected OS is Windows for one of the two.
    named = {
        Path(path) for line in _live_lines() for path in re.findall(r"packaging/bundle/\S+\.csproj", line)
    }
    assert named, "no CI step names a Bundle project"
    missing = sorted(str(p) for p in named if not (REPO_ROOT / p).exists())
    assert not missing, f"CI names Bundle projects that do not exist: {missing}"
