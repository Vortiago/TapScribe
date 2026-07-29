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

  // The /tap wire frame: 20 ms of 16 kHz mono int16 = 320 samples. This is
  // the MAIN world, which cannot import control-client.js (the content script
  // lives in an isolated world), so this is JS's second declaration site for
  // the frame size — and it is stamped from the Recorder rather than typed by
  // hand. Interpolated into WORKLET_SRC below: `String.raw` leaves BACKSLASH
  // escapes raw but still substitutes `${...}` normally.
  // See tools/stamp_tap_wire.py and ADR-0019.
  const FRAME_SAMPLES = 320;

  // ---- Inline AudioWorklet source -------------------------------------------
  // 48 kHz mono float32 in -> 16 kHz mono int16 out. 3:1 decimation with
  // a 3-tap boxcar lowpass (cheap and good enough for speech). 20 ms
  // output chunks posted as transferable Int16Array buffers — matches the
  // Recorder's /tap frame size exactly.
  const WORKLET_SRC = String.raw`
    class TapscribeResampler extends AudioWorkletProcessor {
      constructor() {
        super();
        this.phase = 0;
        this.s0 = 0; this.s1 = 0; this.s2 = 0;
        this.outBuf = new Int16Array(${FRAME_SAMPLES});
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
            if (this.outPos === ${FRAME_SAMPLES}) {
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

  // Notify the content script when the AudioContext flips state. The
  // browser auto-suspends AudioContexts on inactive tabs and (per the
  // autoplay policy) sometimes refuses to leave the suspended state
  // until the user interacts with the page. While suspended the
  // AudioWorklet runs no audio through it, so the bridge would silently
  // capture nothing. Surfacing the state lets the popup / title pill
  // show a clear error instead of "0 frames sent".
  function postCtxState(state) {
    postToContent({ kind: "ctx-state", state });
  }

  // One-shot user-gesture handler that retries resume(). Browsers only
  // allow resume() inside a trusted gesture handler; we register the
  // smallest set of listeners that's likely to fire (mousedown, keydown,
  // touchstart) and remove them all the moment one of them succeeds.
  let gestureRetryArmed = false;
  function armGestureRetry() {
    if (gestureRetryArmed) return;
    gestureRetryArmed = true;
    const disarm = () => {
      gestureRetryArmed = false;
      window.removeEventListener("pointerdown", handler, true);
      window.removeEventListener("keydown", handler, true);
      window.removeEventListener("touchstart", handler, true);
    };
    const handler = () => {
      if (!audioCtx) return;
      audioCtx.resume().then(() => {
        console.log("[tapscribe-bridge/page] AudioContext resumed via user gesture");
        // Disarm ONLY on success. Nothing else would re-arm us: `statechange`
        // fires on an actual transition, and a rejected resume() is precisely
        // the case where no transition happened — so disarming here left
        // capture dead until the operator hid and re-showed the tab. Staying
        // armed costs one listener and buys a retry on the NEXT gesture.
        disarm();
      }).catch((e) => {
        console.warn("[tapscribe-bridge/page] resume() in gesture handler failed; " +
          "staying armed for the next gesture:", e);
      });
    };
    window.addEventListener("pointerdown", handler, true);
    window.addEventListener("keydown", handler, true);
    window.addEventListener("touchstart", handler, true);
  }

  async function ensureAudioGraph() {
    if (audioCtx) {
      await workletReady;
      return audioCtx;
    }
    audioCtx = new AudioContext({ sampleRate: 48000 });
    audioCtx.addEventListener("statechange", () => {
      const st = audioCtx.state;
      console.log("[tapscribe-bridge/page] AudioContext state: " + st);
      postCtxState(st);
      // "interrupted" is iOS-specific; treat it like suspended.
      if (st === "suspended" || st === "interrupted") {
        // Try a non-gesture resume() first — works on backgrounded tabs
        // that just went visible again. If the browser refuses, fall
        // back to the user-gesture path.
        audioCtx.resume().catch(() => armGestureRetry());
      }
    });
    const url = URL.createObjectURL(new Blob([WORKLET_SRC], { type: "application/javascript" }));
    workletReady = audioCtx.audioWorklet.addModule(url).finally(() => URL.revokeObjectURL(url));
    await workletReady;
    // Brand-new AudioContexts are often born `suspended` on Chrome
    // until a user gesture lands. Try once eagerly; the statechange
    // listener handles the fallback.
    if (audioCtx.state === "suspended" || audioCtx.state === "interrupted") {
      audioCtx.resume().catch(() => armGestureRetry());
    }
    postCtxState(audioCtx.state);
    return audioCtx;
  }

  // Tab back in focus → if we'd been auto-suspended, try resume eagerly.
  document.addEventListener("visibilitychange", () => {
    if (!audioCtx) return;
    if (document.visibilityState === "visible" &&
        (audioCtx.state === "suspended" || audioCtx.state === "interrupted")) {
      audioCtx.resume().catch(() => armGestureRetry());
    }
  });

  async function tap(participant, mediaStreamTrack) {
    // Guard on the TRACK, not just the identity. LiveKit's device switch
    // (operator picks a different mic) replaces `pub.track.mediaStreamTrack`
    // IN PLACE and fires no trackUnsubscribed, so an identity-only guard made
    // the 250 ms reconcile a no-op while our MediaStreamAudioSourceNode
    // stayed wrapped around the STOPPED track: the worklet kept emitting
    // frames of zeros, the byte counter climbed, the pill stayed green, and
    // the WAV recorded silence for the rest of the meeting.
    const existing = taps.get(participant.identity);
    if (existing && existing.track === mediaStreamTrack) return;
    if (!mediaStreamTrack || mediaStreamTrack.readyState !== "live") return;
    // A different, live track for an identity we're already tapping: tear the
    // old graph down first so the Utterance closes (its WAV finalises) and the
    // rebind below starts a fresh one.
    if (existing) {
      console.warn("[tapscribe-bridge/page] track replaced for " + participant.identity +
        "; rebinding the tap");
      untap(participant.identity);
    }

    // Every step from here through `new AudioWorkletNode` can throw
    // synchronously (CSP blocks blob: worklet, addModule rejects,
    // MediaStream ctor on a stopped track, worklet name not registered
    // after a prior addModule failure). Without a single envelope,
    // those throws escape into LiveKit's trackSubscribed handler and
    // the bridge dies silently with no signal to the popup — same
    // class of bug as the ws://-from-https:// SecurityError.
    let ctx, source, worklet, silentGain;
    try {
      ctx = await ensureAudioGraph();
      source = ctx.createMediaStreamSource(new MediaStream([mediaStreamTrack]));
      worklet = new AudioWorkletNode(ctx, "tapscribe-resampler");
      silentGain = ctx.createGain();
    } catch (e) {
      console.error("[tapscribe-bridge/page] tap setup failed for " + participant.identity, e);
      // Re-use the ctx-state channel so the popup banner fires. The
      // exact failure ends up in DevTools; the popup just needs to
      // stop showing "no taps" with zero context.
      postCtxState("failed");
      return;
    }
    silentGain.gain.value = 0;

    // Resolve the speaker label now so the FIRST tap-start carries it
    // (content.js puts ch.name into the /tap URL on the first PCM
    // frame). A `let` shadows the global `window.name` — a free
    // reference here would silently pick that up and break the label
    // all the way through to the dashboard.
    const name = getDisplayName(participant);
    const entry = {
      source,
      worklet,
      silentGain,
      name,
      resolvedName: name || "",
      nextNameRetryAtMs: 0,
      // The exact MediaStreamTrack this graph is wrapped around — the guard
      // at the top of tap() compares against it to detect a device switch.
      track: mediaStreamTrack,
      onTrackEnded: null,
    };

    // A track that ENDS (mic unplugged, permission revoked, device switched
    // away) keeps its MediaStreamAudioSourceNode alive and the worklet keeps
    // producing zeros — indistinguishable, from the recorder's side, from a
    // silent speaker. Nothing in LiveKit tells us, and reconcile can't
    // recover on its own (tap() rejects a non-live track), so listen for it:
    // untap closes the /tap WS (finalising the WAV instead of accruing
    // zeros), and the capture-failed signal gives content.js something to
    // surface rather than a green pill over silence.
    entry.onTrackEnded = () => {
      console.error("[tapscribe-bridge/page] media track ended for " + participant.identity +
        "; capture is dead until a live track is republished");
      postToContent({ kind: "capture-failed", identity: participant.identity, reason: "track-ended" });
      untap(participant.identity);
    };
    mediaStreamTrack.addEventListener("ended", entry.onTrackEnded);

    worklet.port.onmessage = (ev) => {
      const buf = ev.data.buffer;
      // The sidebar may not have rendered the user yet at tap time; retry
      // until it gives us a non-empty name, then stop querying the DOM.
      // Throttled to ~1/s: this handler fires per 20 ms PCM chunk (50x/s)
      // and getDisplayName is an O(document) sidebar scan, so an
      // unthrottled retry against a name that never resolves would scan
      // the MAIN-world DOM 50x/s for the tap's lifetime. The name is
      // cosmetic (content.js re-reads it per frame), so ~1 s lag is fine.
      if (!entry.resolvedName) {
        const now = performance.now();
        if (now >= entry.nextNameRetryAtMs) {
          entry.nextNameRetryAtMs = now + 1000;
          const n = getDisplayName(participant);
          if (n) entry.resolvedName = n;
        }
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
    if (!announced.has(participant.identity)) {
      announced.add(participant.identity);
      postToContent({ kind: "tap-start", identity: participant.identity, name });
    }
    console.log("[tapscribe-bridge/page] tapping " + participant.identity + " (" + (name || "no-name") + ")");
  }

  // Tear down every channel we know about — both audio-tapped and
  // presence-only — and emit tap-stop to content for each so the /tap
  // WS is closed on the recorder side. Used when LiveKit hands us a
  // room-wide teardown (disconnected event, window.room swap, polling
  // detected the room is no longer connected).
  function cleanupAllTaps() {
    for (const id of Array.from(announced)) untap(id);
  }

  function untap(identity) {
    const t = taps.get(identity);
    if (t) {
      // Detach first, so tearing the graph down can't re-enter through the
      // ended handler and so a later re-tap of the same track can't stack a
      // second listener on it.
      if (t.track && t.onTrackEnded) {
        // Only an exotic / proxied track object could throw here, and the
        // worst case is a stale listener on a track we're already dropping —
        // never a reason to abort the rest of the teardown below.
        try { t.track.removeEventListener("ended", t.onTrackEnded); } catch (e) {}
      }
      try { t.source.disconnect(); } catch (e) {}
      try { t.worklet.disconnect(); } catch (e) {}
      try { t.silentGain.disconnect(); } catch (e) {}
      taps.delete(identity);
      console.log("[tapscribe-bridge/page] untap " + identity);
    }
    // Always remove the channel from the popup, even if we never tapped
    // (presence-only entries created via announcePresence have no audio
    // graph to tear down but still need their popup row cleared).
    if (announced.delete(identity)) {
      postToContent({ kind: "tap-stop", identity });
    }
  }

  function setMute(identity, muted) {
    postToContent({ kind: "mute", identity, muted });
  }

  // Surface a participant in the popup even when their mic isn't being
  // tapped — typically because they joined the room muted and LiveKit
  // hasn't subscribed their audio publication yet. Posts tap-start so a
  // channel exists, then seeds the muted flag so the popup shows them
  // as "muted" instead of dropping them entirely.
  const announced = new Set();
  function announcePresence(participant, isMuted) {
    if (announced.has(participant.identity)) {
      // Refresh the muted flag in case it changed since last call.
      setMute(participant.identity, !!isMuted);
      return;
    }
    const name = getDisplayName(participant);
    announced.add(participant.identity);
    postToContent({ kind: "tap-start", identity: participant.identity, name });
    setMute(participant.identity, !!isMuted);
  }

  function attachListeners(room) {
    // LiveKit event names are camelCase strings (RoomEvent enum values).

    // Remote audio: subscribe / unsubscribe / mute lifecycle.
    room.on("trackSubscribed", (track, pub, participant) => {
      if (track.kind === "audio" && pub.source === "microphone") {
        tap(participant, track.mediaStreamTrack);
        // If they joined already muted, trackMuted will never fire — seed
        // the muted state here so the bridge waits for the unmute instead
        // of opening a /tap WS on the silence the worklet keeps emitting.
        if (pub.isMuted) setMute(participant.identity, true);
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

    // Late joiners — surface them in the popup right away so the operator
    // sees who's in the room. If they have a mic publication that's
    // muted we show them as "muted"; if they unmute, trackSubscribed +
    // trackUnmuted upgrade them to a real tap.
    room.on("participantConnected", (participant) => {
      announcePresence(participant, anyAudioPubMuted(participant));
    });
    room.on("participantDisconnected", (participant) => {
      untap(participant.identity);
    });

    room.on("disconnected", () => {
      console.warn("[tapscribe-bridge/page] room disconnected; tearing down taps");
      cleanupAllTaps();
    });

    // Existing participants and already-subscribed publications are picked
    // up by reconcile(), which the poll loop runs on every tick — including
    // the one immediately after this attach. Keeping the enumeration in one
    // place (reconcile) is what lets a missed trackSubscribed /
    // participantConnected event self-heal instead of stranding a speaker.
  }

  // True if the participant has at least one mic publication that is
  // currently muted. Used to seed the popup's "muted" pill at attach
  // time / participant-connected time, before any track event fires.
  function anyAudioPubMuted(participant) {
    if (!participant || !participant.audioTrackPublications) return false;
    for (const pub of participant.audioTrackPublications.values()) {
      if (pub.isMuted) return true;
    }
    return false;
  }

  // Bring one participant's bridge state in line with their CURRENT LiveKit
  // publications: surface them in the popup (presence row) the first time we
  // see them, and tap their mic track whenever it's subscribed and live.
  // Idempotent — announcePresence() / tap() early-return once the identity is
  // known — so reconcile() calls this every poll tick without re-emitting
  // tap-start (or flooding the mute / utterance-boundary channel). `isLocal`
  // applies the microphone-source filter the local-publication path has
  // always used; remote audio pubs are tapped regardless of source, matching
  // the original attach-time enumeration this replaced.
  function ensureParticipantTapped(participant, isLocal) {
    if (!participant || !participant.identity) return;
    if (!announced.has(participant.identity)) {
      announcePresence(participant, anyAudioPubMuted(participant));
    }
    if (!participant.audioTrackPublications) return;
    for (const pub of participant.audioTrackPublications.values()) {
      // Local pubs are published, not "subscribed", so we gate them on the
      // mic source instead of pub.isSubscribed (which is undefined for them).
      if (isLocal) {
        if (pub.source !== "microphone") continue;
      } else if (!pub.isSubscribed) {
        continue;
      }
      if (!pub.track || !pub.track.mediaStreamTrack) continue;
      const wasTapped = taps.has(participant.identity);
      tap(participant, pub.track.mediaStreamTrack);
      // Seed the "muted" pill only on the tap we just created; re-posting it
      // every 250 ms would spam the mute (utterance-boundary) channel.
      if (!wasTapped && pub.isMuted) setMute(participant.identity, true);
    }
  }

  // Self-healing membership sweep, run on every poll tick while attached to a
  // connected room. Event-driven tapping (trackSubscribed /
  // participantConnected / …) is necessary but NOT sufficient: LiveKit can
  // drop or coalesce those events across a reconnect, a proximity-driven
  // resubscribe in SpatialChat's spatial audio, or a race where a track
  // subscribes a beat after we attached. When that happens a speaker is
  // audible in the room yet never tapped — their audio never reaches the
  // recorder and they're missing from the popup, while everyone whose event
  // we DID catch shows up fine. Reconcile re-derives the truth from
  // room.remoteParticipants:
  //   - tap/announce every participant currently in the room (idempotent),
  //     recovering anyone whose subscribe/connect event we missed;
  //   - untap any identity we're still tracking that the room no longer
  //     lists, clearing ghost popup rows and closing the leaked /tap WS when
  //     a participantDisconnected / trackUnsubscribed event never arrived.
  function reconcile(room) {
    const present = new Set();
    for (const participant of room.remoteParticipants.values()) {
      present.add(participant.identity);
      ensureParticipantTapped(participant, false);
    }
    const lp = room.localParticipant;
    if (lp && lp.identity) {
      present.add(lp.identity);
      ensureParticipantTapped(lp, true);
    }
    // Drop anyone we're still tracking who has left the room. untap() guards
    // its tap-stop on announced.delete(), so a departed identity fires
    // exactly one tap-stop even though reconcile revisits it every tick.
    for (const id of Array.from(announced)) {
      if (!present.has(id)) untap(id);
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
          cleanupAllTaps();
          // Real room SWAP (a prior room existed) — tell content.js so it
          // can start a fresh recording session if the operator opted in.
          // Deliberately NOT fired on the first attach (else branch) or on
          // teardown, so opening the tab / leaving a room never rotates.
          postToContent({ kind: "room-changed", room: (room && room.name) || "" });
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
      // Re-derive membership from the room on every tick (not only on a
      // fresh attach) so a missed subscribe/connect event can't leave a
      // speaker untapped, nor a departed one lingering. Cheap and
      // idempotent — see reconcile().
      try {
        reconcile(room);
      } catch (e) {
        console.error("[tapscribe-bridge/page] reconcile failed:", e);
      }
    } else if (attachedRoom) {
      // window.room is no longer a connected room we can reconcile against.
      // Belt-and-braces with the "disconnected" event handler: tear our taps
      // down so the recorder isn't left holding stale streams. We must cover
      // TWO shapes, not just a terminal "disconnected":
      //   - the attached room flipped to "disconnected"; or
      //   - we LOST the room handle entirely — window.room was cleared (null)
      //     or swapped for a different/not-yet-connected instance — while the
      //     captured Room may still read "connected". SpatialChat clears
      //     window.room on leaving a space without a "disconnected" event, and
      //     that orphan slipped through the old `state !== "connected"` guard:
      //     reconcile() (which untaps leavers) stopped running AND no teardown
      //     fired, so live tracks kept posting PCM forever and every /tap WS
      //     leaked.
      // The ONE state to preserve is a transient "reconnecting": LiveKit keeps
      // the SAME instance and restores the same participants, so churning /tap
      // WSes on every blip (cutting active utterances) is wrong.
      const lostRoom = !room || room !== attachedRoom;
      if (attachedRoom.state === "disconnected" ||
          (lostRoom && attachedRoom.state !== "reconnecting")) {
        console.log("[tapscribe-bridge/page] room lost (" +
          (room ? "state=" + (room.state || "?") : "window.room cleared") +
          "); cleaning up taps");
        cleanupAllTaps();
        attachedRoom = null;
      }
    }
  }
  // Run immediately so we attach as soon as the room is up, then keep watching.
  maybeAttach();
  setInterval(maybeAttach, POLL_MS);
})();
