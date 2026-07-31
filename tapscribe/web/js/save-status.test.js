// Unit tests for the shared save-status lifecycle (run via `node --test`).
//
// This is the ONE settle path every save with a status cell goes through — the
// save buttons (`wireSave`) and the debounced optimistic field edits
// (next/field-saver.js) alike. It was four hand-rolled copies before #355, the
// last of which (`wireSave`, then in api.js) had already drifted on both the
// badge duration and the guardedness of the "saved" promotion.
//
// DOM-free: `statusTarget` only ever touches `.textContent`, so a plain object
// stands in for a status cell. Only setTimeout is mocked, so the badge timers are
// driven by mock.timers.tick while each case simply awaits the save's own promise
// (no macrotask drain needed here — unlike field-saver.test.js, where the save is
// started by a debounce timer rather than by the test).
//
// The frontend tsconfig excludes *.test.js, so this file is never typechecked.

import { describe, it, mock, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";

import {
  statusTarget,
  cellStatus,
  runSaveWithStatus,
  wireConfigSave,
  SAVED_BADGE_MS,
  SAVING,
  SAVED,
} from "./save-status.js";

/** A stand-in for a status cell (Element.textContent, not an HTMLElement API). */
const fakeCell = (textContent = "") => ({ textContent });

describe("statusTarget", () => {
  it("writes every cell the resolver returns", () => {
    const a = fakeCell();
    const b = fakeCell();
    statusTarget(() => [a, b]).set(SAVING);
    assert.equal(a.textContent, "saving…");
    assert.equal(b.textContent, "saving…");
  });

  it("re-resolves on every write, so a cell rebuilt mid-save still gets the result", () => {
    const before = fakeCell();
    const after = fakeCell();
    // The card holding the status cell is rebuilt between the PUT starting and
    // settling (spine.js: the interaction hold's blur flush does exactly this
    // inside the debounce window). A target that captured `before` would write
    // "saved" to a detached node and the operator would see nothing.
    let live = before;
    const target = statusTarget(() => [live]);
    target.set(SAVING);
    live = after;
    after.textContent = SAVING; // the rebuild reproduces the current text
    target.replace(SAVING, SAVED);

    assert.equal(after.textContent, "saved", "the live cell shows the outcome");
    assert.equal(before.textContent, "saving…", "the detached cell is left as it was");
  });

  it("guards replace per cell, so a superseded cell keeps its newer text", () => {
    const settled = fakeCell(SAVING);
    const superseded = fakeCell("failed: 500 nope");
    statusTarget(() => [settled, superseded]).replace(SAVING, SAVED);
    assert.equal(settled.textContent, "saved");
    assert.equal(superseded.textContent, "failed: 500 nope", "a newer message is never overwritten");
  });
});

describe("runSaveWithStatus", () => {
  beforeEach(() => mock.timers.enable({ apis: ["setTimeout"] }));
  afterEach(() => mock.timers.reset());

  it("narrates a successful save saving… → saved → cleared, and calls onSuccess once", async () => {
    const cell = fakeCell();
    let succeeded = 0;
    let settled = 0;
    const done = runSaveWithStatus(cellStatus(cell), async () => {}, {
      onSuccess: () => void succeeded++,
      afterSettle: () => void settled++,
    });
    assert.equal(cell.textContent, "saving…", "the badge shows while the PUT is in flight");

    await done;
    assert.equal(cell.textContent, "saved");
    assert.equal(succeeded, 1);
    assert.equal(settled, 1);

    mock.timers.tick(SAVED_BADGE_MS);
    assert.equal(cell.textContent, "", "the saved badge clears itself");
  });

  it("reports a rejected save as failed: …, leaves it on screen, and skips onSuccess", async () => {
    const cell = fakeCell();
    let succeeded = 0;
    let settled = 0;
    await runSaveWithStatus(cellStatus(cell), async () => { throw new Error("500 disk full"); }, {
      onSuccess: () => void succeeded++,
      afterSettle: () => void settled++,
    });

    assert.equal(cell.textContent, "failed: 500 disk full", "errText strips the Error: prefix");
    assert.equal(succeeded, 0, "onSuccess fires only on success");
    assert.equal(settled, 1, "afterSettle runs either way — the caller re-enables its button there");

    mock.timers.tick(SAVED_BADGE_MS * 3);
    assert.equal(cell.textContent, "failed: 500 disk full", "a failure never auto-clears");
  });

  it("does not stomp a newer message written while the PUT was in flight", async () => {
    // The guarded promotion: `wireSave` used to write "saved" unconditionally,
    // so a save settling into a cell something else had already claimed would
    // overwrite it.
    const cell = fakeCell();
    await runSaveWithStatus(cellStatus(cell), async () => {
      cell.textContent = "3 of 7 transcribed";
    });
    assert.equal(cell.textContent, "3 of 7 transcribed");
  });

  it("clears a stale failure left by an earlier save sharing the cell", async () => {
    // summary.js wires two buttons to ONE status cell and each disables only its
    // own button, so an earlier save's `failed: …` can still be on screen when a
    // later one succeeds. Failures never auto-clear, so if the success couldn't
    // supersede it the operator would be told a save that worked had failed.
    const cell = fakeCell("failed: 500 disk full");
    await runSaveWithStatus(cellStatus(cell), async () => {});
    assert.equal(cell.textContent, "saved");
  });

  it("works without hooks at all", async () => {
    const cell = fakeCell();
    await runSaveWithStatus(cellStatus(cell), async () => {});
    assert.equal(cell.textContent, "saved");
  });
});

describe("wireConfigSave", () => {
  /** A stand-in for a save button: captures the one click listener wireSave adds. */
  const fakeBtn = () => {
    let handler = null;
    return {
      disabled: false,
      addEventListener: (_type, fn) => { handler = fn; },
      click: () => handler?.(),
    };
  };
  /** A stand-in for <input type="number">: `badInput` is the typo signal. */
  const fakeNumberInput = (value, badInput) => ({ value, validity: { badInput } });

  afterEach(() => { delete globalThis.fetch; });

  it("refuses a number-input typo instead of PUTting an empty clear", async () => {
    // A browser number input coerces text it cannot parse to "", and "" is the
    // DELIBERATE clear that hands a knob back to its default — so without this
    // guard a typo silently resets the knob under a green "saved".
    const cell = fakeCell();
    const btn = fakeBtn();
    let puts = 0;
    globalThis.fetch = async () => { puts++; return new Response("{}"); };

    wireConfigSave({ key: "parakeet-chunk-s", btn, textarea: fakeNumberInput("", true), status: cell });
    await btn.click();

    assert.equal(puts, 0, "nothing reaches the server");
    assert.match(cell.textContent, /^failed: /, "the operator is told, not shown a green saved");
    assert.equal(btn.disabled, false, "the button is re-enabled so they can fix it");
  });

  it("still allows the deliberate clear (empty value, not badInput)", async () => {
    const cell = fakeCell();
    const btn = fakeBtn();
    let body = null;
    globalThis.fetch = async (_url, opts) => {
      body = JSON.parse(opts.body);
      return new Response("{}", { headers: { "content-type": "application/json" } });
    };

    wireConfigSave({ key: "parakeet-chunk-s", btn, textarea: fakeNumberInput("", false), status: cell });
    await btn.click();

    assert.deepEqual(body, { content: "" }, "an empty value still clears the override");
    assert.equal(cell.textContent, SAVED);
  });
});
