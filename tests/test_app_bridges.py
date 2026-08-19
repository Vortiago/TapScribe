"""Route-level tests for GET /api/bridges (the Settings "Get a bridge" card).

Mirrors tests/test_routes.py's TestClient fixture style: a Recorder rooted at
a tmpdir with auth + auto-start disabled, attached via dependency override.
The endpoint is a pure read over the static bridges_catalog — no Recorder state
is touched — but the fixture keeps it consistent with the rest of the route
suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tapscribe import config as _config
from tapscribe.app import app, get_recorder
from tapscribe.bridges_catalog import BRIDGE_ARTIFACTS
from tapscribe.live import LiveConfig
from tapscribe.recorder import Recorder


@pytest.fixture
def recorder_under_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    monkeypatch.setattr(_config, "AUTH_ENABLED", False)
    monkeypatch.setattr(_config, "AUTO_START_LIVE", False)
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path / "recordings")
    (tmp_path / "config").mkdir()
    (tmp_path / "recordings").mkdir()
    return Recorder(
        recordings_dir=tmp_path / "recordings",
        config_dir=tmp_path / "config",
        live_config=LiveConfig(model="tiny.en", language="en", host="localhost", port=8000),
        use_mlx=False,
        auth_password_file=tmp_path / ".auth-password",
    )


@pytest.fixture
def client(recorder_under_test):
    app.dependency_overrides[get_recorder] = lambda: recorder_under_test
    app.state.recorder = recorder_under_test
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_api_bridges_returns_three_items_with_expected_shape(client):
    r = client.get("/api/bridges")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 3
    for row in body:
        assert set(row) == {"id", "filename", "download_url"}
        assert all(isinstance(row[k], str) and row[k] for k in row)


def test_api_bridges_download_urls_point_at_latest_release_assets(client):
    body = client.get("/api/bridges").json()
    by_id = {row["id"]: row for row in body}

    assert by_id["spacialchat"]["filename"] == "tapscribe-spacialchat-bridge.zip"
    assert (
        by_id["spacialchat"]["download_url"]
        == "https://github.com/Vortiago/TapScribe/releases/latest/download/tapscribe-spacialchat-bridge.zip"
    )

    assert by_id["windows-tray"]["filename"] == "TapScribe.TrayBridge-win-x64.zip"
    assert (
        by_id["windows-tray"]["download_url"]
        == "https://github.com/Vortiago/TapScribe/releases/latest/download/TapScribe.TrayBridge-win-x64.zip"
    )

    assert by_id["macos-tray"]["filename"] == "TapScribe.TrayBridge-osx-arm64.zip"
    assert (
        by_id["macos-tray"]["download_url"]
        == "https://github.com/Vortiago/TapScribe/releases/latest/download/TapScribe.TrayBridge-osx-arm64.zip"
    )


def test_api_bridges_download_url_is_composed_from_config_repo(client):
    """The URL host/slug must come from config.GITHUB_REPO, not be hardcoded in
    the route — so a fork that repoints the constant serves its own links."""
    body = client.get("/api/bridges").json()
    for row in body:
        assert (
            row["download_url"]
            == f"https://github.com/{_config.GITHUB_REPO}/releases/latest/download/{row['filename']}"
        )


def test_api_bridges_matches_catalog(client):
    """The served list is exactly the catalog (order + ids), so the two are
    provably one source of truth."""
    body = client.get("/api/bridges").json()
    assert [row["id"] for row in body] == [a.id for a in BRIDGE_ARTIFACTS]


# ---------------------------------------------------------------------------
# The HARD contract with the release build
# ---------------------------------------------------------------------------


def test_every_catalog_filename_is_an_asset_release_yml_uploads():
    """`BridgeArtifact.filename` is documented as a HARD CONTRACT with
    `.github/workflows/release.yml`: the dashboard composes permanent
    `releases/latest/download/<filename>` URLs from it (ADR-0012). Rename an
    asset in the workflow and nothing else notices — every "Get a bridge" link
    404s from the next tagged release onwards. Read as TEXT (a substring check
    needs no YAML dependency, and the names are literals in the workflow, not
    assembled from variables)."""
    release_yml = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"
    assert release_yml.is_file(), f"release workflow missing at {release_yml}"
    text = release_yml.read_text(encoding="utf-8")
    assert BRIDGE_ARTIFACTS, "the catalog must not be empty or this test proves nothing"
    missing = [a.filename for a in BRIDGE_ARTIFACTS if a.filename not in text]
    assert not missing, f"catalog filenames not produced by release.yml: {missing}"


def test_every_catalog_id_has_a_download_anchor_in_the_settings_card():
    """The other half of that contract, on the dashboard's side. The "Get a
    bridge" card maps catalog ids to its own `data-slot` anchors, and its loop
    skips an id the map does not name: a catalog row with no anchor renders a
    link that never gets an href, which reads as "download unavailable" rather
    than as a wiring mistake. Read as TEXT for the same reason as above."""
    settings_js = (
        Path(__file__).resolve().parents[1] / "tapscribe" / "web" / "js" / "next" / "views" / "settings.js"
    )
    assert settings_js.is_file(), f"settings view missing at {settings_js}"
    text = settings_js.read_text(encoding="utf-8")
    anchors = text[text.index("const bridgeAnchors = {") : text.index("getBridgeCatalog()")]
    missing = [a.id for a in BRIDGE_ARTIFACTS if f'"{a.id}"' not in anchors and f"{a.id}:" not in anchors]
    assert not missing, f"catalog ids with no anchor in the Get-a-bridge card: {missing}"
