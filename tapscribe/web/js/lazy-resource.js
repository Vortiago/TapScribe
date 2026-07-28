// @ts-check
// The **lazy resource** mechanism: one signature-keyed client cache plus the
// per-tick `resolve` every /next region that reads a lazily-fetched body
// crosses. api.js declares the resources (URLs, keys, failure policy) over this;
// the mechanism lives here so `resolve` sits in the same module as the cache it
// drives and cannot drift from `peek`/`fetch`.
//
// DOM-free and fetch-free — `load` is injected — so the state machine is
// unit-tested under `node --test` (lazy-resource.test.js), like save-status.js
// and field-saver.js.

/**
 * One cache entry. `order` is the resource-wide fire sequence, so a response can
 * be told apart from a NEWER one for the same body — two requests for one session
 * are in flight whenever a signature flips faster than a round trip.
 * @template T
 * @typedef {{ promise: Promise<T>, settled: boolean, value: T | undefined, order: number }} Entry
 */

// Bound the caches so a long-lived tab that opens hundreds of (id, stamp) pairs
// over its lifetime doesn't grow unbounded. Map preserves insertion order, so
// dropping `keys().next()` evicts the oldest.
const _CACHE_MAX = 64;
/** @param {Map<string, unknown> | Set<string>} cache */
function _capCache(cache) {
  while (cache.size > _CACHE_MAX) {
    const oldest = cache.keys().next().value;
    if (oldest === undefined) break;
    cache.delete(oldest);
  }
}

/**
 * Insert/refresh `key` as the MOST-recently-used entry, then cap. `Map.set` on
 * an EXISTING key does NOT move it in insertion order, so a key re-set every
 * tick sat at the OLDEST position and was the first thing `_capCache` dropped —
 * i.e. the entry IN USE was evicted first, resurrecting #266 (a sig flip found
 * no hold, so the region blanked to "loading…") for exactly the session being
 * worked on. Deleting first makes the hot key most-recently-used before the cap
 * runs.
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
 * @template T
 * @typedef {{
 *   hold(key: string, value: T): void,
 *   get(key: string): T | null,
 * }} LastGoodHold
 */

/**
 * A bounded, MRU-ordered **last-good hold** — the stale-while-revalidate memory
 * a lazily-fetched, signature-keyed region needs so a sig FLIP refreshes it in
 * place instead of blanking it (#266). `hold(key, value)` records the latest
 * resolved value; `get(key)` returns it, or `null` when nothing was EVER
 * resolved for that key — the cold-load sentinel, the one case a caller may
 * render as a placeholder.
 *
 * Private to this module: a resource with a `holdKeyOf` policy keeps its own,
 * and `resolve` is the only door. When the three holds were hand-rolled Maps at
 * their call sites, the MRU fix above landed in one of them while the other two —
 * carrying the BIGGER payloads and unbounded — kept both bugs.
 * @template T
 * @returns {LastGoodHold<T>}
 */
function createLastGoodHold() {
  /** @type {Map<string, T>} */
  const cache = new Map();
  return {
    hold(key, value) { _setMru(cache, key, value); },
    get(key) { return cache.get(key) ?? null; },
  };
}

/**
 * What one `resolve` call tells its caller.
 * @template T
 * @typedef {{
 *   value: T | null,
 *   loading: boolean,
 *   stale: boolean,
 *   error: unknown,
 * }} Resolved
 *
 * - `value`   — the resolved body, the last-good body during a refetch, or
 *               `null` when there is nothing to show. A body that legitimately
 *               resolves to `null` (no transcript / no summary) reports
 *               `loading: false`, so the two are distinguishable.
 * - `loading` — nothing to show and a fetch is in flight (or queued for the
 *               next tick). The one case a caller may paint a placeholder.
 * - `stale`   — `value` is the last-good body, NOT this key's own; a fetch is in
 *               flight behind it. A caller whose render gate is keyed on the
 *               SIGNATURE must include this, or the swap from provisional to
 *               fresh content carries no signature change and is skipped.
 * - `error`   — the rejection from the last failed fetch, under the
 *               `remember-error` policy only; `null` otherwise.
 */

/**
 * A load is in flight (or queued for the next tick) and `held` is whatever there
 * is to show meanwhile — the stale body, or null on a genuine cold load, which is
 * the ONE case a caller may paint a placeholder for.
 * @template T
 * @param {T | null} held
 * @returns {Resolved<T>}
 */
const _pending = (held) => ({ value: held, loading: held === null, stale: held !== null, error: null });

/**
 * One lazily-fetched, signature-keyed resource: a bounded cache Map, a
 * get-or-fetch, a synchronous peek, and `watch` — all sharing one key function,
 * so they can never drift apart.
 *
 * `fetch` fires `load()` once per key and records the resolved value on the
 * entry so `peek` can read it without touching the Promise. A rejection evicts
 * the key so a later call retries.
 *
 * **`watch(onLand)` is the per-tick door**: it binds ONE consumer's repaint
 * callback and returns a reader whose `resolve(args)` a view calls each tick.
 * The callback is bound once, at build time, rather than passed per call, for the
 * same reason `createFieldSaver` takes `afterSave` at construction — the waiting
 * callbacks for a key are deduped by identity, so a fresh closure per tick would
 * repaint once per tick that missed. Binding makes that unwritable instead of
 * merely documented. One resource has many watchers (the WAV listing has two
 * views); they share the fetch and each get their own land.
 *
 * The **failure policy** is declared here, once per resource, because it
 * is a decision and not a property of the ceremony around the call site — the
 * five hand-rolled copies this replaced had three different answers, none of
 * them chosen (#222):
 *
 * - `retry-next-poll` (default) — a rejection is silent and the key retries on a
 *   LATER tick. For a body whose absence is transient (the endpoint was
 *   restarting) and whose region has a placeholder to sit behind.
 * - `remember-error` — the rejection is REPORTED (`error`) and the key is not
 *   refetched until the signature changes. For a body whose absence is a
 *   property of the file (an unreadable WAV has no peaks, and asking again every
 *   500 ms for as long as the operator sits on the stage answers nothing).
 *
 * `holdKeyOf` turns on **stale-while-revalidate**: it names the thing the body
 * belongs to (the session), as opposed to `keyOf`, which names one VERSION of it
 * (session + signature). While a newer signature refetches, a resolve returns the
 * hold-key's last-good body instead of the cold sentinel, so the region
 * reconciles in place rather than blanking to a placeholder once per sibling
 * change (#266). Omit it for a body whose stale version would be WRONG rather
 * than merely old — waveform peaks belong to one (WAV, byte size), so there is no
 * older version of them to show.
 *
 * `knownValue` is an answer this resource can give WITHOUT a fetch, from the
 * arguments alone — the canonical case is an empty `files_sig`, which says there
 * is no session folder on disk (fetching would 404). It is recorded as the
 * last-good body like a fetched one, so a later non-empty flip shows the empty
 * state through the stale path rather than resurrecting pre-deletion rows.
 *
 * @template {unknown[]} A
 * @template T
 * @param {(...args: A) => string} keyOf
 * @param {(...args: A) => Promise<T>} load
 * @param {{
 *   onFailure?: "retry-next-poll" | "remember-error",
 *   holdKeyOf?: (...args: A) => string,
 *   knownValue?: (...args: A) => T | undefined,
 * }} [policy]
 */
export function createResource(keyOf, load, policy = {}) {
  const { onFailure = "retry-next-poll", holdKeyOf, knownValue } = policy;
  /** @type {LastGoodHold<T>} */
  const lastGood = createLastGoodHold();
  /** Hold key → the fire order of the response currently held for it, so a SLOWER
   * request for an OLDER signature landing after a newer one cannot reconcile the
   * hold backward (which would serve the older body on the next flip: a deleted
   * WAV back as a ghost row). Bounded like the cache. @type {Map<string, number>} */
  const heldOrder = new Map();
  /** Resource-wide fire counter — see `Entry.order`. */
  let fires = 0;
  /** @type {Map<string, Entry<T>>} */
  const cache = new Map();
  /** Keys with a `resolve`-fired load in flight → the watchers waiting on it. One
   * entry per key (not per watcher) is what makes a resolve fire the load ONCE and
   * still repaint every watcher: a second view resolving the same key joins the
   * wait instead of skipping (a shared in-flight SET would swallow its repaint) or
   * refetching. @type {Map<string, Set<Watcher>>} */
  const waiting = new Map();

  /**
   * One watcher's failure memory: keys whose last load REJECTED → the rejection.
   * A rejection evicts the cache key (so `fetch` would retry immediately), and the
   * retry has to be paced by something: a resolve runs once per poll tick, so
   * under `retry-next-poll` a remembered key skipping exactly one resolve defers
   * the retry to a later tick; under `remember-error` it stands until the key
   * changes.
   *
   * PER WATCHER, not per resource, for two reasons. The watcher that consumes the
   * skip must be the one that fires the retry — otherwise one view's skip pays for
   * another view's fetch and the first never gets its land. And a `remember-error`
   * memory is a DISPLAY decision belonging to whoever shows the message: a view
   * rebuild (`viewCache.clear()` at boot, once the model catalogs land) makes a
   * fresh watcher, which is what lets a transient boot-time 503 on `/peaks` be
   * retried instead of pinning "503 …" on the canvas for the life of the tab.
   * @typedef {{ onLand: () => void, failed: Map<string, unknown> }} Watcher
   */

  /** @param {A} args @returns {T | undefined} */
  const peek = (...args) => {
    const e = cache.get(keyOf(...args));
    return e && e.settled ? e.value : undefined;
  };

  /**
   * Record `value` as the last-good body for what `args` names — unless a NEWER
   * response for the same hold key already is (`order`), or the hold key is empty
   * (it names nothing: no session is focused), or the value is a resolved
   * null/undefined, which is an ANSWER ("no transcript") rather than a body to
   * fall back on.
   * @param {A} args @param {T | undefined} value @param {number} order
   */
  const holdMaybe = (args, value, order) => {
    if (!holdKeyOf || value === null || value === undefined) return;
    const hk = holdKeyOf(...args);
    if (!hk || order < (heldOrder.get(hk) ?? 0)) return;
    heldOrder.set(hk, order);
    _capCache(heldOrder);
    lastGood.hold(hk, value);
  };

  /** This resource's last-good body for what `args` names, or null when nothing
   * was ever resolved for it (the cold-load sentinel). @param {A} args */
  const stale = (...args) => (holdKeyOf ? lastGood.get(holdKeyOf(...args)) : null);

  const self = {
    /** @param {A} args @returns {Promise<T>} */
    fetch(...args) {
      const key = keyOf(...args);
      const hit = cache.get(key);
      if (hit) return hit.promise;
      /** @type {Entry<T>} */
      const entry = {
        promise: Promise.resolve(/** @type {T} */ (undefined)),
        settled: false,
        value: undefined,
        order: ++fires,
      };
      entry.promise = load(...args)
        .then((v) => { entry.settled = true; entry.value = v; return v; })
        .catch((e) => { cache.delete(key); throw e; });
      cache.set(key, entry);
      _capCache(cache);
      return entry.promise;
    },
    peek,

    /**
     * Bind one consumer's repaint callback and return its per-tick reader. See
     * the module doc above for why the callback is bound here and not per call.
     * @param {() => void} onLand — run when there is something new to show
     * @returns {{ resolve: (args: A) => Resolved<T> }}
     */
    watch(onLand) {
      /** @type {Watcher} */
      const w = { onLand, failed: new Map() };
      return { resolve: (args) => resolveFor(args, w) };
    },
  };

  /**
   * Resolve this resource for ONE render tick, on behalf of one watcher.
   * @param {A} args
   * @param {Watcher} w
   * @returns {Resolved<T>}
   */
  function resolveFor(args, w) {
    const key = keyOf(...args);
    // An answer needing no fetch: one `knownValue` can give from the args alone
    // (as current as an answer gets — it is derived from THIS tick's arguments),
    // or one already settled in the cache. `settled` is the flag to read, not
    // `value !== undefined`: a body that resolves to undefined would otherwise
    // read as unfetched forever, and since a landing notifies a watcher that
    // re-resolves synchronously, the two would ping-pong through microtasks.
    const known = knownValue ? knownValue(...args) : undefined;
    if (known !== undefined) {
      holdMaybe(args, known, ++fires);
      return { value: known, loading: false, stale: false, error: null };
    }
    const entry = cache.get(key);
    if (entry && entry.settled) {
      holdMaybe(args, entry.value, entry.order);
      return { value: entry.value ?? null, loading: false, stale: false, error: null };
    }
    if (onFailure === "remember-error") {
      // The failure stands until the signature changes: report it, fetch nothing.
      const remembered = w.failed.get(key);
      if (remembered !== undefined) {
        const held = stale(...args);
        return { value: held, loading: false, stale: held !== null, error: remembered };
      }
    } else if (w.failed.delete(key)) {
      // Check-AND-consume: a key whose last load failed skips this one resolve,
      // and the next one — the next poll tick — retries. A key change (a new
      // signature) is a different key and fetches at once.
      return _pending(stale(...args));
    }
    const already = waiting.get(key);
    if (already) already.add(w);
    else {
      // Clear the wait BEFORE notifying: `onLand` re-renders synchronously and
      // re-resolves, which must see a settled cache (or the recorded failure)
      // rather than a stale in-flight key. Each callback is isolated — one view's
      // failing repaint must not swallow another view's land, nor escape as an
      // unhandled rejection.
      const done = (/** @type {unknown} */ err, /** @type {boolean} */ notify) => {
        const landed = waiting.get(key) || new Set();
        waiting.delete(key);
        for (const each of landed) {
          if (err !== undefined) { each.failed.set(key, err); _capCache(each.failed); }
          if (notify) {
            try { each.onLand(); } catch (e) { console.error("lazy resource: a watcher's repaint threw", e); }
          }
        }
      };
      /** @type {Promise<T>} */
      let p;
      // A `load` that throws SYNCHRONOUSLY (every declared one calls
      // encodeURIComponent first, which raises on a lone surrogate) must land on
      // the failure path like any rejection — letting it escape here would strand
      // the key in `waiting` forever, pinning the region at its placeholder.
      try { p = self.fetch(...args); } catch (e) { p = Promise.reject(e); }
      // This request's place in the fire order — read from the entry `fetch` just
      // stamped, so joining a load already in flight inherits ITS order rather
      // than claiming to be newer than it is.
      const order = cache.get(key)?.order ?? 0;
      waiting.set(key, new Set([w]));
      p.then(
        (v) => {
          // Record the hold HERE, not only on a later cache-hit resolve: the hold
          // must not depend on a watcher happening to re-resolve this exact key
          // after it lands (a session switch between the fetch and the repaint
          // would lose it, and the NEXT sig flip would blank).
          holdMaybe(args, v, order);
          done(undefined, true);
        },
        // Under retry-next-poll, stay quiet: nothing changed to render, and the
        // repaint's own synchronous re-entry refiring the evicted fetch was the
        // retry storm. Under remember-error the message IS the change.
        (e) => { done(e, onFailure === "remember-error"); },
      );
    }
    return _pending(stale(...args));
  }

  return self;
}
