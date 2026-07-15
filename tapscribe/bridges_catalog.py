"""Distributable-bridge catalog — the download entries the dashboard offers.

A tiny declarative table (mirroring `summarizers/catalog.py`'s tuple-catalog
style) mapping each downloadable Bridge to its GitHub-Release asset name. Each
`filename` is a stable, unversioned asset and a HARD CONTRACT with the release
build (`.github/workflows/release.yml` attaches assets under exactly these
names), so `GET /api/bridges` composes permanent
`releases/latest/download/<filename>` URLs from `config.GITHUB_REPO` (ADR-0012).
The card's visible copy (labels, install steps) lives with the card in
`views.html`, like every other Settings card; this table owns only the
id-to-asset mapping the URL needs.

`local-test-bridge` is deliberately excluded — it is a dev harness, not a
shippable Bridge.
"""

from __future__ import annotations

from typing import NamedTuple


class BridgeArtifact(NamedTuple):
    """One downloadable Bridge release asset. Immutable — the catalog is a
    constant table, not runtime state. `GET /api/bridges` serializes each row
    with the free `NamedTuple._asdict()` and appends the composed download_url."""

    id: str  # stable identity the frontend matches an anchor against
    filename: str  # the release asset name (HARD contract with release.yml)


# The two distributable Bridges. Unversioned, stable filenames so
# `releases/latest/download/<filename>` always resolves to the newest tagged
# release's asset (D7 in the plan). Order is the card's display order.
BRIDGE_ARTIFACTS: tuple[BridgeArtifact, ...] = (
    BridgeArtifact(id="spacialchat", filename="tapscribe-spacialchat-bridge.zip"),
    BridgeArtifact(id="windows-tray", filename="TapScribe.TrayBridge-win-x64.zip"),
)
