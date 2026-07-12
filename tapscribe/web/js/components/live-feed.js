// @ts-check
// Live transcripts feed — the streaming terminal-style panel.
//
// Two layers, merged here:
//
//  1. COALESCING (#80): WhisperLiveKit commits each sentence in small
//     word-level chunks (LocalAgreement-2) and the Recorder's relay forwards
//     every chunk as its own settled line, so the deque holds one entry per
//     few words. We join consecutive fragments from the same speaker and
//     re-split on sentence boundaries (`groupFeed`), so each rendered row is
//     one sentence, not one word. Purely presentational — the deque keeps
//     every fragment and the authoritative transcript is the batch re-run.
//  2. INCREMENTAL RENDER: the coalesced lines are an append-only list (a new
//     sentence appends one row; once the deque saturates the head shifts off),
//     so we diff against what's rendered and append/shift instead of rebuilding
//     all rows each tick — that full rebuild (template clones + replaceChildren
//     + an autoscroll layout pass) ran on every settled caption during a
//     meeting. A clean shifted-prefix appends only the new tail; anything else
//     falls back to a full rebuild. Appending also preserves text selection.

import { tpl, mount, pick, deferIfSelectionInside } from "../templates.js";
import { speakerIndex } from "../speakers.js";
import { fmtClock } from "../formatters.js";

// A fragment joins the current speaker's line unless this many ms have
// elapsed since that line's last fragment. `ts` is the relay's EMISSION
// time (datetime.now at flush in TapFanOut._on_settled_line), not the speech
// timestamp, so it tracks WhisperLiveKit's commit cadence + backlog rather
// than pauses in speech — keep this generous. A change of speaker always
// breaks the run regardless of gap, so this only ever splits one speaker's
// uninterrupted stretch, where a multi-second-plus gap reliably marks a new
// turn rather than the next word of the same sentence.
const GROUP_GAP_MS = 30_000;

// "::empty-live::" / "::empty-archived::" sentinels for the two idle states
// (mounted once, then skipped — re-mounting every poll churned ~100 detached
// nodes/sec; see the idle-churn guard in tests/e2e/test_dashboard_ui.py).
// Otherwise holds the raw-feed signature (focused session + tail + length) so
// an unchanged poll skips all work.
let lastSig = "";
// Set by invalidate(): force the next non-empty render down the full-rebuild
// path even if the rendered keys would match (e.g. after a clear).
let forceNext = false;

// Rendered-line keys per feed body element. Keyed by the body so a remounted
// shell (fresh element) naturally starts from a clean slate.
/** @type {WeakMap<Element, string[]>} */
const _renderedKeys = new WeakMap();

/** One coalesced row: a speaker turn's sentence + the run's starting stamp. */
/** @typedef {{ who: string, identity: string, ts: string, text: string }} FeedLine */

/**
 * Join settled-line fragments into one readable string. A fragment that
 * begins with sentence/clause punctuation attaches without a leading space
 * ("the build is" + ". Done." → "the build is. Done."); everything else is
 * separated by a single space. Pure — no DOM, no shared state.
 * @param {string[]} parts
 */
export function joinFragments(parts) {
  let out = "";
  for (const raw of parts) {
    const p = (raw || "").trim();
    if (!p) continue;
    if (!out) out = p;
    else if (/^[.,!?;:…)\]]/.test(p)) out += p;
    else out += " " + p;
  }
  return out;
}

/**
 * Split a speaker turn's joined text into sentences so each sentence renders
 * as its own line. Pure — no DOM, no shared state.
 *
 * Each match lazily runs up to a sentence terminator (. ! ?) plus any trailing
 * closing quotes/brackets that is followed by whitespace or end-of-text; the
 * final `.+$` alternative sweeps up a trailing fragment that has no terminator.
 * Mid-token dots stay intact ("3.5" has no whitespace after the dot), and
 * punctuation-free input (common on small models like tiny.en) yields a single
 * element, so this degrades to the speaker/gap grouping. Uses lookahead +
 * dotAll only — no lookbehind — so it parses on every browser the dashboard
 * runs in (lookbehind would exclude Safari < 16.4).
 * @param {string} text
 * @returns {string[]}
 */
export function splitSentences(text) {
  return (text.match(/.*?[.!?]["'”’)\]]*(?=\s|$)|.+$/gs) || []).map((s) => s.trim()).filter(Boolean);
}

/**
 * Scope the global live-caption deque to one session. The deque is shared
 * across all open taps (max 200 lines); each line carries the session it was
 * snapshotted to at `/tap` open (tap_fan_out.py), so the dashboard shows only
 * the FOCUSED session's lines — an archived session never displays the live
 * session's captions (CONTEXT.md Detached-session isolation, honored in the
 * UI). No focused session (empty id) shows nothing, not everything. Pure.
 * @param {import('../types.js').LiveFeedEntry[]} feed
 * @param {string} sessionId
 * @returns {import('../types.js').LiveFeedEntry[]}
 */
export function entriesForSession(feed, sessionId) {
  if (!sessionId) return [];
  return feed.filter((e) => e.session === sessionId);
}

/**
 * Collapse the flat feed into per-speaker runs, then split each run into
 * sentences. A run breaks when the speaker changes or the inter-fragment gap
 * exceeds GROUP_GAP_MS; each resulting line carries the run's starting
 * timestamp and one sentence of its joined text.
 * @param {import('../types.js').LiveFeedEntry[]} feed
 * @returns {FeedLine[]}
 */
export function groupFeed(feed) {
  /** @type {{ key: string, who: string, identity: string, ts: string, parts: string[], lastMs: number }[]} */
  const runs = [];
  for (const e of feed) {
    const key = e.identity || e.name || "?";
    const ms = Date.parse(e.ts || "");
    const cur = runs.at(-1);
    // Same speaker, and close enough in time to be the same turn. `cur`
    // narrows non-undefined here, so `cur.lastMs` is safe in the gap test.
    if (cur && cur.key === key && (isNaN(ms) || isNaN(cur.lastMs) || ms - cur.lastMs <= GROUP_GAP_MS)) {
      cur.parts.push(e.text || "");
      if (!isNaN(ms)) cur.lastMs = ms;
    } else {
      runs.push({
        key,
        who: e.name || e.identity || "?",
        identity: e.identity || "",
        ts: e.ts || "",
        parts: [e.text || ""],
        lastMs: ms,
      });
    }
  }
  return runs.flatMap((r) =>
    splitSentences(joinFragments(r.parts)).map((text) => ({
      who: r.who,
      identity: r.identity,
      ts: r.ts,
      text,
    })),
  );
}

/** Identity of one coalesced line. The trailing speaker's LAST line mutates as
 * its run grows (more fragments → re-joined text), which changes its key — the
 * shift diff handles that by falling back to a rebuild; earlier lines are
 * stable. */
/** @param {FeedLine} g */
const keyOf = (g) => `${g.ts || ""} ${g.identity || ""} ${(g.text || "").length} ${(g.text || "").slice(-16)}`;

/** Build one `.line` ELEMENT (not the template fragment — the fragment
 * carries whitespace text nodes that would survive the element-wise removal
 * in the shift path and pile up over a long meeting). */
/** @param {FeedLine} g */
function buildLine(g) {
  const node = tpl("tpl-feed-line");
  pick(node, "ts").textContent = `[${fmtClock(g.ts)}]`;
  const whoEl = pick(node, "who");
  whoEl.textContent = g.who;
  whoEl.dataset.spk = String(speakerIndex(g.who));
  whoEl.title = g.identity || "";
  pick(node, "txt").textContent = g.text || "";
  return /** @type {HTMLElement} */ (node.firstElementChild);
}

/**
 * Find how far the rendered keys have SHIFTED relative to the new lines: the
 * smallest s such that rendered[s..] is a prefix of keys. s=0 is pure append;
 * s>0 means the server deque dropped s head entries. Returns -1 when no
 * clean shift exists (rebuild instead).
 * @param {string[]} rendered
 * @param {string[]} keys
 */
function shiftOf(rendered, keys) {
  const maxShift = Math.min(rendered.length, 50);
  for (let s = 0; s <= maxShift; s++) {
    const overlap = rendered.length - s;
    if (overlap <= 0 || overlap > keys.length) continue;
    let ok = true;
    for (let i = 0; i < overlap; i++) {
      if (rendered[s + i] !== keys[i]) { ok = false; break; }
    }
    if (ok) return s;
  }
  return -1;
}

/**
 * @param {import('../types.js').AppState} j
 * @param {import('../types.js').LiveFeedCtx} ctx
 */
export function render(j, { countEl, shell, autoscrollEl, sessionId, isCurrent }) {
  // The deque is global; scope it to the focused session so an archived
  // session never shows the live session's captions (see entriesForSession).
  const feed = entriesForSession(j.live_feed || [], sessionId);

  if (!feed.length) {
    if (countEl.textContent !== "0") countEl.textContent = "0";
    // Two empty states: the live (current) session is "awaiting" — captions may
    // still arrive; an archived session shows it isn't recording (live captions
    // are ephemeral, its durable text is the merged Transcript). Mount ONCE per
    // state, then skip — re-mounting every poll tick (the common idle state)
    // churns ~100 detached nodes/sec, which the tab accumulates between GCs
    // until it OOMs. The sentinel carries isCurrent so a current↔archived
    // switch (both empty) still swaps the message.
    const emptySig = isCurrent ? "::empty-live::" : "::empty-archived::";
    if (lastSig !== emptySig) {
      mount(shell, tpl(isCurrent ? "tpl-feed-empty" : "tpl-feed-archived"));
      lastSig = emptySig;
    }
    return;
  }

  // Fast skip: the raw feed's tail + length is a cheap proxy for "did anything
  // change" (a new fragment always moves the tail, even after the deque
  // saturates at 200). Avoids re-coalescing + re-diffing on an unchanged poll.
  // forceNext (post-clear) bypasses it.
  // sessionId leads the sig so switching the focused session repaints even
  // when the global deque's tail is unchanged (the panel is a page-singleton).
  const tail = feed.at(-1);
  const feedSig = `${sessionId}::${feed.length}::${tail?.ts || ""}::${tail?.identity || ""}::${(tail?.text || "").slice(0, 20)}`;
  if (!forceNext && feedSig === lastSig) return;

  let body = /** @type {HTMLElement | null} */ (shell.querySelector(".feed-body"));
  if (!body) {
    mount(shell, tpl("tpl-feed-body"));
    body = /** @type {HTMLElement} */ (shell.querySelector(".feed-body"));
  }

  // Hold feed mutations while the operator is select-copying caption text.
  // Appends alone wouldn't disturb a selection, but a same-speaker
  // continuation grows the tail sentence (key change → no clean shift →
  // full replaceChildren) and a saturated deque drops head rows — both
  // dissolve the selection. Placed BEFORE lastSig/forceNext are consumed so
  // the deferred update retries on the next tick after release
  // (deferIfSelectionInside also marks the deferred-render flag, so main.js
  // retries even if the poll goes quiet — 304s — in between; issue #245).
  if (deferIfSelectionInside(body)) return;

  // Coalesce fragments → one row per sentence; the count reflects what's drawn.
  const groups = groupFeed(feed);
  const countStr = String(groups.length);
  if (countEl.textContent !== countStr) countEl.textContent = countStr;

  const keys = groups.map(keyOf);
  const rendered = forceNext ? undefined : _renderedKeys.get(body);
  forceNext = false;
  lastSig = feedSig;

  // Sticky-scroll: only read scroll geometry when we're about to mutate.
  const wasAtBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 10;

  const shift = rendered && body.childElementCount === rendered.length ? shiftOf(rendered, keys) : -1;
  if (rendered && shift >= 0) {
    for (let i = 0; i < shift; i++) body.firstElementChild?.remove();
    const fresh = document.createDocumentFragment();
    for (const g of groups.slice(rendered.length - shift)) fresh.appendChild(buildLine(g));
    if (fresh.childNodes.length) body.appendChild(fresh);
  } else {
    const frag = document.createDocumentFragment();
    for (const g of groups) frag.appendChild(buildLine(g));
    body.replaceChildren(frag); // gate-allow: raw-swap — cold/reset path of the in-place feed updater; gated by deferIfSelectionInside upstream (CLAUDE.md)
  }
  _renderedKeys.set(body, keys);

  if (/** @type {HTMLInputElement} */ (autoscrollEl).checked && wasAtBottom) body.scrollTop = body.scrollHeight;
}

// Reset the sigs so the next render forces a repaint (e.g. after clear).
export const invalidate = () => { lastSig = ""; forceNext = true; };
