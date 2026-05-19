// Mock LiveKit Room and friends for the bridge E2E tests.
//
// page-script.js polls `window.room` every 250 ms and, when it sees a
// connected Room, calls `attachListeners(room)` and starts iterating
// existing publications. To exercise the bridge end-to-end we need a
// `window.room` that:
//   - has `.state === "connected"` so maybeAttach() binds to it
//   - is an EventEmitter for trackSubscribed / trackUnsubscribed /
//     trackMuted / participantConnected / participantDisconnected /
//     disconnected
//   - exposes remoteParticipants (Map) + localParticipant
//
// Event shapes follow bridges/spacialchat-bridge/tests/page-script.test.js
// — they're the source of truth for what page-script.js expects.
//
// The bridge runs in Chrome's ISOLATED content-script world. From the
// MAIN page world we can't directly call into content.js — instead we
// drive the bridge by manipulating window.room (which page-script reads)
// and by calling room.fire(...) to dispatch its LiveKit-style events.
// page-script then posts messages cross-world via window.postMessage,
// which content.js picks up.
//
// __tsTest is the test-facing driver the Playwright test calls into:
//   __tsTest.addRemoteSpeaker(identity, name)  — fires trackSubscribed
//   __tsTest.muteSpeaker(identity)             — fires trackMuted
//   __tsTest.unmuteSpeaker(identity)           — fires trackUnmuted
//   __tsTest.removeSpeaker(identity)           — fires participantDisconnected
//   __tsTest.disconnectRoom()                  — fires disconnected
//   __tsTest.setSidebarUser(id, name)          — seeds the Vue sidebar el
//   __tsTest.emitPcm(identity, int16)          — drives a worklet message
//                                                so PCM lands on /tap
//
// Keep this file dependency-free; it's served as a plain static asset.
(function () {
  "use strict";

  function makeRoom() {
    const handlers = new Map();
    const room = {
      state: "connected",
      remoteParticipants: new Map(),
      localParticipant: null,
      on(ev, fn) {
        if (!handlers.has(ev)) handlers.set(ev, []);
        handlers.get(ev).push(fn);
      },
      fire(ev) {
        const args = Array.prototype.slice.call(arguments, 1);
        for (const fn of handlers.get(ev) || []) fn.apply(null, args);
      },
      _handlers: handlers,
    };
    return room;
  }

  // A real MediaStreamTrack from a WebAudio oscillator → destination.
  // page-script.js calls `new MediaStream([mediaStreamTrack])` and pipes
  // it through `createMediaStreamSource`, so the track must be a genuine
  // browser MediaStreamTrack — a plain object throws TypeError. Each
  // call gets its own track + Source AudioContext so multiple speakers
  // don't share an oscillator (which would make all of them un-tappable
  // when one disconnects).
  const _trackCtxs = [];
  function makeRealMediaStreamTrack() {
    const ctx = new AudioContext({ sampleRate: 48000 });
    _trackCtxs.push(ctx);
    const osc = ctx.createOscillator();
    osc.frequency.value = 220;
    const dst = ctx.createMediaStreamDestination();
    osc.connect(dst);
    osc.start();
    return dst.stream.getAudioTracks()[0];
  }

  function makeTrack() {
    return { kind: "audio", mediaStreamTrack: makeRealMediaStreamTrack() };
  }

  function makeAudioPublication(track, isMuted) {
    return {
      kind: "audio",
      source: "microphone",
      isMuted: !!isMuted,
      isSubscribed: !!track,
      track: track || null,
    };
  }

  function makeParticipant(identity, name) {
    return {
      identity: identity,
      name: name || "",
      audioTrackPublications: new Map(),
    };
  }

  const room = makeRoom();
  window.room = room;

  // Track participants so we can re-fire teardown events later.
  const participants = new Map(); // identity -> { participant, track, pub }
  const sidebar = document.getElementById("sidebar");

  // The page-script.js getDisplayName() walks elements matching
  // `[class*="space-side-bar-room-user"]` and reads .__vue__._props.user.
  // We synthesize that shape so the Vue-sidebar resolution path is
  // exercised in the real DOM.
  function setSidebarUser(id, name) {
    let el = sidebar.querySelector('[data-uid="' + id + '"]');
    if (!el) {
      el = document.createElement("div");
      el.setAttribute("data-uid", id);
      el.className = "space-side-bar-room-user mock";
      sidebar.appendChild(el);
    }
    el.__vue__ = { _props: { user: { id: id, name: name } } };
  }

  // Some tests need to verify that the bridge re-resolves the name on
  // EVERY worklet message (PR #35 regression). Yank the sidebar entry
  // for a participant, then re-add with a fresh name — the next PCM
  // frame should carry the updated name.
  function clearSidebarUser(id) {
    const el = sidebar.querySelector('[data-uid="' + id + '"]');
    if (el) el.parentNode.removeChild(el);
  }

  // Drive a trackSubscribed → page-script.tap() → AudioWorkletNode.
  // The real page-script wires worklet.port.onmessage to forward PCM;
  // we capture the constructed AudioWorkletNode here so the test can
  // poke port.onmessage({ data: Int16Array }) and force a /tap frame.
  const workletNodesByIdentity = new Map();
  const origAudioWorkletNode = window.AudioWorkletNode;
  window.AudioWorkletNode = function (ctx, name, opts) {
    const node = new origAudioWorkletNode(ctx, name, opts);
    // We don't know which participant this belongs to yet — page-script
    // calls `new AudioWorkletNode(...)` synchronously then assigns the
    // onmessage handler immediately after. Capture the most-recently-
    // constructed node; the test's addRemoteSpeaker() resolves it after
    // a flush.
    window.__tsLastWorkletNode = node;
    return node;
  };

  function addRemoteSpeaker(identity, name, opts) {
    opts = opts || {};
    if (name) setSidebarUser(identity, name);
    const track = makeTrack();
    const pub = makeAudioPublication(track, !!opts.muted);
    const participant = makeParticipant(identity, opts.passName ? name : "");
    participant.audioTrackPublications.set("0", pub);
    room.remoteParticipants.set(identity, participant);
    participants.set(identity, { participant: participant, track: track, pub: pub });
    room.fire("trackSubscribed", track, pub, participant);
    if (opts.muted) {
      room.fire("trackMuted", pub, participant);
    }
    // Stash the worklet node so emitPcm() can drive it. The bridge
    // constructs it inside an awaited promise chain, so callers should
    // await a microtask flush before grabbing it.
    return participant;
  }

  function presenceOnlySpeaker(identity, name) {
    if (name) setSidebarUser(identity, name);
    const participant = makeParticipant(identity, name || "");
    // No published track — just announce them as joined.
    room.remoteParticipants.set(identity, participant);
    participants.set(identity, { participant: participant, track: null, pub: null });
    room.fire("participantConnected", participant);
    return participant;
  }

  function muteSpeaker(identity) {
    const entry = participants.get(identity);
    if (!entry || !entry.pub) return;
    entry.pub.isMuted = true;
    room.fire("trackMuted", entry.pub, entry.participant);
  }

  function unmuteSpeaker(identity) {
    const entry = participants.get(identity);
    if (!entry || !entry.pub) return;
    entry.pub.isMuted = false;
    room.fire("trackUnmuted", entry.pub, entry.participant);
  }

  function removeSpeaker(identity) {
    const entry = participants.get(identity);
    if (!entry) return;
    room.remoteParticipants.delete(identity);
    participants.delete(identity);
    room.fire("participantDisconnected", entry.participant);
  }

  function disconnectRoom() {
    room.state = "disconnected";
    room.fire("disconnected");
  }

  // Emit a 320-sample int16 PCM frame from the worklet for `identity`.
  // page-script.js attaches onmessage to each tapped participant's
  // worklet node; we re-resolve which node belongs to which identity by
  // looking up window.__tsWorkletByIdentity (page-script tags the
  // entry under taps; we mirror by wrapping AudioWorkletNode above).
  //
  // Returns true if a frame was dispatched, false if no worklet exists
  // yet for this identity.
  function emitPcm(identity) {
    const node = workletNodesByIdentity.get(identity);
    if (!node || !node.port || typeof node.port.onmessage !== "function") {
      return false;
    }
    const buf = new Int16Array(320);
    node.port.onmessage({ data: buf });
    return true;
  }

  // The bridge sets onmessage on the worklet inside its tap() function.
  // To pair worklet nodes with identities, page-script.js calls
  // `worklet.port.onmessage = (ev) => postToContent({ kind: "pcm", identity, ... })`.
  // We don't see that assignment from here, but we CAN intercept the
  // postToContent call by wrapping window.postMessage and learning which
  // worklet belongs to which identity from the first PCM frame the bridge
  // itself emits — but that's circular.
  //
  // Simpler: hook the worklet constructor to attach an onmessage spy that
  // forwards once the bridge installs its real handler. Track the most
  // recently subscribed identity in addRemoteSpeaker and assume tap()
  // resolves synchronously enough that __tsLastWorkletNode aligns. The
  // test driver below exposes pairLastWorkletWith(identity) so tests can
  // bind explicitly after awaiting a flush.
  function pairLastWorkletWith(identity) {
    if (window.__tsLastWorkletNode) {
      workletNodesByIdentity.set(identity, window.__tsLastWorkletNode);
      window.__tsLastWorkletNode = null;
      return true;
    }
    return false;
  }

  window.__tsTest = {
    addRemoteSpeaker: addRemoteSpeaker,
    presenceOnlySpeaker: presenceOnlySpeaker,
    muteSpeaker: muteSpeaker,
    unmuteSpeaker: unmuteSpeaker,
    removeSpeaker: removeSpeaker,
    disconnectRoom: disconnectRoom,
    setSidebarUser: setSidebarUser,
    clearSidebarUser: clearSidebarUser,
    emitPcm: emitPcm,
    pairLastWorkletWith: pairLastWorkletWith,
    // For diagnostics from tests.
    roomState: function () { return room.state; },
    speakerCount: function () { return participants.size; },
  };
})();
