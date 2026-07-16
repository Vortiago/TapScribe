"""Distributable-bridge catalog — the download entries the dashboard offers.

A tiny declarative table (mirroring `summarizers/catalog.py`'s tuple-catalog
style) listing the Bridges an operator can download straight from the dashboard
Settings "Get a bridge" card. Each row is a stable, unversioned GitHub-Release
asset: the `filename` values are a HARD CONTRACT with the release build
(`.github/workflows/release.yml` attaches assets under exactly these names), so
the dashboard's `releases/latest/download/<filename>` URLs stay permanent across
versions (ADR-0012).

`local-test-bridge` is deliberately excluded — it is a dev harness, not a
shippable Bridge.
"""

from __future__ import annotations

from typing import Any, NamedTuple


class BridgeArtifact(NamedTuple):
    """One downloadable Bridge release asset. Immutable — the catalog is a
    constant table, not runtime state."""

    id: str  # stable identity the frontend matches an anchor against
    label: str  # human-friendly card label
    filename: str  # the release asset name (HARD contract with release.yml)
    kind: str  # artifact shape: "chrome-extension" | "windows-exe"
    notes: str  # one-line install guidance shown under the download link

    def to_mapping(self) -> dict[str, Any]:
        """JSON-friendly view (without the composed download_url, which
        `GET /api/bridges` adds from `config.GITHUB_REPO`)."""
        return {
            "id": self.id,
            "label": self.label,
            "filename": self.filename,
            "kind": self.kind,
            "notes": self.notes,
        }


# The two distributable Bridges. Unversioned, stable filenames so
# `releases/latest/download/<filename>` always resolves to the newest tagged
# release's asset (D7 in the plan). Order is the card's display order.
BRIDGE_ARTIFACTS: tuple[BridgeArtifact, ...] = (
    BridgeArtifact(
        id="spacialchat",
        label="SpatialChat browser bridge",
        filename="tapscribe-spacialchat-bridge.zip",
        kind="chrome-extension",
        notes=(
            "Unzip, then open chrome://extensions, enable Developer mode, and "
            "Load unpacked the spacialchat-bridge folder."
        ),
    ),
    BridgeArtifact(
        id="windows-tray",
        label="Windows tray bridge",
        filename="TapScribe.TrayBridge-win-x64.zip",
        kind="windows-exe",
        notes=("Copy-and-run .exe (unsigned; Windows SmartScreen may warn — choose More info, Run anyway)."),
    ),
)
