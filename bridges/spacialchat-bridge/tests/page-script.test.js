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

function makeAudioMocks({ failWorklet = false } = {}) {
  // ensureAudioGraph awaits audioWorklet.addModule(...) before returning,
  // so the resolved Promise has to actually settle. The rest is just
  // duck-typing of the AudioContext / WorkletNode surface tap() touches.
  const ctx = {
    state: "running",
    sampleRate: 48000,
    destination: { _isDestination: true },
    createMediaStreamSource: () => ({ connect: (next) => next }),
    createGain: () => ({
      gain: { value: 0 },
      connect: (next) => next,
      disconnect: () => {},
    }),
    audioWorklet: { addModule: () => Promise.resolve() },
    addEventListener: () => {},
    resume: () => Promise.resolve(),
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
  function MediaStream(_tracks) { return { _isStream: true }; }
  return { AudioContext, AudioWorkletNode, MediaStream, ctx };
}

function loadPageScript({
  withSidebarNameFor = null,
  remoteParticipants = [],
  localParticipant = null,
  failWorklet = false,
  startWithRoom = true,
} = {}) {
  const audio = makeAudioMocks({ failWorklet });
  const room = makeRoom();
  // Seed any pre-existing participants the test wants attached at
  // load time (covers the "tap was running before the extension
  // injected" path that attachListeners() iterates).
  for (const p of remoteParticipants) room.remoteParticipants.set(p.identity, p);
  if (localParticipant) room.localParticipant = localParticipant;
  const posted = [];
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
  const doc = {
    head: { appendChild: () => {} },
    addEventListener: () => {},
    querySelectorAll: () => sidebarEls,
    visibilityState: "visible",
  };

  const win = {
    __tapscribeBridgePageInstalled: undefined,
    addEventListener: (ev, fn) => {
      if (!eventListeners[ev]) eventListeners[ev] = [];
      eventListeners[ev].push(fn);
    },
    removeEventListener: () => {},
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
    Blob: function Blob() { return {}; },
    crypto: { randomUUID: () => "u-" + Math.random().toString(36).slice(2) },
    console: { log: () => {}, warn: () => {}, error: () => {} },
    // Capture the 250ms room-poll callback (don't auto-run it) so a test
    // can drive a deterministic room swap via sandbox.__pollFn().
    setInterval: (fn) => { sandbox.__pollFn = fn; return 0; },
    clearInterval: () => {},
    setTimeout: (fn, _ms) => { fn(); return 0; },
    clearTimeout: () => {},
  };
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(PAGE_SCRIPT, "utf8"), sandbox, {
    filename: "page-script.js",
  });

  return { room, audio, posted, eventListeners, sandbox };
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

function makeTrack() {
  // LiveKit exposes the underlying MediaStreamTrack on Track via
  // `.mediaStreamTrack`; the bridge passes that into tap(). The Track
  // object itself only carries `kind` in the path we exercise.
  return {
    kind: "audio",
    mediaStreamTrack: { readyState: "live", kind: "audio" },
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
