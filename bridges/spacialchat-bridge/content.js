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
//   - Publish a status snapshot to chrome.storage so the popup can show
//     per-speaker state without needing DevTools, and update the tab
//     title with a tiny tx/state indicator.
//
// Wire contract (see bridges/README.md):
//   ws://<recorder-host>:8001/tap?identity=<id>&name=<display>
//   Binary frames, 20 ms each (320 samples = 640 bytes), int16 LE mono.
//   No JSON, no HTTP, no WhisperLiveKit awareness — the Recorder fans
//   audio out internally to its supervised WlK child and to disk.

(() => {
  if (window.__tapscribeBridgeContentInstalled) return;
  window.__tapscribeBridgeContentInstalled = true;

  // ---- Recorder host (configurable via popup) -------------------------------
  // Read once at startup. Changes via the popup require a SpatialChat tab
  // reload to take effect — we don't tear down existing /tap WSes on
  // storage change.
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

  const tapWsUrl = (identity, name) => {
    const qp = new URLSearchParams({ identity, name: name || "" });
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
   *   tapWs:      WebSocket | null,
   *   framesSent: number,    // frames forwarded on the current /tap WS
   *   bytesSent:  number,    // bytes forwarded across all utterances this session
   *   muted:      boolean,
   *   error:      string | null,
   *   name:       string,
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
      framesSent: 0,
      bytesSent: 0,
      muted: false,
      error: null,
      name: name || "",
    };
    channels.set(identity, ch);
    return ch;
  }

  // ---- /tap WS (per utterance) ----------------------------------------------
  function openTapWs(identity, ch) {
    const url = tapWsUrl(identity, ch.name);
    console.log("[tapscribe-bridge] opening /tap for " + identity + " -> " + url);
    ch.framesSent = 0;
    const ws = openWs(url);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      console.log("[tapscribe-bridge] /tap open for " + identity);
      if (ch.error) ch.error = null;
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
      if (!ev.wasClean || ev.code !== 1000) {
        if (ev.code === 4401) {
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
      publishStatus();
    };
    ch.tapWs = ws;
  }

  function closeTapWs(identity, ch, reason) {
    const w = ch.tapWs;
    ch.tapWs = null;
    if (w && (w.readyState === WebSocket.OPEN || w.readyState === WebSocket.CONNECTING)) {
      try { w.close(1000, reason || "end of utterance"); } catch (e) {}
    }
  }

  // ---- Message handler from page world --------------------------------------
  window.addEventListener("message", (ev) => {
    if (ev.source !== window) return;
    const d = ev.data;
    if (!d || d.source !== "tapscribe-bridge") return;

    switch (d.kind) {
      case "tap-start": {
        console.log("[tapscribe-bridge] tap-start " + d.identity + " (" + (d.name || "?") + ")");
        ensureChannel(d.identity, d.name);
        publishStatus();
        break;
      }
      case "tap-stop": {
        const ch = channels.get(d.identity);
        if (ch) {
          closeTapWs(d.identity, ch, "tap stopped");
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
            // Mute is the "end of utterance" signal — finalise the WAV.
            closeTapWs(d.identity, ch, "muted");
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

        if (!ch.tapWs) openTapWs(d.identity, ch);
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
              if (ch.error === "tap-send-failed" || ch.error === "backpressure") {
                ch.error = null;
              }
            } catch (e) {
              ch.error = "tap-send-failed";
              console.error("[tapscribe-bridge] /tap send failed for " + d.identity, e);
            }
          }
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
        tapWs: ch.tapWs ? wsStateName(ch.tapWs.readyState) : null,
      })),
    };
  }

  // Hash of the parts of the snapshot the popup actually renders. Used to
  // skip storage writes when nothing observable changed, so we don't fan
  // out chrome.storage.onChanged events at 2 Hz for no reason.
  function snapshotFingerprint(snap) {
    return snap.channels.map(c =>
      c.identity + "|" + c.tapWs + "|" + c.muted + "|" + c.error + "|" + c.framesSent,
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
    for (const [id, ch] of channels) {
      total++;
      totalBytes += ch.bytesSent;
      if (ch.error && !firstError) firstError = id;
      if (ch.tapWs && ch.tapWs.readyState === WebSocket.OPEN) openTaps++;
    }

    const suffix = firstError
      ? " [tap ERR " + firstError + "]"
      : " [tap " + openTaps + "/" + total + " " + Math.round(totalBytes / 1024) + "K]";
    const next = origTitle + suffix;
    if (document.title !== next) document.title = next;
  }, 500);
})();
