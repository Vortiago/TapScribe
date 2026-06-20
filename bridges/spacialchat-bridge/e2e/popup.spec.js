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
            Object.assign(data, obj);
            const ch = {};
            for (const k of Object.keys(obj)) ch[k] = { newValue: obj[k] };
            for (const fn of listeners) fn(ch, "local");
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

test("dismissing a failed meeting clears the card and re-enables Start", async ({ page }) => {
  // A finished/failed meeting offers Dismiss (the meeting is no longer active).
  // Dismissing clears the durable result so the next open is idle — the operator
  // can immediately start the next meeting (the restart path).
  await openPopup(page, {
    store: { meetingSessionId: "s", meetingActive: false },
    poll: { ok: true, state: "failed", stage: "transcribe", error: "boom", error_kind: "NoUsableWavs" },
  });

  const failure = page.locator('[data-slot="failure"]');
  await expect(failure).toBeVisible();
  const dismiss = page.getByRole("button", { name: "Dismiss" });
  await expect(dismiss).toBeVisible();

  await dismiss.click();

  await expect(failure).toBeHidden();
  await expect(page.getByRole("button", { name: "Start meeting" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "End meeting" })).toBeDisabled();
  await expect(page.locator("#meetingStatus")).toBeEmpty();
});
