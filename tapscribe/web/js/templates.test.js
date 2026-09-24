// Unit tests for the renderRegionModel primitive (run via `node --test`).
//
// #255: a region whose inputs are a pure derivation of state can render from a
// Region model — a plain JSON-serialisable value whose serialisation IS the
// render signature. These pin the seam's contract: the model reaches `build`
// unchanged, the gate reads the SERIALISATION (not object identity), and the
// interaction hold, the tick-retry, markRegionStale and the sig audit are
// renderRegion's unchanged — the audit stays the backstop, now over modelled
// regions too.
//
// The frontend tsconfig excludes *.test.js, so this file is never typechecked.
// The fake host records swaps and serialises children into `innerHTML` (the
// audit probe compares it); the method definition and the innerHTML getter keep
// check-conventions' raw-swap / html-string regexes clean — the fake only ever
// reads or defines, never calls or assigns through a dot.

import { test } from "node:test";
import assert from "node:assert/strict";

import { consumeDeferredRender, markRegionStale, renderRegionModel } from "./templates.js";

const realDocument = globalThis.document;

/** A fake host: counts swaps, reports containment by `parent`, answers the
 * overlay/selection probes empty, and serialises its children as `innerHTML`. */
const fakeNode = () => {
  let markup = "";
  const node = {
    tagName: "DIV",
    children: [],
    swaps: 0,
    contains: (n) => n?.parent === node,
    querySelector: () => null,
    get innerHTML() {
      return markup;
    },
    replaceChildren(...ns) {
      node.swaps++;
      node.children = ns;
      markup = JSON.stringify(ns);
    },
  };
  return node;
};

/** A document with no focus and no selection — the seam's guards read exactly
 * these three things before handing the swap to canon. */
const fakeDoc = () => ({
  body: {},
  activeElement: null,
  getSelection: () => null,
  createElement: () => fakeNode(),
});

/** Run `fn` under a fresh fake document; never leak globals or the retry flag. */
const withDoc = (fn) => {
  const doc = (globalThis.document = fakeDoc());
  try {
    fn(doc);
  } finally {
    globalThis.document = realDocument;
    consumeDeferredRender();
  }
};

test("renders on the first call, handing build the model itself", () => {
  withDoc(() => {
    const host = fakeNode();
    const model = { here: "s1", rows: [] };
    let seen = null;
    renderRegionModel(host, model, (m) => {
      seen = m;
      return "row";
    });
    assert.equal(host.swaps, 1);
    assert.equal(seen, model, "build receives the model itself, not a copy");
  });
});

test("an equal model, freshly derived, skips the swap", () => {
  withDoc(() => {
    const host = fakeNode();
    renderRegionModel(host, { here: "s1", n: 1 }, () => "row");
    assert.equal(host.swaps, 1);
    consumeDeferredRender();

    renderRegionModel(host, { here: "s1", n: 1 }, () => "row");
    assert.equal(host.swaps, 1, "different object identity, equal serialisation → no swap");
    assert.equal(consumeDeferredRender(), false, "a skip marks no retry");
  });
});

test("a changed model swaps again", () => {
  withDoc(() => {
    const host = fakeNode();
    renderRegionModel(host, { n: 1 }, () => "row");
    renderRegionModel(host, { n: 2 }, () => "row");
    assert.equal(host.swaps, 2);
  });
});

test("a focused control inside the host defers without advancing the gate", () => {
  withDoc((doc) => {
    const host = fakeNode();
    renderRegionModel(host, { n: 1 }, () => "row");
    assert.equal(host.swaps, 1);
    consumeDeferredRender();

    doc.activeElement = { tagName: "INPUT", parent: host };
    renderRegionModel(host, { n: 2 }, () => "row");
    assert.equal(host.swaps, 1, "held: no swap");
    assert.equal(consumeDeferredRender(), true, "the hold marks the tick-retry (ADR-0004)");

    doc.activeElement = null;
    renderRegionModel(host, { n: 2 }, () => "row");
    assert.equal(host.swaps, 2, "the held render lands on re-offer — the gate stayed unadvanced");
  });
});

test("markRegionStale forces the next modelled render, through the hold", () => {
  withDoc((doc) => {
    const host = fakeNode();
    const model = { n: 1 };
    renderRegionModel(host, model, () => "row");
    assert.equal(host.swaps, 1);
    consumeDeferredRender();

    markRegionStale(host);
    doc.activeElement = { tagName: "INPUT", parent: host };
    renderRegionModel(host, model, () => "row");
    assert.equal(host.swaps, 1, "stale + held → still defers, never forces");
    assert.equal(consumeDeferredRender(), true);

    doc.activeElement = null;
    renderRegionModel(host, model, () => "row");
    assert.equal(host.swaps, 2, "after release the same model rebuilds — the sig was invalidated");
  });
});

test("an undefined model gates on a constant serialisation", () => {
  withDoc(() => {
    const host = fakeNode();
    renderRegionModel(host, undefined, () => "row");
    assert.equal(host.swaps, 1);
    renderRegionModel(host, undefined, () => "row");
    // JSON.stringify(undefined) is undefined, which renderRegion reads as "no
    // sig" — an ungated region would swap every tick, and with a caret parked
    // inside it would mark the retry forever (#245). `?? ""` closes that.
    assert.equal(host.swaps, 1);
    assert.equal(consumeDeferredRender(), false);
  });
});

test("the sig audit probes modelled regions with no drift", () => {
  withDoc(() => {
    globalThis.__TAPSCRIBE_SIG_AUDIT = true;
    globalThis.__TAPSCRIBE_SIG_PROBES = { region: 0, list: 0, row: 0 };
    globalThis.__TAPSCRIBE_SIG_DRIFT = [];
    try {
      const host = fakeNode();
      renderRegionModel(host, { n: 1 }, () => "row");
      renderRegionModel(host, { n: 1 }, () => "row"); // sig skip → the probe runs
      assert.ok(globalThis.__TAPSCRIBE_SIG_PROBES.region > 0, "the modelled skip was probed");
      assert.deepEqual(globalThis.__TAPSCRIBE_SIG_DRIFT, [], "a modelled render cannot drift");
    } finally {
      delete globalThis.__TAPSCRIBE_SIG_AUDIT;
      delete globalThis.__TAPSCRIBE_SIG_PROBES;
      delete globalThis.__TAPSCRIBE_SIG_DRIFT;
    }
  });
});
