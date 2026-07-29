// Tests for page-script.js — the MAIN-world script that taps LiveKit
// audio tracks and posts PCM frames + lifecycle events to content.js.
//
// Why a separate file from wire-contract.test.js: that file drives
// content.js via synthesised `tap-start` / `pcm` postMessages, so any
// regression that happens *upstream* in page-script.js (where those
// messages are produced) is invisible to it. The #31 refactor that
// dropped `const name = getDisplayName(participant)` slipped past 25
// passing tests for exactly this reason. These cases load page-script.js
// into a vm with mocked LiveKit + Web Audio + Vue-sidebar surfaces and
// pin the wire-contract messages it emits.
//
// What we pin here:
//   - tap-start / pcm carry the resolved display name (NOT window.name)
//     for remote AND local participants
//   - room attach iterates existing publications and tap()s them
//   - participants that joined muted are announced with the mute flag
//   - trackUnsubscribed / participantDisconnected post tap-stop
//   - room "disconnected" tears down EVERY announced participant
//     (audio-tapped and presence-only) — the #34 cleanupAllTaps contract
//   - tap-setup failures (CSP / addModule reject / etc.) surface as
//     ctx-state="failed" so the popup doesn't sit silent

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const PAGE_SCRIPT = path.join(__dirname, "..", "page-script.js");

// Minimal stand-in for an EventEmitter-style LiveKit Room. We capture
// `.on(event, fn)` registrations so the test can fire `trackSubscribed`
// later with a fake participant and audio publication.
function makeRoom() {
  const handlers = new Map();
  return {
    state: "connected",
    remoteParticipants: new Map(),
    localParticipant: null,
    on(ev, fn) {
      if (!handlers.has(ev)) handlers.set(ev, []);
      handlers.get(ev).push(fn);
    },
    fire(ev, ...args) {
      for (const fn of handlers.get(ev) || []) fn(...args);
    },
  };
}

function makeAudioMocks({ failWorklet = false, ctxState = "running" } = {}) {
  // Every MediaStream the script wraps, in order — lets a test prove WHICH
  // MediaStreamTrack the source node is bound to (the device-switch case
  // swaps the track in place, so identity is the only observable).
  const wrappedStreams = [];
  const resumeCalls = [];
  let resumeRejects = false;
  // ensureAudioGraph awaits audioWorklet.addModule(...) before returning,
  // so the resolved Promise has to actually settle. The rest is just
  // duck-typing of the AudioContext / WorkletNode surface tap() touches.
  const ctx = {
    state: ctxState,
    sampleRate: 48000,
    destination: { _isDestination: true },
    createMediaStreamSource: (stream) => {
      wrappedStreams.push(stream);
      return { connect: (next) => next, disconnect: () => {} };
    },
    createGain: () => ({
      gain: { value: 0 },
      connect: (next) => next,
      disconnect: () => {},
    }),
    audioWorklet: { addModule: () => Promise.resolve() },
    addEventListener: () => {},
    resume: () => {
      resumeCalls.push(ctx.state);
      return resumeRejects
        ? Promise.reject(new Error("autoplay policy: resume() requires a user gesture"))
        : Promise.resolve();
    },
  };
  function AudioContext() { return ctx; }
  function AudioWorkletNode() {
    if (failWorklet) {
      // Mirrors the real browser raising when the worklet name isn't
      // registered (addModule was rejected, e.g. by CSP).
      throw new Error("AudioWorkletNode: unknown processor");
    }
    const node = {
      port: { onmessage: null, postMessage: () => {} },
      connect: (next) => next,
      disconnect: () => {},
    };
    // Expose the latest-constructed worklet so the test can fire a
    // simulated PCM frame through its `port.onmessage` handler.
    AudioWorkletNode._last = node;
    return node;
  }
  function MediaStream(tracks) { return { _isStream: true, tracks }; }
  return {
    AudioContext,
    AudioWorkletNode,
    MediaStream,
    ctx,
    // The MediaStreamTrack behind each source node the script created, in order.
    wrappedTracks: () => wrappedStreams.map((s) => (s.tracks || [])[0]),
    resumeCalls,
    setResumeRejects: (v) => { resumeRejects = !!v; },
  };
}

function loadPageScript({
  withSidebarNameFor = null,
  remoteParticipants = [],
  localParticipant = null,
  failWorklet = false,
  startWithRoom = true,
  ctxState = "running",
} = {}) {
  const audio = makeAudioMocks({ failWorklet, ctxState });
  const room = makeRoom();
  // Seed any pre-existing participants the test wants attached at
  // load time (covers the "tap was running before the extension
  // injected" path that attachListeners() iterates).
  for (const p of remoteParticipants) room.remoteParticipants.set(p.identity, p);
  if (localParticipant) room.localParticipant = localParticipant;
  const posted = [];
  const blobParts = [];
  const eventListeners = {};

  // Document mock: querySelectorAll returns sidebar elements when the
  // test wants getDisplayName() to resolve a name via Vue props (the
  // happy path in production); otherwise empty array forces the
  // `participant.name || ""` fallback inside getDisplayName().
  const sidebarEls = [];
  if (withSidebarNameFor) {
    sidebarEls.push({
      __vue__: {
        _props: { user: { id: withSidebarNameFor.id, name: withSidebarNameFor.name } },
      },
    });
  }
  let sidebarScanCount = 0;
  let nowMs = 0;
  const doc = {
    head: { appendChild: () => {} },
    addEventListener: () => {},
    querySelectorAll: () => { sidebarScanCount++; return sidebarEls; },
    visibilityState: "visible",
  };

  const win = {
    __tapscribeBridgePageInstalled: undefined,
    addEventListener: (ev, fn) => {
      if (!eventListeners[ev]) eventListeners[ev] = [];
      eventListeners[ev].push(fn);
    },
    // A REAL removal, not a no-op: the gesture-retry tests assert on whether
    // the one-shot listeners are still attached after a failed resume(), so a
    // stubbed-out remove would make them pass vacuously.
    removeEventListener: (ev, fn) => {
      const list = eventListeners[ev];
      if (!list) return;
      const i = list.indexOf(fn);
      if (i >= 0) list.splice(i, 1);
    },
    postMessage: (data) => posted.push(data),
    // `name` is the global the regression accidentally read instead of
    // the participant's display name. Setting it to something distinctive
    // makes the bug visible: if the script leaks through to window.name,
    // tap-start carries this token instead of "Alice".
    name: "WINDOW_NAME_DO_NOT_LEAK",
    // page-script polls `window.room`; surfacing the room here makes the
    // initial maybeAttach() call attach our test handlers. Tests that
    // need to delay attachment (e.g. "fresh room arrives later") can
    // pass startWithRoom:false and set sandbox.window.room themselves.
    room: startWithRoom ? room : null,
  };

  const sandbox = {
    window: win,
    document: doc,
    location: { href: "https://app.spatial.chat/test", protocol: "https:" },
    // Browsers define a built-in `name` global (== window.name, an
    // empty string by default). The regression we're guarding against
    // is a free `name` reference inside tap() — in a Node vm context
    // that throws ReferenceError, but in a browser it silently picks
    // up window.name. Mirror the browser behavior so the test fails
    // with a leaked sentinel instead of a thrown error.
    name: "WINDOW_NAME_DO_NOT_LEAK",
    AudioContext: audio.AudioContext,
    AudioWorkletNode: audio.AudioWorkletNode,
    MediaStream: audio.MediaStream,
    URL: { createObjectURL: () => "blob:fake", revokeObjectURL: () => {} },
    // Capture the parts rather than discarding them: the AudioWorklet source
    // is built as a template string and handed to `new Blob([src], …)`, so
    // this is the ONLY place a test can observe what the worklet will
    // actually run — see "the worklet emits …-sample frames" below.
    Blob: function Blob(parts = []) { blobParts.push(parts.join("")); return {}; },
    crypto: { randomUUID: () => "u-" + Math.random().toString(36).slice(2) },
    console: { log: () => {}, warn: () => {}, error: () => {} },
    // Capture the 250ms room-poll callback (don't auto-run it) so a test
    // can drive a deterministic room swap via sandbox.__pollFn().
    setInterval: (fn) => { sandbox.__pollFn = fn; return 0; },
    clearInterval: () => {},
    setTimeout: (fn, _ms) => { fn(); return 0; },
    clearTimeout: () => {},
    // Virtual monotonic clock for the display-name retry throttle; tests
    // advance it via env.setNow(ms).
    performance: { now: () => nowMs },
  };
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(PAGE_SCRIPT, "utf8"), sandbox, {
    filename: "page-script.js",
  });

  return {
    room,
    audio,
    posted,
    eventListeners,
    sandbox,
    sidebarEls,
    workletSource: () => blobParts.join(""),
    setNow: (ms) => { nowMs = ms; },
    sidebarScans: () => sidebarScanCount,
  };
}

function makeAudioPublication({ track, isMuted = false, source = "microphone" }) {
  // Pub structure mirrors LiveKit: pub.track is the Track wrapper;
  // pub.track.mediaStreamTrack is the browser MediaStreamTrack. The
  // page-script reaches through `pub.track.mediaStreamTrack` for the
  // local-mic path and through `track.mediaStreamTrack` (the first
  // arg of trackSubscribed) for the remote path. Both use the same
  // Track object so both paths end up with the same MST.
  return {
    kind: "audio",
    source,
    isMuted,
    isSubscribed: !!track,
    track: track || null,
  };
}

// A MediaStreamTrack stand-in with a real listener registry, so a test can
// fire the browser's `ended` event (mic unplugged, device switched away,
// permission revoked) — the signal the bridge had no listener for.
function makeMst() {
  const listeners = {};
  return {
    readyState: "live",
    kind: "audio",
    addEventListener(ev, fn) {
      (listeners[ev] = listeners[ev] || []).push(fn);
    },
    removeEventListener(ev, fn) {
      const list = listeners[ev];
      if (!list) return;
      const i = list.indexOf(fn);
      if (i >= 0) list.splice(i, 1);
    },
    listenerCount: (ev) => (listeners[ev] || []).length,
    fireEnded() {
      this.readyState = "ended";
      for (const fn of (listeners.ended || []).slice()) fn({ type: "ended" });
    },
  };
}

function makeTrack() {
  // LiveKit exposes the underlying MediaStreamTrack on Track via
  // `.mediaStreamTrack`; the bridge passes that into tap(). The Track
  // object itself only carries `kind` in the path we exercise.
  return {
    kind: "audio",
    mediaStreamTrack: makeMst(),
  };
}

function makeParticipant({ identity, name = "", pubs = [] }) {
  return {
    identity,
    name,
    audioTrackPublications: new Map(pubs.map((p, i) => [String(i), p])),
  };
}

function findMessages(posted, kind, identity) {
  return posted.filter(
    (m) => m && m.kind === kind && (!identity || m.identity === identity),
  );
}

// Flush the chain of microtasks tap() goes through (ensureAudioGraph
// awaits audioWorklet.addModule). Two ticks is plenty for the
// Promise.resolve() chain we set up in makeAudioMocks().
async function flush() {
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));
}

test("tap-start carries the resolved display name from the LiveKit participant", async () => {
  // Regression: the refactor in #31 dropped `const name = getDisplayName(participant);`
  // from tap(). The function then read `name` from the global scope —
  // which in a browser is `window.name`, almost always "" — so the
  // first frame's speaker label was always blank.
  const env = loadPageScript();
  const track = makeTrack();
  const participant = {
    identity: "alice-id",
    // page-script's getDisplayName() falls back to `participant.name`
    // when the sidebar DOM hasn't rendered yet — this is the path that
    // exercises the local-binding regression.
    name: "Alice",
    audioTrackPublications: new Map(),
  };

  env.room.fire(
    "trackSubscribed",
    track,
    makeAudioPublication({ track }),
    participant,
  );
  await flush();

  const tapStart = env.posted.find(
    (m) => m && m.kind === "tap-start" && m.identity === "alice-id",
  );
  assert.ok(tapStart, "tap-start posted for subscribed participant");
  assert.equal(
    tapStart.name, "Alice",
    "tap-start carries the participant's display name, not '' or window.name",
  );
  assert.notEqual(
    tapStart.name, "WINDOW_NAME_DO_NOT_LEAK",
    "name MUST be a local binding — never window.name",
  );
});

test("tap-start picks up the sidebar Vue name when participant.name is empty", async () => {
  // Production case: LiveKit's Participant.name is "" and the real
  // display name lives on the Vue sidebar element's _props.user.name.
  // The local `const name = getDisplayName(participant)` is the only
  // place that ever resolves it for tap-start; the worklet message
  // handler re-resolves later for PCM but that's too late for the
  // initial /tap URL.
  const env = loadPageScript({
    withSidebarNameFor: { id: "bob-id", name: "Bob from Sidebar" },
  });
  const track = makeTrack();
  const participant = {
    identity: "bob-id",
    name: "",
    audioTrackPublications: new Map(),
  };

  env.room.fire(
    "trackSubscribed",
    track,
    makeAudioPublication({ track }),
    participant,
  );
  await flush();

  const tapStart = env.posted.find(
    (m) => m && m.kind === "tap-start" && m.identity === "bob-id",
  );
  assert.ok(tapStart, "tap-start posted");
  assert.equal(
    tapStart.name, "Bob from Sidebar",
    "tap-start carries the name resolved via the SpatialChat Vue sidebar",
  );
});

test("PCM frames forwarded from the worklet include the resolved name", async () => {
  // The first PCM frame is what drives the /tap WS open in content.js.
  // Even if tap-start carried the right name, a PCM frame with name=""
  // would have content.js dial /tap with an empty `name` query param.
  const env = loadPageScript();
  const track = makeTrack();
  const participant = {
    identity: "carol-id",
    name: "Carol",
    audioTrackPublications: new Map(),
  };

  env.room.fire(
    "trackSubscribed",
    track,
    makeAudioPublication({ track }),
    participant,
  );
  await flush();

  const worklet = env.audio.AudioWorkletNode._last;
  assert.ok(worklet, "tap() constructed an AudioWorkletNode");
  assert.ok(
    typeof worklet.port.onmessage === "function",
    "tap() installed the worklet message handler",
  );

  // Simulate the worklet emitting one 320-sample (640-byte) PCM frame.
  const pcm = new Int16Array(320);
  worklet.port.onmessage({ data: pcm });

  const pcmMsg = env.posted.find(
    (m) => m && m.kind === "pcm" && m.identity === "carol-id",
  );
  assert.ok(pcmMsg, "pcm posted from worklet handler");
  assert.equal(
    pcmMsg.name, "Carol",
    "pcm carries the participant's name so /tap is dialed with name=Carol",
  );
});

test("an unresolved display name is retried at most ~1x/s, not per 20 ms PCM frame", async () => {
  // getDisplayName runs an O(document) sidebar querySelectorAll. The worklet
  // message handler fires per 20 ms PCM chunk (50x/s), so for a participant
  // whose name never resolves an unthrottled retry would scan the MAIN-world
  // DOM 50x/s for the tap's lifetime. Pin the per-entry throttle: one second
  // of frames triggers at most a couple of scans — and the retry still
  // resolves the name once the sidebar renders the user.
  const env = loadPageScript(); // no sidebar entry, participant.name is ""
  const track = makeTrack();
  const participant = { identity: "nameless", name: "", audioTrackPublications: new Map() };

  env.room.fire("trackSubscribed", track, makeAudioPublication({ track }), participant);
  await flush();
  const worklet = env.audio.AudioWorkletNode._last;

  const before = env.sidebarScans();
  for (let i = 0; i < 50; i++) {
    env.setNow(i * 20); // one second of 20 ms PCM chunks
    worklet.port.onmessage({ data: new Int16Array(320) });
  }
  const scans = env.sidebarScans() - before;
  assert.ok(
    scans <= 2,
    "50 frames across 1 s must trigger at most 2 sidebar scans, got " + scans,
  );
  assert.equal(
    findMessages(env.posted, "pcm", "nameless").length, 50,
    "throttling the name retry must not throttle the PCM frames themselves",
  );

  // The sidebar finally renders the user; the next due retry picks it up.
  env.sidebarEls.push({
    __vue__: { _props: { user: { id: "nameless", name: "Now Named" } } },
  });
  env.setNow(2000); // past the throttle window
  worklet.port.onmessage({ data: new Int16Array(320) });
  const last = findMessages(env.posted, "pcm", "nameless").pop();
  assert.equal(last.name, "Now Named", "a due retry still resolves the name");
});

test("local participant's mic is tapped and named the same as remote taps", async () => {
  // SpatialChat's local mic publishes via room.localParticipant; we
  // forward it to /tap so the operator's own voice lands in the
  // transcript alongside everyone else's. A regression here would
  // silently lose half the conversation.
  const env = loadPageScript();
  const track = makeTrack();
  const local = makeParticipant({ identity: "me", name: "Operator" });
  const pub = makeAudioPublication({ track });

  env.room.fire("localTrackPublished", pub, local);
  await flush();

  const tapStart = findMessages(env.posted, "tap-start", "me")[0];
  assert.ok(tapStart, "local mic publication produces tap-start");
  assert.equal(tapStart.name, "Operator", "local mic carries the operator's name");
  assert.notEqual(
    tapStart.name, "WINDOW_NAME_DO_NOT_LEAK",
    "local-tap path must also resolve name locally, not via window.name",
  );
});

test("muted-on-join participant is announced as a presence-only channel", async () => {
  // The "muted on entry" UX (PR #9): late joiners that come in muted
  // need to appear in the popup as "muted" instead of being silently
  // ignored until they unmute. announcePresence posts tap-start + a
  // mute message so the popup row shows up immediately.
  const env = loadPageScript();
  const pub = makeAudioPublication({ track: null, isMuted: true });
  const participant = makeParticipant({
    identity: "late-joiner",
    name: "Dana",
    pubs: [pub],
  });

  env.room.fire("participantConnected", participant);
  await flush();

  const tapStart = findMessages(env.posted, "tap-start", "late-joiner")[0];
  assert.ok(tapStart, "muted joiner gets a tap-start so the popup row appears");
  assert.equal(tapStart.name, "Dana", "muted-joiner name is resolved");
  const muteMsg = findMessages(env.posted, "mute", "late-joiner")[0];
  assert.ok(muteMsg, "mute=true is posted right after tap-start");
  assert.equal(muteMsg.muted, true);
});

test("participantDisconnected posts tap-stop so the recorder closes the WS", async () => {
  // Without this, a departed participant keeps a /tap WS open
  // server-side and shows up in ActiveStreams forever.
  const env = loadPageScript();
  const track = makeTrack();
  const participant = makeParticipant({ identity: "leaver", name: "Eve" });

  env.room.fire("trackSubscribed", track, makeAudioPublication({ track }), participant);
  await flush();
  assert.ok(findMessages(env.posted, "tap-start", "leaver").length, "tap-start fired");

  env.room.fire("participantDisconnected", participant);
  await flush();

  const tapStop = findMessages(env.posted, "tap-stop", "leaver")[0];
  assert.ok(tapStop, "participantDisconnected posts tap-stop");
});

test("trackUnsubscribed posts tap-stop", async () => {
  // LiveKit may drop a track without removing the participant
  // (publication paused, network reshuffle). The /tap WS for that
  // utterance needs to close.
  const env = loadPageScript();
  const track = makeTrack();
  const pub = makeAudioPublication({ track });
  const participant = makeParticipant({ identity: "fred", name: "Fred" });

  env.room.fire("trackSubscribed", track, pub, participant);
  await flush();
  env.room.fire("trackUnsubscribed", track, pub, participant);
  await flush();

  assert.ok(
    findMessages(env.posted, "tap-stop", "fred").length,
    "trackUnsubscribed posts tap-stop",
  );
});

test("room 'disconnected' tears down every announced participant (audio-tapped + presence-only)", async () => {
  // The #34 contract: cleanupAllTaps() iterates `announced` (the
  // superset that includes presence-only entries) so leaving a room
  // clears every popup row AND closes every /tap WS. A regression
  // that iterates only `taps` would leak presence-only rows.
  const env = loadPageScript();
  const track = makeTrack();
  // One audio-tapped participant ("speaker") and one presence-only
  // ("listener" — joined muted, never published audio).
  const speaker = makeParticipant({ identity: "speaker", name: "Sam" });
  const listenerPub = makeAudioPublication({ track: null, isMuted: true });
  const listener = makeParticipant({
    identity: "listener",
    name: "Lee",
    pubs: [listenerPub],
  });

  env.room.fire("trackSubscribed", track, makeAudioPublication({ track }), speaker);
  env.room.fire("participantConnected", listener);
  await flush();

  // Sanity: both announced.
  assert.ok(findMessages(env.posted, "tap-start", "speaker").length, "speaker announced");
  assert.ok(findMessages(env.posted, "tap-start", "listener").length, "listener announced");

  env.room.fire("disconnected");
  await flush();

  assert.ok(
    findMessages(env.posted, "tap-stop", "speaker").length,
    "tap-stop fires for the audio-tapped speaker on room disconnect",
  );
  assert.ok(
    findMessages(env.posted, "tap-stop", "listener").length,
    "tap-stop fires for the PRESENCE-ONLY listener on room disconnect — " +
      "iterating `taps` alone would have missed this",
  );
});

test("existing remote participants are tapped at attach time", async () => {
  // When the extension loads AFTER the room is already connected (the
  // common case — user joins SpatialChat, then opens the popup), the
  // bridge needs to iterate room.remoteParticipants and tap whatever
  // is already subscribed. A regression here would mean the bridge
  // captures nothing until somebody mutes-then-unmutes.
  const track = makeTrack();
  const pub = makeAudioPublication({ track });
  const preexisting = makeParticipant({
    identity: "alreadyhere",
    name: "Quinn",
    pubs: [pub],
  });
  const env = loadPageScript({ remoteParticipants: [preexisting] });
  await flush();

  const tapStart = findMessages(env.posted, "tap-start", "alreadyhere")[0];
  assert.ok(
    tapStart,
    "attach-time iteration produces tap-start for pre-existing participant",
  );
  assert.equal(tapStart.name, "Quinn", "pre-existing participant carries name");
});

test("tap setup failure surfaces as ctx-state='failed' so the popup banner fires", async () => {
  // The wrap-everything-in-try/catch from #31 exists so a CSP block,
  // worklet-registration failure, or stopped-track MediaStream ctor
  // throw doesn't escape into LiveKit's event handler and kill the
  // bridge silently. The visible signal is ctx-state="failed" —
  // without that, the popup just shows "no taps" with zero context.
  const env = loadPageScript({ failWorklet: true });
  const track = makeTrack();
  const participant = makeParticipant({ identity: "g", name: "Gail" });

  env.room.fire("trackSubscribed", track, makeAudioPublication({ track }), participant);
  await flush();

  const failed = env.posted.find(
    (m) => m && m.kind === "ctx-state" && m.state === "failed",
  );
  assert.ok(failed, "ctx-state=failed posted so the popup banner can fire");
  assert.equal(
    findMessages(env.posted, "tap-start", "g").length, 0,
    "no tap-start when audio graph couldn't be built",
  );
});

// ---- room-changed (drives the opt-in "new session on room change") --------
// page-script emits a single `room-changed` when SpatialChat swaps
// window.room for a fresh connected instance — and ONLY then. Opening the
// tab (first attach) or leaving a room (disconnect) must stay silent, else
// the bridge would rotate the recorder's session at the wrong moments.

test("first room attach does NOT emit room-changed", async () => {
  const env = loadPageScript({ startWithRoom: true });
  await flush();
  assert.equal(
    findMessages(env.posted, "room-changed").length, 0,
    "opening the tab is not a room change",
  );
});

test("swapping window.room emits exactly one room-changed", async () => {
  const env = loadPageScript({ startWithRoom: true });
  await flush();
  assert.equal(findMessages(env.posted, "room-changed").length, 0);

  // SpatialChat replaces window.room with a fresh connected Room when the
  // user moves rooms; re-run the captured poll fn against the new instance.
  env.sandbox.window.room = makeRoom();
  env.sandbox.__pollFn();
  await flush();

  assert.equal(
    findMessages(env.posted, "room-changed").length, 1,
    "exactly one room-changed on a real swap",
  );
});

test("leaving a room (disconnect) does NOT emit room-changed", async () => {
  const env = loadPageScript({ startWithRoom: true });
  await flush();

  // Attached room drops to disconnected: the bridge tears down taps but a
  // departure is not a new-room arrival.
  env.room.state = "disconnected";
  env.sandbox.__pollFn();
  await flush();

  assert.equal(
    findMessages(env.posted, "room-changed").length, 0,
    "leaving a room is not a room change",
  );
});

// ---- room-lost teardown ---------------------------------------------------
// maybeAttach() tears down taps when window.room is connected→gone, NOT only on
// a terminal "disconnected". SpatialChat can clear window.room when the user
// leaves a space without flipping the captured Room to "disconnected" (and
// without firing the "disconnected" event). The captured Room still reads
// "connected" and its tracks stay live, so reconcile() (which untaps leavers)
// stops running while no teardown fires — every tap leaks, posting PCM forever.

test("window.room cleared while the old room still reads 'connected' tears down taps", async () => {
  const track = makeTrack();
  const pub = makeAudioPublication({ track });
  const speaker = makeParticipant({ identity: "stuck", name: "Stuck", pubs: [pub] });
  const env = loadPageScript({ remoteParticipants: [speaker] });
  await flush();
  assert.equal(findMessages(env.posted, "tap-start", "stuck").length, 1, "tapped at attach");

  // window.room is gone, but the Room we attached to still reads "connected"
  // (no disconnected event, no state flip) — the exact leak shape.
  env.sandbox.window.room = null;
  assert.equal(env.room.state, "connected", "orphan room still reads connected");

  env.sandbox.__pollFn(); // one 250 ms poll with window.room === null
  await flush();

  assert.equal(
    findMessages(env.posted, "tap-stop", "stuck").length, 1,
    "losing window.room must close the leaked /tap WS — exactly one tap-stop",
  );
});

test("window.room replaced by a not-yet-connected instance tears down the old room's taps", async () => {
  // A room SWAP passes through a window in which the new Room exists but hasn't
  // reached "connected" yet, while the old one still reads "connected". The old
  // room's tracks are about to die; don't keep feeding them until the new room
  // connects — tear them down now (the new room attaches fresh once connected).
  const track = makeTrack();
  const pub = makeAudioPublication({ track });
  const speaker = makeParticipant({ identity: "old-spk", name: "Old", pubs: [pub] });
  const env = loadPageScript({ remoteParticipants: [speaker] });
  await flush();
  assert.equal(findMessages(env.posted, "tap-start", "old-spk").length, 1, "tapped at attach");

  const newRoom = makeRoom();
  newRoom.state = "connecting"; // a different instance, not connected yet
  env.sandbox.window.room = newRoom;

  env.sandbox.__pollFn();
  await flush();

  assert.equal(
    findMessages(env.posted, "tap-stop", "old-spk").length, 1,
    "the superseded room's tap is closed instead of leaking",
  );
});

test("a 'reconnecting' room is NOT torn down (a transient blip is preserved)", async () => {
  // LiveKit keeps the SAME Room instance during a reconnect and restores the
  // same participants; tearing taps down on every blip would churn /tap WSes
  // and cut active utterances. window.room still points at the attached room.
  const track = makeTrack();
  const pub = makeAudioPublication({ track });
  const speaker = makeParticipant({ identity: "blip", name: "Blip", pubs: [pub] });
  const env = loadPageScript({ remoteParticipants: [speaker] });
  await flush();
  assert.equal(findMessages(env.posted, "tap-start", "blip").length, 1, "tapped at attach");

  env.room.state = "reconnecting"; // same object, transient state
  env.sandbox.__pollFn();
  await flush();

  assert.equal(
    findMessages(env.posted, "tap-stop", "blip").length, 0,
    "a reconnecting blip must NOT tear down taps",
  );
});

// ---- reconcile(): self-healing membership sweep ---------------------------
// Event-driven tapping is necessary but not sufficient — LiveKit can drop or
// coalesce trackSubscribed / participantConnected / participantDisconnected
// across a reconnect or a proximity resubscribe, and the attach-time
// enumeration only runs once per room instance. The poll loop now re-derives
// membership from room.remoteParticipants every tick so a missed event can't
// strand a speaker (audible in the room, untapped + missing from the popup)
// or leave a departed one as a ghost row with a leaked /tap WS. `__pollFn` is
// the captured 250 ms poll callback; calling it drives one reconcile tick.

test("reconcile taps a participant whose trackSubscribed event was missed", async () => {
  // The reported bug: someone is talking in the room — everyone else shows
  // up in the extension, but this person doesn't, and their audio never
  // reaches the recorder. That's a subscribe/connect event the bridge never
  // saw. Reconcile finds them on the next poll and taps them.
  const env = loadPageScript(); // room connected, no participants yet
  await flush();

  const track = makeTrack();
  const pub = makeAudioPublication({ track });
  const ghost = makeParticipant({ identity: "ghost", name: "Ghost", pubs: [pub] });
  // They joined and their mic track subscribed, but NO event reached us.
  env.room.remoteParticipants.set("ghost", ghost);
  assert.equal(
    findMessages(env.posted, "tap-start", "ghost").length, 0,
    "no tap-start yet — the subscribe event was missed",
  );

  env.sandbox.__pollFn(); // one 250 ms reconcile tick
  await flush();

  const tapStart = findMessages(env.posted, "tap-start", "ghost");
  assert.equal(tapStart.length, 1, "reconcile taps the missed speaker, exactly once");
  assert.equal(tapStart[0].name, "Ghost", "missed speaker is tapped with their name");
});

test("reconcile re-taps a present speaker dropped by a stray unsubscribe", async () => {
  // SpatialChat is spatial audio: walking out of range fires
  // trackUnsubscribed (we untap + drop the popup row); walking back in can
  // resubscribe the track without a fresh trackSubscribed reaching us. The
  // speaker is back, talking, with a live subscribed track — and would stay
  // missing forever without the reconcile sweep.
  const track = makeTrack();
  const pub = makeAudioPublication({ track });
  const wanderer = makeParticipant({ identity: "wanderer", name: "Wendy", pubs: [pub] });
  const env = loadPageScript({ remoteParticipants: [wanderer] });
  await flush();
  assert.equal(
    findMessages(env.posted, "tap-start", "wanderer").length, 1, "tapped at attach",
  );

  // Walked out of range — trackUnsubscribed drops them from taps + popup.
  env.room.fire("trackUnsubscribed", track, pub, wanderer);
  await flush();
  assert.ok(
    findMessages(env.posted, "tap-stop", "wanderer").length, "untapped on unsubscribe",
  );

  // Still in the room with a live subscribed track (walked back in, but the
  // resubscribe event was missed). Reconcile must restore the tap.
  env.sandbox.__pollFn();
  await flush();
  assert.equal(
    findMessages(env.posted, "tap-start", "wanderer").length, 2,
    "reconcile re-taps the speaker the stray unsubscribe had dropped",
  );
});

test("reconcile untaps a participant who left without a disconnect event", async () => {
  // The other half of the drift: if participantDisconnected (or the final
  // trackUnsubscribed) is dropped, the bridge keeps a ghost popup row and a
  // leaked /tap WS open against the recorder. Reconcile notices the identity
  // is gone from room.remoteParticipants and untaps it.
  const track = makeTrack();
  const pub = makeAudioPublication({ track });
  const departed = makeParticipant({ identity: "departed", name: "Dave", pubs: [pub] });
  const env = loadPageScript({ remoteParticipants: [departed] });
  await flush();
  assert.equal(
    findMessages(env.posted, "tap-start", "departed").length, 1, "tapped at attach",
  );

  // They leave, but no participantDisconnected reaches us.
  env.room.remoteParticipants.delete("departed");
  env.sandbox.__pollFn();
  await flush();

  assert.equal(
    findMessages(env.posted, "tap-stop", "departed").length, 1,
    "reconcile untaps the silently-departed participant, exactly once",
  );
});

test("reconcile is idempotent — repeated ticks don't re-announce or churn mute", async () => {
  // Reconcile runs ~4x/second. tap() / announcePresence() must early-return
  // on a known identity, else every tick would re-post tap-start and flood
  // the mute channel — and a stray mute=true would prematurely cut an active
  // speaker's utterance. Pin one tap-start and a single presence-seed mute
  // across the attach plus five reconcile ticks.
  const track = makeTrack();
  const pub = makeAudioPublication({ track });
  const steady = makeParticipant({ identity: "steady", name: "Steve", pubs: [pub] });
  const env = loadPageScript({ remoteParticipants: [steady] });
  await flush();

  for (let i = 0; i < 5; i++) {
    env.sandbox.__pollFn();
    await flush();
  }

  assert.equal(
    findMessages(env.posted, "tap-start", "steady").length, 1,
    "exactly one tap-start across attach + five reconcile ticks",
  );
  assert.equal(
    findMessages(env.posted, "mute", "steady").length, 1,
    "the only mute message is the initial presence seed — no per-tick churn",
  );
});

// ---------------------------------------------------------------------------
// Mid-stream capture failure — a LIVE track that stops being live
// ---------------------------------------------------------------------------

test("a device switch rebinds the source node to the replacement MediaStreamTrack", async () => {
  // LiveKit's device switch swaps `pub.track.mediaStreamTrack` IN PLACE and
  // fires NO trackUnsubscribed. tap() early-returned on IDENTITY alone, so
  // the 250 ms reconcile no-op'd and the MediaStreamAudioSourceNode stayed
  // wrapped around the STOPPED track: the worklet kept emitting frames of
  // zeros, the byte counter climbed, the pill stayed green, and the WAV
  // recorded SILENCE for the rest of the meeting.
  const track = makeTrack();
  const mstA = track.mediaStreamTrack;
  const participant = makeParticipant({ identity: "alice-id", name: "Alice" });
  participant.audioTrackPublications.set("0", makeAudioPublication({ track }));

  // Seeded into the room, so the load-time maybeAttach()'s reconcile does the
  // first tap — the same code path the 250 ms tick below re-runs.
  const env = loadPageScript({ remoteParticipants: [participant] });
  await flush();
  assert.equal(env.audio.wrappedTracks().length, 1, "reconcile tapped once at attach");
  assert.equal(env.audio.wrappedTracks()[0], mstA, "first tap wraps the original track");

  // The device switch: same Track wrapper, brand-new MediaStreamTrack, old
  // one stopped. No LiveKit event fires — only the reconcile tick sees it.
  const mstB = makeMst();
  mstA.readyState = "ended";
  track.mediaStreamTrack = mstB;

  env.sandbox.__pollFn(); // the 250 ms reconcile
  await flush();

  const wrapped = env.audio.wrappedTracks();
  assert.equal(wrapped.length, 2, "reconcile re-tapped instead of no-op'ing");
  assert.equal(wrapped[1], mstB, "the source node is bound to the LIVE replacement track");

  // The utterance boundary is honoured: the old /tap closes (so its WAV
  // finalises) and a fresh one opens, rather than the same channel silently
  // continuing over a dead source.
  assert.equal(findMessages(env.posted, "tap-stop", "alice-id").length, 1);
  assert.equal(findMessages(env.posted, "tap-start", "alice-id").length, 2);
});

test("a second reconcile over an UNCHANGED track does not churn the tap", async () => {
  // The other half of the guard: reconcile runs 4x/s, so widening the
  // early-return must not turn every tick into an untap/re-tap cycle.
  const track = makeTrack();
  const participant = makeParticipant({ identity: "alice-id", name: "Alice" });
  participant.audioTrackPublications.set("0", makeAudioPublication({ track }));

  const env = loadPageScript({ remoteParticipants: [participant] });
  await flush();

  env.sandbox.__pollFn();
  env.sandbox.__pollFn();
  await flush();

  assert.equal(env.audio.wrappedTracks().length, 1, "still exactly one source node");
  assert.equal(findMessages(env.posted, "tap-stop", "alice-id").length, 0, "no spurious tap-stop");
  assert.equal(findMessages(env.posted, "tap-start", "alice-id").length, 1, "no spurious tap-start");
});

test("a track that ends mid-stream untaps and reports a capture failure", async () => {
  // Mic unplugged / permission revoked: the track goes to readyState
  // "ended" and LiveKit sends nothing. Without an `ended` listener the
  // bridge held the stopped track forever, and reconcile could not recover
  // (tap() rejects a non-live track), so the /tap WS stayed open recording
  // zeros. The bridge must close the Utterance and say why.
  const track = makeTrack();
  const mst = track.mediaStreamTrack;
  const participant = makeParticipant({ identity: "alice-id", name: "Alice" });
  participant.audioTrackPublications.set("0", makeAudioPublication({ track }));

  const env = loadPageScript({ remoteParticipants: [participant] });
  await flush();
  assert.equal(mst.listenerCount("ended"), 1, "the tap listens for the track ending");

  mst.fireEnded();
  await flush();

  const failures = findMessages(env.posted, "capture-failed", "alice-id");
  assert.equal(failures.length, 1, "the dead track is reported, not silent");
  assert.equal(failures[0].reason, "track-ended");
  assert.equal(
    findMessages(env.posted, "tap-stop", "alice-id").length, 1,
    "and the Utterance is closed so the WAV finalises instead of accruing zeros",
  );

  // The listener is removed with the tap, so a later re-tap can't stack a
  // second one on the same track.
  assert.equal(mst.listenerCount("ended"), 0, "untap detaches the ended listener");
});

// ---------------------------------------------------------------------------
// AudioContext gesture retry
// ---------------------------------------------------------------------------

test("a gesture whose resume() REJECTS leaves the retry armed", async () => {
  // The old comment claimed "statechange will rearm us if resume failed" —
  // false: statechange only fires on an actual transition, so a rejected
  // resume() disarmed the retry with nothing left to re-arm it and capture
  // stayed dead until the operator hid and re-showed the tab.
  const env = loadPageScript({ ctxState: "suspended" });
  env.audio.setResumeRejects(true);

  const track = makeTrack();
  const participant = makeParticipant({ identity: "alice-id", name: "Alice" });
  const pub = makeAudioPublication({ track });
  participant.audioTrackPublications.set("0", pub);
  env.room.fire("trackSubscribed", track, pub, participant);
  await flush();

  const armed = (env.eventListeners.pointerdown || []).slice();
  assert.equal(armed.length, 1, "the eager resume() rejected → gesture retry armed");

  // Gesture #1 — resume() still rejects.
  armed[0]({ type: "pointerdown" });
  await flush();
  assert.equal(
    (env.eventListeners.pointerdown || []).length, 1,
    "a failed retry must stay armed — nothing else would ever re-arm it",
  );

  // Gesture #2 — the operator interacts again and this time resume() works.
  env.audio.setResumeRejects(false);
  const resumesBefore = env.audio.resumeCalls.length;
  (env.eventListeners.pointerdown || [])[0]({ type: "pointerdown" });
  await flush();
  assert.equal(
    env.audio.resumeCalls.length, resumesBefore + 1,
    "the still-armed listener retried on the next gesture",
  );
  for (const ev of ["pointerdown", "keydown", "touchstart"]) {
    assert.equal(
      (env.eventListeners[ev] || []).length, 0,
      "a SUCCESSFUL resume disarms every one-shot listener (" + ev + ")",
    );
  }
});

test("the worklet emits FRAME_SAMPLES-sample frames, interpolated not hard-coded", async () => {
  // The MAIN world can't import control-client.js, so page-script.js declares
  // the frame size itself and interpolates it into the worklet source. That
  // makes it a /tap wire declaration site, stamped from the Recorder by
  // tools/stamp_tap_wire.py and gated by tests/test_tap_wire_contract.py.
  //
  // Interpolation inside a String.raw template is the one thing that could
  // silently break here — String.raw leaves BACKSLASH escapes raw, and a
  // reader who assumes it leaves `${...}` raw too would ship a worklet that
  // allocates `Int16Array(NaN)` and emits nothing. Every other test in this
  // file hand-feeds frames through port.onmessage and would stay green.
  // The worklet Blob is built lazily by ensureAudioGraph(), so a tap has to
  // happen before there is anything to inspect.
  const env = loadPageScript();
  const track = makeTrack();
  env.room.fire(
    "trackSubscribed",
    track,
    makeAudioPublication({ track }),
    { identity: "alice-id", name: "Alice", audioTrackPublications: new Map() },
  );
  await flush();

  const src = env.workletSource();
  assert.ok(src.includes("class TapscribeResampler"), "the worklet source was captured");
  assert.ok(
    !src.includes("${"),
    "no un-substituted template placeholder survived into the worklet source",
  );

  // Compare the worklet against the DECLARED const rather than a literal 320.
  // Hard-coding 320 here would pass just as happily if the const were stamped
  // to a new frame size and the worklet body left stale — which is the only
  // way this can actually break.
  const declared = /const FRAME_SAMPLES = (\d+);/.exec(
    fs.readFileSync(PAGE_SCRIPT, "utf8"),
  );
  assert.ok(declared, "page-script.js declares FRAME_SAMPLES");
  const n = declared[1];

  assert.match(
    src,
    new RegExp(`new Int16Array\\(${n}\\)`),
    `the output buffer is FRAME_SAMPLES (${n}) samples`,
  );
  assert.match(
    src,
    new RegExp(`this\\.outPos === ${n}`),
    `the flush threshold is the same ${n} samples`,
  );
});
