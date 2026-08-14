"""Every project in the tray Bridge solution must actually be built by CI.

`bridges/tray-bridge/` is a .NET solution, but no CI job builds the `.slnx`:
each OS names its own project paths, because a `net10.0-macos` target needs
Xcode and a `net10.0-windows` one needs Windows (see the comment above
`dotnet-build` in `ci.yml`). That is the right split and it leaves a hole:
a project added to the solution but to no job is silently never compiled and
never tested, and the `.slnx` looks complete to whoever added it.

The rule is coverage, not naming. A job may build a project directly or reach
it through a `ProjectReference` from one it names: `TapScribe.Bridge.Core` has
no step of its own and never needs one, because every test project references
it. What must never happen is a project no job reaches at all.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIDGE = REPO_ROOT / "bridges" / "tray-bridge"
SOLUTION = BRIDGE / "TapScribe.TrayBridge.slnx"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _solution_projects() -> set[Path]:
    """Every project the solution lists, as a repo-relative path."""
    paths = re.findall(r'<Project\s+Path="([^"]+)"', SOLUTION.read_text(encoding="utf-8"))
    assert paths, "the solution lists no projects at all"
    return {(BRIDGE / p).resolve().relative_to(REPO_ROOT) for p in paths}


def _project_references(project: Path) -> set[Path]:
    """The projects `project` references directly, as repo-relative paths."""
    text = (REPO_ROOT / project).read_text(encoding="utf-8")
    refs = re.findall(r'<ProjectReference\s+Include="([^"]+)"', text)
    directory = (REPO_ROOT / project).parent
    return {(directory / ref.replace("\\", "/")).resolve().relative_to(REPO_ROOT) for ref in refs}


def _projects_named_by_ci() -> set[Path]:
    """Every tray-bridge project a CI step names.

    Scanned as text rather than parsed as YAML: a project path only ever appears
    inside a `run:` block, the check does not care which job names it, and this
    file must not depend on PyYAML being installed to have an opinion. Commented
    lines are dropped, so a step commented out stops counting as coverage, which
    is exactly the drift worth catching.
    """
    named: set[Path] = set()
    for line in WORKFLOW.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        for path in re.findall(r"bridges/tray-bridge/\S+\.csproj", line):
            named.add(Path(path))
    assert named, "no CI step builds a tray-bridge project"
    return named


def _reachable_from(roots: set[Path]) -> set[Path]:
    """`roots` plus everything they reach through ProjectReference: what a
    `dotnet build` of each root actually compiles."""
    seen: set[Path] = set()
    pending = list(roots)
    while pending:
        project = pending.pop()
        if project in seen:
            continue
        seen.add(project)
        pending.extend(_project_references(project))
    return seen


def test_every_solution_project_is_built_by_some_ci_job() -> None:
    solution = _solution_projects()
    built = _reachable_from(_projects_named_by_ci())
    unbuilt = sorted(str(p) for p in solution - built)
    assert not unbuilt, (
        "these projects are in TapScribe.TrayBridge.slnx but no CI job builds them, "
        "directly or through a ProjectReference. Add a step to the job for their "
        f"platform in .github/workflows/ci.yml: {unbuilt}"
    )


def test_every_ci_named_project_is_in_the_solution() -> None:
    # The other direction: a project CI builds but the solution omits is invisible
    # in an editor, so nobody opening the solution sees it, and its tests are the
    # ones nobody runs locally before pushing.
    missing = sorted(str(p) for p in _projects_named_by_ci() - _solution_projects())
    assert not missing, (
        f"these projects are built by CI but missing from TapScribe.TrayBridge.slnx: {missing}"
    )
