"""`TapScribe.Bridge.Core` must not name a Windows-specific exception type.

The capture seam (`IAudioDeviceEnumerator`, `IAudioCapture`) declares
`ExternalException` as its native failure type. Windows' `COMException` derives
from it, so on Windows a filter naming either one catches the same throws and
the wrong spelling is invisible: slice 0B-1 converted five of seven catch sites
and the two it missed stayed green. Under Core Audio those two stop catching,
and a throw either escapes a teardown documented as throw-free or sinks a whole
meeting instead of skipping one dead device.

`src/TapScribe.Bridge.Core/BannedSymbols.txt` makes a symbol USAGE (construction,
member access) a build error via BannedApiAnalyzers. RS0030 deliberately does not
report a type reference in a `catch` clause or a type pattern, which is the shape
the defect took, so the analyzer owns the usages and this owns the spelling.

Comments are exempt on purpose: several seams name `COMException` in prose,
because saying that it derives from `ExternalException` is how a backend author
learns which of the two to raise.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parent.parent / "bridges" / "tray-bridge" / "src" / "TapScribe.Bridge.Core"

#: A `//` or `///` line comment, or a `/* … */` block.
_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)


# rglob, not glob: a gate that stops covering the moment Core grows a subdirectory is
# worse than no gate, because it still reports green. The assert is the other half of
# that: an empty parametrize also passes.
SOURCES = sorted(CORE.rglob("*.cs"))
assert SOURCES, f"no C# sources under {CORE}: the gate is pointed at the wrong path"


@pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.name)
def test_bridge_core_names_no_windows_specific_exception_type(source: Path) -> None:
    code = _COMMENT_RE.sub("", source.read_text(encoding="utf-8"))
    assert "COMException" not in code, (
        f"{source.name} names COMException. The capture seam declares ExternalException "
        "as its native failure type and COMException is one Windows backend's subclass "
        "of it, so a filter naming it stops catching under Core Audio. See "
        "bridges/tray-bridge/src/TapScribe.Bridge.Core/BannedSymbols.txt."
    )
