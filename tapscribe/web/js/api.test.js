// Unit tests for the api.js resource layer (run via `node --test`).
//
// These pin the load-bearing caching semantics of the `_resource(keyOf, load)`
// factory (#188) and `loadSessionFiles`'s stale-while-revalidate, which had no
// unit test anywhere (#234): in-flight dedup, peek-vs-fetch, and the
// failure-eviction (api.js `.catch((e) => { cache.delete(key); throw e; })`)
// that keeps a rejected fetch from staying cached and stranding a pane.
//
// The factory is private, so we drive it through the PUBLIC exports it backs
// (sessionTranscript / sessionSummary / sessionFiles / loadSessionFiles) with a
// stubbed globalThis.fetch — same no-deps shape as live-feed.test.js, and it
// also exercises the real _unwrap wiring (a 500 → thrown Error → eviction). The
// frontend tsconfig excludes *.test.js, so this file is never typechecked.
//
// The exported resources are module singletons whose caches persist across
// cases, so each case uses UNIQUE keys (session id / stamp) to stay isolated;
// the eviction cases deliberately reuse one key to prove the retry re-fetches.

import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { sessionTranscript, sessionSummary, sessionFiles, loadSessionFiles } from "./api.js";

// A minimal Response-like the real _unwrap accepts. _unwrap reads .ok, .status,
// .headers.get("content-type") and .json(); an application/json content-type
// routes it to .json() on both the ok and error paths.
const makeRes = (ok, status, body) => ({
  ok,
  status,
  headers: { get: (h) => (h.toLowerCase() === "content-type" ? "application/json" : null) },
  json: async () => body,
});
// A 200 JSON success carrying `body`.
const jsonRes = (body) => makeRes(true, 200, body);
// A non-ok whose JSON `detail` becomes _unwrap's thrown "<status> <detail>".
const errRes = (status, detail) => makeRes(false, status, { detail });

// Install a stubbed globalThis.fetch for the duration of `body`, restoring the
// real one afterwards. `responder(url, callNo)` returns a Response-like (or
// throws, modelling fetch itself rejecting); `callNo` is 1-based so a responder
// can fail the 1st call and succeed the 2nd. The fetch resolves on a later
// microtask (not synchronously), so `.peek()` observes the true in-flight
// window. `calls` records every request so a test can assert dedup / retries.
async function withFetch(responder, body) {
  const orig = globalThis.fetch;
  const calls = [];
  globalThis.fetch = (url, opts) => {
    const u = String(url);
    const n = calls.push({ url: u, opts });
    return Promise.resolve().then(() => responder(u, n));
  };
  try {
    return await body(calls);
  } finally {
    globalThis.fetch = orig;
  }
}

// Drain microtasks + one macrotask so a fetch's .then/.catch/.finally chains
// (settle bookkeeping, pending-key cleanup, onLand) have all run.
const flush = () => new Promise((r) => setTimeout(r, 0));

describe("_resource: in-flight dedup", () => {
  it("shares one promise + one request between concurrent fetches of the same key", async () => {
    await withFetch(() => jsonRes({ text: "hi" }), async (calls) => {
      const p1 = sessionTranscript.fetch("dedup-s", "t1");
      const p2 = sessionTranscript.fetch("dedup-s", "t1");
      // The second caller gets the SAME in-flight promise (api.js: two callers
      // "share one in-flight request"), not a fresh fetch.
      assert.equal(p1, p2);
      const [v1] = await Promise.all([p1, p2]);
      assert.deepEqual(v1, { text: "hi" });
      assert.equal(calls.length, 1);
    });
  });
});

describe("_resource: peek vs fetch", () => {
  it("returns undefined before the fetch settles, the resolved value after", async () => {
    await withFetch(() => jsonRes({ text: "body" }), async () => {
      // Never fetched → undefined.
      assert.equal(sessionTranscript.peek("peek-s", "t1"), undefined);
      const p = sessionTranscript.fetch("peek-s", "t1");
      // In flight, not yet settled → still undefined (peek never touches the promise).
      assert.equal(sessionTranscript.peek("peek-s", "t1"), undefined);
      const v = await p;
      assert.deepEqual(v, { text: "body" });
      // Settled → the resolved value, read without re-fetching.
      assert.deepEqual(sessionTranscript.peek("peek-s", "t1"), { text: "body" });
    });
  });

  it("distinguishes a settled null (loaded but empty) from an unfetched key (undefined)", async () => {
    // sessionTranscript resolves null when a session has no merged transcript.
    // A render must tell that "loaded, nothing there" (null) apart from "not
    // fetched / still loading" (undefined) — peek only returns the value once
    // the entry has actually settled.
    await withFetch(() => jsonRes(null), async () => {
      assert.equal(sessionTranscript.peek("peek-null", "t1"), undefined); // never fetched
      const v = await sessionTranscript.fetch("peek-null", "t1");
      assert.equal(v, null);
      assert.equal(sessionTranscript.peek("peek-null", "t1"), null); // settled-empty, NOT undefined
    });
  });
});

describe("_resource: signature keying", () => {
  it("busts the cache when the stamp changes (one request per (id, stamp))", async () => {
    await withFetch(() => jsonRes({ text: "x" }), async (calls) => {
      await sessionTranscript.fetch("key-s", "stampA");
      await sessionTranscript.fetch("key-s", "stampA"); // same key → cached, no request
      assert.equal(calls.length, 1);
      await sessionTranscript.fetch("key-s", "stampB"); // new stamp → new request
      assert.equal(calls.length, 2);
    });
  });
});

describe("_resource: failure eviction", () => {
  it("evicts a rejected fetch so the next call retries (network reject)", async () => {
    await withFetch(
      (_url, n) => {
        if (n === 1) throw new Error("boom");
        return jsonRes({ text: "recovered" });
      },
      async (calls) => {
        await assert.rejects(sessionTranscript.fetch("rej-s", "t1"), /boom/);
        // Rejected key must not stay cached, or the pane is stuck until reload.
        assert.equal(sessionTranscript.peek("rej-s", "t1"), undefined);
        const v = await sessionTranscript.fetch("rej-s", "t1");
        assert.deepEqual(v, { text: "recovered" });
        assert.equal(calls.length, 2); // the retry actually hit the network again
      },
    );
  });

  it("evicts on an HTTP 500 (recorder error) so the next call retries", async () => {
    await withFetch(
      (_url, n) => (n === 1 ? errRes(500, "recorder exploded") : jsonRes({ text: "ok now" })),
      async (calls) => {
        // _unwrap turns the 500 body's detail into the thrown message.
        await assert.rejects(sessionTranscript.fetch("e500-s", "t1"), /500 recorder exploded/);
        assert.equal(sessionTranscript.peek("e500-s", "t1"), undefined);
        const v = await sessionTranscript.fetch("e500-s", "t1");
        assert.deepEqual(v, { text: "ok now" });
        assert.equal(calls.length, 2);
      },
    );
  });
});

describe("loadSessionFiles: stale-while-revalidate", () => {
  it("holds the last-good listing while a newer files_sig refetches (no blank)", async () => {
    // The /files URL carries no sig (it's only in the cache key), so the
    // responder distinguishes the two sigs by call order: 1st = sig1, 2nd = sig2.
    await withFetch(
      (_url, n) => jsonRes({ files: n === 1 ? [{ name: "a.wav" }] : [{ name: "b.wav" }] }),
      async (calls) => {
        const session = "swr-s";
        const pending = new Set();
        let landed = 0;
        const onLand = () => { landed++; };

        // Cold load: nothing last-good yet → null, and it fires exactly one fetch.
        assert.equal(loadSessionFiles(session, "sig1", pending, onLand), null);
        assert.equal(calls.length, 1);
        assert.equal(pending.size, 1); // one in-flight fetch guarded

        // A second tick before it lands must NOT fire a duplicate fetch (the
        // `pending` set dedups across the ticks until it settles).
        assert.equal(loadSessionFiles(session, "sig1", pending, onLand), null);
        assert.equal(calls.length, 1);

        await flush();
        assert.equal(landed, 1); // onLand ran when the fetch settled
        assert.equal(pending.size, 0); // guard cleared
        assert.deepEqual(sessionFiles.peek(session, "sig1"), [{ name: "a.wav" }]);

        // Same sig on the next tick → the resolved listing, in hand.
        assert.deepEqual(loadSessionFiles(session, "sig1", pending, onLand), [{ name: "a.wav" }]);

        // A sibling re-transcribes → files_sig FLIPS. The new listing is in
        // flight; the view must keep showing the last-good rows, not blank.
        const stale = loadSessionFiles(session, "sig2", pending, onLand);
        assert.deepEqual(stale, [{ name: "a.wav" }]); // last-good, NOT null
        assert.equal(pending.size, 1); // the new sig's fetch is now in flight
        assert.equal(calls.length, 2);

        // Once sig2 lands it reconciles in place to the fresh listing.
        await flush();
        assert.deepEqual(loadSessionFiles(session, "sig2", pending, onLand), [{ name: "b.wav" }]);
      },
    );
  });
});

describe("loadSessionFiles: empty guards + failure pacing", () => {
  it("returns [] without fetching for an empty session or empty files_sig", async () => {
    await withFetch(() => jsonRes({ files: [] }), async (calls) => {
      assert.deepEqual(loadSessionFiles("", "sig", new Set(), () => {}), []);
      assert.deepEqual(loadSessionFiles("empty-s", "", new Set(), () => {}), []);
      assert.equal(calls.length, 0); // neither guard touches the network
    });
  });

  it("paces a failed fetch: no throw, no onLand, the retry waits for a later call", async () => {
    // The retry-storm guard (`_failedFiles`): a rejection used to evict the
    // cache key AND fire onLand, whose re-render synchronously re-entered
    // loadSessionFiles and refired the fetch at HTTP-response rate. Now a
    // failure is remembered per key: onLand stays silent, the NEXT call (the
    // one the failure's own re-render would have been) skips the refetch,
    // and only a later call — the next poll tick — retries.
    await withFetch(
      (_url, n) => {
        if (n === 1) throw new Error("net down");
        return jsonRes({ files: [{ name: "ok.wav" }] });
      },
      async (calls) => {
        const pending = new Set();
        let landed = 0;
        const onLand = () => { landed++; };
        // A cold load whose fetch will reject — must not throw to the caller.
        assert.equal(loadSessionFiles("swallow-s", "sig1", pending, onLand), null);
        assert.equal(pending.size, 1); // one in-flight fetch guarded
        await flush();
        assert.equal(landed, 0); // a failure never fires onLand (nothing changed to render)
        assert.equal(pending.size, 0); // guard freed
        // Failure remembered: the immediate next call skips the refetch…
        assert.equal(loadSessionFiles("swallow-s", "sig1", pending, onLand), null);
        assert.equal(calls.length, 1); // no unpaced refire
        // …and the call after that (a later poll tick) retries for real.
        assert.equal(loadSessionFiles("swallow-s", "sig1", pending, onLand), null);
        assert.equal(calls.length, 2);
        await flush();
        assert.equal(landed, 1); // the successful retry lands via onLand
        assert.deepEqual(loadSessionFiles("swallow-s", "sig1", pending, onLand), [{ name: "ok.wav" }]);
      },
    );
  });
});

describe("_resource: bounded cache", () => {
  it("stays bounded and evicts oldest-first once the cache passes its cap", async () => {
    // The cache is bounded and evicts oldest-first (_capCache). Overshoot the
    // cap GENEROUSLY rather than pinning its exact value (_TX_CACHE_MAX): fetch
    // far more distinct keys than any plausible bound, then assert the earliest
    // keys were evicted while the latest survive. That proves the property
    // without false-failing if the cap is ever retuned. sessionSummary is
    // otherwise untouched here; foreign entries could only evict the oldest
    // keys SOONER, never resurrect them, so the assertions hold regardless.
    const N = 200;
    await withFetch(() => jsonRes({ text: "x" }), async () => {
      for (let i = 0; i < N; i++) {
        await sessionSummary.fetch("cap-s", `stamp${i}`);
      }
      assert.equal(sessionSummary.peek("cap-s", "stamp0"), undefined); // oldest evicted
      assert.equal(sessionSummary.peek("cap-s", "stamp1"), undefined);
      assert.deepEqual(sessionSummary.peek("cap-s", `stamp${N - 1}`), { text: "x" }); // newest kept
    });
  });

  it("keeps the session in USE last-good under pressure from other sessions", async () => {
    // The MRU eviction rule and the #266 blink behind it: see `_setMru`'s JSDoc
    // in api.js. This pins it end-to-end through `loadSessionFiles`.
    //
    // The filler sessions come in through the empty-files_sig branch: it records
    // a last-good WITHOUT touching the network or the resource cache, so this
    // exercises `_lastGoodFiles`' eviction order in isolation (a fetching filler
    // would evict the hot session's RESOURCE entry too, which is a different
    // cache and a different question). N overshoots any plausible cap rather
    // than pinning `_TX_CACHE_MAX`.
    const N = 200;
    await withFetch(
      (_url, n) => jsonRes({ files: n === 1 ? [{ name: "hot.wav" }] : [{ name: "hot2.wav" }] }),
      async (calls) => {
        const hot = "mru-hot";
        const pending = new Set();
        const onLand = () => {};

        assert.equal(loadSessionFiles(hot, "sig1", pending, onLand), null); // cold
        await flush();
        assert.deepEqual(loadSessionFiles(hot, "sig1", pending, onLand), [{ name: "hot.wav" }]);
        assert.equal(calls.length, 1);

        for (let i = 0; i < N; i++) {
          // Each "tick" re-records the focused session's listing…
          loadSessionFiles(hot, "sig1", pending, onLand);
          // …while another session (no WAVs yet → empty files_sig) is recorded
          // for the first time, pushing the cap.
          assert.deepEqual(loadSessionFiles(`mru-fill-${i}`, "", pending, onLand), []);
          // Read the focused session's hold WITHOUT recording it: one fixed,
          // never-settling probe sig, so every call after the first is deduped
          // by `pending` and just returns `_lastGoodFiles.get(hot) ?? null`.
          // (This loop is synchronous, so nothing settles inside it.) The hold
          // must survive every one of the N other sessions — the buggy
          // insertion-order refresh dropped it the moment the cap was passed.
          assert.deepEqual(
            loadSessionFiles(hot, "probe", pending, onLand),
            [{ name: "hot.wav" }],
            `the focused session's last-good was evicted after ${i + 1} other sessions`,
          );
        }
        assert.equal(calls.length, 2); // sig1 + the single probe; the fillers never hit the network

        // A sibling WAV finishes → the focused session's files_sig flips. Its
        // last-good must still be there to hold the list steady during the
        // refetch; with the eviction bug this came back null and blanked it.
        const stale = loadSessionFiles(hot, "sig2", pending, onLand);
        assert.deepEqual(stale, [{ name: "hot.wav" }]);
        await flush();
        assert.deepEqual(loadSessionFiles(hot, "sig2", pending, onLand), [{ name: "hot2.wav" }]);
      },
    );
  });
});
