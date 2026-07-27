// @ts-check
// gate-allow: signal-listener — the `addEventListener` below is the fake media element's own METHOD DEFINITION (it records handlers into a map so a test can fire them), not a listener being attached to anything. There is no event target and nothing to leak.
// Unit tests for the Player's policy (next/components/player.js).
// DOM-free: the media element is injected, so load / queued-seek / forget /
// error handling are testable without a document or an audio device.

import test from "node:test";
import assert from "node:assert/strict";

import { createPlayer, spanPlaybackStep } from "./player.js";

/** A stand-in for HTMLAudioElement, recording what the policy did to it. */
function fakeMedia() {
  /** @type {Record<string, ((e?: any) => void)[]>} */
  const listeners = {};
  return {
    src: "",
    currentTime: 0,
    duration: NaN,
    readyState: 0, // HAVE_NOTHING
    paused: true,
    playCalls: 0,
    loadCalls: 0,
    addEventListener(/** @type {string} */ type, /** @type {(e?: any) => void} */ fn) {
      (listeners[type] ||= []).push(fn);
    },
    play() {
      this.playCalls++;
      this.paused = false;
      return Promise.resolve();
    },
    pause() {
      this.paused = true;
    },
    load() {
      this.loadCalls++;
    },
    removeAttribute(/** @type {string} */ name) {
      if (name === "src") this.src = "";
    },
    /** Fire a media event, the way the browser would — including the state the
     * event implies. A real `play` event never arrives while `paused` is still
     * true, and the Player reads `paused` to decide whether to arm its loop, so
     * a fake that only dispatched the event would misreport that path. */
    emit(/** @type {string} */ type) {
      if (type === "play") this.paused = false;
      if (type === "pause" || type === "ended") this.paused = true;
      for (const fn of listeners[type] || []) fn({ type });
    },
    /** Metadata arriving: readyState advances and duration becomes known. */
    metadataArrives(/** @type {number} */ duration) {
      this.readyState = 1; // HAVE_METADATA
      this.duration = duration;
      this.emit("loadedmetadata");
    },
  };
}

test("load points the element at the WAV route and plays it", () => {
  const media = fakeMedia();
  const player = createPlayer({ media });

  player.load({ session: "s1", name: "a.wav", source: "original" });

  assert.match(media.src, /\/api\/wav\/s1\/a\.wav\?source=original$/);
  assert.equal(media.playCalls, 1);
});

test("a seek arriving before metadata is QUEUED and applied on loadedmetadata", () => {
  // The trap: assigning currentTime at readyState HAVE_NOTHING is unreliable,
  // so a cold-cache click would silently play from 0:00 while a warm-cache one
  // worked. ADR-0017 / issue #191 pin 3.
  const media = fakeMedia();
  const player = createPlayer({ media });

  player.load({ session: "s1", name: "a.wav", source: "original", offsetS: 12 });
  assert.equal(media.currentTime, 0, "not written before metadata exists");

  media.metadataArrives(60);

  assert.equal(media.currentTime, 12, "applied once the element can accept it");
});

test("a seek arriving after metadata is applied immediately", () => {
  const media = fakeMedia();
  const player = createPlayer({ media });
  player.load({ session: "s1", name: "a.wav", source: "original" });
  media.metadataArrives(60);

  player.seek(30);

  assert.equal(media.currentTime, 30);
});

test("forget unloads the file it names, and stops the audio", () => {
  // Explicit eviction exists because the browser has the bytes BUFFERED: after
  // a delete, playback of a removed recording otherwise continues to the end
  // with no error at all. "I deleted it and it kept talking." ADR-0017.
  const media = fakeMedia();
  /** @type {string[]} */
  const reasons = [];
  const player = createPlayer({ media, onChange: (_l, reason) => reason && reasons.push(reason) });
  player.load({ session: "s1", name: "a.wav", source: "original" });

  player.forget({ session: "s1", name: "a.wav", source: "original" });

  assert.equal(player.loaded(), null);
  assert.equal(media.src, "", "src detached, not left pointing at a deleted file");
  assert.equal(media.paused, true);
  assert.deepEqual(reasons, ["deleted"]);
});

test("forget ignores a file the Player isn't holding", () => {
  const media = fakeMedia();
  const player = createPlayer({ media });
  player.load({ session: "s1", name: "a.wav", source: "original" });

  player.forget({ session: "s1", name: "OTHER.wav", source: "original" });
  player.forget({ session: "s2", name: "a.wav", source: "original" });
  player.forget({ session: "s1", name: "a.wav", source: "stripped" });

  assert.deepEqual(player.loaded(), { session: "s1", name: "a.wav", source: "original" });
  assert.equal(media.paused, false, "an unrelated delete must not stop playback");
});

test("a media error unloads with a stated reason", () => {
  // The backstop for changes the dashboard didn't initiate. It pays for itself
  // on the truncated / zero-byte WAVs this system leaves on disk when a tap
  // crashes (`select_session_wavs` buckets those as skipped_bad).
  const media = fakeMedia();
  /** @type {string[]} */
  const reasons = [];
  const player = createPlayer({ media, onChange: (_l, reason) => reason && reasons.push(reason) });
  player.load({ session: "s1", name: "truncated.wav", source: "original" });

  media.emit("error");

  assert.equal(player.loaded(), null);
  assert.deepEqual(reasons, ["unreadable"]);
});

test("a non-finite seek is ignored rather than corrupting currentTime", () => {
  const media = fakeMedia();
  const player = createPlayer({ media });
  player.load({ session: "s1", name: "a.wav", source: "original" });
  media.metadataArrives(60);

  player.seek(NaN);

  assert.equal(media.currentTime, 0);
});

test("forgetWhere unloads on a predicate, for the bulk verbs", () => {
  // `clear stripped` and delete-session-audio remove MANY files at once, so the
  // caller expresses what went rather than naming one file.
  const media = fakeMedia();
  const player = createPlayer({ media });
  player.load({ session: "s1", name: "clip.wav", source: "stripped" });

  player.forgetWhere((f) => f.session === "s1" && f.source === "stripped");

  assert.equal(player.loaded(), null);
});

test("forgetWhere leaves a file the predicate doesn't match", () => {
  const media = fakeMedia();
  const player = createPlayer({ media });
  player.load({ session: "s1", name: "a.wav", source: "original" });

  player.forgetWhere((f) => f.session === "s1" && f.source === "stripped");

  assert.deepEqual(player.loaded(), { session: "s1", name: "a.wav", source: "original" });
});

/** A hand-driven requestAnimationFrame: `flush()` runs one frame.
 * `caf` really DEQUEUES, like the platform's — a fake that merely counted
 * cancellations would report a cancelled loop as still pending. */
function fakeRaf() {
  /** @type {Map<number, () => void>} */
  let queue = new Map();
  let nextHandle = 1;
  return {
    raf: (/** @type {() => void} */ fn) => {
      const h = nextHandle++;
      queue.set(h, fn);
      return h;
    },
    caf: (/** @type {number} */ h) => { queue.delete(h); },
    flush() {
      const q = [...queue.values()];
      queue = new Map();
      for (const fn of q) fn();
    },
    pending: () => queue.size,
  };
}

test("onTick reports position per frame while playing, and stops on pause", () => {
  // The playhead needs ~60fps position, which `timeupdate` (~4/s) can't give.
  // The loop belongs to the Player because it owns the element and its events —
  // and it must NOT keep running once playback stops (a permanent rAF loop on an
  // idle dashboard is exactly the kind of churn the poll pacer backs off from).
  const media = fakeMedia();
  const clock = fakeRaf();
  const player = createPlayer({ media, raf: clock.raf, caf: clock.caf });
  /** @type {number[]} */
  const seen = [];
  player.onTick((_loaded, t) => seen.push(t));

  player.load({ session: "s1", name: "a.wav", source: "original" });
  media.metadataArrives(60);
  media.emit("play");
  media.currentTime = 4;
  clock.flush();

  assert.deepEqual(seen.slice(-1), [4]);

  media.emit("pause");
  const pendingAfterPause = clock.pending();
  clock.flush();
  assert.equal(pendingAfterPause, 0, "the loop must not re-arm after a pause");
});

test("onTick reports the file identity, so a listener can refuse a mismatch", () => {
  // The strict-identity playhead rule: the waveform draws a position only while
  // the Player holds the file the canvas is showing.
  const media = fakeMedia();
  const clock = fakeRaf();
  const player = createPlayer({ media, raf: clock.raf, caf: clock.caf });
  /** @type {any[]} */
  const seen = [];
  player.onTick((loaded) => seen.push(loaded));

  player.load({ session: "s1", name: "b.wav", source: "stripped" });
  media.metadataArrives(10);
  media.emit("play");
  clock.flush();

  assert.deepEqual(seen.at(-1), { session: "s1", name: "b.wav", source: "stripped" });
});

test("onTick fires once on unload so a stale playhead is cleared", () => {
  const media = fakeMedia();
  const clock = fakeRaf();
  const player = createPlayer({ media, raf: clock.raf, caf: clock.caf });
  /** @type {any[]} */
  const seen = [];
  player.onTick((loaded) => seen.push(loaded));
  player.load({ session: "s1", name: "a.wav", source: "original" });

  player.forget({ session: "s1", name: "a.wav", source: "original" });

  assert.equal(seen.at(-1), null, "null position means: erase the playhead");
});

test("report surfaces a message without touching what's loaded", () => {
  // A dead seek target has to be VISIBLE. It is not an unload: whatever is
  // playing keeps playing.
  const media = fakeMedia();
  /** @type {(string | undefined)[]} */
  const reasons = [];
  const player = createPlayer({ media, onChange: (_l, reason) => reasons.push(reason) });
  player.load({ session: "s1", name: "a.wav", source: "original" });

  player.report("that recording is no longer on disk");

  assert.deepEqual(player.loaded(), { session: "s1", name: "a.wav", source: "original" });
  assert.equal(media.paused, false, "reporting is not unloading");
  assert.equal(reasons.at(-1), "that recording is no longer on disk");
});

test("a rejected play() is handled, not left to the crash bar", async () => {
  // `wireErrorBar` beacons every unhandled rejection to /api/client-errors and
  // shows a red bar. A 404 or an undecodable file rejects play() with
  // NotSupportedError, and the `error` event already owns that UX — so an
  // unhandled rejection would be a SECOND, uglier report of the same thing.
  // node --test fails this test if the rejection escapes.
  const media = fakeMedia();
  media.play = () => Promise.reject(new Error("NotSupportedError"));
  const player = createPlayer({ media });

  player.load({ session: "s1", name: "bad.wav", source: "original" });
  await new Promise((r) => setTimeout(r, 0));

  assert.deepEqual(player.loaded(), { session: "s1", name: "bad.wav", source: "original" });
});

test("onTick returns an unsubscribe, so a stale subscriber can drop itself", () => {
  // main.js clears the view cache once the model catalogs land, which can rebuild
  // a view that had already subscribed. Without a way off the list, the old
  // (detached) view's callback runs every frame forever.
  const media = fakeMedia();
  const clock = fakeRaf();
  const player = createPlayer({ media, raf: clock.raf, caf: clock.caf });
  let calls = 0;
  const off = player.onTick(() => { calls++; });

  player.load({ session: "s1", name: "a.wav", source: "original" });
  media.metadataArrives(60);
  media.emit("play");
  clock.flush();
  const afterFirst = calls;
  assert.ok(afterFirst > 0);

  off();
  clock.flush();

  assert.equal(calls, afterFirst, "an unsubscribed ticker must stop being called");
});

test("the frame loop does not run when nothing is subscribed", () => {
  // The flagship #191 flow is "click a transcript timestamp and listen", where
  // the Recordings view (the only ticker) is often never built. Arming a
  // per-vsync loop for a 45-minute listen to call an empty subscriber list is
  // ~160k no-op wake-ups.
  const media = fakeMedia();
  const clock = fakeRaf();
  createPlayer({ media, raf: clock.raf, caf: clock.caf });

  media.emit("play");

  assert.equal(clock.pending(), 0, "no subscribers => no loop");
});

test("subscribing while already playing starts the loop", () => {
  // Corollary of the above: if the loop is suppressed at `play` time because
  // nobody was listening, a later subscriber must still get frames.
  const media = fakeMedia();
  const clock = fakeRaf();
  const player = createPlayer({ media, raf: clock.raf, caf: clock.caf });
  media.emit("play"); // no tickers yet -> no loop

  let calls = 0;
  player.onTick(() => { calls++; });
  clock.flush();

  assert.ok(calls > 0, "a ticker added mid-playback still gets frames");
});

test("the loop stops when the last subscriber leaves", () => {
  const media = fakeMedia();
  const clock = fakeRaf();
  const player = createPlayer({ media, raf: clock.raf, caf: clock.caf });
  const off = player.onTick(() => {});
  media.emit("play");
  clock.flush();
  assert.ok(clock.pending() > 0);

  off();

  assert.equal(clock.pending(), 0, "nothing left to update => stop the loop");
});

// ── Kept-audio playback over cut spans (#191) ──────────────────────────────

test("spanPlaybackStep: inside a kept span, keep playing", () => {
  const spans = [{ start_s: 1, end_s: 3 }, { start_s: 6, end_s: 8 }];
  assert.equal(spanPlaybackStep(spans, 1), null);
  assert.equal(spanPlaybackStep(spans, 2), null);
  assert.equal(spanPlaybackStep(spans, 6.5), null);
});

test("spanPlaybackStep: in a dropped gap, skip to the next kept span", () => {
  const spans = [{ start_s: 1, end_s: 3 }, { start_s: 6, end_s: 8 }];
  // This IS the feature: the silence between kept regions is never heard.
  assert.deepEqual(spanPlaybackStep(spans, 3.01), { seekTo: 6 });
  assert.deepEqual(spanPlaybackStep(spans, 5.9), { seekTo: 6 });
  // Before the first span — the lead-in silence is dropped too.
  assert.deepEqual(spanPlaybackStep(spans, 0), { seekTo: 1 });
});

test("spanPlaybackStep: past the last kept span, stop", () => {
  const spans = [{ start_s: 1, end_s: 3 }, { start_s: 6, end_s: 8 }];
  assert.deepEqual(spanPlaybackStep(spans, 8.01), { stop: true });
  assert.deepEqual(spanPlaybackStep(spans, 99), { stop: true });
});

test("spanPlaybackStep: nothing kept means nothing to play", () => {
  assert.deepEqual(spanPlaybackStep([], 0), { stop: true });
});

test("playing kept audio hops the gaps and stops at the end", () => {
  const media = fakeMedia();
  const clock = fakeRaf();
  const player = createPlayer({ media, raf: clock.raf, caf: clock.caf });

  player.load({
    session: "s1", name: "a.wav", source: "original",
    keptSpans: [{ start_s: 1, end_s: 3 }, { start_s: 6, end_s: 8 }],
  });
  media.metadataArrives(10);

  assert.equal(media.currentTime, 1, "starts at the first kept span, not 0:00");

  // Play to the end of span 1; the next frame must jump the silence.
  media.currentTime = 3.02;
  clock.flush();
  assert.equal(media.currentTime, 6, "skipped the dropped gap");

  // Run past the last span; the next frame stops.
  media.currentTime = 8.05;
  clock.flush();
  assert.equal(media.paused, true, "stopped after the last kept span");
});

test("kept-audio playback drives the frame loop with no ticker subscribed", () => {
  // The loop is otherwise gated on having subscribers; span-hopping is the
  // Player's OWN consumer of it, and works from any view.
  const media = fakeMedia();
  const clock = fakeRaf();
  const player = createPlayer({ media, raf: clock.raf, caf: clock.caf });

  player.load({
    session: "s1", name: "a.wav", source: "original",
    keptSpans: [{ start_s: 1, end_s: 3 }],
  });
  media.metadataArrives(10);

  assert.ok(clock.pending() > 0, "the hop loop must run even with no tickers");
});

test("a plain load clears any previous span playback", () => {
  const media = fakeMedia();
  const clock = fakeRaf();
  const player = createPlayer({ media, raf: clock.raf, caf: clock.caf });
  player.load({
    session: "s1", name: "a.wav", source: "original",
    keptSpans: [{ start_s: 1, end_s: 3 }],
  });
  media.metadataArrives(10);

  player.load({ session: "s1", name: "a.wav", source: "original" });
  media.metadataArrives(10);
  media.currentTime = 4;
  clock.flush();

  assert.equal(media.currentTime, 4, "no hopping once spans are cleared");
  assert.equal(media.paused, false);
});

test("a seek event reports a NUMBER, not the DOM event", () => {
  // `addEventListener` hands the listener an Event; a listener whose first
  // parameter is "the known position" must not be wired to it directly, or every
  // subscriber gets an Event where it expects seconds — and a playhead computing
  // Number.isFinite(Event) silently hides itself.
  const media = fakeMedia();
  const clock = fakeRaf();
  const player = createPlayer({ media, raf: clock.raf, caf: clock.caf });
  /** @type {any[]} */
  const seen = [];
  player.onTick((_l, at) => seen.push(at));
  player.load({ session: "s1", name: "a.wav", source: "original" });
  media.metadataArrives(60);

  media.currentTime = 12;
  media.emit("seeked");

  assert.equal(typeof seen.at(-1), "number", `got ${typeof seen.at(-1)}`);
  assert.equal(seen.at(-1), 12);
});

test("unsubscribing from INSIDE a tick actually stops the loop", () => {
  // The real unsubscribe path (a detached view retiring its ticker) runs inside
  // emitTick. If step() re-arms unconditionally afterwards, stopLoop is undone
  // and the loop spins forever on an empty subscriber list.
  const media = fakeMedia();
  const clock = fakeRaf();
  const player = createPlayer({ media, raf: clock.raf, caf: clock.caf });
  /** @type {() => void} */
  let off = () => {};
  off = player.onTick(() => { off(); });

  player.load({ session: "s1", name: "a.wav", source: "original" });
  media.metadataArrives(60);
  media.emit("play");
  clock.flush(); // the tick unsubscribes itself

  assert.equal(clock.pending(), 0, "the loop must not re-arm after an in-tick stop");
});

test("reaching the end of the kept spans disarms them", () => {
  // Otherwise the transport stays armed: native play re-arms the loop, frame one
  // sees `t` past the last span, and it pauses again — a dead play button.
  const media = fakeMedia();
  const clock = fakeRaf();
  const player = createPlayer({ media, raf: clock.raf, caf: clock.caf });
  player.load({
    session: "s1", name: "a.wav", source: "original",
    keptSpans: [{ start_s: 1, end_s: 3 }],
  });
  media.metadataArrives(10);

  media.currentTime = 3.1;
  clock.flush(); // runs past the last span -> stop

  // The operator presses play again and scrubs nowhere: playback must work.
  media.emit("play");
  media.currentTime = 5;
  clock.flush();

  assert.equal(media.paused, false, "native play must work after kept playback ends");
  assert.equal(media.currentTime, 5, "no stale hopping once the spans are done");
});

test("re-loading the file already loaded seeks instead of re-fetching", () => {
  // Setting src restarts the media load algorithm and discards the buffer. The
  // flagship loop is "click a line, listen, click the next line" on ONE WAV.
  const media = fakeMedia();
  const player = createPlayer({ media });
  player.load({ session: "s1", name: "a.wav", source: "original", offsetS: 5 });
  media.metadataArrives(60);
  const loadsAfterFirst = media.loadCalls;
  const srcAfterFirst = media.src;

  player.load({ session: "s1", name: "a.wav", source: "original", offsetS: 30 });

  assert.equal(media.currentTime, 30, "seeks to the new offset");
  assert.equal(media.src, srcAfterFirst, "same src, untouched");
  assert.equal(media.loadCalls, loadsAfterFirst, "no reload of the same file");
});

test("setKeptSpans re-aims live kept playback at a changed cut", () => {
  // The documented loop is "listen to the preview, drag a knob, listen again".
  // Without this the audio keeps hopping the OLD cut's gaps while the canvas
  // shows the new one, so the operator isn't hearing what they're judging.
  const media = fakeMedia();
  const clock = fakeRaf();
  const player = createPlayer({ media, raf: clock.raf, caf: clock.caf });
  player.load({
    session: "s1", name: "a.wav", source: "original",
    keptSpans: [{ start_s: 1, end_s: 3 }],
  });
  media.metadataArrives(20);

  player.setKeptSpans([{ start_s: 1, end_s: 9 }]);
  media.currentTime = 4; // inside the NEW span, past the old one's end
  clock.flush();

  assert.equal(media.currentTime, 4, "the new cut keeps this position");
  assert.equal(media.paused, false);
});

test("setKeptSpans is inert when kept playback isn't armed", () => {
  // A knob drag while playing a whole file plainly must not start hopping.
  const media = fakeMedia();
  const clock = fakeRaf();
  const player = createPlayer({ media, raf: clock.raf, caf: clock.caf });
  player.load({ session: "s1", name: "a.wav", source: "original" });
  media.metadataArrives(20);

  player.setKeptSpans([{ start_s: 5, end_s: 6 }]);
  media.emit("play");
  media.currentTime = 1;
  clock.flush();

  assert.equal(media.currentTime, 1, "no hopping was requested");
});
