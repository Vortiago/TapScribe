// Config-in-effect three-column card. Signature-gated so we only rebuild
// when the underlying prompt/hotwords/hallucinations files change.

import { tpl, pick } from "../templates.js";

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

export function render(j, { gridEl }) {
  const p = j.prompt || {};
  const h = j.hotwords || {};
  const hl = j.hallucinations || {};
  const sig = [p.length || 0, h.length || 0, hl.count || 0, p.content || "", h.content || "", (hl.rules || []).join("|")].join("§");
  if (sig === lastSig) return;
  lastSig = sig;

  const hotwordList = (h.content || "").split(",").map((s) => s.trim()).filter(Boolean);
  const halRules = hl.rules || [];

  const out = document.createDocumentFragment();
  out.appendChild(buildCol({
    title: "initial prompt",
    file: "prompt.txt",
    count: p.length ? `${p.length} chars` : null,
    body: (el) => {
      if (p.length) el.textContent = p.content;
      else el.appendChild(emptyMsg("empty — no prose context biasing"));
    },
  }));
  out.appendChild(buildCol({
    title: "hotwords",
    file: "hotwords.txt",
    count: hotwordList.length ? `${hotwordList.length} terms` : null,
    body: (el) => {
      if (hotwordList.length) el.appendChild(codeList(hotwordList));
      else el.appendChild(emptyMsg("empty — no keyword biasing"));
    },
  }));
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
