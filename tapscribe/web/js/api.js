// @ts-check
// Thin wrappers over the backend's HTTP API. Each helper returns the
// parsed JSON / text or throws an Error with status + server-provided detail.

/** The Error a non-OK response throws: `${status} ${detail}`, detail from the
 * JSON body when the server provided one, else the statusText. Async — it
 * reads the body. Shared by _unwrap and fetchState so the two fetch paths
 * can't drift.
 * @param {Response} r */
async function _httpError(r) {
  let detail = r.statusText;
  try { detail = (await r.json()).detail || detail; } catch { /* not JSON */ }
  return new Error(`${r.status} ${detail}`);
}

/** Human-readable message for a caught error — String() minus the "Error: " prefix. @param {unknown} e */
export const errText = (e) => String(e).replace(/^Error:\s*/, "");

/** @param {Response} r */
async function _unwrap(r) {
  if (r.ok) {
    const ct = r.headers.get("content-type") || "";
    return ct.includes("application/json") ? r.json() : r.text();
  }
  throw await _httpError(r);
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
  if (!r.ok) throw await _httpError(r);
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

// Bound the caches so a long-lived tab that opens hundreds of (id,
// transcribed_at) pairs over its lifetime doesn't grow unbounded. Map
// preserves insertion order, so dropping `keys().next()` evicts the oldest.
const _TX_CACHE_MAX = 64;
/** @param {Map<string, unknown> | Set<string>} cache */
function _capCache(cache) {
  while (cache.size > _TX_CACHE_MAX) {
    const oldest = cache.keys().next().value;
    if (oldest === undefined) break;
    cache.delete(oldest);
  }
}

/**
 * Insert/refresh `key` as the MOST-recently-used entry. `Map.set` on an
 * EXISTING key does NOT move it in insertion order, so a key re-set every tick
 * (loadSessionFiles re-records the FOCUSED session's listing on every poll)
 * stayed at the OLDEST position and was the first thing `_capCache` dropped —
 * i.e. once a tab had focused more than `_TX_CACHE_MAX` distinct sessions, the
 * session IN USE was evicted on every call. Invisible while files_sig held
 * still, but the moment a sibling WAV finished transcribing and the sig
 * flipped, `_lastGoodFiles.get(session) ?? null` yielded the COLD-load
 * sentinel and both WAV lists blanked to "loading…" on every per-track
 * completion — #266, resurrected for exactly the session being worked on.
 * Deleting first makes the hot key most-recently-used before the cap runs.
 * @template T
 * @param {Map<string, T>} cache
 * @param {string} key
 * @param {T} value
 */
function _setMru(cache, key, value) {
  cache.delete(key);
  cache.set(key, value);
  _capCache(cache);
}

/**
 * One lazily-fetched, signature-keyed resource: a bounded cache Map plus a
 * get-or-fetch and a synchronous peek that share the same key function, so
 * the two can never drift apart. `fetch` fires `load()` once per key and
 * records the resolved value on the entry so `peek` can read it without
 * touching the Promise (a render uses the cached value inline without
 * re-rendering when it's already in hand). A rejection evicts the key so a
 * later call retries.
 * @template {unknown[]} A
 * @template T
 * @param {(...args: A) => string} keyOf
 * @param {(...args: A) => Promise<T>} load
 */
function _resource(keyOf, load) {
  /** @type {Map<string, TxEntry<T>>} */
  const cache = new Map();
  return {
    /** @param {A} args @returns {Promise<T>} */
    fetch(...args) {
      const key = keyOf(...args);
      const hit = cache.get(key);
      if (hit) return hit.promise;
      /** @type {TxEntry<T>} */
      const entry = { promise: Promise.resolve(/** @type {T} */ (undefined)), settled: false, value: undefined };
      entry.promise = load(...args)
        .then((v) => { entry.settled = true; entry.value = v; return v; })
        .catch((e) => { cache.delete(key); throw e; });
      cache.set(key, entry);
      _capCache(cache);
      return entry.promise;
    },
    /** @param {A} args @returns {T | undefined} */
    peek(...args) {
      const e = cache.get(keyOf(...args));
      return e && e.settled ? e.value : undefined;
    },
  };
}

/**
 * Full merged session transcript, cached per (session, transcribedAt).
 * `transcribedAt` comes from the slim marker on /api/state; passing it means a
 * re-transcribe (new stamp) invalidates the cache while an idle poll reuses
 * the cached promise and fires no request. Resolves null when there's no
 * merged transcript. `.fetch(session, transcribedAt)` / `.peek(...)` — peek
 * returns the resolved value if the fetch already settled, else undefined.
 */
export const sessionTranscript = _resource(
  (/** @type {string} */ session, /** @type {string} */ transcribedAt) => `${session}@${transcribedAt}`,
  (session) =>
    /** @type {Promise<import('./types.js').MergedTranscript | null>} */ (
      fetch(`/api/sessions/${encodeURIComponent(session)}/transcript`, { cache: "no-store" }).then(_unwrap)
    ),
);

/**
 * Full persisted session summary, cached per (session, summarizedAt).
 * `summarizedAt` comes from the slim marker on /api/state; a re-generate (new
 * stamp) invalidates, an idle poll reuses the cached promise. Resolves null
 * when the session has no persisted summary.
 */
export const sessionSummary = _resource(
  (/** @type {string} */ session, /** @type {string} */ summarizedAt) => `${session}@${summarizedAt}`,
  (session) =>
    /** @type {Promise<import('./types.js').PersistedSummary | null>} */ (
      fetch(`/api/sessions/${encodeURIComponent(session)}/summary`, { cache: "no-store" }).then(_unwrap)
    ),
);

/**
 * Full per-WAV transcript, cached per (session, name, source, transcribedAt).
 * Resolves null when the WAV has no cached transcript.
 */
export const wavTranscript = _resource(
  (
    /** @type {string} */ session,
    /** @type {string} */ name,
    /** @type {"original" | "stripped"} */ source,
    /** @type {string} */ transcribedAt,
  ) => `${session}/${name}@${source}@${transcribedAt}`,
  (session, name, source) => {
    const qs = source === "stripped" ? "?source=stripped" : "";
    const url = `/api/wav/${encodeURIComponent(session)}/${encodeURIComponent(name)}/transcript${qs}`;
    return /** @type {Promise<import('./types.js').WavTranscript | null>} */ (
      fetch(url, { cache: "no-store" }).then(_unwrap)
    );
  },
);

// ---- Lazy per-session file listing + client cache ------------------------
//
// /api/state no longer embeds each session's per-WAV `files[]` (a huge session
// re-shipped + re-parsed O(WAVs) every ~0.5s tick). It carries a `files_sig`
// instead; the full listing is fetched here on demand and cached per
// (session, files_sig), so it crosses the wire ONCE per change — a new WAV /
// re-transcribe / strip flips files_sig and busts the key, while an idle poll
// reuses the cached promise and fires no request.

/**
 * The full per-session WAV listing (originals + their stripped regions),
 * cached per (session, filesSig). `filesSig` comes from the slim `files_sig`
 * field on /api/state. Callers MUST skip the call when files_sig is "" (no
 * folder on disk yet → the endpoint would 404).
 */
export const sessionFiles = _resource(
  (/** @type {string} */ session, /** @type {string} */ filesSig) => `${session}@${filesSig}`,
  (session) =>
    fetch(`/api/sessions/${encodeURIComponent(session)}/files`, { cache: "no-store" })
      .then(_unwrap)
      .then((r) => /** @type {import('./types.js').SessionFiles} */ (r).files || []),
);

/**
 * Last resolved WAV listing per session — the stale-while-revalidate memory that
 * keeps a files_sig FLIP from blanking the view. `files_sig` flips whenever a
 * WAV is added / re-recorded / (re-)transcribed, so during a batch transcribe it
 * flips ONCE PER TRACK as each finishes. Returning `null` on every flip made both
 * multi-track views (Recordings, Transcript) blank the whole WAV list — and the
 * Recordings header + waveform + stats — to a "loading…" placeholder for the
 * round-trip until the fresh listing landed, then rebuild it: a visible blink on
 * every per-WAV completion. Holding the last good listing here refreshes the list
 * IN PLACE instead (the view reconciles by key when the fresh data lands via
 * onLand). Capped like the sibling resource caches, so a long-lived tab that
 * browses many sessions doesn't retain every listing — through `_setMru`, so
 * the session being polled right now is the LAST thing evicted, not the first.
 * The stored array is the resource's own value BY REFERENCE (the same array the
 * view assigns to `currentFiles`), so callers must treat the listing as
 * read-only — mutating it in place would corrupt every holder of it.
 * @type {Map<string, import('./types.js').WavFile[]>} */
const _lastGoodFiles = new Map();

/**
 * Keys (`session@files_sig`) whose last /files fetch REJECTED — the failure
 * memory that paces retries at the poll cadence (mirrors the `failedWave` /
 * `failedCutMeta` discipline in recordings.js). Without it, a rejection
 * evicted the cache key (api.js `_resource`) and the settle callback's
 * re-render re-entered here synchronously with `pending` already cleared, so
 * a persistently-failing endpoint was refetched in a tight unpaced loop at
 * HTTP-response rate (plus a full re-render per iteration). A remembered key
 * skips exactly one call — the caller invokes this once per poll tick — so
 * the retry fires on a LATER tick; a sig change is a different key and
 * fetches immediately. Capped like the sibling caches.
 * @type {Set<string>} */
const _failedFiles = new Set();

/**
 * Resolve a focused session's WAV listing for a per-tick render, the shape both
 * the Recordings and Transcript views need: returns the cached array when it's
 * in hand, `[]` when there's nothing to fetch (empty `filesSig` → no folder /
 * no WAVs yet), the session's last-good listing while a NEWER sig's fetch is in
 * flight (stale-while-revalidate — a refresh must not blank the view), or `null`
 * only on a genuine COLD load (a session with no last-good listing yet → the
 * caller shows a loading placeholder). On a cache miss it fires the fetch ONCE
 * (deduped via the caller's `pending` set across the ticks before it lands) and
 * calls `onLand` when it SUCCEEDS so the view can drop its render gates and
 * reconcile the fresh list in place. A rejected fetch never calls `onLand`
 * (nothing changed to re-render — the stale hold stays up) and is remembered
 * per key (`_failedFiles`), so the retry is paced by the poll instead of the
 * failure's own re-render refiring it synchronously.
 * @param {string} session
 * @param {string} filesSig
 * @param {Set<string>} pending - per-view in-flight (session@filesSig) keys
 * @param {() => void} onLand - run after a SUCCESSFUL fetch lands
 * @returns {import('./types.js').WavFile[] | null}
 */
export function loadSessionFiles(session, filesSig, pending, onLand) {
  if (!session) return [];
  if (!filesSig) {
    // No files on disk yet, or every WAV was just deleted (files_sig == ""):
    // this session's last-good IS now empty. Record that, so a later non-empty
    // flip (a new tap records in) shows the empty state through the stale path
    // rather than resurrecting the pre-deletion rows as ghosts.
    _setMru(_lastGoodFiles, session, []);
    return [];
  }
  const cached = sessionFiles.peek(session, filesSig);
  if (cached !== undefined) {
    _setMru(_lastGoodFiles, session, cached);
    return cached;
  }
  const k = `${session}@${filesSig}`;
  // `_failedFiles.delete(k)` is check-AND-consume: a key whose last fetch
  // failed skips this one call, and the next call — the next poll tick —
  // retries (see `_failedFiles`' doc for why the skip is load-bearing).
  if (!pending.has(k) && !_failedFiles.delete(k)) {
    pending.add(k);
    sessionFiles.fetch(session, filesSig)
      .then(onLand, () => {
        // Transient failure: remember it (paces the retry to a later poll
        // tick) and do NOT call onLand — nothing changed to re-render, and
        // the failure's own re-render refiring the fetch was the retry storm.
        _failedFiles.add(k);
        _capCache(_failedFiles);
      })
      .finally(() => { pending.delete(k); });
  }
  // Stale-while-revalidate: hold this session's last-good listing during the
  // refetch; `null` only on a cold load (a session that never resolved yet).
  return _lastGoodFiles.get(session) ?? null;
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
 * Server-computed waveform peaks for one WAV, cached per (session, name,
 * source, sig) — sig is the WAV's byte size. Resolves the fixed-size
 * downsample; rejects (and evicts the key, so a later call retries) when the
 * WAV can't be read as peaks.
 */
export const wavePeaks = _resource(
  (
    /** @type {string} */ session,
    /** @type {string} */ name,
    /** @type {"original" | "stripped"} */ source,
    /** @type {string} */ sig,
  ) => `${session}/${name}@${source}@${sig}`,
  (session, name, source) => {
    const qs = new URLSearchParams({ bins: String(WAVE_PEAK_BINS) });
    if (source === "stripped") qs.set("source", "stripped");
    const url = `/api/wav/${encodeURIComponent(session)}/${encodeURIComponent(name)}/peaks?${qs}`;
    return /** @type {Promise<import('./types.js').WavePeaks>} */ (
      fetch(url, { cache: "no-store" }).then(_unwrap)
    );
  },
);

/**
 * The committed strip-silence cut for one ORIGINAL wav, cached per (session,
 * name, sig) — callers pass the session's stripped_at stamp as sig so a
 * re-strip busts the key. Resolves null when the wav has no committed cut.
 */
export const wavStripMeta = _resource(
  (/** @type {string} */ session, /** @type {string} */ name, /** @type {string} */ sig) =>
    `${session}/${name}@${sig}`,
  (session, name) =>
    /** @type {Promise<import('./types.js').WavStripMeta | null>} */ (
      getJson(`/api/wav/${encodeURIComponent(session)}/${encodeURIComponent(name)}/strip-meta`)
    ),
);

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

// ONE memoized fetch of the summarizer catalog (models + command presets +
// max-token bounds): the Summary view and the Settings summarizer-default
// card both populate from it, and the boot-time view rebuild would otherwise
// re-fetch — this dedupes all of them to one request per page. A rejection
// clears the memo so the next view build retries instead of inheriting a
// poisoned promise.
/** @type {Promise<import('./types.js').SummaryModelCatalog> | null} */
let _summaryCatalog = null;
/** @returns {Promise<import('./types.js').SummaryModelCatalog>} */
export function getSummaryCatalog() {
  if (!_summaryCatalog) {
    _summaryCatalog = getJson("/api/summarize/models");
    _summaryCatalog.catch(() => { _summaryCatalog = null; });
  }
  return _summaryCatalog;
}

// ONE memoized fetch of the bridge-download catalog (GET /api/bridges): the
// Settings "Get a bridge" card fills its release-asset hrefs from it on
// build, and the boot-time view rebuild (loadModelCatalogs → viewCache
// clear + re-render, when the first /api/state tick lands before the model
// catalogs) would otherwise re-fetch — this dedupes to one request per page,
// same shape as getSummaryCatalog above (and what the "exactly ONE
// /api/bridges fetch" e2e pins). A rejection clears the memo so the next
// view build retries instead of inheriting a poisoned promise.
/** @type {Promise<{ id: string, download_url: string }[]> | null} */
let _bridgeCatalog = null;
/** @returns {Promise<{ id: string, download_url: string }[]>} */
export function getBridgeCatalog() {
  if (!_bridgeCatalog) {
    _bridgeCatalog = getJson("/api/bridges");
    _bridgeCatalog.catch(() => { _bridgeCatalog = null; });
  }
  return _bridgeCatalog;
}

// The disable/await-mutate/catch-alert/finally-reenable core shared by
// wireToggles (components/active-taps.js) and wireRecPill (next/shell.js) —
// both bind their OWN click listener (each needs its own pre-mutate DOM step:
// the tap-toggle's optimistic flip, the rec-pill's getState() read) and then
// hand the actual network call to this core instead of duplicating the
// disable/catch/finally wrapping. Lives here (not in either caller's module)
// since both already import from api.js.
/**
 * @param {HTMLButtonElement} btn
 * @param {() => Promise<unknown>} mutate
 * @param {{ afterMutate: () => void, failMessage: (e: unknown) => string }} opts
 */
export function mutateButton(btn, mutate, { afterMutate, failMessage }) {
  btn.disabled = true;
  return mutate()
    .catch((e) => { alert(failMessage(e)); })
    .finally(() => { btn.disabled = false; afterMutate(); });
}

// Wire a save button to an async PUT with the shared status-badge lifecycle
// (saving… / saved / failed, success badge clears after 1.5s). The generic
// core under wireConfigSave; structured saves (the #84 summarizer-default
// card, the Summary view's per-session override) call it with their own
// `put` instead of the /api/config/{key} content shape.
/**
 * `onSuccess` fires only on a successful put; `afterSettle` runs in the
 * finally (success OR failure) — for callers that re-poll either way (e.g.
 * the Capture view's per-session override saves).
 * @param {{
 *   btn: HTMLButtonElement,
 *   status: HTMLElement | null,
 *   put: () => Promise<unknown>,
 *   onSuccess?: (() => void) | undefined,
 *   afterSettle?: (() => void) | undefined,
 * }} opts
 */
export function wireSave({ btn, status, put, onSuccess, afterSettle }) {
  btn.addEventListener("click", async () => { // gate-allow: signal-listener — wireSave wires the button once when the caller builds it; the listener dies with the button
    if (!status) return;
    btn.disabled = true;
    status.textContent = "saving…";
    try {
      await put();
      status.textContent = "saved";
      onSuccess?.();
      setTimeout(() => { if (status.textContent === "saved") status.textContent = ""; }, 1500);
    } catch (e) {
      status.textContent = `failed: ${errText(e)}`;
    } finally {
      btn.disabled = false;
      afterSettle?.();
    }
  });
}

// Wire a textarea + save button to PUT /api/config/{key}. Used by both the
// "default config" card editors and the live-channel's init-prompt
// expandable. The {content: textarea.value} specialisation of wireSave.
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
  if (!textarea) return;
  wireSave({
    btn,
    status,
    put: () => putJson(`/api/config/${key}`, { content: textarea.value }),
    onSuccess: () => onSuccess?.(textarea.value),
  });
}
