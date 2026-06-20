"""Multi-pass runtime perf soak for the Stages dashboard (served at /).

NOT part of the default suite — every test here self-skips unless
`TAPSCRIBE_PERF_SOAK=1` is set, so `pytest tests` (and the pre-push hook)
never pays for it. Run it deliberately:

    $env:TAPSCRIBE_PERF_SOAK = "1"          # PowerShell
    pytest tests/e2e/test_next_perf_soak.py -s -q

Knobs (env):
    TAPSCRIBE_PERF_PASSES       passes per scenario (default 3)
    TAPSCRIBE_PERF_SOAK_S       soak window per pass, seconds (default 30)
    TAPSCRIBE_PERF_TX_SEGMENTS  segment count for the heavy transcript (3000)
    TAPSCRIBE_PERF_REPORT_DIR   where JSON reports land (default
                                <repo>/perf-reports/, gitignored)

Each scenario boots the real FastAPI app under uvicorn (`running_recorder`),
drives real headless Chromium at `/`, and measures one soak window per
pass with `perf_probe.PerfProbe`: long tasks (lockups), poll cadence/tick
work, post-GC node/listener/heap growth, layout passes per poll. Passes run
sequentially on a fresh browser context each, and the report keeps both the
per-pass values and the median/max across passes so a noisy outlier is
visible instead of silently averaged away.

Scenario shapes (each models a real operator situation):
    idle              — open tab, nothing happening. Floor + leak check.
    live_meeting      — 4 taps streaming paced PCM + live captions settling.
                        The "operator watching a meeting" load.
    big_library       — 24 sessions × 8 WAVs on disk + 2 streaming taps:
                        every tick re-parses a big /api/state and recomputes
                        the O(sessions·files) signatures. Sessions view.
    transcript_heavy  — a 3000-segment merged transcript open while a job
                        progresses and a tap streams: every sig change
                        rebuilds the merged-transcript DOM synchronously.
    view_cycle        — big library + streaming taps while hopping across
                        all 7 views every 2 s: mount/unmount + first-render.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import pytest

from tapscribe.recorder import JobState

from .conftest import RunningRecorder
from .harness import (
    playwright_session,
    stream_wav_via_tap,
    streams_drained,
    synth_speech_like_wav,
    wait_until,
)
from .perf_probe import PROBE_INIT_JS, PassMetrics, PerfProbe, compute_pass, passes_as_json, summarize

if os.environ.get("TAPSCRIBE_PERF_SOAK") != "1":  # pragma: no cover
    pytest.skip(
        "perf soak: set TAPSCRIBE_PERF_SOAK=1 to run (multi-minute, measurement-only)",
        allow_module_level=True,
    )

if importlib.util.find_spec("playwright") is None:  # pragma: no cover
    pytest.skip("playwright not installed", allow_module_level=True)

REPO_ROOT = Path(__file__).resolve().parents[2]

PASSES = int(os.environ.get("TAPSCRIBE_PERF_PASSES", "3"))
SOAK_S = float(os.environ.get("TAPSCRIBE_PERF_SOAK_S", "30"))
TX_SEGMENTS = int(os.environ.get("TAPSCRIBE_PERF_TX_SEGMENTS", "3000"))
SETTLE_S = 3.0

# Same view list the focus-clobber sweep uses (test_dashboard_ui.py).
_NEXT_VIEWS = ("capture", "recordings", "transcript", "taps", "sessions", "people", "settings")


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def _write_merged_transcript(
    session_dir: Path, *, segments: int, speakers: tuple[str, ...] = ("Alice", "Bob", "Carol")
) -> None:
    """Hand-write a session-transcript.json in the served wire shape (see
    merged-transcript.js + _session_transcript_marker)."""
    t0 = datetime(2025, 1, 1, tzinfo=UTC)
    segs = [
        {
            "speaker": speakers[i % len(speakers)],
            "text": f"Segment {i}: the quick brown fox jumps over the lazy dog near the riverbank.",
            "abs_start": (t0 + timedelta(seconds=4 * i)).isoformat(),
            "avg_logprob": -0.31,
            "low_confidence": i % 17 == 0,
        }
        for i in range(segments)
    ]
    data = {
        "transcribed_at": t0.isoformat(),
        "segments": segs,
        "speakers": list(speakers),
        "speaking_seconds": {s: 600.0 for s in speakers},
        "suppressed": [],
        "suppressed_count": 0,
        "wav_count": 5,
        "transcribe_ms": 12345,
        "model": "tiny.en",
        "backend": "fake",
        "device": "cpu",
    }
    (session_dir / "session-transcript.json").write_text(json.dumps(data), encoding="utf-8")


def _seed_library(rr: RunningRecorder, *, sessions: int, wavs_per: int) -> list[str]:
    """N non-current sessions × M tiny WAVs each; merged transcript on every
    other session so markers/badges/sigs all have real values."""
    sids: list[str] = []
    for i in range(sessions):
        sid = f"2025-03-{(i % 27) + 1:02d}T{(10 + i // 27):02d}-00-00Z"
        sids.append(sid)
        d = rr.recorder.recordings_dir / sid
        d.mkdir(parents=True, exist_ok=True)
        for j in range(wavs_per):
            name = f"{sid}_speaker{j % 4}_speaker_{i:04x}{j:04x}.wav"
            synth_speech_like_wav(d / name, seconds=0.3, freq_hz=200.0 + j * 20.0)
        if i % 2 == 0:
            _write_merged_transcript(d, segments=20)
    return sids


# ---------------------------------------------------------------------------
# Churn (runs around/within the soak window)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _streaming_taps(
    rr: RunningRecorder, tmp_path: Path, *, k: int, seconds: float, tag: str
) -> AsyncIterator[None]:
    """K concurrent /tap WebSockets streaming real-time-paced PCM for the
    whole block. Every poll tick then sees mutated active rows → fresh ETag
    → a full 200 + parse + render, which is exactly the under-load shape."""
    tasks = []
    for i in range(k):
        wav = synth_speech_like_wav(tmp_path / f"{tag}-tap{i}.wav", seconds=seconds, freq_hz=180.0 + i * 40.0)
        tasks.append(
            asyncio.create_task(
                stream_wav_via_tap(
                    ws_base_url=rr.ws_base_url,
                    identity=f"{tag}-spk{i}",
                    name=f"Speaker {i}",
                    wav_path=wav,
                    frame_interval_s=0.02,  # real-time pacing keeps the WS open
                )
            )
        )
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Drain so the next pass starts from a clean active-taps state.
        # partial (not a lambda): a lambda's implicit return inside a finally
        # trips CodeQL's py/exit-from-finally; partial has no return node.
        await wait_until(partial(streams_drained, rr.recorder), timeout=10.0)


async def _feed_churn(rr: RunningRecorder) -> None:
    """Live captions: an in-flight hypothesis every second, a settled line
    every other second — broadcast through the fake WlK to every open relay,
    landing in buffer_transcription + the live feed like a real meeting."""
    n = 0
    while True:
        n += 1
        await asyncio.to_thread(rr.fake_wlk.push_buffer, f"in-flight hypothesis {n} lorem ipsum dolor")
        if n % 2 == 0:
            await asyncio.to_thread(
                rr.fake_wlk.push_committed,
                f"Settled caption {n}: the quick brown fox jumps over the lazy dog.",
            )
        await asyncio.sleep(1.0)


async def _job_churn(rr: RunningRecorder, sid: str) -> None:
    """Simulate an in-flight transcribe job's progress ticks. Direct dict
    write (same field /api/state reads via jobs.snapshot()) — going through
    the async lock from this loop would race the server loop's."""
    n = 0
    while True:
        n += 1
        rr.recorder.jobs._by_session[sid] = JobState(
            session=sid,
            kind="transcribe",
            current=n % 50,
            total=50,
            started_at=datetime.now(UTC),
            current_file=f"file-{n % 50}.wav",
            model="tiny.en",
        )
        await asyncio.sleep(1.0)


@asynccontextmanager
async def _background(coro_fn: Callable[[], Awaitable[None]]) -> AsyncIterator[None]:
    task = asyncio.create_task(coro_fn())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------------
# Pass runner
# ---------------------------------------------------------------------------


async def _select_session(page, sid: str) -> None:
    """Pin the spine's session picker to `sid` (explicit selection survives a
    new current session appearing mid-soak)."""
    await page.wait_for_function(
        """(sid) => {
            const s = document.querySelector('[data-slot="sessionPick"]');
            return !!s && Array.from(s.options).some((o) => o.value === sid);
        }""",
        arg=sid,
        timeout=15000,
    )
    await page.evaluate(
        """(sid) => {
            const s = document.querySelector('[data-slot="sessionPick"]');
            s.value = sid;
            s.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        sid,
    )


async def _sleep_soak(page, soak_s: float) -> None:
    await page.wait_for_timeout(soak_s * 1000)


async def _run_pass(
    browser,
    base_url: str,
    *,
    scenario: str,
    pass_index: int,
    view: str,
    soak: Callable[[object, float], Awaitable[None]] = _sleep_soak,
    select_sid: str | None = None,
) -> PassMetrics:
    """One pass: fresh context → boot the dashboard on `view` → settle → forced-GC
    snapshot → soak window → snapshot → forced-GC snapshot → window-filtered
    metrics. Churn contexts are entered by the caller AROUND this, so the
    window measures steady state, not connection setup."""
    context = await browser.new_context(viewport={"width": 1400, "height": 900})
    await context.add_init_script(PROBE_INIT_JS)
    page = await context.new_page()
    try:
        await page.goto(f"{base_url}/#{view}", wait_until="domcontentloaded")
        await page.wait_for_function(
            "() => !!window.__perfProbe && document.getElementById('spine').childElementCount > 0",
            timeout=15000,
        )
        if select_sid is not None:
            await _select_session(page, select_sid)
        await page.wait_for_timeout(SETTLE_S * 1000)

        probe = await PerfProbe.attach(page)
        await probe.force_gc()
        start = await probe.snapshot()
        await soak(page, SOAK_S)
        end = await probe.snapshot()
        await probe.force_gc()
        end_post_gc = await probe.snapshot()
        data = await probe.client_probe()
        return compute_pass(
            scenario=scenario,
            pass_index=pass_index,
            start=start,
            end=end,
            end_post_gc=end_post_gc,
            probe_data=data,
        )
    finally:
        await context.close()


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_TABLE_FIELDS = (
    ("long_task_count", "long tasks", "{:.0f}"),
    ("longest_task_ms", "longest ms", "{:.0f}"),
    ("blocked_ms", "blocked ms", "{:.0f}"),
    ("tick_work_ms_p50", "tick p50 ms", "{:.1f}"),
    ("tick_work_ms_p95", "tick p95 ms", "{:.1f}"),
    ("fetch_ms_p50", "fetch p50 ms", "{:.1f}"),
    ("body_bytes_p50", "body p50 B", "{:.0f}"),
    ("full_responses", "full 200s", "{:.0f}"),
    ("polls", "polls", "{:.0f}"),
    # ASCII labels on purpose — Windows consoles default to cp1252.
    ("node_growth_post_gc", "dNodes gc", "{:+.0f}"),
    ("listener_growth_post_gc", "dLsnr gc", "{:+.0f}"),
    ("heap_growth_mb_post_gc", "dHeap gc MB", "{:+.2f}"),
    ("layout_per_poll", "layout/poll", "{:.2f}"),
    ("recalc_per_poll", "recalc/poll", "{:.2f}"),
    ("script_ms_per_poll", "script ms/poll", "{:.1f}"),
    ("main_thread_busy_pct", "busy %", "{:.1f}"),
)


def _report(scenario: str, passes: list[PassMetrics], extra: dict | None = None) -> None:
    summary = summarize(passes)
    print(f"\n=== perf soak | {scenario} | {len(passes)} passes x {SOAK_S:.0f}s ===")
    print(f"{'metric':<16} {'median':>10} {'max':>10}")
    for field_name, label, fmt in _TABLE_FIELDS:
        s = summary[field_name]
        print(f"{label:<16} {fmt.format(s['median']):>10} {fmt.format(s['max']):>10}")

    report_dir = Path(os.environ.get("TAPSCRIBE_PERF_REPORT_DIR") or REPO_ROOT / "perf-reports")
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out = report_dir / f"{stamp}-{scenario}.json"
    out.write_text(
        json.dumps(
            {
                "scenario": scenario,
                "config": {"passes": PASSES, "soak_s": SOAK_S, "settle_s": SETTLE_S, **(extra or {})},
                "summary": summary,
                "passes": passes_as_json(passes),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"report: {out}")


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def test_soak_idle(running_recorder: RunningRecorder):
    """Floor: open the dashboard on an empty recorder, do nothing. Long tasks should
    be ~zero and post-GC growth flat; this is also the leak detector."""
    async with playwright_session() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"Chromium not available: {e}")
            return
        try:
            passes = [
                await _run_pass(
                    browser, running_recorder.base_url, scenario="idle", pass_index=i, view="capture"
                )
                for i in range(PASSES)
            ]
        finally:
            await browser.close()
    _report("idle", passes)


async def test_soak_live_meeting(running_recorder: RunningRecorder, tmp_path: Path):
    """4 taps streaming real-time PCM + captions settling through the relay,
    watched from the Capture view (live feed + rail + header)."""
    rr = running_recorder
    stream_s = SETTLE_S + SOAK_S + 25.0
    passes: list[PassMetrics] = []
    async with playwright_session() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"Chromium not available: {e}")
            return
        try:
            for i in range(PASSES):
                async with _streaming_taps(rr, tmp_path, k=4, seconds=stream_s, tag=f"mtg{i}"):
                    # Let the relays connect before captions start flowing.
                    await asyncio.sleep(1.0)
                    async with _background(lambda: _feed_churn(rr)):
                        passes.append(
                            await _run_pass(
                                browser, rr.base_url, scenario="live_meeting", pass_index=i, view="capture"
                            )
                        )
        finally:
            await browser.close()
    _report("live_meeting", passes, extra={"taps": 4})


async def test_soak_big_library(running_recorder: RunningRecorder, tmp_path: Path):
    """24 sessions × 8 WAVs on disk while 2 taps stream: every tick is a full
    200 over a big payload + O(sessions·files) sig recompute. Sessions view.
    Also micro-benches the server's own /api/state latency at this size."""
    rr = running_recorder
    _seed_library(rr, sessions=24, wavs_per=8)
    stream_s = SETTLE_S + SOAK_S + 25.0
    passes: list[PassMetrics] = []
    async with playwright_session() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"Chromium not available: {e}")
            return
        try:
            for i in range(PASSES):
                async with _streaming_taps(rr, tmp_path, k=2, seconds=stream_s, tag=f"lib{i}"):
                    passes.append(
                        await _run_pass(
                            browser, rr.base_url, scenario="big_library", pass_index=i, view="sessions"
                        )
                    )
        finally:
            await browser.close()

    # Server-side floor: /api/state latency without a browser in the loop.
    import httpx

    async with httpx.AsyncClient(base_url=rr.base_url) as client:
        latencies: list[float] = []
        etag: str | None = None
        not_modified = 0
        for _ in range(60):
            headers = {"If-None-Match": etag} if etag else {}
            t0 = time.perf_counter()
            r = await client.get("/api/state", headers=headers)
            latencies.append((time.perf_counter() - t0) * 1000.0)
            etag = r.headers.get("ETag", etag)
            not_modified += r.status_code == 304
        latencies.sort()
        server = {
            "server_ms_p50": latencies[len(latencies) // 2],
            "server_ms_p95": latencies[int(0.95 * (len(latencies) - 1))],
            "responses_304": not_modified,
        }
    print(
        f"server /api/state: p50={server['server_ms_p50']:.1f}ms p95={server['server_ms_p95']:.1f}ms 304s={server['responses_304']}/60 (idle)"
    )
    _report("big_library", passes, extra={"sessions": 24, "wavs_per": 8, "taps": 2, **server})


async def test_soak_transcript_heavy(running_recorder: RunningRecorder, tmp_path: Path):
    """A big merged transcript open in the Transcript view while a job
    progresses and a tap streams. Every sig change (job tick) rebuilds the
    merged-transcript DOM synchronously — the prime 'locks up' suspect."""
    rr = running_recorder
    sid = "2025-02-01T09-00-00Z"
    d = rr.recorder.recordings_dir / sid
    d.mkdir(parents=True)
    for j in range(4):
        synth_speech_like_wav(d / f"{sid}_alice_speaker_{j:08x}.wav", seconds=0.3, freq_hz=220.0)
    _write_merged_transcript(d, segments=TX_SEGMENTS)

    stream_s = SETTLE_S + SOAK_S + 25.0
    passes: list[PassMetrics] = []
    async with playwright_session() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"Chromium not available: {e}")
            return
        try:
            for i in range(PASSES):
                async with _streaming_taps(rr, tmp_path, k=1, seconds=stream_s, tag=f"tx{i}"):
                    async with _background(lambda: _job_churn(rr, sid)):
                        passes.append(
                            await _run_pass(
                                browser,
                                rr.base_url,
                                scenario="transcript_heavy",
                                pass_index=i,
                                view="transcript",
                                select_sid=sid,
                            )
                        )
                rr.recorder.jobs._by_session.pop(sid, None)
        finally:
            await browser.close()
    _report("transcript_heavy", passes, extra={"segments": TX_SEGMENTS})


async def test_soak_view_cycle(running_recorder: RunningRecorder, tmp_path: Path):
    """Big library + 2 streaming taps while hopping across all 7 views every
    2 s — catches expensive view (re)mounts and first renders."""
    rr = running_recorder
    _seed_library(rr, sessions=24, wavs_per=8)
    stream_s = SETTLE_S + SOAK_S + 25.0

    async def cycle(page, soak_s: float) -> None:
        deadline = asyncio.get_running_loop().time() + soak_s
        i = 0
        while asyncio.get_running_loop().time() < deadline:
            view = _NEXT_VIEWS[i % len(_NEXT_VIEWS)]
            await page.evaluate("(v) => window.gotoView(v)", view)
            await page.wait_for_timeout(2000)
            i += 1

    passes: list[PassMetrics] = []
    async with playwright_session() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"Chromium not available: {e}")
            return
        try:
            for i in range(PASSES):
                async with _streaming_taps(rr, tmp_path, k=2, seconds=stream_s, tag=f"cyc{i}"):
                    passes.append(
                        await _run_pass(
                            browser,
                            rr.base_url,
                            scenario="view_cycle",
                            pass_index=i,
                            view="capture",
                            soak=cycle,
                        )
                    )
        finally:
            await browser.close()
    _report("view_cycle", passes, extra={"sessions": 24, "wavs_per": 8, "taps": 2})
