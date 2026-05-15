// Top-of-page ribbon: session status line + recording pill state.

import { tpl, mount, slot } from "../templates.js";
import { fmtElapsed } from "../formatters.js";

export function renderStatus(j, { statusEl }) {
  const sess = (j.sessions || []).find((s) => s.is_current) || (j.sessions || [])[0];
  const elapsed = sess?.earliest_iso
    ? Math.max(0, Math.floor((Date.now() - new Date(sess.earliest_iso).getTime()) / 1000))
    : null;
  const frag = slot(tpl("tpl-ribbon-status"), {
    session: sess?.session ?? "—",
    elapsed: fmtElapsed(elapsed),
    wavs: sess?.wav_count ?? 0,
  });
  mount(statusEl, frag);
}

export function renderError(statusEl, msg) {
  mount(statusEl, slot(tpl("tpl-ribbon-error"), { msg: `recorder unreachable: ${msg}` }));
}

export function renderRecPill({ pillEl }, enabled) {
  pillEl.classList.toggle("paused", !enabled);
  pillEl.title = enabled
    ? "Recording new utterances. Click to pause."
    : "Recording paused — /record WSes are accepted then immediately closed. Click to resume.";
  mount(pillEl, tpl(enabled ? "tpl-rec-pill-recording" : "tpl-rec-pill-paused"));
}
