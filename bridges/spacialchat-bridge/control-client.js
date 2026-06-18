// SpatialChat Bridge — control-client.js (shared, dependency-free)
//
// One helper for the Recorder's tap-token *control plane* (the HTTP/WS
// calls that are NOT the per-utterance /tap audio stream). Loaded as a
// plain global into BOTH worlds — no bundler, no ES modules:
//   - the content-script isolated world, via manifest content_scripts.js
//     (listed before content.js so the global exists when content.js runs)
//   - the popup page, via a <script> tag before popup.js
//
// It mirrors the proven Windows tray `ControlClient`: unlike the /tap WS
// handshake (which can only carry the tap token in the subprotocol slot),
// an HTTP request can set arbitrary headers, so the tap token rides as
// `Authorization: Bearer <token>` (auth.py:check_tap_bearer).
//
// Every method takes a config object `{ host, port, useTls, token }`.
// The client centralises, in one tested place that used to be copy-pasted
// across content.js and popup.js:
//   - scheme derivation from the TLS toggle (http/https, ws/wss)
//   - the mixed-content guard: an http:// call carrying the bearer token
//     from an https:// page to a non-trustworthy host would put the token
//     on a cleartext wire, so it is skipped and the cause is surfaced
//   - the `Authorization: Bearer <token>` header (omitted when no token,
//     i.e. a --no-auth recorder)
//   - response parsing + classified errors
//   - timeouts / abort
//
// Blast-radius note: the surface is create / trigger / poll / rotate /
// probe ONLY. There is deliberately no delete or prune call — the
// low-privilege tap token can start and read a meeting's session but can
// never destroy one, so a leaked Bridge token's reach stays bounded right
// here at the client boundary. Dashboard Basic-auth owns deletion.

(function (root) {
  "use strict";

  const TAP_SUBPROTOCOL_PREFIX = "tapscribe.v1.tap.";

  // A classified error so callers can branch on `e.kind` instead of
  // string-matching messages. `status` is set for HTTP errors.
  //   - "mixed-content-blocked": the http→non-trustworthy-host guard fired
  //   - "http-error":            a non-success HTTP status (see .status)
  //   - "bad-response":          success status but the body lacked an
  //                              expected field (e.g. no `session` id)
  //   - "network":              fetch rejected / timed out / fetch missing
  class ControlError extends Error {
    constructor(kind, message, extra) {
      super(message || kind);
      this.name = "ControlError";
      this.kind = kind;
      if (extra && extra.status != null) this.status = extra.status;
      if (extra && extra.cause != null) this.cause = extra.cause;
    }
  }

  function httpScheme(cfg) { return cfg && cfg.useTls ? "https" : "http"; }
  function wsScheme(cfg) { return cfg && cfg.useTls ? "wss" : "ws"; }
  function httpBase(cfg) { return httpScheme(cfg) + "://" + cfg.host + ":" + cfg.port; }

  // Chrome/Edge treat these origins as "potentially trustworthy", so a
  // cleartext http:// (or ws://) call to them is allowed from an https://
  // page. Anything else is mixed-content-blocked. Kept identical to the
  // predicate content.js applies to the /tap WS so the HTTP control plane
  // and the audio transport agree on what "trustworthy" means.
  function isTrustworthyHost(host) {
    if (!host) return false;
    const h = String(host).toLowerCase();
    return (
      h === "localhost" ||
      h.endsWith(".localhost") ||
      h === "127.0.0.1" ||
      h === "[::1]" ||
      h === "::1"
    );
  }

  // True when a cleartext http:// control call from the current page would
  // be mixed-content-blocked. Only meaningful from an https:// origin (the
  // SpatialChat content-script world); from the popup's own
  // chrome-extension:// origin `location.protocol` isn't "https:", so this
  // returns false and the popup keeps its existing always-fire behaviour.
  function wouldBlockHttp(cfg) {
    if (cfg && cfg.useTls) return false;
    if (typeof location === "undefined") return false;
    if (location.protocol !== "https:") return false;
    return !isTrustworthyHost(cfg && cfg.host);
  }

  function bearerHeaders(cfg, extra) {
    const headers = extra ? Object.assign({}, extra) : {};
    if (cfg && cfg.token) headers.Authorization = "Bearer " + cfg.token;
    return headers;
  }

  // Resolve the AbortSignal for a call. The client owns timeouts/abort:
  // pass `{ signal }` to thread your own, or `{ timeoutMs }` to have the
  // client arm an AbortController and tear it down when the call settles.
  // With neither, the call has no timeout (fire-and-forget callers).
  function resolveSignal(opts) {
    if (opts && opts.signal) return { signal: opts.signal, clear: null };
    if (opts && opts.timeoutMs) {
      const ctrl = new AbortController();
      const id = setTimeout(() => ctrl.abort(), opts.timeoutMs);
      return { signal: ctrl.signal, clear: () => clearTimeout(id) };
    }
    return { signal: undefined, clear: null };
  }

  // Shared fetch wrapper for the bearer-carrying HTTP control calls. Throws
  // a ControlError for the mixed-content guard and for fetch/parse failure;
  // otherwise returns `{ ok, status, body }` (body = parsed JSON, or `{}`
  // for an empty/non-JSON body) so each caller maps the status itself. The
  // body is read INSIDE the armed-signal try, so a `timeoutMs` abort covers
  // the body stream too — not just the connection (a stalled body still
  // trips the timeout instead of hanging).
  async function controlFetch(cfg, path, init, opts) {
    if (wouldBlockHttp(cfg)) {
      throw new ControlError(
        "mixed-content-blocked",
        "recorder is http:// on a non-trustworthy host — enable TLS",
      );
    }
    if (typeof fetch !== "function") {
      throw new ControlError("network", "fetch is unavailable in this context");
    }
    const { signal, clear } = resolveSignal(opts);
    try {
      // `init` never carries its own signal (the client owns timeouts/abort),
      // but apply `{ signal }` last so the client's signal always wins.
      const r = await fetch(httpBase(cfg) + path, Object.assign({}, init, { signal }));
      const body = await r.json().catch(() => ({}));
      return { ok: r.ok, status: r.status, body };
    } catch (e) {
      throw new ControlError("network", String((e && e.message) || e), { cause: e });
    } finally {
      if (clear) clear();
    }
  }

  // POST /api/tap/new-session with {"detached": true}. Returns the
  // server-minted detached session — pass `sessionId` as the `?session=`
  // tap param so the bridge's audio lands in its own folder. Throws a
  // classified ControlError on any failure (mixed-content, HTTP error, or
  // a success body missing the `session` id).
  async function createDetachedSession(cfg, opts) {
    const { ok, status, body } = await controlFetch(
      cfg,
      "/api/tap/new-session",
      {
        method: "POST",
        headers: bearerHeaders(cfg, { "Content-Type": "application/json" }),
        body: JSON.stringify({ detached: true }),
      },
      opts,
    );
    if (!ok) {
      throw new ControlError("http-error", "new-session HTTP " + status, { status });
    }
    if (typeof body.session !== "string" || !body.session) {
      throw new ControlError("bad-response", "new-session response did not contain a 'session' id");
    }
    return { sessionId: body.session, path: body.path };
  }

  // POST /api/tap/sessions/{session}/pipeline — fire the end-of-meeting
  // pipeline (strip → transcribe → summarize) for a session. The Recorder
  // ignores the request body by design (it uses operator-configured
  // defaults), so none is sent. Distinguishes accepted (202) from busy
  // (409 — another job already running on this session) and throws a
  // classified ControlError for any other status.
  async function triggerPipeline(cfg, sessionId, opts) {
    const { status } = await controlFetch(
      cfg,
      "/api/tap/sessions/" + encodeURIComponent(sessionId) + "/pipeline",
      { method: "POST", headers: bearerHeaders(cfg) },
      opts,
    );
    if (status === 202) return { outcome: "accepted", status: 202 };
    if (status === 409) return { outcome: "busy", status: 409 };
    throw new ControlError("http-error", "pipeline trigger HTTP " + status, { status });
  }

  // GET /api/tap/sessions/{session}/pipeline — poll the pipeline's
  // progress/summary. Returns the RAW parsed response body; mapping a poll
  // response to a UI phase/progress/summary view-model is a separate
  // concern (a later slice). Throws a classified ControlError on
  // mixed-content / network failure / a non-success HTTP status.
  async function pollPipeline(cfg, sessionId, opts) {
    const { ok, status, body } = await controlFetch(
      cfg,
      "/api/tap/sessions/" + encodeURIComponent(sessionId) + "/pipeline",
      { method: "GET", headers: bearerHeaders(cfg) },
      opts,
    );
    if (!ok) {
      throw new ControlError("http-error", "pipeline poll HTTP " + status, { status });
    }
    return body;
  }

  // POST /api/tap/new-session with no body — the legacy GLOBAL rotate
  // (rotate the recorder's shared current session, prune empties). Returns
  // `{ ok, status, body }` so each caller renders its own status text;
  // throws ControlError only for the mixed-content guard / network failure.
  // (The button/room-change consumers render different copy from the
  // parsed body, so success/non-success is reported as data, not a throw.)
  async function rotateSession(cfg, opts) {
    return await controlFetch(
      cfg,
      "/api/tap/new-session",
      { method: "POST", headers: bearerHeaders(cfg) },
      opts,
    );
  }

  // GET /health — a pure reachability probe. Carries NO token (the health
  // endpoint is auth-exempt and there's nothing to leak), so it is not
  // subject to the mixed-content guard either. Returns a result object
  // rather than throwing so the popup can render a reachable/unreachable
  // pill directly.
  async function checkHealth(cfg, opts) {
    const url = httpBase(cfg) + "/health";
    if (typeof fetch !== "function") {
      return { ok: false, error: "fetch is unavailable in this context", url };
    }
    const { signal, clear } = resolveSignal(opts);
    try {
      const r = await fetch(url, { method: "GET", signal });
      if (!r.ok) return { ok: false, status: r.status, url };
      const body = await r.json().catch(() => ({}));
      return { ok: true, body, url };
    } catch (e) {
      return { ok: false, error: String((e && e.message) || e), url };
    } finally {
      if (clear) clear();
    }
  }

  // Verify the tap token by opening a /tap WS with the right subprotocol
  // and closing it immediately. A successful upgrade (onopen) means the
  // token is good; a 4401 close (or onerror) means it was rejected. An
  // empty token + an --no-auth recorder upgrades without subprotocol
  // negotiation. Returns `{ ok, error? }`.
  function probeTapToken(cfg, opts) {
    return new Promise((resolve) => {
      const url =
        wsScheme(cfg) + "://" + cfg.host + ":" + cfg.port + "/tap?identity=__probe__&name=probe";
      let ws;
      try {
        ws = cfg.token ? new WebSocket(url, [TAP_SUBPROTOCOL_PREFIX + cfg.token]) : new WebSocket(url);
      } catch (e) {
        resolve({ ok: false, error: String((e && e.message) || e) });
        return;
      }
      let settled = false;
      const { signal, clear } = resolveSignal(opts);
      const finish = (res) => {
        if (settled) return;
        settled = true;
        if (clear) clear();
        try { ws.close(1000, "probe done"); } catch (e) { /* already closing/closed */ }
        resolve(res);
      };
      if (signal) signal.addEventListener("abort", () => finish({ ok: false, error: "timeout" }));
      ws.onopen = () => finish({ ok: true });
      ws.onerror = () => finish({ ok: false, error: "rejected" });
      ws.onclose = (ev) => finish({ ok: ev.code === 1000, error: "code " + ev.code });
    });
  }

  root.TapscribeControlClient = {
    // The thrown-error contract for the create/trigger/poll methods.
    ControlError,
    // The trustworthy-host primitive + a plain recorder base URL, exposed
    // so callers stop hand-rolling scheme/host/port (the popup's
    // mixed-content warning and dashboard link use these). Scheme/guard
    // internals (httpScheme/wsScheme/wouldBlockHttp/subprotocol prefix)
    // stay private — every call that needs them already routes through a
    // method here.
    isTrustworthyHost,
    httpBase,
    createDetachedSession,
    triggerPipeline,
    pollPipeline,
    rotateSession,
    checkHealth,
    probeTapToken,
  };
})(typeof globalThis !== "undefined" ? globalThis : typeof self !== "undefined" ? self : this);
