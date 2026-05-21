// @ts-check
// Live transcripts feed — the streaming terminal-style panel.
//
// The feed is signature-gated so we only re-emit the DOM when the tail
// utterance actually changed. Skipping rebuilds preserves the user's scroll
// position and any text selection inside an in-progress utterance.

import { tpl, mount, pick } from "../templates.js";
import { speakerIndex } from "../speakers.js";

let lastSig = "";

/**
 * @param {import('../types.js').AppState} j
 * @param {import('../types.js').LiveFeedCtx} ctx
 */
export function render(j, { countEl, shell, autoscrollEl }) {
  const feed = j.live_feed || [];
  countEl.textContent = String(feed.length);

  if (!feed.length) {
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
  // after the server-side deque saturates at maxlen=200.
  const tail = feed.at(-1);
  const sig = `${feed.length}::${tail?.ts || ""}::${tail?.identity || ""}::${(tail?.text || "").slice(0, 20)}`;
  if (sig === lastSig) return;
  lastSig = sig;

  const frag = document.createDocumentFragment();
  for (const e of feed) {
    const who = e.name || e.identity || "?";
    const node = tpl("tpl-feed-line");
    pick(node, "ts").textContent = `[${(e.ts || "").slice(11, 19)}]`;
    const whoEl = pick(node, "who");
    whoEl.textContent = who;
    whoEl.dataset.spk = String(speakerIndex(who));
    whoEl.title = e.identity || "";
    pick(node, "txt").textContent = e.text || "";
    frag.appendChild(node);
  }
  body.replaceChildren(frag);

  if (/** @type {HTMLInputElement} */ (autoscrollEl).checked && wasAtBottom) body.scrollTop = body.scrollHeight;
}

// Reset the sig so the next render forces a rebuild (e.g. after clear).
export const invalidate = () => { lastSig = ""; };
