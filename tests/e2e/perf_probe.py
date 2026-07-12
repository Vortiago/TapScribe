"""CDP runtime-performance probe for dashboard soak measurements.

The idle-churn guard in `test_dashboard_ui.py` watches Nodes/JSEventListeners
over ~10 s of idle. This module is the deeper instrument behind
`test_next_perf_soak.py`: it measures the *runtime* failure modes a polling
SPA exhibits over minutes under load — the things an operator reports as
"the tab locks up now and again":

- **Long tasks** (main-thread tasks > 50 ms, via `PerformanceObserver
  ('longtask')`): each one is a window where the page cannot respond to
  input. `blocked_ms` sums the over-50ms excess (Total Blocking Time shape).
- **Poll health** (resource timings for `/api/state`): cadence drift between
  polls minus the 500 ms sleep approximates per-tick client work; transfer
  sizes separate 304-not-modified ticks from full 200 parses.
- **Post-GC growth** (CDP `HeapProfiler.collectGarbage` around snapshots):
  node/listener/heap deltas *after* a forced GC are retained growth (a leak),
  not collectable churn. Raw deltas without GC measure churn pressure.
- **Layout/recalc counts** (CDP `Performance.getMetrics`): layout passes per
  poll catch forced-layout thrash that long tasks are too coarse to see.

The probe is one CDP session + one injected init script per page; scenarios
in the soak module own seeding/churn and call `snapshot()`/`client_probe()`
around their soak window.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from typing import Any

# Injected via context.add_init_script BEFORE any page script runs, so the
# observers see the page's first tasks too. Collects into `window.__perfProbe`:
#   longTasks[]  — {start, dur} ms on the performance.now() timeline
#   statePolls[] — {start, dur, transfer, body} per /api/state fetch
PROBE_INIT_JS = """
(() => {
  const probe = { longTasks: [], statePolls: [] };
  Object.defineProperty(window, '__perfProbe', { value: probe });
  // Deterministic leak census (the toolkit's mem.js WeakRef pattern): scenarios
  // call __tagViewRoot() after each view mount; view_root_census() then counts
  // which tagged roots survived a forced GC. A survivor that is no longer in
  // the document is either a CACHED view (bounded: 6 LRU transcript views + 7
  // singletons) or a leak — the integer gate needs no heap-size heuristics.
  const viewRefs = [];
  Object.defineProperty(window, '__viewRefs', { value: viewRefs });
  Object.defineProperty(window, '__tagViewRoot', { value: () => {
    const el = document.getElementById('viewRoot');
    const root = el && el.firstElementChild;
    if (root) viewRefs.push(new WeakRef(root));
  } });
  // Default resource buffer is 250 entries; a multi-minute 2 Hz soak needs more.
  try { performance.setResourceTimingBufferSize(60000); } catch (e) { /* older engines */ }
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) probe.longTasks.push({ start: e.startTime, dur: e.duration });
    }).observe({ type: 'longtask', buffered: true });
  } catch (e) { /* longtask unsupported -> longTasks stays empty, metrics read as 0 */ }
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        if (e.name.indexOf('/api/state') !== -1) {
          probe.statePolls.push({
            start: e.startTime, dur: e.duration,
            transfer: e.transferSize || 0, body: e.decodedBodySize || 0,
          });
        }
      }
    }).observe({ type: 'resource', buffered: true });
  } catch (e) { /* resource observer unsupported */ }
})();
"""

# The /next poll loop sleeps this long AFTER each tick's fetch+render resolves
# (tapscribe/web/js/next/main.js). Used to back per-tick client work out of
# the observed poll-to-poll gap.
NEXT_POLL_SLEEP_MS = 500.0


@dataclass
class Snapshot:
    """One CDP Performance.getMetrics reading, stamped with page time."""

    now_ms: float  # performance.now() at snapshot time
    nodes: float
    listeners: float
    heap_used: float  # bytes
    layout_count: float
    recalc_count: float
    layout_dur_s: float
    recalc_dur_s: float
    script_dur_s: float
    task_dur_s: float


class PerfProbe:
    """One per page: CDP metrics + the injected client-side probe."""

    def __init__(self, page: Any, cdp: Any) -> None:
        self._page = page
        self._cdp = cdp

    @classmethod
    async def attach(cls, page: Any) -> PerfProbe:
        cdp = await page.context.new_cdp_session(page)
        await cdp.send("Performance.enable")
        await cdp.send("HeapProfiler.enable")
        return cls(page, cdp)

    async def snapshot(self) -> Snapshot:
        res = await self._cdp.send("Performance.getMetrics")
        m = {d["name"]: d["value"] for d in res["metrics"]}
        now_ms = await self._page.evaluate("performance.now()")
        return Snapshot(
            now_ms=now_ms,
            nodes=m.get("Nodes", 0.0),
            listeners=m.get("JSEventListeners", 0.0),
            heap_used=m.get("JSHeapUsedSize", 0.0),
            layout_count=m.get("LayoutCount", 0.0),
            recalc_count=m.get("RecalcStyleCount", 0.0),
            layout_dur_s=m.get("LayoutDuration", 0.0),
            recalc_dur_s=m.get("RecalcStyleDuration", 0.0),
            script_dur_s=m.get("ScriptDuration", 0.0),
            task_dur_s=m.get("TaskDuration", 0.0),
        )

    async def force_gc(self) -> None:
        await self._cdp.send("HeapProfiler.collectGarbage")

    async def client_probe(self) -> dict:
        """The injected probe's collected entries (longTasks, statePolls)."""
        return await self._page.evaluate("window.__perfProbe")

    async def view_root_census(self) -> dict:
        """Deterministic leak gate over the __tagViewRoot() WeakRefs — run it
        AFTER force_gc(). `alive_detached` counts tagged view roots that are
        retained but no longer in the document: legitimate only for the
        bounded view cache (6 LRU transcript views + 7 singletons in
        next/main.js); anything past that bound is a structural leak, caught
        as an integer — no heap-size heuristics."""
        return await self._page.evaluate(
            """() => {
              const refs = window.__viewRefs || [];
              // Dedupe by node identity: a view revisited during the cycle is
              // tagged once per VISIT but is one root — counting raw refs
              // would inflate the census past the cache bound.
              const seen = new Set();
              let alive = 0, alive_detached = 0;
              for (const r of refs) {
                const el = r.deref();
                if (el && !seen.has(el)) {
                  seen.add(el);
                  alive++;
                  if (!el.isConnected) alive_detached++;
                }
              }
              return { tagged: refs.length, alive, alive_detached };
            }"""
        )


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]


@dataclass
class PassMetrics:
    """Everything one soak pass produced, already window-filtered."""

    scenario: str
    pass_index: int
    window_s: float

    # Lockups
    long_task_count: int
    longest_task_ms: float
    blocked_ms: float  # sum of (dur - 50) over long tasks — TBT-shaped
    long_tasks: list[dict] = field(repr=False, default_factory=list)

    # Poll health
    polls: int = 0
    full_responses: int = 0  # transfer > ~1KB ⇒ a real 200, not a 304
    body_bytes_p50: float = 0.0
    fetch_ms_p50: float = 0.0
    fetch_ms_p95: float = 0.0
    cadence_ms_p95: float = 0.0
    tick_work_ms_p50: float = 0.0  # poll gap - sleep - fetch ⇒ parse + render
    tick_work_ms_p95: float = 0.0

    # Growth (start snapshot is post-GC; *_post_gc compares to the post-GC end)
    node_growth: float = 0.0
    node_growth_post_gc: float = 0.0
    listener_growth: float = 0.0
    listener_growth_post_gc: float = 0.0
    heap_growth_mb: float = 0.0
    heap_growth_mb_post_gc: float = 0.0

    # Per-poll render-pipeline cost
    layout_per_poll: float = 0.0
    recalc_per_poll: float = 0.0
    script_ms_per_poll: float = 0.0
    main_thread_busy_pct: float = 0.0  # TaskDuration delta / wall-clock


def compute_pass(
    *,
    scenario: str,
    pass_index: int,
    start: Snapshot,
    end: Snapshot,
    end_post_gc: Snapshot,
    probe_data: dict,
) -> PassMetrics:
    """Project raw snapshots + probe entries onto the soak window
    [start.now_ms, end.now_ms]."""
    t0, t1 = start.now_ms, end.now_ms
    window_ms = max(1.0, t1 - t0)

    lts = [lt for lt in probe_data.get("longTasks", []) if t0 <= lt["start"] < t1]
    blocked = sum(max(0.0, lt["dur"] - 50.0) for lt in lts)

    polls = sorted(
        (p for p in probe_data.get("statePolls", []) if t0 <= p["start"] < t1),
        key=lambda p: p["start"],
    )
    gaps = [b["start"] - a["start"] for a, b in zip(polls, polls[1:], strict=False)]
    # Gap = fetch + parse + render + the fixed 500 ms sleep. Back out the
    # constants to estimate the client's own per-tick work.
    tick_work = [
        max(0.0, gap - NEXT_POLL_SLEEP_MS - a["dur"]) for gap, a in zip(gaps, polls[:-1], strict=True)
    ]
    fetch_ms = [p["dur"] for p in polls]
    bodies = [p["body"] for p in polls]
    n_polls = max(1, len(polls))

    return PassMetrics(
        scenario=scenario,
        pass_index=pass_index,
        window_s=window_ms / 1000.0,
        long_task_count=len(lts),
        longest_task_ms=max((lt["dur"] for lt in lts), default=0.0),
        blocked_ms=blocked,
        long_tasks=lts,
        polls=len(polls),
        full_responses=sum(1 for p in polls if p["transfer"] > 1024),
        body_bytes_p50=statistics.median(bodies) if bodies else 0.0,
        fetch_ms_p50=statistics.median(fetch_ms) if fetch_ms else 0.0,
        fetch_ms_p95=_p95(fetch_ms),
        cadence_ms_p95=_p95(gaps),
        tick_work_ms_p50=statistics.median(tick_work) if tick_work else 0.0,
        tick_work_ms_p95=_p95(tick_work),
        node_growth=end.nodes - start.nodes,
        node_growth_post_gc=end_post_gc.nodes - start.nodes,
        listener_growth=end.listeners - start.listeners,
        listener_growth_post_gc=end_post_gc.listeners - start.listeners,
        heap_growth_mb=(end.heap_used - start.heap_used) / 1e6,
        heap_growth_mb_post_gc=(end_post_gc.heap_used - start.heap_used) / 1e6,
        layout_per_poll=(end.layout_count - start.layout_count) / n_polls,
        recalc_per_poll=(end.recalc_count - start.recalc_count) / n_polls,
        script_ms_per_poll=(end.script_dur_s - start.script_dur_s) * 1000.0 / n_polls,
        main_thread_busy_pct=100.0 * (end.task_dur_s - start.task_dur_s) * 1000.0 / window_ms,
    )


_SUMMARY_FIELDS = (
    "long_task_count",
    "longest_task_ms",
    "blocked_ms",
    "polls",
    "full_responses",
    "body_bytes_p50",
    "fetch_ms_p50",
    "fetch_ms_p95",
    "cadence_ms_p95",
    "tick_work_ms_p50",
    "tick_work_ms_p95",
    "node_growth",
    "node_growth_post_gc",
    "listener_growth",
    "listener_growth_post_gc",
    "heap_growth_mb",
    "heap_growth_mb_post_gc",
    "layout_per_poll",
    "recalc_per_poll",
    "script_ms_per_poll",
    "main_thread_busy_pct",
)


def summarize(passes: list[PassMetrics]) -> dict[str, dict[str, float]]:
    """Median + worst across passes, per metric."""
    out: dict[str, dict[str, float]] = {}
    for name in _SUMMARY_FIELDS:
        vals = [float(getattr(p, name)) for p in passes]
        out[name] = {
            "median": statistics.median(vals) if vals else 0.0,
            "max": max(vals, default=0.0),
        }
    return out


def passes_as_json(passes: list[PassMetrics]) -> list[dict]:
    return [asdict(p) for p in passes]
