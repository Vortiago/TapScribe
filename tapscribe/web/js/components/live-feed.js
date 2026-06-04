// @ts-check
// Live transcripts feed — the streaming terminal-style panel.
//
// WhisperLiveKit commits each sentence in small word-level chunks
// (LocalAgreement-2), and the Recorder's relay forwards every chunk as its
// own settled line (see live_relay.WlKRelay._consider_emit_line, which emits
// only the new suffix when WlK grows a line). One settled line = one entry in
// the LiveTranscripts deque, so left untouched the panel paints one row per
// few words — a single spoken sentence ends up stacked across a dozen
// timestamped rows. We coalesce on the way to the DOM: consecutive fragments
// from the same speaker are joined and then re-split on sentence boundaries,
// so each row is one sentence rather than one word. This is purely
// presentational — the deque behind
// /api/state keeps every fragment, and the authoritative transcript still
// comes from batch re-transcription of the per-utterance WAVs.
//
// The feed is signature-gated so we only re-emit the DOM when the tail
// utterance actually changed. Skipping rebuilds preserves the user's scroll
// position and any text selection inside an in-progress utterance.

import { tpl, mount, pick } from "../templates.js";
import { speakerIndex } from "../speakers.js";

// A fragment joins the current speaker's line unless this many ms have
// elapsed since that line's last fragment. `ts` is the relay's EMISSION
// time (datetime.now at flush in TapFanOut._on_settled_line), not the speech
// timestamp, so it tracks WhisperLiveKit's commit cadence + backlog rather
// than pauses in speech — keep this generous. A change of speaker always
// breaks the run regardless of gap, so this only ever splits one speaker's
// uninterrupted stretch, where a multi-second-plus gap reliably marks a new
// turn rather than the next word of the same sentence.
const GROUP_GAP_MS = 30_000;

let lastSig = "";

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
 * as its own line. A break falls after a run of sentence terminators
 * (. ! ?, with any trailing closing quotes/brackets) when the next character
 * is whitespace or end-of-text — so mid-token dots ("3.5") don't split.
 * Punctuation-free input (common on small models like tiny.en) yields a
 * single element, so this gracefully degrades to the speaker/gap grouping.
 * Pure — no DOM, no shared state.
 * @param {string} text
 * @returns {string[]}
 */
export function splitSentences(text) {
  /** @type {string[]} */
  const out = [];
  let buf = "";
  for (let i = 0; i < text.length; i++) {
    const ch = text.charAt(i);
    buf += ch;
    if (ch === "." || ch === "!" || ch === "?") {
      // Swallow any adjacent terminators / closing quotes ("?!", '."').
      while (i + 1 < text.length && /[.!?"'”’)\]]/.test(text.charAt(i + 1))) {
        buf += text.charAt(++i);
      }
      // A real boundary only if the next char is whitespace or the end —
      // otherwise it's mid-token (a decimal, an abbreviation run-on).
      if (i + 1 >= text.length || /\s/.test(text.charAt(i + 1))) {
        const s = buf.trim();
        if (s) out.push(s);
        buf = "";
      }
    }
  }
  const tail = buf.trim();
  if (tail) out.push(tail);
  return out;
}

/**
 * Collapse the flat feed into per-speaker runs, then split each run into
 * sentences. A run breaks when the speaker changes or the inter-fragment gap
 * exceeds GROUP_GAP_MS; each resulting line carries the run's starting
 * timestamp and one sentence of its joined text.
 * @param {import('../types.js').LiveFeedEntry[]} feed
 * @returns {{ who: string, identity: string, ts: string, text: string }[]}
 */
export function groupFeed(feed) {
  /** @type {{ key: string, who: string, identity: string, ts: string, parts: string[], lastMs: number }[]} */
  const runs = [];
  for (const e of feed) {
    const key = e.identity || e.name || "?";
    const ms = Date.parse(e.ts || "");
    const cur = runs.at(-1);
    const withinGap = !!cur && (isNaN(ms) || isNaN(cur.lastMs) || ms - cur.lastMs <= GROUP_GAP_MS);
    if (cur && cur.key === key && withinGap) {
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

/**
 * @param {import('../types.js').AppState} j
 * @param {import('../types.js').LiveFeedCtx} ctx
 */
export function render(j, { countEl, shell, autoscrollEl }) {
  const feed = j.live_feed || [];

  if (!feed.length) {
    countEl.textContent = "0";
    mount(shell, tpl("tpl-feed-empty"));
    lastSig = "";
    return;
  }

  let body = /** @type {HTMLElement | null} */ (shell.querySelector(".feed-body"));
  let wasAtBottom = true;
  if (body) {
    wasAtBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 10;
  } else {
    mount(shell, tpl("tpl-feed-body"));
    body = /** @type {HTMLElement} */ (shell.querySelector(".feed-body"));
  }

  // Sig uses the tail entry, not just length, so updates keep flowing
  // after the server-side deque saturates at maxlen=200. A new fragment
  // always moves the tail (text or identity), so coalescing never hides
  // an update from the gate.
  const tail = feed.at(-1);
  const sig = `${feed.length}::${tail?.ts || ""}::${tail?.identity || ""}::${(tail?.text || "").slice(0, 20)}`;
  if (sig === lastSig) return;
  lastSig = sig;

  // The count reflects what's actually drawn — coalesced lines, not raw
  // deque entries — so the header number matches the rows on screen.
  const groups = groupFeed(feed);
  countEl.textContent = String(groups.length);

  const frag = document.createDocumentFragment();
  for (const g of groups) {
    const node = tpl("tpl-feed-line");
    pick(node, "ts").textContent = `[${(g.ts || "").slice(11, 19)}]`;
    const whoEl = pick(node, "who");
    whoEl.textContent = g.who;
    whoEl.dataset.spk = String(speakerIndex(g.who));
    whoEl.title = g.identity || "";
    pick(node, "txt").textContent = g.text;
    frag.appendChild(node);
  }
  body.replaceChildren(frag);

  if (/** @type {HTMLInputElement} */ (autoscrollEl).checked && wasAtBottom) body.scrollTop = body.scrollHeight;
}

// Reset the sig so the next render forces a rebuild (e.g. after clear).
export const invalidate = () => { lastSig = ""; };
