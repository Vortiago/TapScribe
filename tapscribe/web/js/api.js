// @ts-check
// Thin wrappers over the backend's HTTP API. Each helper returns the
// parsed JSON / text or throws an Error with status + server-provided detail.

/** @param {Response} r */
async function _unwrap(r) {
  if (r.ok) {
    const ct = r.headers.get("content-type") || "";
    return ct.includes("application/json") ? r.json() : r.text();
  }
  let detail = r.statusText;
  try { detail = (await r.json()).detail || detail; } catch { /* not JSON */ }
  throw new Error(`${r.status} ${detail}`);
}

/** @param {unknown} body */
const _body = (body) => ({
  headers: { "content-type": "application/json" },
  body: JSON.stringify(body ?? {}),
});

// /api/state is polled every ~0.5-1s. The server returns a weak ETag; we send
// it back as If-None-Match and reuse the last parsed state on a 304, so an idle
// poll skips the body transfer + JSON parse + state-object allocation. Module
// state is fine — there's one poller per page.
/** @type {string | null} */
let _stateEtag = null;
/** @type {import('./types.js').AppState | null} */
let _lastState = null;
export async function fetchState() {
  const r = await fetch("/api/state", {
    cache: "no-store",
    headers: _stateEtag ? { "If-None-Match": _stateEtag } : undefined,
  });
  if (r.status === 304 && _lastState !== null) return _lastState;
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch { /* not JSON */ }
    throw new Error(`${r.status} ${detail}`);
  }
  _stateEtag = r.headers.get("ETag");
  _lastState = /** @type {import('./types.js').AppState} */ (await r.json());
  return _lastState;
}

// ---- Lazy transcript fetch + client cache --------------------------------
//
// /api/state's `session_transcript` and per-WAV `transcript` are now slim
// MARKERS (transcribed_at + counts), not the full bodies — the poll used to
// ship megabytes of segments[]/text every ~0.5s. The full transcript is
// fetched on demand here and cached keyed by (id, transcribed_at), so it
// crosses the wire ONCE per (session/wav, transcribed_at) — a re-transcribe
// bumps transcribed_at and busts the key; an idle poll reuses the cached
// promise and fires no network request. The cached value is a Promise so two
// near-simultaneous callers (e.g. a render + a prefetch) share one in-flight
// request.

/**
 * @template T
 * @typedef {{ promise: Promise<T>, settled: boolean, value: T | undefined }} TxEntry
 */

/** @type {Map<string, TxEntry<import('./types.js').MergedTranscript | null>>} */
const _sessionTxCache = new Map();
/** @type {Map<string, TxEntry<import('./types.js').PersistedSummary | null>>} */
const _sessionSummaryCache = new Map();
/** @type {Map<string, TxEntry<import('./types.js').WavTranscript | null>>} */
const _wavTxCache = new Map();
/** @type {Map<string, TxEntry<import('./types.js').WavePeaks>>} */
const _wavPeaksCache = new Map();
/** @type {Map<string, TxEntry<import('./types.js').WavStripMeta | null>>} */
const _wavStripMetaCache = new Map();

// Bound the caches so a long-lived tab that opens hundreds of (id,
// transcribed_at) pairs over its lifetime doesn't grow unbounded. Map
// preserves insertion order, so dropping `keys().next()` evicts the oldest.
const _TX_CACHE_MAX = 64;
/** @param {Map<string, unknown>} cache */
function _capCache(cache) {
  while (cache.size > _TX_CACHE_MAX) {
    const oldest = cache.keys().next().value;
    if (oldest === undefined) break;
    cache.delete(oldest);
  }
}

/**
 * Shared get-or-fetch with a settled-value side-channel. On miss, fires
 * `make()` once, stores the entry, and records the resolved value on the same
 * entry so a synchronous peek can read it without touching the Promise. A
 * rejection evicts the key so a later call retries.
 * @template T
 * @param {Map<string, TxEntry<T>>} cache
 * @param {string} key
 * @param {() => Promise<T>} make
 * @returns {Promise<T>}
 */
function _getOrFetch(cache, key, make) {
  const hit = cache.get(key);
  if (hit) return hit.promise;
  /** @type {TxEntry<T>} */
  const entry = { promise: Promise.resolve(/** @type {T} */ (undefined)), settled: false, value: undefined };
  entry.promise = make()
    .then((v) => { entry.settled = true; entry.value = v; return v; })
    .catch((e) => { cache.delete(key); throw e; });
  cache.set(key, entry);
  _capCache(cache);
  return entry.promise;
}

/**
 * Full merged session transcript, cached per (session, transcribedAt).
 * `transcribedAt` comes from the slim marker on /api/state; passing it means a
 * re-transcribe (new stamp) invalidates the cache while an idle poll reuses
 * the cached promise and fires no request. Returns null when there's no
 * merged transcript.
 * @param {string} session
 * @param {string} transcribedAt
 * @returns {Promise<import('./types.js').MergedTranscript | null>}
 */
export function fetchSessionTranscript(session, transcribedAt) {
  return _getOrFetch(_sessionTxCache, `${session}@${transcribedAt}`, () =>
    fetch(`/api/sessions/${encodeURIComponent(session)}/transcript`, { cache: "no-store" }).then(_unwrap),
  );
}

/**
 * Synchronous peek: the resolved merged transcript for (session,
 * transcribedAt) if its fetch already settled, else undefined. Lets a render
 * use the cached value inline without re-rendering when it's already in hand.
 * @param {string} session
 * @param {string} transcribedAt
 * @returns {import('./types.js').MergedTranscript | null | undefined}
 */
export function peekSessionTranscript(session, transcribedAt) {
  const e = _sessionTxCache.get(`${session}@${transcribedAt}`);
  return e && e.settled ? e.value : undefined;
}

/**
 * Full persisted session summary, cached per (session, summarizedAt).
 * `summarizedAt` comes from the slim marker on /api/state; passing it means a
 * re-generate (new stamp) invalidates the cache while an idle poll reuses the
 * cached promise and fires no request. Returns null when the session has no
 * persisted summary.
 * @param {string} session
 * @param {string} summarizedAt
 * @returns {Promise<import('./types.js').PersistedSummary | null>}
 */
export function fetchSessionSummary(session, summarizedAt) {
  return _getOrFetch(_sessionSummaryCache, `${session}@${summarizedAt}`, () =>
    fetch(`/api/sessions/${encodeURIComponent(session)}/summary`, { cache: "no-store" }).then(_unwrap),
  );
}

/**
 * Synchronous peek: the resolved summary for (session, summarizedAt) if its
 * fetch already settled, else undefined.
 * @param {string} session
 * @param {string} summarizedAt
 * @returns {import('./types.js').PersistedSummary | null | undefined}
 */
export function peekSessionSummary(session, summarizedAt) {
  const e = _sessionSummaryCache.get(`${session}@${summarizedAt}`);
  return e && e.settled ? e.value : undefined;
}

/**
 * Full per-WAV transcript, cached per (session, name, source, transcribedAt).
 * Returns null when the WAV has no cached transcript.
 * @param {string} session
 * @param {string} name
 * @param {"original" | "stripped"} source
 * @param {string} transcribedAt
 * @returns {Promise<import('./types.js').WavTranscript | null>}
 */
export function fetchWavTranscript(session, name, source, transcribedAt) {
  const qs = source === "stripped" ? "?source=stripped" : "";
  const url = `/api/wav/${encodeURIComponent(session)}/${encodeURIComponent(name)}/transcript${qs}`;
  return _getOrFetch(_wavTxCache, `${session}/${name}@${source}@${transcribedAt}`, () =>
    fetch(url, { cache: "no-store" }).then(_unwrap),
  );
}

/**
 * Synchronous peek for a per-WAV transcript — see `peekSessionTranscript`.
 * @param {string} session
 * @param {string} name
 * @param {"original" | "stripped"} source
 * @param {string} transcribedAt
 * @returns {import('./types.js').WavTranscript | null | undefined}
 */
export function peekWavTranscript(session, name, source, transcribedAt) {
  const e = _wavTxCache.get(`${session}/${name}@${source}@${transcribedAt}`);
  return e && e.settled ? e.value : undefined;
}

// ---- Waveform peaks fetch + client cache ---------------------------------
//
// Same lazy-cache shape as the per-WAV transcript above, keyed by a file
// SIGNATURE (the WAV's byte size) instead of a transcribed_at stamp: a
// re-recording changes the size and busts the key, while the ~0.5s /api/state
// poll reuses the cached promise and fires no request. The payload is a fixed
// `bins` floats regardless of recording length, so one fetch per (WAV, source)
// is all the waveform ever needs.

/** Downsample resolution the waveform component requests. Shared client/server
 * default; one value because the dashboard never needs a second resolution. */
export const WAVE_PEAK_BINS = 800;

/**
 * @param {string} session
 * @param {string} name
 * @param {"original" | "stripped"} source
 * @param {string} sig
 */
const _peaksKey = (session, name, source, sig) => `${session}/${name}@${source}@${sig}`;

/**
 * Server-computed waveform peaks for one WAV, cached per (session, name,
 * source, sig). Returns the fixed-size downsample; rejects (and evicts the
 * key, so a later call retries) when the WAV can't be read as peaks.
 * @param {string} session
 * @param {string} name
 * @param {"original" | "stripped"} source
 * @param {string} sig
 * @returns {Promise<import('./types.js').WavePeaks>}
 */
export function fetchWavePeaks(session, name, source, sig) {
  const qs = new URLSearchParams({ bins: String(WAVE_PEAK_BINS) });
  if (source === "stripped") qs.set("source", "stripped");
  const url = `/api/wav/${encodeURIComponent(session)}/${encodeURIComponent(name)}/peaks?${qs}`;
  return _getOrFetch(_wavPeaksCache, _peaksKey(session, name, source, sig), () =>
    fetch(url, { cache: "no-store" }).then(_unwrap),
  );
}

/**
 * Synchronous peek — the resolved peaks for (session, name, source, sig) if
 * the fetch already settled, else undefined. See `peekWavTranscript`.
 * @param {string} session
 * @param {string} name
 * @param {"original" | "stripped"} source
 * @param {string} sig
 * @returns {import('./types.js').WavePeaks | undefined}
 */
export function peekWavePeaks(session, name, source, sig) {
  const e = _wavPeaksCache.get(_peaksKey(session, name, source, sig));
  return e && e.settled ? e.value : undefined;
}

/**
 * @param {string} session
 * @param {string} name
 * @param {string} sig
 */
const _stripMetaKey = (session, name, sig) => `${session}/${name}@${sig}`;

/**
 * The committed strip-silence cut for one ORIGINAL wav, cached per (session,
 * name, sig) — callers pass the session's stripped_at stamp as sig so a
 * re-strip busts the key. Resolves null when the wav has no committed cut.
 * @param {string} session
 * @param {string} name
 * @param {string} sig
 * @returns {Promise<import('./types.js').WavStripMeta | null>}
 */
export function fetchWavStripMeta(session, name, sig) {
  const url = `/api/wav/${encodeURIComponent(session)}/${encodeURIComponent(name)}/strip-meta`;
  return _getOrFetch(_wavStripMetaCache, _stripMetaKey(session, name, sig), () =>
    fetch(url, { cache: "no-store" }).then(_unwrap),
  );
}

/**
 * Synchronous peek — the resolved strip-meta for (session, name, sig) if the
 * fetch already settled, else undefined. See `peekWavTranscript`.
 * @param {string} session
 * @param {string} name
 * @param {string} sig
 * @returns {import('./types.js').WavStripMeta | null | undefined}
 */
export function peekWavStripMeta(session, name, sig) {
  const e = _wavStripMetaCache.get(_stripMetaKey(session, name, sig));
  return e && e.settled ? e.value : undefined;
}

/**
 * What ✂ strip WOULD cut for one WAV at the given knobs — the live
 * strip-preview (#89). Deliberately NOT cached: the knob space is unbounded
 * and the caller debounces; latest-wins is the view's request token's job.
 * @param {string} session
 * @param {string} name
 * @param {import('./types.js').StripOpts} knobs
 * @returns {Promise<import('./types.js').StripPreview>}
 */
export function fetchStripPreview(session, name, knobs) {
  const qs = new URLSearchParams({
    min_silence_ms: String(knobs.min_silence_ms),
    pad_ms: String(knobs.pad_ms),
    speech_floor_db: String(knobs.speech_floor_db),
  });
  return getJson(`/api/wav/${encodeURIComponent(session)}/${encodeURIComponent(name)}/strip-preview?${qs}`);
}

/** @param {string} url */
export const getJson = (url) => fetch(url, { cache: "no-store" }).then(_unwrap);
/**
 * @param {string} url
 * @param {unknown} [body]
 */
export const postJson = (url, body) => fetch(url, { method: "POST", ..._body(body) }).then(_unwrap);
/**
 * @param {string} url
 * @param {unknown} [body]
 */
export const putJson  = (url, body) => fetch(url, { method: "PUT",  ..._body(body) }).then(_unwrap);
/** @param {string} url */
export const del      = (url)       => fetch(url, { method: "DELETE" }).then(_unwrap);

// Wire a textarea + save button to PUT /api/config/{key}. Used by both the
// "default config" card editors and the live-channel's init-prompt
// expandable. Manages the status badge lifecycle (saving / saved / failed)
// and clears the success badge after a short delay.
/**
 * @param {{
 *   key: string,
 *   btn: HTMLButtonElement,
 *   textarea: HTMLTextAreaElement | HTMLInputElement | null,
 *   status: HTMLElement | null,
 *   onSuccess?: ((value: string) => void) | undefined,
 * }} opts
 */
export function wireConfigSave({ key, btn, textarea, status, onSuccess }) {
  btn.addEventListener("click", async () => {
    if (!textarea || !status) return;
    btn.disabled = true;
    status.textContent = "saving…";
    try {
      await putJson(`/api/config/${key}`, { content: textarea.value });
      status.textContent = "saved";
      onSuccess?.(textarea.value);
      setTimeout(() => { if (status.textContent === "saved") status.textContent = ""; }, 1500);
    } catch (e) {
      status.textContent = `failed: ${String(e).replace(/^Error:\s*/, "")}`;
    } finally {
      btn.disabled = false;
    }
  });
}
