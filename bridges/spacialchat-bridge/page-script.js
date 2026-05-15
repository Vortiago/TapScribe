// SpatialChat Bridge - page-script.js (MAIN world)
//
// Taps remote (and local) audio tracks via LiveKit's Room API as exposed
// at `window.room`, resamples 48 kHz float32 -> 16 kHz mono int16 via an
// inline AudioWorklet loaded from a blob URL, and forwards PCM frames to
// the content script via window.postMessage. The content script holds
// the /tap WebSocket; this script never touches the network.

(() => {
  if (window.__tapscribeBridgePageInstalled) {
    console.warn("[tapscribe-bridge/page] already installed; skipping");
    return;
  }
  window.__tapscribeBridgePageInstalled = true;

  // ---- Inline AudioWorklet source -------------------------------------------
  // 48 kHz mono float32 in -> 16 kHz mono int16 out. 3:1 decimation with
  // a 3-tap boxcar lowpass (cheap and good enough for speech). 20 ms
  // output chunks (320 samples) posted as transferable Int16Array
  // buffers — matches the Recorder's /tap frame size exactly.
  const WORKLET_SRC = String.raw`
    class TapscribeResampler extends AudioWorkletProcessor {
      constructor() {
        super();
        this.phase = 0;
        this.s0 = 0; this.s1 = 0; this.s2 = 0;
        this.outBuf = new Int16Array(320);
        this.outPos = 0;
      }
      process(inputs) {
        const ch = inputs[0] && inputs[0][0];
        if (!ch || ch.length === 0) return true;
        for (let i = 0; i < ch.length; i++) {
          this.s0 = this.s1; this.s1 = this.s2; this.s2 = ch[i];
          this.phase++;
          if (this.phase === 3) {
            this.phase = 0;
            const y = (this.s0 + this.s1 + this.s2) / 3;
            const c = y < -1 ? -1 : y > 1 ? 1 : y;
            this.outBuf[this.outPos++] = Math.round(c * 32767);
            if (this.outPos === 320) {
              const out = new Int16Array(this.outBuf);
              this.port.postMessage(out, [out.buffer]);
              this.outPos = 0;
            }
          }
        }
        return true;
      }
    }
    registerProcessor('tapscribe-resampler', TapscribeResampler);
  `;

  // ---- State -----------------------------------------------------------------
  const taps = new Map(); // identity -> { source, worklet, silentGain, name }
  let audioCtx = null;
  let workletReady = null;

  function postToContent(msg, transfer) {
    window.postMessage(Object.assign({ source: "tapscribe-bridge" }, msg), "*", transfer || []);
  }

  function getDisplayName(participant) {
    // SpatialChat keeps display names on Vue 2 sidebar user components,
    // NOT on the LiveKit Participant (which is always "") and NOT on the
    // internal remote-participant map (also always ""). We walk the
    // existing sidebar user elements and pull `_props.user.name`. Returns
    // "" if the sidebar hasn't rendered the user yet — caller can
    // re-resolve later.
    try {
      const els = document.querySelectorAll('[class*="space-side-bar-room-user"]');
      for (const el of els) {
        const vm = el.__vue__;
        const user = vm && vm._props && vm._props.user;
        if (user && user.id === participant.identity && user.name) {
          return user.name;
        }
      }
    } catch (e) { /* fall through */ }
    return participant.name || "";
  }

  async function ensureAudioGraph() {
    if (audioCtx) {
      await workletReady;
      return audioCtx;
    }
    audioCtx = new AudioContext({ sampleRate: 48000 });
    const url = URL.createObjectURL(new Blob([WORKLET_SRC], { type: "application/javascript" }));
    workletReady = audioCtx.audioWorklet.addModule(url).finally(() => URL.revokeObjectURL(url));
    await workletReady;
    return audioCtx;
  }

  async function tap(participant, mediaStreamTrack) {
    if (taps.has(participant.identity)) return;
    if (!mediaStreamTrack || mediaStreamTrack.readyState !== "live") return;

    let ctx;
    try {
      ctx = await ensureAudioGraph();
    } catch (e) {
      console.error("[tapscribe-bridge/page] failed to set up AudioContext/worklet:", e);
      return;
    }

    const name = getDisplayName(participant);
    const source = ctx.createMediaStreamSource(new MediaStream([mediaStreamTrack]));
    const worklet = new AudioWorkletNode(ctx, "tapscribe-resampler");
    const silentGain = ctx.createGain();
    silentGain.gain.value = 0;

    const entry = { source, worklet, silentGain, name, resolvedName: name || "" };

    worklet.port.onmessage = (ev) => {
      const buf = ev.data.buffer;
      // The sidebar may not have rendered the user yet at tap time; retry
      // until it gives us a non-empty name, then stop querying the DOM.
      if (!entry.resolvedName) {
        const n = getDisplayName(participant);
        if (n) entry.resolvedName = n;
      }
      postToContent({
        kind: "pcm",
        identity: participant.identity,
        name: entry.resolvedName,
        buffer: buf,
      }, [buf]);
    };

    source.connect(worklet);
    worklet.connect(silentGain).connect(ctx.destination);

    taps.set(participant.identity, entry);
    postToContent({ kind: "tap-start", identity: participant.identity, name });
    console.log("[tapscribe-bridge/page] tapping " + participant.identity + " (" + (name || "no-name") + ")");
  }

  function untap(identity) {
    const t = taps.get(identity);
    if (!t) return;
    try { t.source.disconnect(); } catch (e) {}
    try { t.worklet.disconnect(); } catch (e) {}
    try { t.silentGain.disconnect(); } catch (e) {}
    taps.delete(identity);
    postToContent({ kind: "tap-stop", identity });
    console.log("[tapscribe-bridge/page] untap " + identity);
  }

  function setMute(identity, muted) {
    postToContent({ kind: "mute", identity, muted });
  }

  function attachListeners(room) {
    // LiveKit event names are camelCase strings (RoomEvent enum values).

    // Remote audio: subscribe / unsubscribe / mute lifecycle.
    room.on("trackSubscribed", (track, pub, participant) => {
      if (track.kind === "audio" && pub.source === "microphone") {
        tap(participant, track.mediaStreamTrack);
      }
    });
    room.on("trackUnsubscribed", (track, pub, participant) => {
      if (track.kind === "audio") untap(participant.identity);
    });

    // Local audio: SpatialChat publishes the user's own mic; we tap it
    // too so their voice is recorded alongside remote participants.
    room.on("localTrackPublished", (pub, participant) => {
      if (pub.kind === "audio" && pub.source === "microphone" && pub.track && pub.track.mediaStreamTrack) {
        tap(participant, pub.track.mediaStreamTrack);
        if (pub.isMuted) setMute(participant.identity, true);
      }
    });
    room.on("localTrackUnpublished", (pub, participant) => {
      if (pub.kind === "audio") untap(participant.identity);
    });

    // Mute applies to both local and remote audio publications. We use
    // the mute toggle as our utterance boundary: closing the /tap WS on
    // mute tells the Recorder to finalise the current WAV.
    room.on("trackMuted", (pub, participant) => {
      if (pub.kind === "audio") setMute(participant.identity, true);
    });
    room.on("trackUnmuted", (pub, participant) => {
      if (pub.kind === "audio") setMute(participant.identity, false);
    });

    room.on("disconnected", () => {
      console.warn("[tapscribe-bridge/page] room disconnected; tearing down taps");
      for (const id of Array.from(taps.keys())) untap(id);
    });

    // Iterate existing publications already in place at attach time.
    // Remote participants:
    for (const participant of room.remoteParticipants.values()) {
      for (const pub of participant.audioTrackPublications.values()) {
        if (pub.isSubscribed && pub.track && pub.track.mediaStreamTrack) {
          tap(participant, pub.track.mediaStreamTrack);
          if (pub.isMuted) setMute(participant.identity, true);
        }
      }
    }
    // Local participant (mic enabled before extension attached):
    const lp = room.localParticipant;
    if (lp && lp.audioTrackPublications) {
      for (const pub of lp.audioTrackPublications.values()) {
        if (pub.source === "microphone" && pub.track && pub.track.mediaStreamTrack) {
          tap(lp, pub.track.mediaStreamTrack);
          if (pub.isMuted) setMute(lp.identity, true);
        }
      }
    }
  }

  // ---- Watch window.room ----------------------------------------------------
  // SpatialChat replaces window.room with a fresh LiveKit Room instance
  // every time the user enters or switches a room. A one-shot attach
  // would only see the first room and miss every subsequent one (and the
  // old room's "disconnected" event would tear down all our taps,
  // leaving the dashboard quiet until the operator reloads the tab). We
  // poll forever and re-attach whenever the current `window.room` is a
  // NEW instance (or null and an attached one has disconnected).
  const POLL_MS = 250;
  let attachedRoom = null;
  function maybeAttach() {
    const room = window.room;
    if (room && room.state === "connected") {
      if (room !== attachedRoom) {
        if (attachedRoom) {
          // Old room got swapped out under us — drop its taps before
          // binding to the new one so we don't keep stale forwarders alive.
          console.log("[tapscribe-bridge/page] window.room replaced; rebinding to new instance");
          for (const id of Array.from(taps.keys())) untap(id);
        } else {
          console.log("[tapscribe-bridge/page] window.room connected; attaching listeners");
        }
        attachedRoom = room;
        try {
          attachListeners(room);
        } catch (e) {
          console.error("[tapscribe-bridge/page] attachListeners failed:", e);
        }
      }
    } else if (attachedRoom && attachedRoom.state !== "connected") {
      // Previously-attached room dropped; clear the reference so a fresh
      // connection re-triggers the attach branch above.
      attachedRoom = null;
    }
  }
  // Run immediately so we attach as soon as the room is up, then keep watching.
  maybeAttach();
  setInterval(maybeAttach, POLL_MS);
})();
