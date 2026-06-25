// @ts-check
// Popup E2E — drives the real popup.html in Chromium (served statically).
// Verifies the rendering the node tests can't: the ES-module graph loads, the
// vendored vanilla-components fetch + render their HTML/CSS, and the meeting
// card flow shows the right thing for each pipeline state.
//
// The popup runs as a normal page here (not an extension), so we shim chrome.*
// (an in-memory storage seeded per test) and stub the recorder's /health +
// pipeline poll. That covers the rendering risk; the real extension's
// chrome.storage + content-script bridge are out of scope for this UI test.
import { test, expect } from "@playwright/test";

const POLL_RE = /\/api\/tap\/sessions\/[^/]+\/pipeline/;

/**
 * @param {import("@playwright/test").Page} page
 * @param {{ store?: Record<string, unknown>, poll?: unknown }} [opts]
 */
async function openPopup(page, { store = {}, poll = null } = {}) {
  await page.addInitScript((initial) => {
    const data = { ...initial };
    /** @type {Function[]} */ const listeners = [];
    // @ts-ignore — define the chrome.* surface the popup uses.
    globalThis.chrome = {
      storage: {
        local: {
          get: (keys) =>
            Promise.resolve(Object.fromEntries(keys.map((/** @type {string} */ k) => [k, data[k]]))),
          set: (obj) => {
            // Match real chrome.storage.onChanged: fire ONLY for keys whose value
            // actually changed. A no-op re-set of an unchanged key (e.g. dismiss
            // re-writing meetingActive:false) fires nothing — emitting it here
            // would drive listeners down code paths Chrome never reaches.
            const ch = {};
            for (const k of Object.keys(obj)) {
              if (data[k] === obj[k]) continue;
              ch[k] = { oldValue: data[k], newValue: obj[k] };
            }
            Object.assign(data, obj);
            if (Object.keys(ch).length) for (const fn of listeners) fn(ch, "local");
            return Promise.resolve();
          },
        },
        onChanged: { addListener: (fn) => listeners.push(fn), removeListener: () => {} },
      },
      tabs: { create: () => {} },
    };
    // Deterministic clipboard: record the copied text instead of touching the
    // real (headless-flaky) clipboard.
    let copied = "";
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: (/** @type {string} */ t) => { copied = t; /* @ts-ignore */ globalThis.__copied = t; return Promise.resolve(); },
        readText: () => Promise.resolve(copied),
      },
    });
  }, store);

  await page.route("**/health", (r) => r.fulfill({ json: { status: "ok" } }));
  await page.route(POLL_RE, (r) => r.fulfill({ json: poll || { ok: true, state: "idle" } }));
  await page.goto("/popup.html");
}

test("loads and renders its structure with no page errors", async ({ page }) => {
  /** @type {string[]} */ const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await openPopup(page);

  await expect(page.getByRole("button", { name: "Start meeting" })).toBeVisible();
  await expect(page.getByRole("button", { name: "End meeting" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Save" })).toBeVisible();
  await expect(page.locator("#host")).toBeVisible();
  await expect(page.locator("#tapState")).toContainText(/No taps|No status/i);

  expect(errors, "no uncaught errors during popup load").toEqual([]);
});

test("an active meeting disables Start and enables End", async ({ page }) => {
  await openPopup(page, { store: { meetingSessionId: "sess-1", meetingActive: true } });
  await expect(page.getByRole("button", { name: "Start meeting" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "End meeting" })).toBeEnabled();
  await expect(page.locator("#meetingStatus")).toContainText("Meeting active");
});

test("a running pipeline renders the stage progress", async ({ page }) => {
  await openPopup(page, {
    store: { meetingSessionId: "s", meetingActive: false },
    poll: { ok: true, state: "running", stage: "transcribe", status: "transcribing", current: 3, total: 12 },
  });
  await expect(page.locator('[data-slot="progress"]')).toContainText("Transcribing 3/12");
});

test("a finished meeting renders the summary, metadata, and Copy copies it", async ({ page }) => {
  await openPopup(page, {
    store: { meetingSessionId: "2026-06-19T10-00-00Z", meetingActive: false },
    poll: {
      ok: true, state: "done",
      summary: { summary: "We agreed to ship Friday.", model: "qwen3-0.6b", source: "local" },
    },
  });
  await expect(page.locator('[data-slot="summaryText"]')).toHaveText("We agreed to ship Friday.");
  await expect(page.locator("#meetingCardHost")).toContainText("qwen3-0.6b");

  await page.getByRole("button", { name: "Copy" }).click();
  const copied = await page.evaluate(() => /** @type {any} */ (globalThis).__copied);
  expect(copied).toBe("We agreed to ship Friday.");
});

test("a re-render tick does not clobber a mid-copy selection in the summary", async ({ page }) => {
  // ADR-0004 interaction-hold, applied by hand in the popup: the summary pane
  // is rendered ONCE on the transition to done (popup.js `summaryRenderedFor`
  // gate). A later re-render — e.g. a `meetingEnd` storage tick landing while
  // the user is mid-copy — must NOT rewrite the summary text node, or the
  // selection collapses and the copy gesture is lost.
  await openPopup(page, {
    store: { meetingSessionId: "2026-06-19T10-00-00Z", meetingActive: false },
    poll: {
      ok: true, state: "done",
      summary: { summary: "We agreed to ship Friday.", model: "qwen3-0.6b", source: "local" },
    },
  });
  await expect(page.locator('[data-slot="summaryText"]')).toHaveText("We agreed to ship Friday.");

  // The user starts copying: select the summary line at the TEXT-NODE level
  // (character offsets), so that replacing the text node would collapse it —
  // an element-level selectNodeContents would survive a same-text rewrite and
  // make this assertion vacuous.
  await page.evaluate(() => {
    const el = document.querySelector('[data-slot="summaryText"]');
    const textNode = el.firstChild;
    const range = document.createRange();
    range.setStart(textNode, 0);
    range.setEnd(textNode, textNode.textContent.length);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  });
  expect(await page.evaluate(() => window.getSelection().toString()))
    .toBe("We agreed to ship Friday.");

  // A storage tick lands mid-copy (content.js publishing the End-handshake
  // result as the pipeline completes) → the popup re-derives the card on BOTH
  // the synchronous applyMeeting path AND the async pollCardOnce re-poll it
  // kicks off. Gate the final assertion on both: the headline proves the sync
  // render ran, and awaiting the re-poll's response proves the async,
  // post-await render landed too — so a regression confined to that later
  // render can't slip past before the selection is read on a slow runner.
  const rePoll = page.waitForResponse(POLL_RE);
  await page.evaluate(() => chrome.storage.local.set({ meetingEnd: { phase: "started" } }));
  await expect(page.locator("#meetingStatus")).toContainText("processing started");
  await rePoll;

  // The selection still spans the summary → the render-once gate held. Without
  // it, the summary text node is reassigned and the selection collapses to "".
  expect(await page.evaluate(() => window.getSelection().toString()))
    .toBe("We agreed to ship Friday.");
});

test("a failed pipeline surfaces the stage and a human-readable reason", async ({ page }) => {
  await openPopup(page, {
    store: { meetingSessionId: "s", meetingActive: false },
    poll: { ok: true, state: "failed", stage: "transcribe", error: "boom", error_kind: "NoUsableWavs" },
  });
  const failure = page.locator('[data-slot="failure"]');
  await expect(failure).toBeVisible();
  await expect(failure).toContainText("transcribe");
  await expect(failure).toContainText(/no usable audio/i);
});

// ── busy / failure / restart branches ───────────────────────────────────────
// The popup re-derives its headline from durable meeting state on open, so the
// busy + end-failed branches are storage-driven (no poll). The restart path —
// Dismiss after a finished/failed meeting — was untested entirely; only the
// happy-path Copy used the card's buttons.

test("a busy end-of-meeting surfaces the recorder-busy headline", async ({ page }) => {
  // content.js publishes meetingEnd { phase: "busy" } when the pipeline trigger
  // gets a 409 (another job already running on the session). The popup derives
  // the headline from that durable state on open — the poll stays idle.
  await openPopup(page, {
    store: {
      meetingSessionId: "2026-06-19T10-00-00Z",
      meetingActive: false,
      meetingEnd: { phase: "busy" },
    },
  });
  const status = page.locator("#meetingStatus");
  await expect(status).toContainText("Recorder busy");
  await expect(status).toHaveClass(/err/);
});

test("an end-meeting failure surfaces the failure headline", async ({ page }) => {
  // Distinct from the failed-pipeline CARD above: this is the End trigger itself
  // failing (meetingEnd { phase: "failed" }), surfaced as a headline.
  await openPopup(page, {
    store: {
      meetingSessionId: "2026-06-19T10-00-00Z",
      meetingActive: false,
      meetingEnd: { phase: "failed", error: "the recorder rejected the range" },
    },
  });
  const status = page.locator("#meetingStatus");
  await expect(status).toContainText("End meeting failed: the recorder rejected the range");
  await expect(status).toHaveClass(/err/);
});

test("dismissing a failed meeting clears the headline and the card", async ({ page }) => {
  // Seed BOTH a failed-End headline and a failed pipeline poll, so there is
  // something to clear: the headline AND the card are present before Dismiss
  // (otherwise an empty-after-dismiss check would pass vacuously). Dismiss clears
  // the durable result, returning the popup to idle — the restart path; Start is
  // already enabled whenever Dismiss is offered, since the meeting is inactive.
  await openPopup(page, {
    store: {
      meetingSessionId: "s",
      meetingActive: false,
      meetingEnd: { phase: "failed", error: "the recorder rejected the range" },
    },
    poll: { ok: true, state: "failed", stage: "transcribe", error: "boom", error_kind: "NoUsableWavs" },
  });

  const status = page.locator("#meetingStatus");
  const failure = page.locator('[data-slot="failure"]');
  await expect(status).toContainText("End meeting failed");
  await expect(failure).toBeVisible();
  const dismiss = page.getByRole("button", { name: "Dismiss" });
  await expect(dismiss).toBeVisible();

  await dismiss.click();

  // Both the headline and the card are gone; the popup is idle and ready.
  await expect(status).toBeEmpty();
  await expect(failure).toBeHidden();
  await expect(page.getByRole("button", { name: "Start meeting" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "End meeting" })).toBeDisabled();
});

// ── Active taps staleness ───────────────────────────────────────────────────
// bridgeStatus lives in chrome.storage.local and OUTLIVES the content script
// that wrote it. A closed/crashed SpatialChat tab leaves its last snapshot
// behind; without a staleness check the popup renders departed speakers as live
// OPEN/active taps with no tab open at all (the reported bug). content.js
// refreshes the snapshot's `ts` while it runs (taps-view.snapshotIsLive), so the
// popup shows channels only for a fresh snapshot and the no-tab empty state for
// a stale one.

/** A two-speaker "Active taps" snapshot stamped at `ts`. @param {number} ts */
function tapStatusSnapshot(ts) {
  return {
    ts,
    audioContextState: "running",
    settingsReady: true,
    meetingActive: false,
    meetingSessionId: null,
    channels: [
      { identity: "man-id", name: "Maneevannan", muted: false, draining: false, error: null, framesSent: 101305, tapWs: "OPEN" },
      { identity: "khiem-id", name: "Khiem Nguyen", muted: false, draining: false, error: null, framesSent: 67265, tapWs: "OPEN" },
    ],
  };
}

test("a fresh bridgeStatus renders the per-speaker tap rows", async ({ page }) => {
  // ts in the (near) future → always within the freshness window for the test's
  // duration, so the live-tab path is exercised deterministically.
  await openPopup(page, { store: { bridgeStatus: tapStatusSnapshot(Date.now() + 600_000) } });
  const taps = page.locator("#tapState");
  await expect(taps).toContainText("Maneevannan");
  await expect(taps).toContainText("Khiem Nguyen");
  await expect(taps).toContainText("OPEN");
  await expect(taps).toContainText("active");
});

test("a stale bridgeStatus (closed tab's leftover) shows the no-tab empty state, not live taps", async ({ page }) => {
  // The reported bug verbatim: no SpatialChat windows open, yet the popup showed
  // Maneevannan / Khiem as OPEN + active. ts 10 min old ⇒ content.js is gone.
  await openPopup(page, { store: { bridgeStatus: tapStatusSnapshot(Date.now() - 600_000) } });
  const taps = page.locator("#tapState");
  await expect(taps).toContainText("No active SpatialChat tab");
  await expect(taps).not.toContainText("Maneevannan");
  await expect(taps).not.toContainText("OPEN");
});
