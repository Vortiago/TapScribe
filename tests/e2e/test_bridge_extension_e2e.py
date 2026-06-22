"""End-to-end tests for the SpatialChat Bridge as a loaded Chrome MV3 extension.

Approach (cleared with track owner): Playwright launches a persistent
Chromium context with `--load-extension`, which loads the bridge as a
real MV3 extension — content.js in the isolated world, page-script.js
injected into the MAIN world, popup.html available at
`chrome-extension://<id>/popup.html`. A static mock SpatialChat page
served via `page.route` interception gives the bridge a realistic
`window.room` surface to attach to. /tap WebSocket frames the bridge
sends land on a small in-test asyncio websockets server (we do NOT
spin up the full Recorder — these are bridge-side integration tests,
not full-pipeline tests).

Why this matters: the existing vm-isolated suite drives content.js by
synthesising postMessage events, and drives page-script.js with mocked
LiveKit + Web Audio. That layer caught wire-shape regressions but
missed three integration-level bugs in PRs #33 (in-page pill), #34
(room-disconnect cleanup), #35 (display-name binding in tap()). These
tests close that gap by running the actual extension in a real
Chromium, where the page-script and content-script worlds interact
through real cross-world postMessage and the audio graph is driven
by an actual oscillator → MediaStreamDestination MediaStreamTrack.

Skipped when Playwright's Chromium isn't installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import socket
import tempfile
import urllib.parse
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

if importlib.util.find_spec("playwright") is None:  # pragma: no cover
    pytest.skip("playwright not installed", allow_module_level=True)

import websockets  # noqa: E402

from .harness import playwright_session  # noqa: E402

pytestmark = pytest.mark.browser_e2e


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EXT_DIR = REPO_ROOT / "bridges" / "spacialchat-bridge"
FIXTURE_DIR = REPO_ROOT / "tests" / "e2e" / "fixtures" / "mock-spatial-page"


# ---------------------------------------------------------------------------
# Test /tap server — accepts every /tap WS, records frames + URL params.
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


@dataclass
class TapConnection:
    """One bridge → /tap WS as seen by the test server."""

    identity: str
    name: str
    utterance_id: str
    frames: list[bytes] = field(default_factory=list)
    subprotocol: str | None = None
    closed: bool = False
    close_code: int | None = None


class FakeTapServer:
    """Minimal stand-in for the Recorder's /tap endpoint.

    Accepts every WS, parses identity / name / utterance_id from the
    query string, appends every binary frame to the matching
    `TapConnection`. We intentionally don't reassemble PCM into WAV —
    these are bridge-side checks; the Recorder has its own test suite
    for the WAV-append + UtteranceIndex.resume path.

    Tests can:
      - read `connections` to see which speakers connected
      - call `drop_connection(identity)` to force-close a WS so the
        bridge's reconnect ladder fires
      - inspect frames per identity / utterance_id
    """

    def __init__(self, *, expected_token: str = "") -> None:
        self.port = _free_port()
        self.expected_token = expected_token
        self.connections: list[TapConnection] = []
        self._server: Any = None
        self._open_ws: dict[str, Any] = {}  # identity -> ws
        # When True, the next incoming /tap WS is closed with 1006 before
        # the handler logs it. Used by the pill-transition test to keep
        # the bridge in a non-ok state long enough to assert on it
        # (without this, the reconnect ladder lands a fresh WS in ~200 ms
        # and the pill flips back to green before we can read it).
        self.refuse_connections: bool = False
        # Identities for which the FIRST connection has already arrived.
        # The pill test wants the initial WS to succeed but later
        # reconnects to be refused; this lets the bridge get into the
        # "ok" state once, THEN flip refuse_connections=True for the
        # blip.
        self._handshakes_seen: int = 0

    async def start(self) -> None:
        async def handler(ws):
            self._handshakes_seen += 1
            if self.refuse_connections:
                # Close 1011 — the bridge treats anything that isn't
                # 1000 (clean) or 4401 (auth) as a recoverable transport
                # blip and keeps the reconnect ladder running. We can't
                # send 1006 from the server (RFC reserves it for browser
                # internal use); 1011 ("server error") has the same
                # effect on the bridge's onclose branch.
                await ws.close(code=1011)
                return
            # websockets 13+: path lives on ws.request, no positional arg.
            request_path = ws.request.path
            qs = urllib.parse.urlparse(request_path).query
            params = dict(urllib.parse.parse_qsl(qs, keep_blank_values=True))
            conn = TapConnection(
                identity=params.get("identity", ""),
                name=params.get("name", ""),
                utterance_id=params.get("utterance_id", ""),
                subprotocol=ws.subprotocol,
            )
            self.connections.append(conn)
            self._open_ws[conn.identity] = ws
            try:
                async for msg in ws:
                    if isinstance(msg, bytes):
                        conn.frames.append(msg)
            except websockets.exceptions.ConnectionClosed:
                pass
            finally:
                conn.closed = True
                conn.close_code = ws.close_code
                if self._open_ws.get(conn.identity) is ws:
                    del self._open_ws[conn.identity]

        # Subprotocol negotiation: echo back any tapscribe.v1.tap.<token>
        # offer whose token matches `expected_token`. When expected_token
        # is empty we accept any subprotocol (mirrors --no-auth recorder).
        def select_subprotocol(ws, offered):
            if not self.expected_token:
                return offered[0] if offered else None
            for proto in offered or ():
                if proto == f"tapscribe.v1.tap.{self.expected_token}":
                    return proto
            return None

        self._server = await websockets.serve(
            handler,
            "localhost",
            self.port,
            select_subprotocol=select_subprotocol,
        )

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()

    async def drop_connection(self, identity: str, *, code: int = 1011) -> bool:
        """Force-close the live WS for `identity` so the bridge's
        reconnect ladder fires. Returns True if a WS was closed."""
        ws = self._open_ws.get(identity)
        if ws is None:
            return False
        # close(1006) is "abnormal" — exactly the shape the bridge treats
        # as a recoverable transport blip (unlike 1000 = clean / 4401 = auth).
        with contextlib.suppress(Exception):
            await ws.close(code=code)
        return True

    def connection_for(self, identity: str, utterance_id: str | None = None) -> TapConnection | None:
        """Latest TapConnection matching `identity` (and `utterance_id` if given)."""
        for conn in reversed(self.connections):
            if conn.identity != identity:
                continue
            if utterance_id is not None and conn.utterance_id != utterance_id:
                continue
            return conn
        return None


@pytest.fixture
async def fake_tap_server() -> AsyncIterator[FakeTapServer]:
    server = FakeTapServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# Loaded extension fixture
# ---------------------------------------------------------------------------


@dataclass
class LoadedExtension:
    """Bundle of everything a test needs to drive the loaded bridge:
    the Playwright context, the SpatialChat tab `page`, and the
    extension ID (so tests can open the popup to mutate storage)."""

    ctx: Any  # BrowserContext — playwright sync types are a pain
    page: Any
    ext_id: str

    async def open_popup(self) -> Any:
        """Open the bridge popup.html so a test can call into
        `chrome.storage.local` (only available from extension contexts)."""
        popup = await self.ctx.new_page()
        await popup.goto(f"chrome-extension://{self.ext_id}/popup.html")
        return popup


@pytest.fixture
async def loaded_bridge(fake_tap_server: FakeTapServer) -> AsyncIterator[LoadedExtension]:
    """Launch a persistent Chromium with the bridge loaded, navigate the
    SpatialChat tab to the mock page, and pre-configure storage so the
    bridge dials the test /tap server on the right port with no token."""
    fixture_index = (FIXTURE_DIR / "index.html").read_text(encoding="utf-8")
    fixture_mock = (FIXTURE_DIR / "mock-room.js").read_text(encoding="utf-8")

    async with playwright_session() as pw:
        # tempfile contextmanager so the user-data-dir is cleaned up
        # even if pytest cancels the test mid-run.
        with tempfile.TemporaryDirectory() as udd:
            try:
                ctx = await pw.chromium.launch_persistent_context(
                    user_data_dir=udd,
                    headless=False,  # MV3 extensions don't load in headless mode
                    args=[
                        f"--disable-extensions-except={EXT_DIR}",
                        f"--load-extension={EXT_DIR}",
                        "--no-sandbox",
                        # Make AudioContext start running without a gesture
                        # so the worklet emits frames as soon as it's wired.
                        "--autoplay-policy=no-user-gesture-required",
                        # Without these, the oscillator-backed MediaStreamTrack
                        # the mock-room.js synthesises won't have permission.
                        "--use-fake-ui-for-media-stream",
                        "--use-fake-device-for-media-stream",
                        # Recent Chromium blocks a public https page (the mock
                        # SpatialChat tab) from opening a ws:// to loopback
                        # (Private Network Access / insecure content), which
                        # silently strands EVERY /tap WS — the bridge dials but
                        # the handshake never lands. In production the operator
                        # runs on localhost or enables TLS; for the test we relax
                        # the policy so the content script's /tap reaches the
                        # in-test server. (The sibling test_bridge_meeting_e2e.py
                        # fixture already carries these; this one predated them.)
                        "--allow-running-insecure-content",
                        "--disable-features=BlockInsecurePrivateNetworkRequests,"
                        "PrivateNetworkAccessSendPreflights,LocalNetworkAccessChecks",
                    ],
                )
            except Exception as e:  # pragma: no cover
                pytest.skip(f"Chromium not available: {e}")

            try:
                ext_id = await _discover_extension_id(ctx)
                await _seed_storage(
                    ctx,
                    ext_id,
                    {
                        "recorderHost": "localhost",
                        "recorderPort": fake_tap_server.port,
                        "tapToken": fake_tap_server.expected_token,
                        "useTls": False,
                    },
                )
                page = await ctx.new_page()

                async def route_handler(route, request):
                    if request.url.endswith("mock-room.js"):
                        await route.fulfill(
                            status=200,
                            content_type="application/javascript",
                            body=fixture_mock,
                        )
                    else:
                        await route.fulfill(
                            status=200,
                            content_type="text/html",
                            body=fixture_index,
                        )

                await page.route("https://app.spatial.chat/**", route_handler)
                await page.goto("https://app.spatial.chat/test/room")
                # Wait for our mock to load AND the page-script.js polled
                # window.room and attached its listeners. The "attached"
                # marker is the AudioContext flipping to running, which
                # only happens once tap() runs — instead we wait for the
                # bridge's in-page pill to mount on documentElement (PR #33
                # contract): every test relies on the pill being there.
                await page.wait_for_function(
                    "typeof window.__tsTest === 'object'",
                    timeout=5000,
                )
                await page.wait_for_function(
                    "!!document.getElementById('__tapscribe_indicator_host__')",
                    timeout=5000,
                )
                yield LoadedExtension(ctx=ctx, page=page, ext_id=ext_id)
            finally:
                with contextlib.suppress(Exception):
                    await ctx.close()


async def _discover_extension_id(ctx) -> str:
    """Look up the bridge's extension ID by opening chrome://extensions
    and reading the manager DOM. The ID is path-derived and stable
    across runs that load from the same EXT_DIR, but we resolve it at
    runtime so tests don't need to hard-code it."""
    page = await ctx.new_page()
    try:
        await page.goto("chrome://extensions/")
        ids = await page.evaluate(
            """
            () => {
              const mgr = document.querySelector('extensions-manager');
              if (!mgr) return [];
              const list = mgr.shadowRoot && mgr.shadowRoot.querySelector('extensions-item-list');
              if (!list) return [];
              const items = list.shadowRoot.querySelectorAll('extensions-item');
              return Array.from(items).map((i) => i.id);
            }
            """
        )
    finally:
        await page.close()
    if not ids:
        raise RuntimeError("Could not discover bridge extension ID")
    return ids[0]


async def _seed_storage(ctx, ext_id: str, values: dict[str, Any]) -> None:
    """Open the extension popup and write `values` into chrome.storage.local.

    The bridge reads these at content.js boot — by seeding before the
    SpatialChat tab is opened we avoid the racy "dial old defaults then
    teardown on storage.onChanged" path the bridge has for live config
    edits.
    """
    popup = await ctx.new_page()
    try:
        await popup.goto(f"chrome-extension://{ext_id}/popup.html")
        await popup.evaluate(
            "(v) => chrome.storage.local.set(v)",
            values,
        )
    finally:
        await popup.close()


# ---------------------------------------------------------------------------
# Pill helpers
# ---------------------------------------------------------------------------


async def read_pill(page) -> dict[str, str] | None:
    """Read the in-page status pill — the PR #33 indicator. Returns
    {kind, label, title} or None if the host element isn't mounted."""
    return await page.evaluate(
        """
        () => {
          const host = document.getElementById('__tapscribe_indicator_host__');
          if (!host || !host.shadowRoot) return null;
          const pill = host.shadowRoot.querySelector('.pill');
          if (!pill) return null;
          const label = pill.querySelector('.label');
          return {
            kind: pill.className.replace(/^pill\\s+/, ''),
            label: label ? label.textContent : '',
            title: host.title || '',
          };
        }
        """
    )


async def wait_for_pill_kind(page, kind: str, *, timeout_ms: int = 5000) -> None:
    """Wait until the pill's `kind` class slot matches `kind`."""
    await page.wait_for_function(
        f"""
        () => {{
          const host = document.getElementById('__tapscribe_indicator_host__');
          if (!host || !host.shadowRoot) return false;
          const pill = host.shadowRoot.querySelector('.pill');
          if (!pill) return false;
          return pill.className.replace(/^pill\\s+/, '') === {json.dumps(kind)};
        }}
        """,
        timeout=timeout_ms,
    )


async def add_speaker(page, identity: str, name: str, *, sidebar_name: str | None = None) -> None:
    """Trigger trackSubscribed in the mock room. Optionally seeds the
    Vue sidebar DOM so getDisplayName() picks the name up from there
    instead of falling back to participant.name."""
    if sidebar_name is not None:
        await page.evaluate(
            "([id, n]) => window.__tsTest.setSidebarUser(id, n)",
            [identity, sidebar_name],
        )
    await page.evaluate(
        "([id, n]) => window.__tsTest.addRemoteSpeaker(id, n)",
        [identity, name],
    )


# ---------------------------------------------------------------------------
# Tests — one per known integration gap
# ---------------------------------------------------------------------------


async def test_pill_transitions_idle_to_ok_to_warn_on_tap_drop(
    loaded_bridge: LoadedExtension,
    fake_tap_server: FakeTapServer,
):
    """Closes the PR #33 integration gap: the in-page status pill must
    visibly reflect the bridge's connection lifecycle on the SpatialChat
    tab itself.

    Idle (no speakers) → green (`N stream(s)` while WS is OPEN and bytes
    flow) → yellow / red (transport error / reconnecting) when the
    Recorder drops the WS mid-utterance.

    Previously the indicator code path was covered only by vm tests
    against a mocked DOM (indicator.test.js); none of that exercise
    proved the shadow-root + documentElement mount survives in real
    Chromium against a real SPA-like page. A regression that breaks
    the mount (CSP, browser policy, attachShadow throwing) would slip
    past the vm suite but fail here.
    """
    page = loaded_bridge.page

    # Idle: pill should report "idle" before any speaker is tapped. The
    # bridge ships with audioContextState=null until ensureAudioGraph
    # runs, so the initial label is "idle" / "loading…".
    pill = await read_pill(page)
    assert pill is not None, "pill must be mounted on documentElement"
    assert pill["kind"] in ("idle", "idle "), f"unexpected initial pill kind: {pill}"
    assert "TapScribe" in pill["label"]

    # Add a speaker → real oscillator drives the worklet → /tap WS opens
    # → pill flips to ok ("1 stream").
    await add_speaker(page, "alice-id", "Alice")
    await wait_for_pill_kind(page, "ok", timeout_ms=5000)
    pill = await read_pill(page)
    assert pill["kind"] == "ok"
    assert "stream" in pill["label"].lower(), f"green pill should mention 'stream', got {pill}"

    # Wait until at least one frame has reached the server — proves the
    # ws is genuinely OPEN, not just constructed.
    await asyncio.sleep(0.3)
    conn = fake_tap_server.connection_for("alice-id")
    assert conn is not None and len(conn.frames) > 0

    # Drop the WS abnormally (code 1006 = transport blip) AND refuse
    # future connection attempts so the bridge can't immediately heal
    # itself back to green. Without the refuse-toggle the bridge would
    # land a fresh WS in ~200 ms (first backoff step) and the pill
    # would flip back to ok before we could read it — a flaky test.
    fake_tap_server.refuse_connections = True
    assert await fake_tap_server.drop_connection("alice-id")
    await page.wait_for_function(
        """
        () => {
          const host = document.getElementById('__tapscribe_indicator_host__');
          if (!host || !host.shadowRoot) return false;
          const pill = host.shadowRoot.querySelector('.pill');
          const kind = pill && pill.className.replace(/^pill\\s+/, '');
          return kind !== 'ok' && kind !== 'idle';
        }
        """,
        timeout=8000,
    )
    pill = await read_pill(page)
    assert pill["kind"] in ("warn", "err"), f"post-drop pill must be warn/err, got {pill}"


async def test_room_disconnect_cleans_up_audio_and_presence_only_taps(
    loaded_bridge: LoadedExtension,
    fake_tap_server: FakeTapServer,
):
    """Closes the PR #34 integration gap: room "disconnected" must tear
    down EVERY announced participant — both audio-tapped speakers AND
    presence-only (joined-muted) rows.

    The vm-isolated page-script.test.js pins the same contract by
    inspecting `postMessage` payloads. This run-through proves the
    contract holds with the real cross-world message channel: the
    /tap WS for the audio-tapped speaker must actually close, and the
    presence-only row's tap-stop must reach content.js so its channel
    is cleared from the popup snapshot too.

    Regression: a prior cleanup that iterated only `taps` (the audio
    map) leaked presence-only `announced` entries. The popup would
    keep showing those phantom rows forever.
    """
    page = loaded_bridge.page

    # An audio-tapped speaker and a presence-only one.
    await add_speaker(page, "speaker-id", "Sam")
    await page.evaluate(
        "() => window.__tsTest.presenceOnlySpeaker('listener-id', 'Lee')",
    )

    # Sam's /tap WS must actually open and start receiving frames.
    await wait_for_pill_kind(page, "ok", timeout_ms=5000)
    await asyncio.sleep(0.3)
    sam_conn = fake_tap_server.connection_for("speaker-id")
    assert sam_conn is not None
    assert not sam_conn.closed, "Sam's /tap WS should still be open"
    initial_frames = len(sam_conn.frames)
    assert initial_frames > 0

    # Disconnect the room. cleanupAllTaps() must iterate `announced`
    # (which includes Lee) and emit tap-stop for both — closing Sam's
    # WS and clearing Lee's popup row.
    await page.evaluate("() => window.__tsTest.disconnectRoom()")

    # Sam's WS should close cleanly. We poll because the close is async
    # all the way through cross-world postMessage + ws.close().
    async def sam_closed() -> bool:
        c = fake_tap_server.connection_for("speaker-id")
        return c is not None and c.closed

    deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline:
        if await sam_closed():
            break
        await asyncio.sleep(0.1)
    assert await sam_closed(), "Sam's /tap WS must close on room disconnect"

    # The popup-side snapshot (bridgeStatus in chrome.storage.local) is
    # the most reliable proxy for "did Lee's row get cleared?" — the
    # popup tab isn't open here, but content.js writes to storage on
    # every cleanup. Read it via the popup page.
    popup = await loaded_bridge.open_popup()
    try:
        # Wait for the post-disconnect publishStatus() to write the
        # cleared channel list to storage. If cleanupAllTaps iterated
        # `taps` alone, Lee's channel would remain.
        await popup.wait_for_function(
            """
            async () => {
              const { bridgeStatus } = await chrome.storage.local.get(['bridgeStatus']);
              if (!bridgeStatus) return false;
              const ids = (bridgeStatus.channels || []).map(c => c.identity);
              return !ids.includes('listener-id') && !ids.includes('speaker-id');
            }
            """,
            timeout=5000,
        )
    finally:
        await popup.close()


async def test_pcm_frames_carry_resolved_display_name_after_sidebar_rerender(
    loaded_bridge: LoadedExtension,
    fake_tap_server: FakeTapServer,
):
    """Closes the PR #35 integration gap at the WORKLET path.

    page-script.test.js pins that tap-start AND the first PCM message
    carry the resolved name — but only against a mocked sidebar that
    never changes mid-stream. SpatialChat's Vue sidebar re-renders on
    every layout shift; the worklet's onmessage handler re-resolves on
    every frame via getDisplayName(), so the second-and-later /tap
    requests need to keep working after the sidebar element is yanked
    and re-mounted with a different `_props.user.name`.

    This test proves the worklet path tolerates a sidebar swap mid-
    utterance: the URL is set on first frame (utterance_id is locked
    in then), but the name that lands on the wire MUST be the resolved
    one — never "" or window.name. We assert by reading the WS query
    string identity= and name= on the server.
    """
    page = loaded_bridge.page

    # Seed the sidebar with Carol's name, then add the speaker. Pass
    # participant.name="" so the bridge MUST go through the Vue path
    # to resolve "Carol" (mirrors the production case).
    await page.evaluate(
        "([id, n]) => window.__tsTest.setSidebarUser(id, n)",
        ["carol-id", "Carol"],
    )
    await page.evaluate(
        "([id]) => window.__tsTest.addRemoteSpeaker(id, '', { passName: false })",
        ["carol-id"],
    )

    # Wait for the first WS open + at least one PCM frame on the wire.
    await wait_for_pill_kind(page, "ok", timeout_ms=5000)
    await asyncio.sleep(0.4)
    conn = fake_tap_server.connection_for("carol-id")
    assert conn is not None
    assert conn.name == "Carol", (
        f"/tap was dialed with name={conn.name!r} — display-name resolution broken; #35-style regression"
    )
    assert len(conn.frames) > 0

    # Now simulate a Vue re-render: clear the sidebar entry, then add
    # it back with a fresh name. The worklet's onmessage handler should
    # see the new name on the next PCM frame's resolution attempt — but
    # because entry.resolvedName is sticky once non-empty (correct
    # behaviour: don't change name mid-utterance), the wire name MUST
    # remain "Carol". A regression that re-resolves and forwards the
    # new name would scramble the dashboard's per-speaker attribution.
    await page.evaluate("() => window.__tsTest.clearSidebarUser('carol-id')")
    await page.evaluate(
        "([id, n]) => window.__tsTest.setSidebarUser(id, n)",
        ["carol-id", "Carol-Renamed"],
    )
    # Frames continue to flow on the same WS (one utterance). Wait for
    # several more frames to land.
    target = len(conn.frames) + 30
    deadline = asyncio.get_event_loop().time() + 3.0
    while asyncio.get_event_loop().time() < deadline:
        if len(conn.frames) >= target:
            break
        await asyncio.sleep(0.05)
    assert len(conn.frames) >= target, (
        f"expected {target} frames after sidebar rerender, got {len(conn.frames)}"
    )
    # Critical assertion: the WS url's name= query param doesn't change
    # mid-utterance (URL is locked at WS-open time). What we're really
    # pinning is that the bridge didn't tear down the WS and re-dial
    # with name="" because of a missing local binding (PR #35's bug).
    assert conn.name == "Carol"
    assert not conn.closed, (
        "WS must not have been torn down mid-utterance — a name-binding "
        "regression that nulled entry.resolvedName could have caused this"
    )


async def test_popup_token_rotation_triggers_reconnect_with_new_subprotocol(
    loaded_bridge: LoadedExtension,
    fake_tap_server: FakeTapServer,
):
    """Closes a documented invariant: editing the tap token in the popup
    (chrome.storage.local.set) must tear down in-flight /tap WSes and
    redial with the new subprotocol, without a tab reload.

    The vm suite covers `useTls` rotation
    (wire-contract.test.js#flipping-use-tls-on); token rotation is the
    same code path but the visible effect is on the subprotocol the
    server echoes. The bridge re-presents the new
    `tapscribe.v1.tap.<token>` subprotocol; we verify by switching the
    fake server's expected token mid-utterance and confirming the
    bridge's next /tap WS lands with the new subprotocol negotiated.
    """
    page = loaded_bridge.page

    # Bring up a tapped speaker with token = "" (no-auth mode the
    # fixture starts in). The fake server accepts any offered protocol.
    await add_speaker(page, "dave-id", "Dave")
    await wait_for_pill_kind(page, "ok", timeout_ms=5000)
    await asyncio.sleep(0.3)
    first = fake_tap_server.connection_for("dave-id")
    assert first is not None
    # With no token, the bridge constructs the WS without a subprotocol;
    # websockets-server replies with None. (The `select_subprotocol`
    # hook returns offered[0] when expected_token == ""; an empty
    # `offered` yields None.) We don't assert on this directly — the
    # interesting assertion is the SECOND connection's subprotocol after
    # the rotation, below.
    assert first.subprotocol in (None, ""), (
        f"pre-rotation no-token connection should have no subprotocol, got {first.subprotocol!r}"
    )

    # Operator pastes a new token into the popup. Switch the server's
    # expectation first so a wrong-token redial would be rejected with
    # 4401 (we WANT the redial to succeed).
    fake_tap_server.expected_token = "rotated-token-xyz"
    popup = await loaded_bridge.open_popup()
    try:
        await popup.evaluate(
            "() => chrome.storage.local.set({ tapToken: 'rotated-token-xyz' })",
        )
    finally:
        await popup.close()

    # The storage.onChanged handler in content.js drops in-flight WSes
    # and the reconnect ladder fires within a few hundred ms (backoff
    # starts at ~200 ms). Wait for a NEW connection to land.
    async def has_new_connection() -> bool:
        conns = [c for c in fake_tap_server.connections if c.identity == "dave-id"]
        return len(conns) >= 2

    deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline:
        if await has_new_connection():
            break
        await asyncio.sleep(0.1)
    assert await has_new_connection(), "bridge must redial after a tap-token rotation in storage"

    second = [c for c in fake_tap_server.connections if c.identity == "dave-id"][-1]
    assert second is not first
    assert second.subprotocol == "tapscribe.v1.tap.rotated-token-xyz", (
        f"redial must offer the NEW token's subprotocol, got {second.subprotocol!r} "
        "— PR-style regression where storage.onChanged dropped the WS but the "
        "reconnect path read the stale token"
    )
    # Same utterance — utterance_id must be preserved across the
    # settings-induced redial so the Recorder appends to the same WAV.
    assert second.utterance_id == first.utterance_id, (
        "utterance_id must persist across a settings-change redial (one utterance = one WAV invariant)"
    )


async def test_mute_drain_reconnect_continues_same_utterance(
    loaded_bridge: LoadedExtension,
    fake_tap_server: FakeTapServer,
):
    """Closes a documented invariant for the drain path under real
    cross-world message flow: a mute that arrives while the /tap WS
    is RECONNECTING (i.e. buffered frames are sitting in ch.buffer)
    must NOT drop the trailing audio. The bridge enters drain mode,
    keeps the reconnect ladder running, lands a fresh WS, flushes
    the buffer, then closes cleanly — all on the SAME utterance_id so
    the Recorder appends to the same WAV.

    The vm `drain.test.js` covers the state machine in isolation with
    a virtual clock. This test proves the contract holds with real
    timers, real cross-world postMessage latency, and a real WS server.
    """
    page = loaded_bridge.page

    # Bring up a speaker, get bytes flowing.
    await add_speaker(page, "ellie-id", "Ellie")
    await wait_for_pill_kind(page, "ok", timeout_ms=5000)
    await asyncio.sleep(0.3)
    first = fake_tap_server.connection_for("ellie-id")
    assert first is not None
    initial_utt = first.utterance_id
    initial_frames = len(first.frames)
    assert initial_frames > 0

    # Drop the WS abnormally so the bridge enters reconnect-with-buffer
    # mode. Frames produced while the WS is down get buffered in
    # ch.buffer.
    assert await fake_tap_server.drop_connection("ellie-id")

    # Brief wait so the bridge's onclose actually fires + a few PCM
    # frames from the still-running worklet land in ch.buffer. Without
    # this, mute can race ahead of the close handler — the bridge then
    # sees ws.readyState==OPEN with no buffered frames and takes the
    # endUtteranceImmediate path (no drain to test).
    await asyncio.sleep(0.15)

    # While the bridge is buffering frames against a closed WS, the
    # speaker mutes. The bridge's mute handler sees a non-empty buffer
    # and ch.tapWs==null and enters DRAIN mode rather than closing
    # immediately. (This was the #17 bug — drain trailing PCM on mute.)
    await page.evaluate("() => window.__tsTest.muteSpeaker('ellie-id')")

    # The reconnect ladder fires (~200 ms first attempt). When the new
    # WS opens the buffered frames flush, then the bridge close()s
    # cleanly. We MUST see a second connection arrive on the same
    # utterance_id — that's the resume contract.
    async def has_drain_connection() -> bool:
        conns = [c for c in fake_tap_server.connections if c.identity == "ellie-id"]
        return len(conns) >= 2

    deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline:
        if await has_drain_connection():
            break
        await asyncio.sleep(0.1)
    assert await has_drain_connection(), "bridge must reconnect during drain so trailing PCM can flush"
    drain_conn = [c for c in fake_tap_server.connections if c.identity == "ellie-id"][-1]
    assert drain_conn is not first

    # Same utterance — the Recorder side relies on this to append the
    # tail audio to the existing WAV (CONTEXT.md invariant: "one
    # utterance = one WAV"). A regression that minted a new
    # utterance_id on the drain reconnect would silently fragment
    # the WAV.
    assert drain_conn.utterance_id == initial_utt, (
        f"drain reconnect must reuse utterance_id={initial_utt!r}, got {drain_conn.utterance_id!r}"
    )

    # The drain WS should land at least one frame (the buffered tail)
    # and then close cleanly.
    async def drain_closed_cleanly() -> bool:
        c = fake_tap_server.connection_for("ellie-id", initial_utt)
        # We want the LATEST connection for this utterance, which is
        # drain_conn — connection_for returns it.
        return c is drain_conn and c.closed

    deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline:
        if await drain_closed_cleanly():
            break
        await asyncio.sleep(0.1)
    assert await drain_closed_cleanly(), "drain WS should close cleanly once tail flushed"
    # close_code 1000 == clean close; that's the Recorder's "end of
    # utterance" signal. A non-clean close here would mean drain
    # buffer was discarded.
    assert drain_conn.close_code == 1000, f"drain WS must close cleanly (1000), got {drain_conn.close_code}"


async def test_window_room_cleared_without_disconnect_closes_taps(
    loaded_bridge: LoadedExtension,
    fake_tap_server: FakeTapServer,
):
    """Closes the page-script maybeAttach() room-lost leak end-to-end.

    SpatialChat clears window.room when the user leaves a space WITHOUT
    transitioning the captured Room to "disconnected" and without firing
    the "disconnected" event. The orphan Room still reads "connected" and
    its track stays live, so before the fix maybeAttach() slipped through
    both branches: reconcile() (which untaps leavers) stopped running AND
    no teardown fired — the worklet kept posting PCM forever and the /tap
    WS leaked against the Recorder (and the popup showed the speaker as a
    live OPEN/active tap with no SpatialChat window open).

    The vm page-script.test.js pins the teardown by inspecting postMessage
    payloads; this proves the /tap WS actually closes through the real
    cross-world channel + a real WS server.
    """
    page = loaded_bridge.page

    # Bring up a tapped speaker; frames must be flowing on a live WS.
    await add_speaker(page, "ghost-id", "Ghost")
    await wait_for_pill_kind(page, "ok", timeout_ms=5000)
    await asyncio.sleep(0.3)
    conn = fake_tap_server.connection_for("ghost-id")
    assert conn is not None
    assert not conn.closed, "the /tap WS should be open while the speaker is tapped"
    assert len(conn.frames) > 0

    # Clear window.room without a disconnected event — the orphan Room still
    # reads "connected" (the exact leak shape).
    await page.evaluate("() => window.__tsTest.clearRoomWithoutDisconnect()")
    assert await page.evaluate("() => window.room === null")
    assert await page.evaluate("() => window.__tsTest.roomState() === 'connected'"), (
        "the captured Room must still read 'connected' to reproduce the leak"
    )

    # page-script polls every 250 ms; the room-lost teardown must close the WS.
    async def ghost_closed() -> bool:
        c = fake_tap_server.connection_for("ghost-id")
        return c is not None and c.closed

    deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline:
        if await ghost_closed():
            break
        await asyncio.sleep(0.1)
    assert await ghost_closed(), (
        "clearing window.room (orphan room still 'connected') must close the "
        "/tap WS — without the maybeAttach room-lost teardown it leaks forever"
    )


async def test_closed_spatialchat_tab_flips_popup_to_no_active_tab(
    loaded_bridge: LoadedExtension,
    fake_tap_server: FakeTapServer,
):
    """Closes the popup-staleness bug end-to-end (the reported symptom).

    bridgeStatus lives in chrome.storage.local and OUTLIVES the content
    script that wrote it. When the SpatialChat tab closes, content.js dies
    (its /tap WS closes, the dashboard empties) but its last snapshot
    lingers in storage — so the popup kept rendering departed speakers as
    live OPEN/active taps with no tab open at all.

    content.js refreshes the snapshot's `ts` on a heartbeat while it runs;
    the popup treats a snapshot older than taps-view.STALE_AFTER_MS as a
    dead tab. This drives the full transition: a live tap shows in the
    popup, the SpatialChat tab is closed, and the popup flips to the
    no-tab empty state (not the frozen roster).
    """
    page = loaded_bridge.page

    # A live tap — bridgeStatus now carries Amy's channel with a fresh ts.
    await add_speaker(page, "amy-id", "Amy")
    await wait_for_pill_kind(page, "ok", timeout_ms=5000)
    await asyncio.sleep(0.3)

    popup = await loaded_bridge.open_popup()
    try:
        # While the tab is alive the popup shows Amy's row.
        await popup.wait_for_function(
            """
            () => {
              const e = document.getElementById('tapState');
              return !!e && /Amy/.test(e.textContent);
            }
            """,
            timeout=5000,
        )

        # Close the SpatialChat tab: content.js stops refreshing `ts`, so the
        # leftover snapshot ages past STALE_AFTER_MS and the popup must fall
        # back to the no-tab empty state instead of the frozen tap rows.
        await page.close()
        await popup.wait_for_function(
            """
            () => {
              const e = document.getElementById('tapState');
              if (!e) return false;
              const t = e.textContent || '';
              return /No active SpatialChat tab/.test(t) && !/Amy/.test(t);
            }
            """,
            timeout=12000,
        )
    finally:
        await popup.close()
