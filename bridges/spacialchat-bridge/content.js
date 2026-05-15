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
//   - Publish a status snapshot to chrome.storage so the popup can show
//     per-speaker state without needing DevTools, and update the tab
//     title with a tiny tx/state indicator.
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
  let settingsReady = false;
  chrome.storage.local.get(["recorderHost", "recorderPort", "tapToken", "useTls"]).then((s) => {
    if (s && s.recorderHost) recorderHost = s.recorderHost;
    if (s && s.recorderPort) recorderPort = Number(s.recorderPort) || 8001;
    if (s && typeof s.tapToken === "string") tapToken = s.tapToken;
    if (s && typeof s.useTls === "boolean") recorderUseTls = s.useTls;
    settingsReady = true;
    console.log(
      "[tapscribe-bridge] recorder: " + (recorderUseTls ? "wss://" : "ws://") +
      recorderHost + ":" + recorderPort + " (tap-token " + (tapToken ? "set" : "MISSING") + ")",
    );
  }).catch((e) => {
    settingsReady = true; // fall back to default rather than block forever
    console.warn("[tapscribe-bridge] could not read recorder settings; using defaults", e);
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
      // actually mid-utterance.
      if (!ch.muted && !ch.stopped && ch.utteranceId) {
        ch.reconnectAttempt = 0;
        scheduleReconnect(identity, ch);
      }
    }
    publishStatus();
  }

  const tapWsUrl = (identity, name, utteranceId) => {
    const qp = new URLSearchParams({
      identity,
      name: name || "",
      utterance_id: utteranceId,
    });
    const scheme = recorderUseTls ? "wss" : "ws";
    return scheme + "://" + recorderHost + ":" + recorderPort + "/tap?" + qp.toString();
  };

  // Construct the WebSocket with the tap-token carried via subprotocol
  // when the operator set one. With --no-auth the recorder ignores
  // subprotocols entirely; we omit it so the upgrade succeeds without
  // server-side echo.
  const openWs = (url) => {
    if (tapToken) return new WebSocket(url, [TAP_SUBPROTOCOL_PREFIX + tapToken]);
    return new WebSocket(url);
  };

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

  function ensureChannel(identity, name) {
    let ch = channels.get(identity);
    if (ch) {
      if (name && name !== ch.name) ch.name = name;
      return ch;
    }
    ch = {
      tapWs: null,
      utteranceId: null,
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

  function scheduleReconnect(identity, ch) {
    if (ch.muted || ch.stopped) return;
    if (ch.reconnectTimer !== null) return;
    if (!ch.utteranceId) return;
    const delay = nextBackoffMs(ch.reconnectAttempt);
    ch.reconnectAttempt++;
    console.log(
      "[tapscribe-bridge] /tap reconnect for " + identity +
      " in " + delay + "ms (attempt " + ch.reconnectAttempt + ")",
    );
    ch.reconnectTimer = setTimeout(() => {
      ch.reconnectTimer = null;
      // State might have changed while we waited.
      if (ch.muted || ch.stopped) return;
      if (ch.tapWs) return;
      openTapWs(identity, ch);
    }, delay);
    publishStatus();
  }

  function openTapWs(identity, ch) {
    if (!ch.utteranceId) ch.utteranceId = newUtteranceId();
    const url = tapWsUrl(identity, ch.name, ch.utteranceId);
    console.log(
      "[tapscribe-bridge] opening /tap for " + identity +
      " utt=" + ch.utteranceId.slice(0, 8) + " -> " + url,
    );
    ch.framesSent = 0;
    const ws = openWs(url);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      console.log("[tapscribe-bridge] /tap open for " + identity);
      ch.reconnectAttempt = 0;
      ch.everOpened = true;
      if (ch.error) ch.error = null;
      bufferFlush(ch);
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
      if (!clean && !authFailed && !ch.muted && !ch.stopped) {
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

  function endUtterance(identity, ch, reason) {
    closeTapWs(identity, ch, reason);
    ch.utteranceId = null;
    ch.reconnectAttempt = 0;
    ch.buffer = [];
    ch.bufferBytes = 0;
    ch.everOpened = false;
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
          endUtterance(d.identity, ch, "tap stopped");
          channels.delete(d.identity);
          console.log("[tapscribe-bridge] tap-stop " + d.identity);
          publishStatus();
        }
        break;
      }
      case "mute": {
        const ch = channels.get(d.identity);
        if (ch) {
          const wasMuted = ch.muted;
          ch.muted = !!d.muted;
          if (ch.muted && !wasMuted) {
            // Mute is the "end of utterance" signal — finalise the WAV
            // and discard any buffered PCM / pending reconnect.
            endUtterance(d.identity, ch, "muted");
            console.log("[tapscribe-bridge] mute " + d.identity + " -> closed /tap");
          } else {
            console.log("[tapscribe-bridge] mute " + d.identity + " -> " + ch.muted);
          }
          publishStatus();
        }
        break;
      }
      case "pcm": {
        const ch = ensureChannel(d.identity, d.name);
        if (d.name && d.name !== ch.name) ch.name = d.name;
        if (ch.muted) return;

        // Defer the first WS open until settings have loaded, so we don't
        // connect to the default host and then have to throw it away.
        if (!settingsReady) return;

        // First frame of a new utterance — mint the id the recorder will
        // use to stitch reconnects together.
        if (!ch.utteranceId) ch.utteranceId = newUtteranceId();

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
      default: {
        // Unknown kind; ignore.
      }
    }
  });

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
      channels: Array.from(channels.entries()).map(([id, ch]) => ({
        identity: id,
        name: ch.name,
        muted: ch.muted,
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
    return snap.channels.map(c =>
      c.identity + "|" + c.tapWs + "|" + c.muted + "|" + c.error + "|" +
      c.framesSent + "|" + c.reconnecting + "|" + c.bufferedFrames,
    ).join(";");
  }

  let lastFingerprint = "";
  // Event-driven: called whenever channel state changes so the popup
  // reflects the change within ~50 ms instead of waiting for the periodic
  // tick. chrome.storage.set can reject in odd states (extension
  // reloading, quota) — swallow.
  function publishStatus() {
    try {
      const snap = buildStatusSnapshot();
      const fp = snapshotFingerprint(snap);
      if (fp === lastFingerprint) return;
      lastFingerprint = fp;
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

    let suffix;
    if (firstError) {
      suffix = " [tap ERR " + firstError + "]";
    } else if (anyReconnecting) {
      suffix = " [tap reconnecting…]";
    } else {
      suffix = " [tap " + openTaps + "/" + total + " " + Math.round(totalBytes / 1024) + "K]";
    }
    const next = origTitle + suffix;
    if (document.title !== next) document.title = next;
  }, 500);
})();
