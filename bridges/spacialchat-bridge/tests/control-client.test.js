// Unit tests for control-client.js — the shared, dependency-free tap-token
// control plane loaded into both the content-script and popup worlds.
//
// Like the other bridge tests, control-client.js is browser JS with no Node
// awareness. We load it into a `vm` context with a fake `fetch` / `location`
// / `WebSocket` and exercise the public surface, asserting on the request
// that went out (URL, method, bearer header, body) and the value/classified
// error returned — never on internals. This mirrors the Windows
// ControlClientTests (a fake control server) conceptually, with a fake
// fetch standing in for the in-process Kestrel.

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const CONTROL_CLIENT_JS = path.join(__dirname, "..", "control-client.js");

// Build a vm sandbox, load control-client.js, and return the global it
// exports plus the recorded fetch calls.
//   - `fetchImpl(url, init)` returns the fake Response (or rejects to model
//     a network failure). Omit it to model a context with no fetch.
//   - `location` is optional: omit it (undefined) to model the popup's own
//     chrome-extension:// origin where the mixed-content guard is inactive;
//     pass an `{ protocol: "https:" }` one to model the content-script world.
//   - `WebSocketImpl` is the fake WebSocket class for the tap-token probe.
function loadClient({ fetchImpl, location, WebSocketImpl } = {}) {
  const fetchCalls = [];
  const sandbox = {
    fetch: fetchImpl
      ? (url, init) => {
          fetchCalls.push({ url, init: init || {} });
          return fetchImpl(url, init || {});
        }
      : undefined,
    location,
    WebSocket: WebSocketImpl,
    AbortController,
    setTimeout,
    clearTimeout,
    console: { log() {}, warn() {}, error() {} },
  };
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(CONTROL_CLIENT_JS, "utf8"), sandbox, { filename: "control-client.js" });
  return { client: sandbox.TapscribeControlClient, fetchCalls };
}

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  };
}

const CFG = { host: "localhost", port: 8001, useTls: false, token: "t" };

// ---- createDetachedSession ------------------------------------------------

test("createDetachedSession parses the session id + path and sends the Bearer header", async () => {
  const { client, fetchCalls } = loadClient({
    fetchImpl: () =>
      Promise.resolve(
        jsonResponse(200, {
          ok: true,
          detached: true,
          session: "2026-06-12T10-00-00",
          path: "recordings/2026-06-12T10-00-00",
        }),
      ),
  });

  const res = await client.createDetachedSession({
    host: "localhost",
    port: 8001,
    useTls: false,
    token: "tok-abc",
  });

  // Field-wise (not deepEqual): the returned object is constructed inside
  // the vm realm, so its prototype isn't reference-equal to the test
  // realm's Object.prototype and strict deep-equal would reject it.
  assert.equal(res.sessionId, "2026-06-12T10-00-00");
  assert.equal(res.path, "recordings/2026-06-12T10-00-00");
  assert.equal(fetchCalls.length, 1);
  assert.equal(fetchCalls[0].url, "http://localhost:8001/api/tap/new-session");
  assert.equal(fetchCalls[0].init.method, "POST");
  assert.equal(fetchCalls[0].init.headers.Authorization, "Bearer tok-abc");
  assert.equal(fetchCalls[0].init.headers["Content-Type"], "application/json");
  assert.equal(fetchCalls[0].init.body, JSON.stringify({ detached: true }));
});

test("createDetachedSession throws a classified bad-response when the body has no session id", async () => {
  const { client } = loadClient({ fetchImpl: () => Promise.resolve(jsonResponse(200, { ok: true })) });
  await assert.rejects(
    () => client.createDetachedSession(CFG),
    (e) => e.kind === "bad-response",
  );
});

test("createDetachedSession throws http-error carrying the status on a non-ok response", async () => {
  const { client } = loadClient({ fetchImpl: () => Promise.resolve(jsonResponse(500, {})) });
  await assert.rejects(
    () => client.createDetachedSession(CFG),
    (e) => e.kind === "http-error" && e.status === 500,
  );
});

test("createDetachedSession throws a network error when fetch rejects", async () => {
  const { client } = loadClient({ fetchImpl: () => Promise.reject(new Error("ECONNREFUSED")) });
  await assert.rejects(
    () => client.createDetachedSession(CFG),
    (e) => e.kind === "network",
  );
});

test("createDetachedSession omits the Authorization header when no token is set", async () => {
  const { client, fetchCalls } = loadClient({
    fetchImpl: () => Promise.resolve(jsonResponse(200, { session: "s1" })),
  });
  await client.createDetachedSession({ host: "localhost", port: 8001, useTls: false, token: "" });
  assert.equal(fetchCalls[0].init.headers.Authorization, undefined);
});

// ---- triggerPipeline ------------------------------------------------------

test("triggerPipeline returns accepted for a 202 and targets the session pipeline endpoint", async () => {
  const { client, fetchCalls } = loadClient({ fetchImpl: () => Promise.resolve(jsonResponse(202, {})) });
  const res = await client.triggerPipeline(CFG, "sess-1");
  assert.equal(res.outcome, "accepted");
  assert.equal(fetchCalls[0].url, "http://localhost:8001/api/tap/sessions/sess-1/pipeline");
  assert.equal(fetchCalls[0].init.method, "POST");
  assert.equal(fetchCalls[0].init.headers.Authorization, "Bearer t");
});

test("triggerPipeline returns busy for a 409", async () => {
  const { client } = loadClient({ fetchImpl: () => Promise.resolve(jsonResponse(409, {})) });
  const res = await client.triggerPipeline(CFG, "sess-1");
  assert.equal(res.outcome, "busy");
});

test("triggerPipeline throws http-error for any other status", async () => {
  const { client } = loadClient({ fetchImpl: () => Promise.resolve(jsonResponse(500, {})) });
  await assert.rejects(
    () => client.triggerPipeline(CFG, "sess-1"),
    (e) => e.kind === "http-error" && e.status === 500,
  );
});

// ---- pollPipeline ---------------------------------------------------------

test("pollPipeline GETs the session pipeline endpoint and returns the raw body", async () => {
  const raw = { state: "running", stage: "transcribe", status: "transcribing", current: 3, total: 12 };
  const { client, fetchCalls } = loadClient({ fetchImpl: () => Promise.resolve(jsonResponse(200, raw)) });
  const res = await client.pollPipeline(CFG, "sess-1");
  assert.deepEqual(res, raw);
  assert.equal(fetchCalls[0].url, "http://localhost:8001/api/tap/sessions/sess-1/pipeline");
  assert.equal(fetchCalls[0].init.method, "GET");
  assert.equal(fetchCalls[0].init.headers.Authorization, "Bearer t");
});

test("pollPipeline passes each Recorder state through untouched (mapping is a later slice)", async () => {
  const states = [
    { state: "idle" },
    { state: "running", stage: "strip", status: "stripping" },
    { state: "done", summary: { summary: "meeting notes" } },
    { state: "failed", error_kind: "NoUsableWavs", error: "no usable WAVs in session" },
  ];
  for (const raw of states) {
    const { client } = loadClient({ fetchImpl: () => Promise.resolve(jsonResponse(200, raw)) });
    assert.deepEqual(await client.pollPipeline(CFG, "s"), raw);
  }
});

test("pollPipeline throws http-error on a non-ok status", async () => {
  const { client } = loadClient({ fetchImpl: () => Promise.resolve(jsonResponse(404, {})) });
  await assert.rejects(
    () => client.pollPipeline(CFG, "s"),
    (e) => e.kind === "http-error" && e.status === 404,
  );
});

// ---- mixed-content guard --------------------------------------------------

test("the guard skips every token-bearing http call to a non-trustworthy host from an https page", async () => {
  const { client, fetchCalls } = loadClient({
    fetchImpl: () => Promise.resolve(jsonResponse(200, { session: "s1" })),
    location: { protocol: "https:", origin: "https://app.spatial.chat" },
  });
  const cfg = { host: "rec.example", port: 8001, useTls: false, token: "t" };

  for (const call of [
    () => client.createDetachedSession(cfg),
    () => client.triggerPipeline(cfg, "s"),
    () => client.pollPipeline(cfg, "s"),
    () => client.rotateSession(cfg),
  ]) {
    await assert.rejects(call, (e) => e.kind === "mixed-content-blocked");
  }
  assert.equal(fetchCalls.length, 0, "no bearer-carrying request reached the wire");
});

test("the guard allows http to a trustworthy localhost host from an https page", async () => {
  const { client, fetchCalls } = loadClient({
    fetchImpl: () => Promise.resolve(jsonResponse(200, { session: "s1" })),
    location: { protocol: "https:", origin: "https://app.spatial.chat" },
  });
  await client.createDetachedSession({ host: "localhost", port: 8001, useTls: false, token: "t" });
  assert.equal(fetchCalls.length, 1);
});

test("the guard allows TLS even to a non-trustworthy host from an https page", async () => {
  const { client, fetchCalls } = loadClient({
    fetchImpl: () => Promise.resolve(jsonResponse(200, { session: "s1" })),
    location: { protocol: "https:", origin: "https://app.spatial.chat" },
  });
  await client.createDetachedSession({ host: "rec.example", port: 8443, useTls: true, token: "t" });
  assert.equal(fetchCalls.length, 1);
  assert.match(fetchCalls[0].url, /^https:\/\/rec\.example:8443\//);
});

test("the guard is inactive from the popup's own (non-https) origin", async () => {
  // No `location` models the chrome-extension popup origin where
  // location.protocol !== "https:", so the http:// call to a non-trustworthy
  // host fires — the popup's own probe can still reach the recorder.
  const { client, fetchCalls } = loadClient({
    fetchImpl: () => Promise.resolve(jsonResponse(200, { session: "s1" })),
  });
  await client.createDetachedSession({ host: "rec.example", port: 8001, useTls: false, token: "t" });
  assert.equal(fetchCalls.length, 1);
});

// ---- scheme follows the TLS toggle ---------------------------------------

test("scheme follows the TLS toggle", async () => {
  const plain = loadClient({ fetchImpl: () => Promise.resolve(jsonResponse(200, { session: "s1" })) });
  await plain.client.createDetachedSession({ host: "rec.example", port: 8001, useTls: false, token: "t" });
  assert.match(plain.fetchCalls[0].url, /^http:\/\/rec\.example:8001\//);

  const tls = loadClient({ fetchImpl: () => Promise.resolve(jsonResponse(200, { session: "s1" })) });
  await tls.client.createDetachedSession({ host: "rec.example", port: 8443, useTls: true, token: "t" });
  assert.match(tls.fetchCalls[0].url, /^https:\/\/rec\.example:8443\//);
});

// ---- rotateSession (legacy global rotate) ---------------------------------

test("rotateSession posts new-session with NO body and returns {ok,status,body}", async () => {
  const body = { ok: true, rotated: true, current: "2026-06-02T12-00-00Z" };
  const { client, fetchCalls } = loadClient({ fetchImpl: () => Promise.resolve(jsonResponse(200, body)) });
  const res = await client.rotateSession(CFG);
  assert.equal(res.ok, true);
  assert.equal(res.status, 200);
  assert.equal(res.body.rotated, true);
  assert.equal(res.body.current, "2026-06-02T12-00-00Z");
  assert.equal(fetchCalls[0].url, "http://localhost:8001/api/tap/new-session");
  assert.equal(fetchCalls[0].init.method, "POST");
  assert.equal(fetchCalls[0].init.headers.Authorization, "Bearer t");
  assert.equal(fetchCalls[0].init.body, undefined, "the legacy rotate carries no body");
});

test("rotateSession reports a non-ok status as data rather than throwing", async () => {
  const { client } = loadClient({ fetchImpl: () => Promise.resolve(jsonResponse(500, {})) });
  const res = await client.rotateSession(CFG);
  assert.equal(res.ok, false);
  assert.equal(res.status, 500);
});

// ---- checkHealth (token-free reachability probe) --------------------------

test("checkHealth probes /health with NO auth header and returns the body", async () => {
  const { client, fetchCalls } = loadClient({
    fetchImpl: () => Promise.resolve(jsonResponse(200, { status: "ok" })),
  });
  const res = await client.checkHealth(CFG);
  assert.equal(res.ok, true);
  assert.deepEqual(res.body, { status: "ok" });
  assert.equal(fetchCalls[0].url, "http://localhost:8001/health");
  assert.equal(fetchCalls[0].init.method, "GET");
  assert.equal(fetchCalls[0].init.headers, undefined, "health carries no auth header");
});

test("checkHealth reports unreachable on a rejected fetch", async () => {
  const { client } = loadClient({ fetchImpl: () => Promise.reject(new Error("ECONNREFUSED")) });
  const res = await client.checkHealth(CFG);
  assert.equal(res.ok, false);
  assert.match(res.error, /ECONNREFUSED/);
});

test("checkHealth reports the status on a non-ok response", async () => {
  const { client } = loadClient({ fetchImpl: () => Promise.resolve(jsonResponse(503, {})) });
  const res = await client.checkHealth(CFG);
  assert.equal(res.ok, false);
  assert.equal(res.status, 503);
});

// ---- probeTapToken (WS upgrade probe) -------------------------------------

function wsHarness(behaviour) {
  const created = [];
  class FakeWS {
    constructor(url, protocols) {
      this.url = url;
      this.protocols = protocols;
      this.onopen = null;
      this.onerror = null;
      this.onclose = null;
      this.closedWith = null;
      created.push(this);
      if (behaviour === "hang") return;
      Promise.resolve().then(() => {
        if (behaviour === "open" && this.onopen) this.onopen({});
        else if (behaviour === "error" && this.onerror) this.onerror({});
        else if (behaviour && behaviour.closeCode != null && this.onclose) {
          this.onclose({ code: behaviour.closeCode });
        }
      });
    }
    close(code, reason) { this.closedWith = { code, reason }; }
  }
  return { FakeWS, created };
}

test("probeTapToken resolves ok and sends the tap subprotocol when the upgrade opens", async () => {
  const { FakeWS, created } = wsHarness("open");
  const { client } = loadClient({ WebSocketImpl: FakeWS });
  const res = await client.probeTapToken({ host: "localhost", port: 8001, useTls: false, token: "tok-abc" });
  assert.equal(res.ok, true);
  assert.equal(created[0].url, "ws://localhost:8001/tap?identity=__probe__&name=probe");
  assert.equal(created[0].protocols.length, 1);
  assert.equal(created[0].protocols[0], "tapscribe.v1.tap.tok-abc");
});

test("probeTapToken resolves not-ok when the upgrade errors", async () => {
  const { FakeWS } = wsHarness("error");
  const { client } = loadClient({ WebSocketImpl: FakeWS });
  const res = await client.probeTapToken(CFG);
  assert.equal(res.ok, false);
  assert.equal(res.error, "rejected");
});

test("probeTapToken treats a 4401 close as a rejected token", async () => {
  const { FakeWS } = wsHarness({ closeCode: 4401 });
  const { client } = loadClient({ WebSocketImpl: FakeWS });
  const res = await client.probeTapToken(CFG);
  assert.equal(res.ok, false);
  assert.equal(res.error, "code 4401");
});

test("probeTapToken uses wss:// under TLS and omits the subprotocol with no token", async () => {
  const { FakeWS, created } = wsHarness("open");
  const { client } = loadClient({ WebSocketImpl: FakeWS });
  const res = await client.probeTapToken({ host: "rec.example", port: 8443, useTls: true, token: "" });
  assert.equal(res.ok, true);
  assert.equal(created[0].url, "wss://rec.example:8443/tap?identity=__probe__&name=probe");
  assert.equal(created[0].protocols, undefined);
});

test("probeTapToken resolves timeout when its abort signal fires before the upgrade settles", async () => {
  const { FakeWS } = wsHarness("hang");
  const { client } = loadClient({ WebSocketImpl: FakeWS });
  const ctrl = new AbortController();
  const p = client.probeTapToken(CFG, { signal: ctrl.signal });
  ctrl.abort();
  const res = await p;
  assert.equal(res.ok, false);
  assert.equal(res.error, "timeout");
});

// ---- timeout/abort ownership + blast-radius surface -----------------------

test("a timeoutMs option arms an AbortSignal that the client threads into fetch", async () => {
  let seenSignal = null;
  const { client } = loadClient({
    fetchImpl: (url, init) => {
      seenSignal = init.signal;
      return Promise.resolve(jsonResponse(200, { session: "s1" }));
    },
  });
  await client.createDetachedSession(CFG, { timeoutMs: 4000 });
  assert.ok(seenSignal, "an AbortSignal was passed to fetch");
  assert.equal(typeof seenSignal.aborted, "boolean");
});

test("the client surface exposes no delete or prune call (bounded tap-token blast radius)", () => {
  const { client } = loadClient({});
  for (const name of Object.keys(client)) {
    assert.doesNotMatch(
      name.toLowerCase(),
      /delete|prune|remove|destroy/,
      "unexpected destructive method on the control client: " + name,
    );
  }
  for (const expected of [
    "createDetachedSession",
    "triggerPipeline",
    "pollPipeline",
    "rotateSession",
    "checkHealth",
    "probeTapToken",
  ]) {
    assert.equal(typeof client[expected], "function", expected + " should be on the surface");
  }
});
