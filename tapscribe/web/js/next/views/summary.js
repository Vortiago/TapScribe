// @ts-check
// Stages · Summary (SESSION stage 4) — PREVIEW, mock, NOT wired.
//
// A preview of a future "summarize this session" feature so the operator can
// see the intended shape: a Summarizer panel (source selector Local/API/
// Command, a prompt field, a Generate button — all disabled) + a sample
// summary output. There are NO backend calls; everything is static and clearly
// tagged "mock · not wired" in the template.
//
// Built once for the page; `update(j, session)` only refreshes the
// session-dependent header sub-line (the mock body is static, built once).

import { tpl, pick } from "../../templates.js";
import { header, strong, inline } from "../shell.js";

/**
 * @returns {{ node: DocumentFragment, update: (j: import('../../types.js').AppState, session: import('../../types.js').Session | null) => void }}
 */
export function build() {
  const frag = tpl("tpl-next-view-summary");
  const headHost = pick(frag, "head");
  let lastSig = " "; // sentinel so the first update always renders the header

  /**
   * @param {import('../../types.js').AppState} _j
   * @param {import('../../types.js').Session | null} sess
   */
  const update = (_j, sess) => {
    const sig = sess?.session || "";
    if (sig === lastSig) return;
    lastSig = sig;
    header(headHost, {
      eyebrow: "Session · 4 Summary",
      title: "Summary",
      sub: sess
        ? inline("a future feature · preview for ", strong(sess.session_meta?.label || sess.session))
        : "a future feature · pick a session from the spine",
    });
  };

  return { node: frag, update };
}
