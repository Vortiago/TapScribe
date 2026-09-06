"""The three things the Bundle's C# tray says about the Recorder it boots.

`BundleDefaults.RecorderPort`, `BundleDefaults.DashboardUser` and the `"path"` key
`LoginLink` reads out of `POST /api/login-link` are the Recorder's own values,
re-typed on the other side of a language boundary. CLAUDE.md's rule for that shape
is a mechanical lock-step check — `tools/stamp_tap_wire.py` plus
`tests/test_tap_wire_contract.py` for the `/tap` wire — and these three had none.

Each side is tested today and both stay green through a drift, which is what makes
this worth pinning:

- `LoginLinkTests.cs` asserts the `path` key against a FAKE handler it writes itself;
- `test_a_minted_link_is_a_path_not_a_url` asserts it against the real route;
- `RealRecorderMeetingE2ETests.cs` never touches LoginLink at all.

So change `AUTH_USER` and every mint 401s — which `LoginLink` answers by silently
opening the dashboard signed out, exactly the fallback it was written for, with the
operator seeing a password prompt and no error anywhere. This test is the cheap half
of the answer; the expensive half is a C#-`SignedInUrl`-against-live-Python E2E,
which needs a Recorder and a `dotnet` leg in the same job.

A stamper is deliberately NOT the answer here. The `/tap` wire has four languages
and a dozen constants restated in prose; this is three values in one direction, and
a check that fails loudly costs a fraction of a generator nobody would run.
"""

from __future__ import annotations

import re
from pathlib import Path

from tapscribe import config

REPO_ROOT = Path(__file__).resolve().parent.parent
BUNDLE_CORE = REPO_ROOT / "packaging" / "bundle" / "src" / "TapScribe.Bundle.Core"
DEFAULTS = BUNDLE_CORE / "RecorderCommand.cs"
LOGIN_LINK = BUNDLE_CORE / "LoginLink.cs"


def _const(source: Path, declaration: str) -> str:
    """The initialiser of one `const` in a C# file, as written."""
    text = source.read_text(encoding="utf-8")
    found = re.search(rf"\bconst\s+{re.escape(declaration)}\s*=\s*([^;]+);", text)
    assert found, f"{source.name} no longer declares `const {declaration}`"
    return found.group(1).strip()


def test_the_tray_and_the_recorder_agree_on_the_port() -> None:
    assert _const(DEFAULTS, "int RecorderPort") == str(config.PORT), (
        "BundleDefaults.RecorderPort and config.PORT have drifted. The tray would boot the "
        "Recorder on one port and point Open dashboard at the other."
    )


def test_the_tray_and_the_recorder_agree_on_the_basic_username() -> None:
    assert _const(DEFAULTS, "string DashboardUser") == f'"{config.AUTH_USER}"', (
        "BundleDefaults.DashboardUser and config.AUTH_USER have drifted. Every login-link "
        "mint would 401, and LoginLink answers a 401 by silently opening the dashboard "
        "signed out — so the failure reaches the operator as a password prompt and reaches "
        "no log as an error."
    )


def test_the_tray_reads_the_key_the_mint_route_writes() -> None:
    # Read off the route rather than hard-coded here, so this test cannot become the third
    # place the key is spelled.
    route = (REPO_ROOT / "tapscribe" / "routes" / "login.py").read_text(encoding="utf-8")
    written = re.search(r'return \{"(\w+)": f"/login\?', route)
    assert written, "routes/login.py no longer answers the mint with a single-key object"

    read = re.search(r'GetProperty\("(\w+)"\)', LOGIN_LINK.read_text(encoding="utf-8"))
    assert read, "LoginLink.cs no longer reads a named property off the mint response"

    assert read.group(1) == written.group(1), (
        f"the mint writes {written.group(1)!r} and the tray reads {read.group(1)!r}; "
        "LoginLink treats the miss as 'could not mint' and opens the dashboard signed out"
    )
