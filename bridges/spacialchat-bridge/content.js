// SpatialChat Bridge - content.js (ISOLATED world)
//
// Responsibilities:
//   - Inject page-script.js into the MAIN world.
//   - Listen for PCM frames + tap/mute lifecycle events posted from the
//     page world.
//   - Hold ONE /tap WebSocket per active speaker per utterance.
//   - Forward raw 16 kHz mono int16 PCM frames upstream.
//   - Close the /tap WS on TrackMuted (= end of utterance) so the
//     Recorder finalises its WAV. Open a fresh /tap WS on the next
//     non-muted PCM frame (= start of the next utterance).
//   - Survive network blips mid-utterance: if the /tap WS dies for any
//     reason while the speaker is still unmuted, schedule a reconnect
//     with jittered backoff, buffer up to ~3 s of PCM in the meantime,
//     and flush it on reconnect. The bridge reuses the same
//     `utterance_id` query param across reconnects so the Recorder
//     appends to the same WAV instead of producing a new file.
//   - Drain on mute: if the speaker mutes while there's still buffered
//     PCM (because the WS was reconnecting through a blip), don't
//     drop that audio. Keep the reconnect ladder running through the
//     mute, flush the buffer once a WS lands, then close cleanly so
//     the recorder still gets — and transcribes — the trailing
//     sound. Bounded by DRAIN_MAX_MS so an unreachable recorder can't
//     wedge the utterance indefinitely.
//   - Publish a status snapshot to chrome.storage so the popup can show
//     per-speaker state without needing DevTools, and update the tab
//     title with a tiny tx/state indicator.
//   - Render a small in-page status pill on the SpatialChat tab itself
//     so the operator can tell at a glance whether audio is flowing
//     without clicking the popup or watching the tab title — green
//     "streaming", yellow "reconnecting", red "refresh needed", etc.
//
// Wire contract (see bridges/README.md):
//   ws://<recorder-host>:8001/tap?identity=<id>&name=<display>&utterance_id=<uuid>
//   Binary frames, 20 ms each (320 samples = 640 bytes), int16 LE mono.
//   No JSON, no HTTP, no WhisperLiveKit awareness — the Recorder fans
//   audio out internally to its supervised WlK child and to disk.

(() => {
  if (window.__tapscribeBridgeContentInstalled) return;
  window.__tapscribeBridgeContentInstalled = true;

  // ---- Recorder host (configurable via popup) -------------------------------
  // Read at startup and kept live via chrome.storage.onChanged: popup edits
  // (host/port/token/TLS) apply immediately. Open /tap WSes are torn down
  // and reopened against the new settings; if the operator rotates the tap
  // token while a speaker is in a 4401-retry loop, the next retry uses the
  // fresh token without a SpatialChat tab reload.
  const TAP_SUBPROTOCOL_PREFIX = "tapscribe.v1.tap.";
  let recorderHost = "localhost";
  let recorderPort = 8001;
  let recorderUseTls = false;
  let tapToken = "";
  // Bracketed-meeting routing (#133). The popup mints a detached Session
  // ("Start meeting"), persists its server-minted id to
  // chrome.storage.local, and the content script reads it here as the live
  // source of truth for tap routing: while it is set, every /tap open and
  // reconnect carries `&session=<meetingSessionId>` so the meeting's audio
  // lands in its own Session. null → no meeting → taps fall back to the
  // Recorder's global Session, unchanged. Normalised to null-or-nonempty so
  // truthiness alone answers "is a meeting active?".
  let meetingSessionId = null;
  // End-meeting teardown state (#134). While `endingSessionId` is set, every
  // open channel is draining+closing toward a close-all barrier; once all
  // taps reach CLOSED the pipeline trigger fires for that id (captured before
  // meetingSessionId is cleared back to the global Session). Truthiness alone
  // answers "is an End in progress?".
  let endingSessionId = null;
  let settingsReady = false;
  const SETTINGS_KEYS = ["recorderHost", "recorderPort", "tapToken", "useTls", "meetingSessionId", "meetingActive"];
  chrome.storage.local.get(SETTINGS_KEYS).then((s) => {
    if (s && s.recorderHost) recorderHost = s.recorderHost;
    if (s && s.recorderPort) recorderPort = Number(s.recorderPort) || 8001;
    if (s && typeof s.tapToken === "string") tapToken = s.tapToken;
    if (s && typeof s.useTls === "boolean") recorderUseTls = s.useTls;
    // The stored meetingSessionId is DURABLE — it lingers after End as the
    // popup card's poll target. Only resume live tap routing into it if the
    // meeting is still active (meetingActive !== false), so a tab reload after
    // End doesn't re-home fresh taps into the just-ended Session. Absent
    // meetingActive (the only-id-was-written case) is treated as active.
    if (s && typeof s.meetingSessionId === "string" && s.meetingSessionId && s.meetingActive !== false) {
      meetingSessionId = s.meetingSessionId;
    }
    settingsReady = true;
    console.log(
      "[tapscribe-bridge] recorder: " + (recorderUseTls ? "wss://" : "ws://") +
      recorderHost + ":" + recorderPort + " (tap-token " + (tapToken ? "set" : "MISSING") + ")",
    );
    try { publishStatus(); } catch (e) { /* indicator may not be ready yet */ }
  }).catch((e) => {
    settingsReady = true; // fall back to default rather than block forever
    console.warn("[tapscribe-bridge] could not read recorder settings; using defaults", e);
    try { publishStatus(); } catch (e2) { /* ignore */ }
  });

  // Re-pick up popup edits without a tab reload. We update the in-memory
  // settings and tear down any in-flight /tap WS so the reconnect path
  // dials the new host/port and presents the new token. Active speakers
  // keep their utterance_id, so the recorder appends to the same WAV
  // when the new connection lands on the same recorder.
  if (chrome.storage && chrome.storage.onChanged) {
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area !== "local") return;
      let touched = false;
      if (changes.recorderHost) {
        recorderHost = changes.recorderHost.newValue || "localhost";
        touched = true;
      }
      if (changes.recorderPort) {
        recorderPort = Number(changes.recorderPort.newValue) || 8001;
        touched = true;
      }
      if (changes.tapToken) {
        tapToken = typeof changes.tapToken.newValue === "string" ? changes.tapToken.newValue : "";
        touched = true;
      }
      if (changes.useTls) {
        recorderUseTls = !!changes.useTls.newValue;
        touched = true;
      }
      if (changes.meetingSessionId) {
        // The popup started or ended a bracketed meeting. This only changes
        // where NEW utterances route (their affiliation is snapshotted at
        // utterance start), so no open WS is affected — don't set `touched`
        // / trigger a reconnect. publishStatus so the pill reflects it.
        const v = changes.meetingSessionId.newValue;
        meetingSessionId = (typeof v === "string" && v) ? v : null;
        publishStatus();
      }
      if (changes.meetingEndRequestedAt) {
        // The popup clicked "End meeting": drain + close every open tap, then
        // trigger the end-of-meeting pipeline. Gated on settingsReady so the
        // trigger uses the real recorder config (not boot defaults).
        if (settingsReady) endMeeting();
      }
      if (!touched) return;
      console.log(
        "[tapscribe-bridge] settings updated live: " + (recorderUseTls ? "wss://" : "ws://") +
        recorderHost + ":" + recorderPort + " (tap-token " + (tapToken ? "set" : "MISSING") + ")",
      );
      reconnectAllForSettingsChange();
    });
  }

  function reconnectAllForSettingsChange() {
    for (const [identity, ch] of channels) {
      // Sticky configuration errors (tls-required, tap-auth-failed)
      // can only be cleared by a settings change. Drop them so the
      // next PCM frame redials with the fresh settings.
      if (ch.error === "tls-required" || ch.error === "tap-auth-failed") {
        ch.error = null;
      }
      const hadWs = !!ch.tapWs;
      const hadTimer = ch.reconnectTimer !== null;
      if (!hadWs && !hadTimer) continue;
      clearReconnectTimer(ch);
      const w = ch.tapWs;
      ch.tapWs = null;
      if (w && (w.readyState === WebSocket.OPEN || w.readyState === WebSocket.CONNECTING)) {
        try { w.close(1000, "settings changed"); } catch (e) {}
      }
      // Restart the reconnect ladder so the first retry fires quickly
      // (~200ms) with the new settings, but only while the speaker is
      // actually mid-utterance — or finishing draining one. Also refresh
      // the drain timer so the new host gets a full DRAIN_MAX_MS window.
      if (shouldReconnect(ch)) {
        ch.reconnectAttempt = 0;
        if (ch.draining) restartDrainTimer(identity, ch);
        scheduleReconnect(identity, ch);
      }
    }
    publishStatus();
  }

  const tapWsUrl = (identity, name, utteranceId, sessionId) => {
    const qp = new URLSearchParams({
      identity,
      name: name || "",
      utterance_id: utteranceId,
    });
    // Route into the meeting's detached Session when this utterance was
    // affiliated to one (snapshotted at utterance start; see ensureUtterance).
    // Absent → the Recorder uses its global Session, unchanged.
    if (sessionId) qp.set("session", sessionId);
    const scheme = recorderUseTls ? "wss" : "ws";
    return scheme + "://" + recorderHost + ":" + recorderPort + "/tap?" + qp.toString();
  };

  // The recorder config the shared control-client takes, snapshotted from the
  // live in-memory settings at call time. Used by the end-of-meeting pipeline
  // trigger — the one control-plane HTTP call the content script makes.
  const recorderCfg = () => ({
    host: recorderHost,
    port: recorderPort,
    useTls: recorderUseTls,
    token: tapToken,
  });

  // Construct the WebSocket with the tap-token carried via subprotocol
  // when the operator set one. With --no-auth the recorder ignores
  // subprotocols entirely; we omit it so the upgrade succeeds without
  // server-side echo.
  const openWs = (url) => {
    if (tapToken) return new WebSocket(url, [TAP_SUBPROTOCOL_PREFIX + tapToken]);
    return new WebSocket(url);
  };

  // ws:// to a "potentially trustworthy" host (Chrome/Edge's term) is
  // allowed from an https:// page; anything else gets blocked by mixed-
  // content policy — `new WebSocket(ws://...)` throws SecurityError
  // synchronously, which would otherwise escape out of the PCM handler on
  // every frame and leave /tap stuck at "idle". The host predicate itself
  // lives in control-client.js's TapscribeControlClient.isTrustworthyHost —
  // shared with the HTTP control plane's mixed-content guard so the two
  // never drift apart (#251).
  function wouldBeMixedContentBlocked() {
    if (recorderUseTls) return false;
    if (typeof location === "undefined") return false;
    if (location.protocol !== "https:") return false;
    return !TapscribeControlClient.isTrustworthyHost(recorderHost);
  }

  // Backoff: jittered exponential, capped. Indexed by ch.reconnectAttempt
  // (0 = first retry). The cap matches what feels reasonable for an
  // operator watching the popup — long enough not to hammer a downed
  // recorder, short enough that recovery feels live.
  const BACKOFF_MS = [200, 400, 800, 1600, 3200];
  const BACKOFF_CAP_MS = 5000;
  // ~3 s of int16 mono 16 kHz audio = 96000 bytes. We drop the OLDEST
  // buffered frames when this is exceeded so a long outage doesn't
  // grow without bound. Choosing 3 s means a typical reconnect cycle
  // preserves all audio; a multi-second outage loses only the tail.
  const MAX_BUFFER_BYTES = 96000;
  // Max time we'll keep the utterance alive after a mute, waiting for a
  // /tap WS to come up so we can flush the trailing buffered PCM. Past
  // this point the trailing audio is lost — but we'd rather give up than
  // wedge an utterance forever against an unreachable recorder.
  const DRAIN_MAX_MS = 8000;

  // Begin an utterance for `ch` if one isn't already in flight: mint the
  // utterance_id the Recorder uses to stitch reconnects together, and
  // snapshot the meeting affiliation. The affiliation is fixed for the
  // life of the utterance — a meeting that starts or ends mid-utterance
  // doesn't re-route the open utterance; the next one picks up the change.
  function ensureUtterance(ch) {
    if (ch.utteranceId) return;
    ch.utteranceId = newUtteranceId();
    ch.sessionId = meetingSessionId; // null when no meeting is active
  }

  function newUtteranceId() {
    if (crypto && typeof crypto.randomUUID === "function") {
      return crypto.randomUUID().replace(/-/g, "");
    }
    // Fallback for older environments — not strictly UUID but unique enough.
    return Math.random().toString(36).slice(2) + Date.now().toString(36);
  }

  function nextBackoffMs(attempt) {
    const base = attempt < BACKOFF_MS.length ? BACKOFF_MS[attempt] : BACKOFF_CAP_MS;
    // ±25% jitter so a roomful of reconnecting bridges doesn't synchronise.
    const jitter = 1 + (Math.random() - 0.5) * 0.5;
    return Math.min(BACKOFF_CAP_MS, Math.round(base * jitter));
  }

  // ---- Inject page-script.js -------------------------------------------------
  const pageScriptUrl = chrome.runtime.getURL("page-script.js");
  const s = document.createElement("script");
  s.src = pageScriptUrl;
  s.async = false;
  s.onload = () => s.remove();
  (document.head || document.documentElement).appendChild(s);
  console.log("[tapscribe-bridge] injected page-script.js");

  // ---- State -----------------------------------------------------------------
  /**
   * identity -> {
   *   tapWs:           WebSocket | null,
   *   utteranceId:     string | null,        // stable across reconnects within an utterance
   *   reconnectAttempt:number,
   *   reconnectTimer:  number | null,
   *   buffer:          ArrayBuffer[],         // pending PCM during gap
   *   bufferBytes:     number,
   *   framesSent:      number,
   *   bytesSent:       number,
   *   muted:           boolean,
   *   stopped:         boolean,               // tap-stop has fired; do not reconnect
   *   error:           string | null,
   *   name:            string,
   * }
   */
  const channels = new Map();
  let origTitle = null;
  // Last known state of the page-world AudioContext. "running" is the
  // happy path; "suspended" / "interrupted" / "closed" mean the worklet
  // is producing silence and the bridge would silently capture nothing.
  // Surfaced in the status snapshot and title pill so the operator
  // notices instead of staring at a stuck "0 frames" counter.
  let audioContextState = null;

  function ensureChannel(identity, name) {
    let ch = channels.get(identity);
    if (ch) {
      if (name && name !== ch.name) ch.name = name;
      return ch;
    }
    ch = {
      tapWs: null,
      utteranceId: null,
      // Meeting Session this utterance is affiliated to, snapshotted at
      // utterance start (ensureUtterance). Reused across reconnects so the
      // whole utterance lands in ONE Session — matching the Recorder
      // snapshotting Session at WS open and stitching reconnects by
      // utterance_id. null → routes to the global Session.
      sessionId: null,
      reconnectAttempt: 0,
      reconnectTimer: null,
      buffer: [],
      bufferBytes: 0,
      framesSent: 0,
      bytesSent: 0,
      muted: false,
      stopped: false,
      error: null,
      name: name || "",
      // Set true once /tap has opened at least once in the current
      // utterance. Distinguishes "lost audio mid-utterance because the
      // link flapped" (buffer-overflow) from "recorder isn't reachable
      // at all, never connected" (recorder-unreachable). Reset by
      // endUtterance().
      everOpened: false,
      // True between mute and the trailing PCM actually reaching the
      // recorder. While set, reconnect attempts continue (despite
      // ch.muted) and the next onopen flushes-and-closes instead of
      // waiting for fresh PCM. drainTimer caps the wait at DRAIN_MAX_MS.
      draining: false,
      drainTimer: null,
      drainReason: null,
    };
    channels.set(identity, ch);
    return ch;
  }

  // ---- /tap WS (per utterance, resilient across blips) ----------------------
  // Errors that describe the underlying transport failure. Buffer-overflow
  // is a *consequence* of one of these, so we don't overwrite them.
  function isTransportError(err) {
    if (!err) return false;
    return (
      err === "tap-ws-error" ||
      err === "tap-auth-failed" ||
      err.startsWith("tap-ws-closed-")
    );
  }

  function bufferPush(ch, buf) {
    ch.buffer.push(buf);
    ch.bufferBytes += buf.byteLength;
    while (ch.bufferBytes > MAX_BUFFER_BYTES && ch.buffer.length > 0) {
      const dropped = ch.buffer.shift();
      ch.bufferBytes -= dropped.byteLength;
      // Pick the most diagnostic label:
      //   - keep any real transport error (tap-ws-closed-1006, etc.)
      //   - if we never managed to open the WS this utterance, the
      //     recorder is unreachable, not the buffer at fault
      //   - otherwise (had a successful open, link flapped) it is a
      //     genuine buffer overflow during a mid-utterance gap
      const nextErr = isTransportError(ch.error)
        ? ch.error
        : (ch.everOpened ? "buffer-overflow" : "recorder-unreachable");
      if (ch.error !== nextErr) {
        ch.error = nextErr;
        console.warn(
          "[tapscribe-bridge] dropping oldest PCM frame (" + nextErr + "); " +
          "outage exceeded " + Math.round(MAX_BUFFER_BYTES / 32) + "ms",
        );
      }
    }
  }

  function bufferFlush(ch) {
    // Called on onopen. Sends everything we have, oldest first.
    if (ch.buffer.length === 0) return;
    const ws = ch.tapWs;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    let flushed = 0;
    while (ch.buffer.length > 0) {
      const buf = ch.buffer.shift();
      ch.bufferBytes -= buf.byteLength;
      try {
        ws.send(buf);
        ch.framesSent++;
        ch.bytesSent += buf.byteLength;
        flushed++;
      } catch (e) {
        // Put it back at the head; reconnect path will try again.
        ch.buffer.unshift(buf);
        ch.bufferBytes += buf.byteLength;
        ch.error = "tap-send-failed";
        console.error("[tapscribe-bridge] flush failed; will retry on reconnect", e);
        return;
      }
    }
    if (flushed > 0) {
      console.log(
        "[tapscribe-bridge] flushed " + flushed + " buffered frame(s) after reconnect",
      );
    }
  }

  function clearReconnectTimer(ch) {
    if (ch.reconnectTimer !== null) {
      clearTimeout(ch.reconnectTimer);
      ch.reconnectTimer = null;
    }
  }

  // Drain is the one case where we keep reconnecting despite ch.muted —
  // the trailing buffered PCM still needs a WS to land on so the
  // recorder can transcribe it.
  function shouldReconnect(ch) {
    return !ch.stopped && !!ch.utteranceId && (!ch.muted || ch.draining);
  }

  function scheduleReconnect(identity, ch) {
    if (!shouldReconnect(ch)) return;
    if (ch.reconnectTimer !== null) return;
    const delay = nextBackoffMs(ch.reconnectAttempt);
    ch.reconnectAttempt++;
    console.log(
      "[tapscribe-bridge] /tap reconnect for " + identity +
      " in " + delay + "ms (attempt " + ch.reconnectAttempt + ")" +
      (ch.draining ? " [draining]" : ""),
    );
    ch.reconnectTimer = setTimeout(() => {
      ch.reconnectTimer = null;
      // State might have changed while we waited.
      if (!shouldReconnect(ch)) return;
      if (ch.tapWs) return;
      openTapWs(identity, ch);
    }, delay);
    publishStatus();
  }

  function openTapWs(identity, ch) {
    // Pre-flight: ws:// to a non-trustworthy host from an https:// page
    // is blocked by the browser's mixed-content policy. `new WebSocket`
    // throws SecurityError synchronously here, so detect-and-surface
    // before we dial. Retrying is pointless until the operator either
    // enables TLS on the recorder or points the bridge at localhost.
    if (wouldBeMixedContentBlocked()) {
      if (ch.error !== "tls-required") {
        ch.error = "tls-required";
        console.error(
          "[tapscribe-bridge] cannot dial ws:// to " + recorderHost +
          " from " + location.origin + " — enable TLS on the recorder " +
          "and tick \"Use TLS\" in the popup, or run the recorder on localhost.",
        );
        publishStatus();
      }
      return;
    }
    ensureUtterance(ch);
    const url = tapWsUrl(identity, ch.name, ch.utteranceId, ch.sessionId);
    console.log(
      "[tapscribe-bridge] opening /tap for " + identity +
      " utt=" + ch.utteranceId.slice(0, 8) + " -> " + url,
    );
    ch.framesSent = 0;
    let ws;
    try {
      ws = openWs(url);
    } catch (e) {
      // Defensive: anything that makes `new WebSocket` throw (malformed
      // url, mixed-content slipping past the pre-flight, exotic browser
      // policy) lands here instead of escaping the PCM handler.
      ch.error = "tap-ws-construct-failed";
      console.error(
        "[tapscribe-bridge] /tap WS construct failed for " + identity, e,
      );
      publishStatus();
      return;
    }
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      console.log("[tapscribe-bridge] /tap open for " + identity +
        (ch.draining ? " [draining]" : ""));
      ch.reconnectAttempt = 0;
      ch.everOpened = true;
      if (ch.error) ch.error = null;
      bufferFlush(ch);
      // If we only reopened to drain trailing PCM after a mute, finalise
      // the utterance now that the buffer has been handed off. Closing
      // cleanly is the recorder's "end of utterance" signal.
      if (ch.draining) {
        finalizeDrain(identity, ch);
      }
      publishStatus();
    };
    ws.onerror = () => {
      ch.error = "tap-ws-error";
      console.error("[tapscribe-bridge] /tap ws error for " + identity);
      publishStatus();
    };
    ws.onclose = (ev) => {
      // 1000 = clean close (we closed it on mute / tap-stop / paused).
      // 4401 = the recorder rejected our tap token. Anything else means
      // the link died or the recorder rejected us for another reason.
      const clean = ev.wasClean && ev.code === 1000;
      const authFailed = ev.code === 4401;
      if (!clean) {
        if (authFailed) {
          ch.error = "tap-auth-failed";
          console.error(
            "[tapscribe-bridge] /tap auth failed for " + identity +
            " — check the tap token in the popup",
          );
        } else {
          ch.error = "tap-ws-closed-" + ev.code;
          console.error(
            "[tapscribe-bridge] /tap ws closed for " + identity +
            " code=" + ev.code + " reason=" + (ev.reason || "<none>"),
          );
        }
      }
      if (ch.tapWs === ws) ch.tapWs = null;
      // Reconnect only if the speaker is still active and the close was
      // recoverable. A clean close (operator pause, mute, tap-stop) means
      // the utterance is over; an auth rejection won't fix itself by
      // retrying — leave it surfaced so the operator updates the token.
      // shouldReconnect carves out the drain-after-mute exception.
      if (!clean && !authFailed && shouldReconnect(ch)) {
        scheduleReconnect(identity, ch);
      }
      publishStatus();
    };
    ch.tapWs = ws;
  }

  function closeTapWs(identity, ch, reason) {
    clearReconnectTimer(ch);
    const w = ch.tapWs;
    ch.tapWs = null;
    if (w && (w.readyState === WebSocket.OPEN || w.readyState === WebSocket.CONNECTING)) {
      try { w.close(1000, reason || "end of utterance"); } catch (e) {}
    }
  }

  function clearDrainTimer(ch) {
    if (ch.drainTimer !== null) {
      clearTimeout(ch.drainTimer);
      ch.drainTimer = null;
    }
  }

  function resetUtteranceState(ch) {
    ch.utteranceId = null;
    ch.sessionId = null;
    ch.reconnectAttempt = 0;
    ch.buffer = [];
    ch.bufferBytes = 0;
    ch.everOpened = false;
    ch.draining = false;
    ch.drainReason = null;
    clearDrainTimer(ch);
  }

  // Hard cap on how long we'll wait for the trailing buffered PCM to
  // reach a /tap WS after the speaker muted. Past this point the audio
  // is lost — but we'd rather give up and surface an error than wedge
  // the utterance forever against an unreachable recorder.
  function restartDrainTimer(identity, ch) {
    clearDrainTimer(ch);
    ch.drainTimer = setTimeout(() => {
      ch.drainTimer = null;
      console.warn("[tapscribe-bridge] drain timeout for " + identity +
        "; discarding " + ch.bufferBytes + " buffered byte(s)");
      // Surface the loss so the popup pill flips to an error rather
      // than silently dropping back to "muted".
      ch.error = "drain-timeout";
      closeTapWs(identity, ch, "drain timeout");
      resetUtteranceState(ch);
      publishStatus();
      // Give up on this tap's tail audio — but it's still CLOSED now, so the
      // End-meeting barrier shouldn't wait on it forever.
      if (endingSessionId) maybeFinishEndMeeting();
    }, DRAIN_MAX_MS);
  }

  // Called from the WS onopen handler once we've flushed the buffered
  // PCM that was waiting for a transport. The buffer is now on the wire
  // (or in the browser's WS send queue, which close() will drain before
  // the FIN), so a clean close is the recorder's "end of utterance"
  // signal and trailing transcripts will still arrive.
  function finalizeDrain(identity, ch) {
    const reason = ch.drainReason || "muted";
    console.log("[tapscribe-bridge] drain complete for " + identity +
      "; closing /tap (reason=" + reason + ")");
    closeTapWs(identity, ch, reason);
    resetUtteranceState(ch);
    // A draining channel finishing may be the last tap the End-meeting
    // close-all barrier was waiting on.
    if (endingSessionId) maybeFinishEndMeeting();
  }

  // Force-close the utterance and reset state, regardless of any
  // pending buffer. Used when there's nothing to drain (empty buffer)
  // or when draining would be pointless (tap-stop = speaker is gone).
  function endUtteranceImmediate(identity, ch, reason) {
    closeTapWs(identity, ch, reason);
    resetUtteranceState(ch);
  }

  // Mute ends an utterance. The naive version closes the WS and clears
  // the buffer immediately — but if a network blip left PCM in
  // ch.buffer with the WS still reconnecting, that trailing audio (and
  // its transcript) would be lost. Instead: if there's nothing to
  // drain, close immediately; if the WS is open with leftover buffer,
  // flush-and-close synchronously; otherwise enter drain mode so the
  // reconnect ladder can land a WS, flush, and only then close.
  function endUtterance(identity, ch, reason) {
    const hasBuffered = ch.buffer.length > 0;
    const ws = ch.tapWs;
    const wsOpen = ws && ws.readyState === WebSocket.OPEN;

    if (!hasBuffered) {
      endUtteranceImmediate(identity, ch, reason);
      return;
    }

    if (wsOpen) {
      bufferFlush(ch);
      endUtteranceImmediate(identity, ch, reason);
      return;
    }

    console.log("[tapscribe-bridge] mute with " + ch.bufferBytes +
      " buffered byte(s) for " + identity + "; draining before close");
    ch.draining = true;
    ch.drainReason = reason;
    // Reset the backoff ladder so the drain gets a fresh fast retry
    // (~200ms) instead of inheriting a long delay from prior failed
    // attempts and burning the DRAIN_MAX_MS budget waiting.
    ch.reconnectAttempt = 0;
    restartDrainTimer(identity, ch);

    if (!ch.tapWs && ch.reconnectTimer === null) {
      scheduleReconnect(identity, ch);
    }
  }

  // ---- End meeting: drain → close-all barrier → pipeline trigger (#134) ------
  // Publish the end-of-meeting outcome to chrome.storage so the (ephemeral)
  // popup can show "ending" / "started" / "Recorder busy" / "failed" — the
  // content script owns the sequence because it outlives the popup.
  function publishMeetingEnd(phase, sessionId, error) {
    try {
      chrome.storage.local.set({
        meetingEnd: { phase, sessionId, error: error || null, ts: Date.now() },
      });
    } catch (e) { /* best-effort; absence just means no end-state to show */ }
  }

  // Begin the End-meeting teardown. Idempotent: a stale/duplicate request
  // (or one with no active meeting) is a no-op. Closes every open channel
  // through the existing Drain-on-mute path — fresh utterances mid-teardown
  // are blocked by the endingSessionId gate in the "pcm" handler, so we do
  // NOT force-mute here (that would corrupt the platform mute mirror
  // finishEndMeeting preserves) — then waits for the close-all barrier.
  function endMeeting() {
    if (!meetingSessionId || endingSessionId) return;
    endingSessionId = meetingSessionId;
    publishMeetingEnd("ending", endingSessionId);
    console.log("[tapscribe-bridge] end meeting " + endingSessionId +
      "; draining " + channels.size + " channel(s)");
    for (const [identity, ch] of channels) {
      // Don't force ch.muted here: it mirrors the platform's TRUE mute state,
      // and finishEndMeeting preserves that mirror so a still-muted speaker
      // stays gated after End (forcing it true would also gate a speaker who
      // is still talking). New utterances mid-teardown are already blocked by
      // the endingSessionId gate in the "pcm" handler, and the reconnect ladder
      // gates on utteranceId/draining — not on ch.muted — so the drain still
      // finishes.
      endUtterance(identity, ch, "meeting ended");
    }
    publishStatus();
    // Synchronous closes are already done; channels still draining will
    // finish via finalizeDrain / the drain timeout. Check the barrier now in
    // case nothing needed draining (or there were no open taps at all).
    maybeFinishEndMeeting();
  }

  // The close-all barrier: fire the trigger only once EVERY tap has reached
  // CLOSED (no live/connecting/closing WS, none draining or mid-reconnect),
  // so the last Utterance's WAV is finalised before processing starts.
  function maybeFinishEndMeeting() {
    if (!endingSessionId) return;
    for (const [, ch] of channels) {
      const ws = ch.tapWs;
      const wsLive = ws && (
        ws.readyState === WebSocket.OPEN ||
        ws.readyState === WebSocket.CONNECTING ||
        ws.readyState === WebSocket.CLOSING
      );
      if (wsLive || ch.draining || ch.reconnectTimer !== null) return; // not all CLOSED yet
    }
    finishEndMeeting();
  }

  function finishEndMeeting() {
    const sessionId = endingSessionId;
    endingSessionId = null;
    // The meeting's taps are all closed. Reset each channel to a fresh,
    // meeting-less state so a later speaker starts a new utterance routed to
    // the global Session — but PRESERVE each channel's true mute state (and
    // name). A speaker muted at End keeps emitting worklet PCM; the old
    // channels.clear() dropped that mute, so the silence sailed past the pcm
    // gate and opened a ghost tap into the global Session. A departed speaker
    // (tap-stop → stopped) is dropped so a rejoin starts clean.
    for (const [identity, ch] of channels) {
      if (ch.stopped) {
        channels.delete(identity);
        continue;
      }
      resetUtteranceState(ch); // clears utteranceId/sessionId(→global)/buffer/timers
      ch.framesSent = 0;
      ch.bytesSent = 0;
      ch.error = null;
      // ch.muted (true platform state) and ch.name are deliberately preserved.
    }
    // Routing falls back to the global Session now: clear the IN-MEMORY id so
    // new utterances carry no session param. But KEEP the stored
    // meetingSessionId — it's the popup card's durable poll target (it polls
    // the pipeline/summary even after the popup closed or the Recorder
    // restarted). Only mark the meeting no longer active; the stored id is
    // cleared on the next Start meeting or an explicit Dismiss.
    meetingSessionId = null;
    try { chrome.storage.local.set({ meetingActive: false }); } catch (e) { /* best-effort */ }
    console.log("[tapscribe-bridge] all taps CLOSED; triggering pipeline for " + sessionId);
    // Fire the end-of-meeting pipeline. No body: the Recorder uses
    // operator-configured defaults, so a low-privilege tap token can't choose
    // the model. 409 = Session busy → surface it; do NOT auto-hammer.
    TapscribeControlClient.triggerPipeline(recorderCfg(), sessionId)
      .then((res) => {
        if (res.outcome === "busy") {
          console.warn("[tapscribe-bridge] pipeline trigger: recorder busy (409) for " + sessionId);
          publishMeetingEnd("busy", sessionId);
        } else {
          publishMeetingEnd("started", sessionId);
        }
      })
      .catch((e) => {
        const reason = (e && e.kind === "mixed-content-blocked")
          ? "recorder is http:// on a non-trustworthy host — enable TLS"
          : String((e && e.message) || e);
        console.warn("[tapscribe-bridge] pipeline trigger failed for " + sessionId + ": " + reason);
        publishMeetingEnd("failed", sessionId, reason);
      });
    publishStatus();
  }

  // ---- Message handler from page world --------------------------------------
  window.addEventListener("message", (ev) => {
    if (ev.source !== window) return;
    const d = ev.data;
    if (!d || d.source !== "tapscribe-bridge") return;

    switch (d.kind) {
      case "tap-start": {
        console.log("[tapscribe-bridge] tap-start " + d.identity + " (" + (d.name || "?") + ")");
        const ch = ensureChannel(d.identity, d.name);
        ch.stopped = false;
        publishStatus();
        break;
      }
      case "tap-stop": {
        const ch = channels.get(d.identity);
        if (ch) {
          ch.stopped = true;
          // Tap-stop means the speaker is gone entirely (left the room,
          // track unsubscribed). There's no point draining trailing
          // PCM: even if we landed a WS, the operator doesn't expect a
          // late transcript from a departed speaker. Force close.
          endUtteranceImmediate(d.identity, ch, "tap stopped");
          channels.delete(d.identity);
          console.log("[tapscribe-bridge] tap-stop " + d.identity);
          publishStatus();
        }
        break;
      }
      case "room-changed": {
        // The bracketed-meeting model (#133) replaced the legacy global
        // rotate: a SpatialChat room change performs NO Session action. An
        // active meeting's detached Session persists across room swaps until
        // the user ends it; with no meeting active, taps keep falling back
        // to the global Session. Kept as an explicit no-op so the next
        // contributor sees the deliberate decision rather than wondering
        // where the old auto-rotate went.
        break;
      }
      case "mute": {
        const ch = channels.get(d.identity);
        if (ch) {
          const wasMuted = ch.muted;
          ch.muted = !!d.muted;
          if (ch.muted && !wasMuted) {
            // Mute is the "end of utterance" signal — drain any trailing
            // PCM still in our local buffer so the recorder gets the
            // whole utterance, then close cleanly to finalise the WAV.
            endUtterance(d.identity, ch, "muted");
          } else if (!ch.muted && wasMuted && ch.draining) {
            // Speaker is talking again before the previous utterance
            // finished draining. Abandon the drain: tear down anything
            // that was still pending so the next PCM frame starts a
            // fresh utterance with a new utterance_id.
            console.log("[tapscribe-bridge] unmute " + d.identity +
              " mid-drain; abandoning drain");
            endUtteranceImmediate(d.identity, ch, "unmuted mid-drain");
          }
          console.log("[tapscribe-bridge] mute " + d.identity + " -> " +
            (ch.muted ? (ch.draining ? "draining /tap" : "closed /tap") : "unmuted"));
          publishStatus();
        }
        break;
      }
      case "pcm": {
        // While an End is in progress, ignore new audio. The meeting's taps
        // are muted and draining toward the close-all barrier; a fresh tap
        // opened here (a new speaker, or a new utterance) would route into
        // the Session we're about to process and keep the barrier from ever
        // completing — leaving the meeting wedged in "Ending…".
        if (endingSessionId) return;
        const ch = ensureChannel(d.identity, d.name);
        if (d.name && d.name !== ch.name) ch.name = d.name;
        if (ch.muted) return;

        // Defer the first WS open until settings have loaded, so we don't
        // connect to the default host and then have to throw it away.
        if (!settingsReady) return;

        // First frame of a new utterance — mint the id the recorder will
        // use to stitch reconnects together and snapshot the meeting routing.
        ensureUtterance(ch);

        if (!ch.tapWs && ch.reconnectTimer === null) {
          openTapWs(d.identity, ch);
        }

        if (ch.tapWs && ch.tapWs.readyState === WebSocket.OPEN) {
          if (ch.tapWs.bufferedAmount > 1_000_000) {
            if (ch.error !== "backpressure") {
              ch.error = "backpressure";
              console.warn(
                "[tapscribe-bridge] backpressure for " + d.identity +
                " buffered=" + ch.tapWs.bufferedAmount,
              );
              publishStatus();
            }
          } else {
            try {
              ch.tapWs.send(d.buffer);
              ch.framesSent++;
              ch.bytesSent += d.buffer.byteLength;
              // Clear transient errors after a successful send. Real link
              // failures will be re-set by onerror/onclose.
              if (
                ch.error === "tap-send-failed" ||
                ch.error === "backpressure" ||
                ch.error === "buffer-overflow" ||
                ch.error === "recorder-unreachable"
              ) {
                ch.error = null;
              }
            } catch (e) {
              ch.error = "tap-send-failed";
              console.error("[tapscribe-bridge] /tap send failed for " + d.identity, e);
              // Buffer the frame; the close handler will schedule reconnect.
              bufferPush(ch, d.buffer);
            }
          }
        } else {
          // CONNECTING, or waiting on a reconnect timer — keep audio
          // around so we can flush it once the WS comes up.
          bufferPush(ch, d.buffer);
        }
        break;
      }
      case "ctx-state": {
        const next = typeof d.state === "string" ? d.state : null;
        if (audioContextState !== next) {
          audioContextState = next;
          if (next && next !== "running") {
            console.warn(
              "[tapscribe-bridge] AudioContext is " + next +
              " — capture is paused until the tab gets a user gesture",
            );
          }
          publishStatus();
        }
        break;
      }
      default: {
        // Unknown kind; ignore.
      }
    }
  });

  // ---- In-page status pill --------------------------------------------------
  // A fixed-position pill rendered on the SpatialChat page itself, so the
  // operator can see "is audio flowing right now?" without opening the
  // popup or staring at the tab title. Lives in a shadow root so the
  // page's CSS can't restyle or hide it. The host element is appended to
  // <html> rather than <body> so SpatialChat's SPA router (which often
  // swaps <body>'s subtree) doesn't blow it away on route changes.
  const INDICATOR_HOST_ID = "__tapscribe_indicator_host__";
  let indicatorHost = null;
  let indicatorPill = null;
  let indicatorDot = null;
  let indicatorLabel = null;

  function buildIndicator() {
    let host, pill, dot, label;
    try {
      host = document.createElement("div");
      host.id = INDICATOR_HOST_ID;
      // `all:initial` resets every inherited property from the page so
      // SpatialChat's global CSS can't reach in and recolor / resize us.
      host.style.cssText =
        "all:initial;position:fixed;bottom:12px;right:12px;z-index:2147483647;";
      const shadow = host.attachShadow
        ? host.attachShadow({ mode: "open" })
        : host;
      const style = document.createElement("style");
      style.textContent =
        ".pill{display:flex;align-items:center;gap:6px;padding:5px 10px;" +
        "background:rgba(20,20,20,0.85);color:#fff;border-radius:14px;" +
        "font:12px/1.2 system-ui,-apple-system,Segoe UI,sans-serif;" +
        "box-shadow:0 2px 6px rgba(0,0,0,0.25);user-select:none;cursor:default}" +
        ".dot{display:inline-block;width:8px;height:8px;border-radius:50%;" +
        "background:#888;flex:0 0 8px}" +
        ".pill.ok .dot{background:#2c2}" +
        ".pill.warn .dot{background:#fa3}" +
        ".pill.err .dot{background:#e44}";
      pill = document.createElement("div");
      pill.className = "pill idle";
      dot = document.createElement("span");
      dot.className = "dot";
      label = document.createElement("span");
      label.className = "label";
      label.textContent = "TapScribe";
      pill.appendChild(dot);
      pill.appendChild(label);
      shadow.appendChild(style);
      shadow.appendChild(pill);
    } catch (e) {
      return false;
    }
    indicatorHost = host;
    indicatorPill = pill;
    indicatorDot = dot;
    indicatorLabel = label;
    return true;
  }

  function ensureIndicatorMounted() {
    if (typeof document === "undefined") return false;
    const root = document.documentElement;
    if (!root) return false;
    if (
      indicatorHost &&
      typeof root.contains === "function" &&
      root.contains(indicatorHost)
    ) {
      return true;
    }
    if (!indicatorHost && !buildIndicator()) return false;
    try {
      root.appendChild(indicatorHost);
    } catch (e) {
      return false;
    }
    return true;
  }

  function setIndicator(kind, text, tooltip) {
    if (!ensureIndicatorMounted()) return;
    if (indicatorPill) indicatorPill.className = "pill " + kind;
    if (indicatorLabel) indicatorLabel.textContent = "TapScribe: " + text;
    if (indicatorHost) indicatorHost.title = tooltip || "";
  }

  function errorTooltip(err) {
    if (err === "tap-auth-failed") {
      return "Recorder rejected the tap token. Open the popup and re-paste " +
        "the token from the recorder's startup banner.";
    }
    if (err === "tls-required") {
      return "Mixed-content blocked: enable TLS on the recorder and tick " +
        "\"Use TLS\" in the popup, or run the recorder on localhost.";
    }
    if (err === "buffer-overflow") {
      return "Recorder dropped the connection mid-utterance and we couldn't " +
        "reconnect in time. Trailing audio was lost.";
    }
    if (err === "recorder-unreachable") {
      return "Recorder isn't responding. Use the popup's \"Test connection\" " +
        "to diagnose.";
    }
    if (err === "drain-timeout") {
      return "Recorder didn't come back within the drain window; trailing " +
        "audio after mute was dropped.";
    }
    if (err === "backpressure") {
      return "Send queue is backing up — the recorder may be overloaded.";
    }
    if (err === "tap-send-failed") {
      return "WebSocket send() threw; retrying via reconnect.";
    }
    if (err === "tap-ws-construct-failed") {
      return "Browser refused to open the WebSocket. Check the popup's " +
        "host / port / TLS settings.";
    }
    if (typeof err === "string" && err.indexOf("tap-ws-closed-") === 0) {
      return "WebSocket closed unexpectedly (" + err + "). Reconnecting…";
    }
    return err || "";
  }

  function updateIndicator() {
    if (!settingsReady) {
      setIndicator("idle", "loading…", "Loading bridge settings…");
      return;
    }
    if (audioContextState === "failed") {
      setIndicator(
        "err",
        "refresh tab",
        "Audio capture failed to initialise. Reload the SpatialChat tab.",
      );
      return;
    }
    if (audioContextState && audioContextState !== "running") {
      setIndicator(
        "err",
        "audio " + audioContextState,
        "Audio capture is paused. Click anywhere in the SpatialChat tab to resume.",
      );
      return;
    }
    let openTaps = 0;
    let total = 0;
    let firstError = null;
    let anyReconnecting = false;
    let anyDraining = false;
    let anyMuted = false;
    for (const [, ch] of channels) {
      total++;
      if (ch.tapWs && ch.tapWs.readyState === WebSocket.OPEN) openTaps++;
      if (ch.error && !firstError) firstError = ch.error;
      if (ch.reconnectTimer !== null) anyReconnecting = true;
      if (ch.draining) anyDraining = true;
      if (ch.muted) anyMuted = true;
    }
    if (firstError) {
      setIndicator("err", firstError, errorTooltip(firstError));
      return;
    }
    if (anyReconnecting) {
      setIndicator(
        "warn",
        "reconnecting…",
        "Lost the recorder connection — retrying with backoff. " +
          "If this never clears, refresh the SpatialChat tab.",
      );
      return;
    }
    if (anyDraining) {
      setIndicator("warn", "draining", "Flushing trailing audio after mute.");
      return;
    }
    // While a meeting is active, mark the non-error states so the operator
    // can tell at a glance that capture is going into a bracketed detached
    // Session rather than the Recorder's global default.
    const mtg = meetingSessionId ? " · meeting" : "";
    const mtgTip = meetingSessionId
      ? " Capturing into the meeting session " + meetingSessionId + "."
      : "";
    if (openTaps > 0) {
      const noun = openTaps === 1 ? " stream" : " streams";
      setIndicator(
        "ok",
        openTaps + noun + mtg,
        "Live audio is reaching the recorder." + mtgTip,
      );
      return;
    }
    if (total > 0 && anyMuted) {
      setIndicator(
        "idle",
        "muted" + mtg,
        "Speakers are tapped but currently muted." + mtgTip,
      );
      return;
    }
    setIndicator(
      "idle",
      "idle" + mtg,
      "No remote speakers detected yet. The bridge will start when someone speaks." + mtgTip,
    );
  }

  // ---- Status publishing + title indicator ----------------------------------
  function stripSuffix(t) { return t.replace(/ \[tap [^\]]*\]$/, ""); }

  function wsStateName(s) {
    if (s === WebSocket.CONNECTING) return "CONNECTING";
    if (s === WebSocket.OPEN) return "OPEN";
    if (s === WebSocket.CLOSING) return "CLOSING";
    if (s === WebSocket.CLOSED) return "CLOSED";
    return "?";
  }

  function buildStatusSnapshot() {
    return {
      ts: Date.now(),
      url: location.href,
      recorderHost,
      recorderPort,
      settingsReady,
      audioContextState,
      // Bracketed-meeting state so the popup can show that taps are routing
      // into a detached Session rather than the global one.
      meetingActive: !!meetingSessionId,
      meetingSessionId,
      channels: Array.from(channels.entries()).map(([id, ch]) => ({
        identity: id,
        name: ch.name,
        muted: ch.muted,
        draining: ch.draining,
        error: ch.error,
        framesSent: ch.framesSent,
        bytesSent: ch.bytesSent,
        bufferedFrames: ch.buffer.length,
        bufferedBytes: ch.bufferBytes,
        reconnectAttempt: ch.reconnectAttempt,
        reconnecting: ch.reconnectTimer !== null,
        tapWs: ch.tapWs ? wsStateName(ch.tapWs.readyState) : null,
      })),
    };
  }

  // Hash of the parts of the snapshot the popup actually renders. Used to
  // skip storage writes when nothing observable changed, so we don't fan
  // out chrome.storage.onChanged events at 2 Hz for no reason.
  function snapshotFingerprint(snap) {
    return (snap.meetingSessionId || "") + "::" +
      (snap.audioContextState || "") + "::" + snap.channels.map(c =>
      c.identity + "|" + c.tapWs + "|" + c.muted + "|" + c.draining + "|" +
      c.error + "|" + c.framesSent + "|" + c.reconnecting + "|" + c.bufferedFrames,
    ).join(";");
  }

  // Liveness heartbeat: refresh the snapshot's `ts` at least this often even
  // when nothing observable changed, so the popup can tell a quiet-but-live tab
  // (everyone muted → no fingerprint change) from one whose content script is
  // gone. The popup's staleness window (taps-view.STALE_AFTER_MS) sits above
  // this; keep the two in step if either moves.
  const STATUS_HEARTBEAT_MS = 2000;
  let lastFingerprint = "";
  let lastPublishTs = 0;
  // Event-driven: called whenever channel state changes so the popup
  // reflects the change within ~50 ms instead of waiting for the periodic
  // tick. chrome.storage.set can reject in odd states (extension
  // reloading, quota) — swallow.
  function publishStatus() {
    try {
      updateIndicator();
    } catch (e) { /* ignore — indicator is best-effort */ }
    try {
      const snap = buildStatusSnapshot();
      const fp = snapshotFingerprint(snap);
      // Skip the write only when nothing observable changed AND we refreshed
      // `ts` recently. The heartbeat re-stamps an unchanged snapshot so a
      // live-but-quiet tab keeps its `ts` fresh and the popup doesn't flip it
      // to the no-tab empty state during a silent stretch. The fingerprint
      // (which includes per-frame counters) still covers the common active
      // case, so this isn't a 2 Hz write — only a ~0.5 Hz idle heartbeat.
      if (fp === lastFingerprint && (snap.ts - lastPublishTs) < STATUS_HEARTBEAT_MS) return;
      lastFingerprint = fp;
      lastPublishTs = snap.ts;
      chrome.storage.local.set({ bridgeStatus: snap });
    } catch (e) { /* ignore */ }
  }

  setInterval(() => {
    publishStatus();

    if (channels.size === 0) {
      if (origTitle !== null && document.title !== origTitle) {
        document.title = origTitle;
      }
      return;
    }
    if (origTitle === null) origTitle = stripSuffix(document.title);

    let totalBytes = 0;
    let openTaps = 0;
    let total = 0;
    let firstError = null;
    let anyReconnecting = false;
    for (const [id, ch] of channels) {
      total++;
      totalBytes += ch.bytesSent;
      if (ch.error && !firstError) firstError = id;
      if (ch.tapWs && ch.tapWs.readyState === WebSocket.OPEN) openTaps++;
      if (ch.reconnectTimer !== null) anyReconnecting = true;
    }

    // An AudioContext that's not running means the worklet is producing
    // no audio at all — overrides any per-channel indicator because the
    // channel counts are stale-but-not-wrong (the WS may be OPEN with
    // zero frames flowing through it).
    const ctxBlocked =
      audioContextState && audioContextState !== "running";

    // Mark the title when capture is bracketed into a meeting Session so the
    // operator can tell the tab is recording into a named Session, not the
    // global default — even when glancing at the title bar.
    const mtg = meetingSessionId ? " mtg" : "";
    let suffix;
    if (ctxBlocked) {
      suffix = " [tap" + mtg + " PAUSED audio " + audioContextState + "]";
    } else if (firstError) {
      suffix = " [tap" + mtg + " ERR " + firstError + "]";
    } else if (anyReconnecting) {
      suffix = " [tap" + mtg + " reconnecting…]";
    } else {
      suffix = " [tap" + mtg + " " + openTaps + "/" + total + " " + Math.round(totalBytes / 1024) + "K]";
    }
    const next = origTitle + suffix;
    if (document.title !== next) document.title = next;
  }, 500);
})();
