// Config-in-effect card. Renders three columns:
//   • batch prompt (prompt.txt) — editable, gated by inputs_support.batch_prompt
//   • live prompt (live-prompt.txt) — editable, gated by inputs_support.live_prompt
//   • hotwords (hotwords.txt) — editable, gated by inputs_support.batch_hotwords
//   • hallucination filter (hallucinations.txt) — read-only, always shown
//
// Each editor renders a textarea + save button. The save handler PUTs to
// /api/config/<key> and reflects the result in a status badge.
//
// Rebuilds are signature-gated so the user's in-progress edit isn't
// blown away by the per-second /api/state poll. While the user is focused
// inside any editor textarea, the whole card skips re-render — the same
// pattern live-channel.js uses to keep <select>s open.

import { tpl, pick } from "../templates.js";
import { putJson } from "../api.js";

let lastSig = "";

function buildCol({ title, file, count, body }) {
  const frag = tpl("tpl-cfg-col");
  pick(frag, "title").textContent = title;
  pick(frag, "file").textContent = file;
  if (count) pick(frag, "count").textContent = `· ${count}`;
  const bodyEl = pick(frag, "body");
  body(bodyEl);
  return frag;
}

function emptyMsg(text) {
  const frag = tpl("tpl-cfg-empty");
  pick(frag, "msg").textContent = text;
  return frag;
}

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

// Build the editor: textarea + save button. `placeholder` hints what the
// field is for when empty. Returns the fragment ready to append.
function buildEditor({ key, content, placeholder }) {
  const frag = tpl("tpl-cfg-editor");
  const ta = pick(frag, "textarea");
  ta.value = content || "";
  ta.placeholder = placeholder || "";
  ta.dataset.cfgKey = key;
  const btn = pick(frag, "saveBtn");
  btn.dataset.cfgKey = key;
  const status = pick(frag, "status");
  status.dataset.cfgKey = key;

  // Track dirty state so the status badge can flip back to "unsaved" once
  // the operator edits after a save. The save handler clears `dirty`.
  let dirty = false;
  let original = content || "";
  ta.addEventListener("input", () => {
    dirty = ta.value !== original;
    status.textContent = dirty ? "unsaved" : "";
  });
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    status.textContent = "saving…";
    try {
      await putJson(`/api/config/${key}`, { content: ta.value });
      original = ta.value;
      dirty = false;
      status.textContent = "saved";
      // Fade the status after a moment so the badge isn't a permanent
      // distraction in the corner of the operator's eye.
      setTimeout(() => {
        if (status.textContent === "saved") status.textContent = "";
      }, 1500);
    } catch (e) {
      status.textContent = `failed: ${String(e).replace(/^Error:\s*/, "")}`;
    } finally {
      btn.disabled = false;
    }
  });
  return frag;
}

export function render(j, { gridEl }) {
  // Skip rebuild while the operator is mid-edit — replacing the DOM
  // would yank focus and lose unsaved keystrokes.
  const active = document.activeElement;
  if (active && active.dataset && active.dataset.cfgKey && gridEl.contains(active)) return;

  const p = j.prompt || {};
  const lp = j.live_prompt || {};
  const h = j.hotwords || {};
  const hl = j.hallucinations || {};
  const support = j.inputs_support || {
    // Default-true so a server that hasn't been updated yet keeps showing
    // both editors. The flags only HIDE; they never silently un-render
    // an editor when the server failed to send them.
    live_prompt: true,
    batch_prompt: true,
    batch_hotwords: true,
  };
  const sig = [
    p.length || 0, p.content || "",
    lp.length || 0, lp.content || "",
    h.length || 0, h.content || "",
    hl.count || 0, (hl.rules || []).join("|"),
    support.live_prompt ? 1 : 0,
    support.batch_prompt ? 1 : 0,
    support.batch_hotwords ? 1 : 0,
  ].join("§");
  if (sig === lastSig) return;
  lastSig = sig;

  const hotwordList = (h.content || "").split(",").map((s) => s.trim()).filter(Boolean);
  const halRules = hl.rules || [];

  const out = document.createDocumentFragment();

  if (support.batch_prompt) {
    out.appendChild(buildCol({
      title: "initial prompt (batch)",
      file: "prompt.txt",
      count: p.length ? `${p.length} chars` : null,
      body: (el) => {
        el.appendChild(buildEditor({
          key: "prompt",
          content: p.content || "",
          placeholder: "meeting context shown to Whisper before each batch job…",
        }));
      },
    }));
  }

  if (support.live_prompt) {
    out.appendChild(buildCol({
      title: "initial prompt (live)",
      file: "live-prompt.txt",
      count: lp.length ? `${lp.length} chars` : null,
      body: (el) => {
        el.appendChild(buildEditor({
          key: "live-prompt",
          content: lp.content || "",
          placeholder: "always-on context fed to the live channel — independent from batch…",
        }));
      },
    }));
  }

  if (support.batch_hotwords) {
    out.appendChild(buildCol({
      title: "hotwords",
      file: "hotwords.txt",
      count: hotwordList.length ? `${hotwordList.length} terms` : null,
      body: (el) => {
        el.appendChild(buildEditor({
          key: "hotwords",
          content: h.content || "",
          placeholder: "comma-separated names / jargon, e.g. Acme Inc., Patricia Lin",
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
}

export const invalidate = () => { lastSig = ""; };
