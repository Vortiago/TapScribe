// @ts-check
// gate-allow: signal-listener — the two listeners here attach to the media element the Player is BUILT AROUND, once, at boot. That element is declared statically in next.html and is never re-mounted (that is the whole point of ADR-0017), and the Player itself is created exactly once, so there is no re-mount for a listener to leak across and no lifetime shorter than the page's for a signal to bound.
// The Player — the dashboard's ONE audio element, as policy.
//
// Playback belongs to the operator's session work, not to a view (CONTEXT.md ·
// Player · seek target · open WAV · playhead). The element is owned by the
// shell and mounted outside `#viewRoot`, because every per-view home destroys a
// playing element: a region swaps whole, a keyed-list row is rebuilt when its
// key changes, and a view host is detached on stage navigation — which per spec
// PAUSES a media element. Views own only play affordances. ADR-0017.
//
// The media element is INJECTED so this policy is unit-testable with no DOM and
// no audio device (the same shape as field-saver.js taking `put`). Everything
// here is driven by media events, never by the /api/state poll, so the Player
// sits outside the interaction hold and the render signatures entirely.

// The route URL comes from api.js, which owns every /api/wav/* URL — the
// Recordings download href builds the SAME one. A plain browser GET is why
// playback works at all: the dashboard is HTTP-Basic gated and the browser
// attaches those to same-origin subresource loads, where a media element could
// never send an Authorization header itself.
import { wavUrl } from "../../api.js";

/**
 * @typedef {{ session: string, name: string, source: "original" | "stripped" }} LoadedFile
 * @typedef {LoadedFile & { offsetS?: number, keptSpans?: {start_s: number, end_s: number}[] }} LoadRequest
 */

/**
 * What kept-audio playback should do at time `t` — the strip-preview's "play
 * what ✂ would leave" affordance (#191).
 *
 * `spans` are the KEPT regions (what a strip run would write as clips), so the
 * silence BETWEEN them is exactly what the operator is deciding to throw away
 * and must never be heard. Stateless on purpose: it answers from `t` alone, so
 * scrubbing the native scrub bar mid-preview lands correctly instead of
 * desyncing a remembered span index.
 *
 * @param {{start_s: number, end_s: number}[]} spans kept regions, ascending
 * @param {number} t current position, seconds
 * @returns {{ seekTo: number } | { stop: true } | null} null = keep playing
 */
export function spanPlaybackStep(spans, t) {
  for (const s of spans || []) {
    if (t < s.start_s) return { seekTo: s.start_s }; // in a gap (or the lead-in)
    if (t <= s.end_s) return null; // inside a kept region
  }
  return { stop: true }; // past the last kept region — or nothing kept at all
}

/**
 * `raf`/`caf` are injected so the position loop is unit-testable with no
 * browser: a test drives frames by hand instead of waiting for a compositor.
 * @param {{
 *   media: any,
 *   onChange?: (loaded: LoadedFile | null, reason?: string) => void,
 *   raf?: (cb: () => void) => number,
 *   caf?: (handle: number) => void,
 * }} deps
 */
export function createPlayer({
  media,
  onChange = () => {},
  raf = (cb) => globalThis.requestAnimationFrame(cb),
  caf = (h) => globalThis.cancelAnimationFrame(h),
}) {
  /** @type {LoadedFile | null} */
  let loaded = null;
  /** A seek requested before the element could accept it. */
  /** @type {number | null} */
  let pendingSeek = null;
  /** Kept spans while playing "what ✂ would leave", else null. */
  /** @type {{start_s: number, end_s: number}[] | null} */
  let keptSpans = null;

  /** Position subscribers — the playhead is the only one today. */
  /** @type {((loaded: LoadedFile | null, currentTime: number) => void)[]} */
  const tickers = [];
  /** @type {number | null} */
  let frame = null;

  /** @param {number} [knownAt] position, when the caller already has it */
  const emitTick = (knownAt) => {
    // One read of the native currentTime getter per frame, not one per
    // subscriber — and every subscriber sees the same instant. A caller that
    // just WROTE currentTime passes the value, so the reported position is the
    // post-write one by construction rather than by trusting the setter to have
    // landed synchronously.
    const at = knownAt ?? (loaded ? media.currentTime : 0);
    for (const fn of tickers) fn(loaded, at);
  };

  /** One frame of the position loop, re-armed only while audio is playing.
   * `timeupdate` fires ~4/s, which reads as a stuttering playhead; a frame loop
   * is smooth AND idle whenever nothing plays, so an untouched dashboard runs no
   * loop at all. */
  const step = () => {
    // ONE read of currentTime per frame, shared by the hop check and the tick.
    let at = loaded ? media.currentTime : 0;
    // Hop the dropped gaps BEFORE reporting position, so a subscriber never
    // sees a playhead inside a region the operator is cutting.
    if (keptSpans) {
      const next = spanPlaybackStep(keptSpans, at);
      if (next && "seekTo" in next) {
        media.currentTime = next.seekTo;
        at = next.seekTo;
      } else if (next && next.stop) {
        // Kept playback is DONE. Disarm the spans as well as pausing: leaving
        // them armed makes the native play button appear dead (frame one would
        // see `t` past the last span and pause again) and hijacks a later
        // waveform click that lands in a dropped region.
        keptSpans = null;
        media.pause();
      }
    }
    // This handle has fired; clear it BEFORE the tick so a subscriber that
    // unsubscribes from inside emitTick (a detached view retiring its ticker)
    // sees an honest `frame == null` and its stopLoop actually sticks.
    frame = null;
    emitTick(at);
    // Re-arm through startLoop, NOT unconditionally: the tick may have removed
    // the last subscriber or ended span playback, and re-arming regardless would
    // undo that and spin the loop forever on an empty list.
    startLoop();
  };
  /** Arm the frame loop — but only if anyone is listening. The Player is
   * shell-owned and plays on every view, while its one ticker (the Recordings
   * playhead) exists only while that view is mounted; without this guard a long
   * listen from the Transcript stage spins ~60 no-op wake-ups a second for its
   * whole duration. `onTick` re-arms if a subscriber shows up mid-playback. */
  const startLoop = () => {
    // Span-hopping is the Player's OWN consumer of the loop — it must run even
    // when no view is subscribed (the operator can tune knobs, hit play, and
    // walk to another stage).
    if (frame == null && (tickers.length || keptSpans) && !media.paused) frame = raf(step);
  };
  const stopLoop = () => {
    if (frame != null) caf(frame);
    frame = null;
    emitTick(); // land the final position instead of freezing a frame short
  };

  media.addEventListener("play", startLoop);
  media.addEventListener("pause", stopLoop);
  media.addEventListener("ended", stopLoop);
  // A seek while paused still moves the playhead. Wrapped, NOT passed directly:
  // the listener receives an Event, which would land in `emitTick`'s knownAt
  // parameter and reach every subscriber as "the current position".
  media.addEventListener("seeked", () => emitTick());

  media.addEventListener("loadedmetadata", () => {
    if (pendingSeek != null) {
      media.currentTime = pendingSeek;
      pendingSeek = null;
    }
    startLoop(); // spans need the loop even with nothing subscribed
    onChange(loaded);
  });

  /** Detach the element from its source. `removeAttribute` + `load()` rather
   * than `src = ""`, which resolves against the document URL and makes the
   * element fetch the page. */
  const unload = (/** @type {string} */ reason) => {
    if (!loaded) return;
    loaded = null;
    pendingSeek = null;
    keptSpans = null;
    media.pause();
    media.removeAttribute("src");
    media.load();
    onChange(null, reason);
    // Erase any playhead: `loaded` is already null, so this says "no position".
    emitTick();
  };

  /** @param {(f: LoadedFile) => boolean} pred */
  const forgetWhere = (pred) => {
    if (loaded && pred(loaded)) unload("deleted");
  };

  // The BACKSTOP for a file that changed without the dashboard doing it: an
  // external delete, an absorb, a re-strip mid-seek — and the truncated /
  // zero-byte WAVs a crashed tap leaves behind, which would otherwise present
  // as a dead transport that never plays.
  media.addEventListener("error", () => unload("unreadable"));

  /** Apply a seek now if the element can take one, otherwise queue it.
   * Assigning `currentTime` at HAVE_NOTHING is unreliable — a cold cache would
   * silently play from 0:00 while a warm one worked. */
  const seek = (/** @type {number} */ offsetS) => {
    if (!Number.isFinite(offsetS) || offsetS < 0) return;
    if (media.readyState > 0) media.currentTime = offsetS;
    else pendingSeek = offsetS;
  };

  return {
    /** Load a file and start playing it. `offsetS` lands the caret somewhere
     * other than the start; `keptSpans` additionally CONSTRAINS playback to
     * those regions, skipping everything between them — one verb ("play this
     * file"), with a stated restriction on where playback is allowed. */
    load(/** @type {LoadRequest} */ req) {
      const same =
        loaded
        && loaded.session === req.session
        && loaded.name === req.name
        && loaded.source === req.source;
      loaded = { session: req.session, name: req.name, source: req.source };
      pendingSeek = null;
      // Absent `keptSpans` CLEARS any previous kept-audio playback: a plain load
      // is "play this file", not "keep hopping the last preview's gaps".
      keptSpans = req.keptSpans && req.keptSpans.length ? req.keptSpans : null;
      // Re-assigning src restarts the media load algorithm and throws away the
      // buffer, so clicking successive transcript lines of ONE WAV would
      // re-download it every time. Same file => keep the element, just move.
      if (!same) media.src = wavUrl(loaded);
      // Kept-audio playback starts at the first kept region, never at 0:00 —
      // the lead-in silence is part of what's being cut.
      const firstKept = keptSpans?.[0];
      seek(firstKept ? firstKept.start_s : (req.offsetS ?? 0));
      // A 404 / undecodable source rejects play() (NotSupportedError). Swallow it
      // HERE: the `error` listener above already unloads and states the reason,
      // and an unhandled rejection would also hit wireErrorBar's red crash bar
      // and beacon /api/client-errors for the same one fact.
      Promise.resolve(media.play()).catch(() => {});
      onChange(loaded);
    },
    /** Surface a message about playback WITHOUT changing what's loaded — a seek
     * target whose file is gone, or a listing still loading. The docked bar is
     * where the operator is looking; a hover title is not.
     * @param {string} message */
    report(message) {
      onChange(loaded, message);
    },
    /** Subscribe to position updates: (loaded, currentTime) per frame while
     * playing, plus one final call on pause/seek/unload. A `null` loaded means
     * "no position" — erase, don't freeze. */
    onTick(/** @type {(loaded: LoadedFile | null, currentTime: number) => void} */ fn) {
      tickers.push(fn);
      startLoop(); // may already be playing — a late subscriber still gets frames
      return () => {
        const i = tickers.indexOf(fn);
        if (i >= 0) tickers.splice(i, 1);
        // Nothing left to update: stop the loop rather than spin on an empty list.
        if (!tickers.length) stopLoop();
      };
    },
    /** Re-aim kept-audio playback at a changed cut, without restarting the
     * media element. INERT unless kept playback is currently armed: a knob drag
     * while an ordinary whole-file playback is running must not start hopping.
     * An empty cut ends it, since there is then nothing kept to play.
     * @param {{start_s: number, end_s: number}[] | null} spans */
    setKeptSpans(spans) {
      if (!keptSpans) return;
      keptSpans = spans && spans.length ? spans : null;
      if (!keptSpans) media.pause();
    },
    seek,
    /** Stop and unload IF the Player is holding exactly this file. The single
     * -file door onto `forgetWhere`, for the per-WAV delete.
     * @param {LoadedFile} f */
    forget(f) {
      forgetWhere(
        (l) => l.session === f.session && l.name === f.name && l.source === f.source,
      );
    },
    /** Stop and unload if the loaded file matches `pred`. The eviction door for
     * the mutating verbs — the ONLY mechanism that covers buffered playback,
     * since deleting a file the browser already downloaded raises no error at
     * all. The bulk verbs (`clear stripped`, delete-session-audio, delete a
     * whole session) remove many files at once, so the caller expresses WHAT
     * WENT rather than naming one file. A file that doesn't match is none of the
     * Player's business.
     * @param {(f: LoadedFile) => boolean} pred */
    forgetWhere,
    /** What the Player currently holds — the playhead's identity check reads
     * this, so it can refuse to draw a position on a file it isn't playing. */
    loaded: () => loaded,
  };
}
