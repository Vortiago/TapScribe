// Unit tests for the live-feed coalescing helpers (run via `node --test`).
//
// These exercise the pure functions only — joinFragments / splitSentences /
// groupFeed touch no DOM, so they import cleanly in Node without a browser.
// `render` is left to the playwright dashboard e2e. The frontend tsconfig
// excludes *.test.js, so this file is never typechecked.

import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { joinFragments, splitSentences, groupFeed } from "./live-feed.js";

describe("joinFragments", () => {
  it("joins fragments with single spaces and trims each", () => {
    assert.equal(joinFragments(["It would fix", "the thing"]), "It would fix the thing");
    assert.equal(joinFragments(["  spaced  ", " out "]), "spaced out");
  });

  it("skips empty / whitespace-only fragments", () => {
    assert.equal(joinFragments(["a", "", "   ", "b"]), "a b");
    assert.equal(joinFragments([]), "");
    assert.equal(joinFragments(["", "   "]), "");
  });

  it("attaches punctuation-leading fragments without a leading space", () => {
    // WhisperLiveKit emits the period that ends one sentence at the START
    // of the next fragment; joining must not insert a space before it.
    assert.equal(joinFragments(["I was late", ". Sorry."]), "I was late. Sorry.");
    assert.equal(joinFragments(["wait", ", then go"]), "wait, then go");
  });
});

describe("splitSentences", () => {
  it("returns a single element for punctuation-free text (tiny.en degrade)", () => {
    assert.deepEqual(
      splitSentences("It would fix the thing where you got only the first patient in"),
      ["It would fix the thing where you got only the first patient in"],
    );
  });

  it("splits on . ! ? and keeps the terminator with its sentence", () => {
    assert.deepEqual(splitSentences("The build is green. Ship it."), ["The build is green.", "Ship it."]);
    assert.deepEqual(splitSentences("Did it work?! Yes."), ["Did it work?!", "Yes."]);
    assert.deepEqual(splitSentences("One. Two. Three."), ["One.", "Two.", "Three."]);
  });

  it("does not split mid-token dots (decimals)", () => {
    assert.deepEqual(splitSentences("it cost 3.50 dollars total"), ["it cost 3.50 dollars total"]);
  });

  it("keeps trailing closing quotes/brackets on the sentence", () => {
    assert.deepEqual(splitSentences('He said "go." Then left.'), ['He said "go."', "Then left."]);
  });

  it("returns [] for empty or whitespace-only input (no blank rows)", () => {
    assert.deepEqual(splitSentences(""), []);
    assert.deepEqual(splitSentences("   "), []);
  });
});

/** Build a live_feed entry with sensible defaults. */
const entry = (over) => ({ ts: "", identity: "", name: "", text: "", session: "s", ...over });

describe("groupFeed", () => {
  it("merges consecutive same-speaker fragments, then splits into sentences", () => {
    const out = groupFeed([
      entry({ ts: "2026-06-04T07:13:37Z", identity: "otto", name: "Otto", text: "Did it." }),
      entry({ ts: "2026-06-04T07:13:38Z", identity: "otto", name: "Otto", text: "It would fix the thing." }),
    ]);
    // One run (same speaker, 1s apart) → two sentences, each carrying the
    // run's STARTING timestamp and the speaker attribution.
    assert.deepEqual(out, [
      { who: "Otto", identity: "otto", ts: "2026-06-04T07:13:37Z", text: "Did it." },
      { who: "Otto", identity: "otto", ts: "2026-06-04T07:13:37Z", text: "It would fix the thing." },
    ]);
  });

  it("breaks the run when the speaker changes", () => {
    const out = groupFeed([
      entry({ ts: "2026-06-04T07:13:37Z", identity: "otto", name: "Otto", text: "Yeah" }),
      entry({ ts: "2026-06-04T07:13:38Z", identity: "mikkel", name: "Mikkel", text: "No this is part of that" }),
      entry({ ts: "2026-06-04T07:13:39Z", identity: "otto", name: "Otto", text: "Sorry" }),
    ]);
    assert.equal(out.length, 3);
    assert.deepEqual(out.map((g) => g.who), ["Otto", "Mikkel", "Otto"]);
    assert.deepEqual(out.map((g) => g.text), ["Yeah", "No this is part of that", "Sorry"]);
  });

  it("breaks the run when the same speaker pauses longer than the gap", () => {
    const out = groupFeed([
      entry({ ts: "2026-06-04T07:13:00Z", identity: "otto", name: "Otto", text: "first turn" }),
      // 31s later — past GROUP_GAP_MS (30s) → a new run, not a continuation.
      entry({ ts: "2026-06-04T07:13:31Z", identity: "otto", name: "Otto", text: "second turn" }),
    ]);
    assert.deepEqual(out.map((g) => g.text), ["first turn", "second turn"]);
    assert.deepEqual(out.map((g) => g.ts), ["2026-06-04T07:13:00Z", "2026-06-04T07:13:31Z"]);
  });

  it("groups by identity but displays name; falls back when one is missing", () => {
    // Same identity, different display name on the second entry → still one
    // run, keyed on identity; `who` is the run's first entry's name.
    const sameId = groupFeed([
      entry({ ts: "2026-06-04T07:13:00Z", identity: "u1", name: "Alice", text: "one" }),
      entry({ ts: "2026-06-04T07:13:01Z", identity: "u1", name: "Alice (mobile)", text: "two" }),
    ]);
    assert.equal(sameId.length, 1);
    assert.equal(sameId[0].who, "Alice");
    assert.equal(sameId[0].text, "one two");

    // No identity → key falls back to name.
    const nameOnly = groupFeed([
      entry({ ts: "2026-06-04T07:13:00Z", name: "Bob", text: "hi" }),
      entry({ ts: "2026-06-04T07:13:01Z", name: "Bob", text: "there" }),
    ]);
    assert.equal(nameOnly.length, 1);
    assert.equal(nameOnly[0].who, "Bob");
  });

  it("still groups by speaker when timestamps are missing/unparseable", () => {
    const out = groupFeed([
      entry({ identity: "otto", name: "Otto", text: "no ts here" }),
      entry({ identity: "otto", name: "Otto", text: "still otto" }),
    ]);
    assert.equal(out.length, 1);
    assert.equal(out[0].text, "no ts here still otto");
  });

  it("returns [] for an empty feed", () => {
    assert.deepEqual(groupFeed([]), []);
  });
});
