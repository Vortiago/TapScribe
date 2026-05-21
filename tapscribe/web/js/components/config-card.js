// @ts-check
// "Default config" card — global batch defaults (prompt.txt, hotwords.txt)
// + the hallucination filter. The live prompt has moved into the live
// channel panel; each session's per-batch override lives in the session
// detail. This panel surfaces the system-wide defaults only.
//
// Each editor is hidden when no installed batch model declares the
// corresponding input (registry-driven via inputs_support.batch_*).
// Save buttons PUT to /api/config/{prompt|hotwords}. Atomic on the
// server. While focus is inside any editor textarea the whole card
// skips its per-second rebuild so polling can't blow away unsaved edits.

import { tpl, pick } from "../templates.js";
import { wireConfigSave } from "../api.js";

let lastSig = "";

/**
 * @param {{
 *   title: string,
 *   file: string,
 *   count: string | null,
 *   body: (el: HTMLElement) => void,
 * }} opts
 */
function buildCol({ title, file, count, body }) {
  const frag = tpl("tpl-cfg-col");
  pick(frag, "title").textContent = title;
  pick(frag, "file").textContent = file;
  if (count) pick(frag, "count").textContent = `· ${count}`;
  body(pick(frag, "body"));
  return frag;
}

/** @param {string} text */
function emptyMsg(text) {
  const frag = tpl("tpl-cfg-empty");
  pick(frag, "msg").textContent = text;
  return frag;
}

/** @param {string[]} values */
function codeList(values) {
  const frag = document.createDocumentFragment();
  for (const v of values) {
    const c = tpl("tpl-cfg-code");
    pick(c, "val").textContent = v;
    frag.appendChild(c);
    frag.appendChild(document.createTextNode(" "));
  }
  return frag;
}

/**
 * @param {{
 *   key: string,
 *   content: string,
 *   placeholder: string,
 *   overrideCount: number,
 * }} opts
 */
function buildEditor({ key, content, placeholder, overrideCount }) {
  const frag = tpl("tpl-cfg-editor");
  const ta = /** @type {HTMLTextAreaElement} */ (pick(frag, "textarea"));
  ta.value = content || "";
  ta.placeholder = placeholder || "";
  ta.dataset.cfgKey = key;
  const btn = /** @type {HTMLButtonElement} */ (pick(frag, "saveBtn"));
  btn.dataset.cfgKey = key;
  const status = pick(frag, "status");
  status.dataset.cfgKey = key;

  // Override-count footnote — empty when zero so the cell stays clean
  // on installs with no per-session overrides set.
  const overrideEl = pick(frag, "overrideCount");
  if (overrideCount > 0) {
    overrideEl.textContent = `${overrideCount} session${overrideCount === 1 ? "" : "s"} override${overrideCount === 1 ? "s" : ""} this`;
  }

  // Track baseline for the "unsaved" badge. wireConfigSave owns the
  // saving/saved/failed transitions and advances `baseline` only after
  // a successful save, so a failed save leaves the badge unsaved.
  let baseline = content || "";
  ta.addEventListener("input", () => {
    if (status.textContent === "saving…" || status.textContent === "saved") return;
    status.textContent = ta.value !== baseline ? "unsaved" : "";
  });
  wireConfigSave({ key, btn, textarea: ta, status, onSuccess: (v) => { baseline = v; } });
  return frag;
}

/**
 * @param {import('../types.js').AppState} j
 * @param {import('../types.js').ConfigCardCtx} ctx
 */
export function render(j, { gridEl, headerNoteEl }) {
  const active = /** @type {HTMLElement | null} */ (document.activeElement);
  if (active && active.dataset && active.dataset.cfgKey && gridEl.contains(active)) return;

  const p = j.prompt || { path: "", content: "", length: 0 };
  const h = j.hotwords || { path: "", content: "", length: 0 };
  const hl = j.hallucinations || { path: "", rules: [], count: 0 };
  const support = j.inputs_support || { live_prompt: true, batch_prompt: true, batch_hotwords: true };
  const counts = j.default_override_counts || { prompt: 0, hotwords: 0 };
  const sig = [
    p.length || 0, p.content || "",
    h.length || 0, h.content || "",
    hl.count || 0, (hl.rules || []).join("|"),
    support.batch_prompt ? 1 : 0,
    support.batch_hotwords ? 1 : 0,
    counts.prompt | 0, counts.hotwords | 0,
  ].join("§");
  if (sig === lastSig) return;
  lastSig = sig;

  const hotwordList = (h.content || "").split(",").map((s) => s.trim()).filter(Boolean);
  const halRules = hl.rules || [];

  const out = document.createDocumentFragment();

  if (support.batch_prompt) {
    out.appendChild(buildCol({
      title: "default prompt",
      file: "prompt.txt",
      count: p.length ? `${p.length} chars` : null,
      body: (el) => {
        el.appendChild(buildEditor({
          key: "prompt",
          content: p.content || "",
          placeholder: "default context biasing for batch transcription — sessions can override below",
          overrideCount: counts.prompt,
        }));
      },
    }));
  }

  if (support.batch_hotwords) {
    out.appendChild(buildCol({
      title: "default hotwords",
      file: "hotwords.txt",
      count: hotwordList.length ? `${hotwordList.length} terms` : null,
      body: (el) => {
        el.appendChild(buildEditor({
          key: "hotwords",
          content: h.content || "",
          placeholder: "comma-separated names / jargon, e.g. Acme Inc., Patricia Lin",
          overrideCount: counts.hotwords,
        }));
      },
    }));
  }

  out.appendChild(buildCol({
    title: "hallucination filter",
    file: "hallucinations.txt",
    count: halRules.length ? `${halRules.length} rule${halRules.length === 1 ? "" : "s"}` : null,
    body: (el) => {
      if (halRules.length) {
        el.appendChild(tpl("tpl-cfg-hal-prefix"));
        el.appendChild(codeList(halRules));
      } else {
        el.appendChild(emptyMsg("no patterns — nothing will be suppressed"));
      }
    },
  }));

  gridEl.replaceChildren(out);
  if (headerNoteEl) {
    headerNoteEl.textContent = "batch defaults — overridden per-session in the controls below";
  }
}

export const invalidate = () => { lastSig = ""; };
