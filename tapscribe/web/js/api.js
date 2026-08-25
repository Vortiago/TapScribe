// @ts-check
// Thin wrappers over the backend's HTTP API. Each helper returns the
// parsed JSON / text or throws an Error with status + server-provided detail.
//
// The lazily-fetched, signature-keyed resources below are DECLARATIONS over the
// mechanism in ./lazy-resource.js — this module owns the URL, the cache key, and
// the failure policy; that one owns the cache, the stale-while-revalidate hold,
// and the per-tick `resolve` every view crosses (#222).

import { createResource } from "./lazy-resource.js";

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
 * Full merged session transcript, cached per (session, transcribedAt).
 * `transcribedAt` comes from the slim marker on /api/state; passing it means a
 * re-transcribe (new stamp) invalidates the cache while an idle poll reuses
 * the cached promise and fires no request. Resolves null when there's no
 * merged transcript.
 *
 * Held per SESSION: a re-transcribe must refresh the merged pane in place rather
 * than blanking the transcript the operator is reading (#266). Retries a failed
 * body on a later poll tick — the pane has a placeholder to sit behind and the
 * next `transcribed_at` is a fresh key anyway.
 */
export const sessionTranscript = createResource(
  (/** @type {string} */ session, /** @type {string} */ transcribedAt) => `${session}@${transcribedAt}`,
  (session) =>
    /** @type {Promise<import('./types.js').MergedTranscript | null>} */ (
      fetch(`/api/sessions/${encodeURIComponent(session)}/transcript`, { cache: "no-store" }).then(_unwrap)
    ),
  { holdKeyOf: (session) => session },
);

/**
 * Full persisted session summary, cached per (session, summarizedAt).
 * `summarizedAt` comes from the slim marker on /api/state; a re-generate (new
 * stamp) invalidates, an idle poll reuses the cached promise. Resolves null
 * when the session has no persisted summary.
 *
 * Held per SESSION so an EXTERNAL re-summarize (the end-of-meeting pipeline, a
 * second tab) refreshes the output pane in place instead of dropping it to the
 * "No summary yet" empty state for a whole round trip.
 */
export const sessionSummary = createResource(
  (/** @type {string} */ session, /** @type {string} */ summarizedAt) => `${session}@${summarizedAt}`,
  (session) =>
    /** @type {Promise<import('./types.js').PersistedSummary | null>} */ (
      fetch(`/api/sessions/${encodeURIComponent(session)}/summary`, { cache: "no-store" }).then(_unwrap)
    ),
  { holdKeyOf: (session) => session },
);

/**
 * The Voices a diarization run found, cached per (session, voicesSig).
 * `voicesSig` is /api/state's projection of each identity's run stamp, so a
 * re-diarize refetches and an idle poll fires nothing.
 *
 * Deliberately does NOT carry the operator's Voice-to-Person mapping: this body
 * changes only when a diarize runs, which is what makes `voicesSig` a valid key,
 * while a mapping changes on a click. The mapping rides session_meta on the
 * poll, and the panel joins the two.
 *
 * Held per SESSION: re-diarizing one tap flips the sig for the whole session, so
 * without the hold a second tap's rows would blank to "loading..." for a round
 * trip (#266's shape). `knownValue` answers the empty sig from the args alone --
 * an undiarized session has no body to fetch, and a later non-empty flip must
 * not resurrect pre-diarization rows as ghosts.
 */
export const sessionVoices = createResource(
  (/** @type {string} */ session, /** @type {string} */ voicesSig) => `${session}@${voicesSig}`,
  (session) =>
    /** @type {Promise<import('./types.js').SessionVoices>} */ (
      fetch(`/api/sessions/${encodeURIComponent(session)}/voices`, { cache: "no-store" }).then(_unwrap)
    ),
  {
    holdKeyOf: (/** @type {string} */ session) => session,
    knownValue: (/** @type {string} */ session, /** @type {string} */ voicesSig) =>
      voicesSig ? undefined : { session, identities: [] },
  },
);

/**
 * Full per-WAV transcript, cached per (session, name, source, transcribedAt).
 * Resolves null when the WAV has no cached transcript. Read through `fetch` from
 * a row's expand handler rather than a per-tick `resolve`, so it needs neither a
 * hold nor a failure policy.
 */
export const wavTranscript = createResource(
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
 * field on /api/state.
 *
 * An empty `filesSig` is a KNOWN empty listing rather than a fetch: there is no
 * folder on disk yet (or every WAV was just deleted) and the endpoint would 404.
 * Held per SESSION because `files_sig` flips once per TRACK during a batch
 * transcribe — without the hold both multi-track views blanked their WAV list on
 * every per-WAV completion (#266). The held array is the resource's own value BY
 * REFERENCE (the same array a view assigns to `currentFiles`), so callers must
 * treat the listing as read-only — mutating it in place would corrupt every
 * holder of it. Read it through `next/session-files.js`, which owns the
 * four-state derivation both views need.
 */
export const sessionFiles = createResource(
  (/** @type {string} */ session, /** @type {string} */ filesSig) => `${session}@${filesSig}`,
  (session) =>
    fetch(`/api/sessions/${encodeURIComponent(session)}/files`, { cache: "no-store" })
      .then(_unwrap)
      .then((r) => /** @type {import('./types.js').SessionFiles} */ (r).files || []),
  {
    holdKeyOf: (session) => session,
    knownValue: (session, filesSig) =>
      (!session || !filesSig ? /** @type {import('./types.js').WavFile[]} */ ([]) : undefined),
  },
);

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
 * source, sig) — sig is the WAV's byte size. Resolves the fixed-size downsample.
 *
 * `remember-error`, and NO hold: an unreadable WAV has no peaks, so re-asking
 * every poll tick for as long as the operator sits on the stage answers nothing —
 * the canvas shows the reason instead, until the byte size (a new key) changes.
 * Peaks belong to one (WAV, size); an older version of them would be the wrong
 * picture, not a stale one.
 */
export const wavePeaks = createResource(
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
  { onFailure: "remember-error" },
);

/**
 * The committed strip-silence cut for one ORIGINAL wav, cached per (session,
 * name, sig) — callers pass the session's stripped_at stamp as sig so a
 * re-strip busts the key. Resolves null when the wav has no committed cut.
 *
 * `remember-error` for the same reason as the peaks beside it (the sidecar either
 * parses or it doesn't), and no hold: a cut belongs to one `stripped_at`, so a
 * previous strip's spans would overlay the wrong picture.
 */
export const wavStripMeta = createResource(
  (/** @type {string} */ session, /** @type {string} */ name, /** @type {string} */ sig) =>
    `${session}/${name}@${sig}`,
  (session, name) =>
    /** @type {Promise<import('./types.js').WavStripMeta | null>} */ (
      getJson(`/api/wav/${encodeURIComponent(session)}/${encodeURIComponent(name)}/strip-meta`)
    ),
  { onFailure: "remember-error" },
);

/**
 * The bare-WAV route's URL — `GET /api/wav/{session}/{name}?source=…`.
 *
 * Here rather than at its callers because this module owns every other
 * /api/wav/* URL (wavTranscript, wavePeaks, wavStripMeta, fetchStripPreview),
 * and this one has TWO consumers that must not drift: the Recordings row's
 * download href and the Player's `src`. It builds a URL and fetches nothing, so
 * the DOM-free Player can import it without pulling in a request.
 * @param {{ session: string, name: string, source: "original" | "stripped" }} f
 * @returns {string}
 */
export function wavUrl(f) {
  return (
    `/api/wav/${encodeURIComponent(f.session)}/${encodeURIComponent(f.name)}` +
    `?source=${encodeURIComponent(f.source)}`
  );
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

// The save-status lifecycle (saving…/saved/failed) and the button + config-card
// wirings that use it live in ./save-status.js — this module stays the fetch /
// data layer, and that one imports errText + putJson from here (one direction,
// no cycle). `mutateButton` above stays: it's alert()-based, with no status cell.
