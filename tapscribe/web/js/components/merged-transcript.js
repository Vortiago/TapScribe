// Merged session transcript: one chronological flow of segments (including
// suppressed lines as strikethrough) plus the speaking-time stacked bar,
// meta strip, and a collapsible hallucination-audit table.

import { tpl, slot, pick } from "../templates.js";
import { aliasOf } from "../speakers.js";
import { fmtClock, fmtDur, fmtMs, truncMid } from "../formatters.js";

// `spkClass(name)` resolves the canonical 0..4 colour index from the
// session's speaker list — the same indexing the merged transcript itself
// emits, so a speaker's colour matches the speaking-time bar.
function spkClassOf(speakers, raw) {
  const i = speakers.indexOf(raw);
  return `who-${i >= 0 ? i % 5 : 0}`;
}

function buildItems(t) {
  const items = [];
  for (const seg of t.segments || []) {
    items.push({
      kind: "ok",
      ts: seg.abs_start || "",
      hms: fmtClock(seg.abs_start),
      speaker: seg.speaker || "",
      text: seg.text || "",
      lowConf: !!seg.low_confidence,
      confidence: typeof seg.avg_logprob === "number" ? seg.avg_logprob : null,
    });
  }
  for (const sup of t.suppressed || []) {
    items.push({
      kind: "sup",
      ts: sup.abs_start || "",
      hms: fmtClock(sup.abs_start),
      speaker: sup.speaker || "",
      text: sup.text || "",
      rule: sup.matched_rule || "",
    });
  }
  items.sort((a, b) => (a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : 0));
  return items;
}

function buildSpkBar(frag, speakers, speakingByName, aliases) {
  const totalRaw = speakers.reduce((acc, name) => acc + (speakingByName[name] || 0), 0);
  if (!speakers.length || !totalRaw) return;
  const total = totalRaw || 1;

  const bar = pick(frag, "spkBar");
  const legend = pick(frag, "spkLegend");
  bar.hidden = false;
  legend.hidden = false;

  for (let i = 0; i < speakers.length; i++) {
    const sec = speakingByName[speakers[i]] || 0;
    const pct = ((sec / total) * 100).toFixed(2);
    const display = aliasOf(speakers[i], aliases);

    const cell = tpl("tpl-spk-bar-cell").firstElementChild;
    cell.dataset.spk = i % 5;
    cell.style.width = `${pct}%`;
    cell.title = `${display} · ${fmtDur(sec)}`;
    bar.appendChild(cell);

    const entry = tpl("tpl-spk-legend-entry");
    const root = entry.firstElementChild;
    root.querySelector(".sw").dataset.spk = i % 5;
    const nameEl = pick(root, "name");
    nameEl.textContent = display;
    nameEl.dataset.spk = i % 5;
    pick(root, "pct").textContent = `${((sec / total) * 100).toFixed(0)}%`;
    legend.appendChild(entry);
  }
}

function buildMetaStrip(host, t, lowCount) {
  host.append(
    "merged at ", coloredSpan("fg", fmtClock(t.transcribed_at)),
    " · via ", coloredSpan("fg", t.transcriber || t.backend || "faster-whisper"),
    " on ", coloredSpan("fg", t.device || "CPU"),
  );
  if (lowCount > 0) {
    host.append(" · ");
    const s = document.createElement("span");
    s.style.color = "var(--warn)";
    s.textContent = `${lowCount} low-confidence`;
    host.appendChild(s);
  }
  if (t.suppressed_count > 0) {
    host.append(" · ");
    const s = document.createElement("span");
    s.style.color = "var(--rec)";
    s.textContent = `${t.suppressed_count} suppressed`;
    host.appendChild(s);
  }
}

function coloredSpan(cls, text) {
  const s = document.createElement("span");
  s.className = cls;
  s.textContent = text;
  return s;
}

function buildLine(it, speakers, aliases) {
  const node = tpl("tpl-merged-line");
  const row = node.firstElementChild;
  pick(row, "ts").textContent = `[${it.hms}]`;
  const label = pick(row, "speakerLabel");
  label.className = spkClassOf(speakers, it.speaker);
  label.textContent = `${aliasOf(it.speaker, aliases)}: `;
  const body = pick(row, "body");

  if (it.kind === "sup") {
    const seg = tpl("tpl-merged-seg-suppressed").firstElementChild;
    seg.title = `suppressed · matched: ${it.rule}`;
    seg.textContent = it.text;
    body.replaceWith(seg);
  } else if (it.lowConf) {
    const seg = tpl("tpl-merged-seg-lowconf");
    const lc = pick(seg, "lc");
    const confLabel = it.confidence != null ? it.confidence.toFixed(2) : "?";
    lc.title = `low confidence · avg_logprob ${confLabel}`;
    pick(seg, "text").textContent = it.text;
    // exp(avg_logprob) ≈ geometric-mean per-token prob, a reasonable proxy.
    const pct = it.confidence != null ? (Math.exp(it.confidence) * 100).toFixed(0) : "?";
    pick(seg, "pct").textContent = `⚑ ${pct}%`;
    body.replaceWith(seg.firstElementChild);
  } else {
    // The template's <span> *is* the slot — set textContent directly
    // rather than picking a descendant that doesn't exist.
    const seg = tpl("tpl-merged-seg-ok").firstElementChild;
    seg.textContent = it.text;
    body.replaceWith(seg);
  }
  return node;
}

function buildAudit(host, t, showAudit) {
  const wrapper = tpl("tpl-audit-wrapper");
  pick(wrapper, "toggle").textContent =
    `${showAudit ? "▾" : "▸"} hallucination audit · ${t.suppressed_count} segment${t.suppressed_count === 1 ? "" : "s"} dropped`;
  if (showAudit) {
    const tbl = tpl("tpl-audit-table");
    const rows = pick(tbl, "rows");
    for (const sup of t.suppressed) {
      rows.appendChild(slot(tpl("tpl-audit-row"), {
        time: fmtClock(sup.abs_start),
        speaker: sup.speaker || "",
        text: sup.text || "",
        rule: sup.matched_rule || "",
        from: truncMid(sup.source_wav || "", 28),
      }));
    }
    pick(wrapper, "table").appendChild(tbl);
  }
  host.hidden = false;
  host.appendChild(wrapper);
}

export function render(t, meta, { showAudit }) {
  const items = buildItems(t);
  const speakers = t.speakers || [];
  const aliases = meta?.aliases || {};
  const speakingByName = (t.speaking_seconds && typeof t.speaking_seconds === "object" && !Array.isArray(t.speaking_seconds))
    ? t.speaking_seconds : {};

  const lowCount = typeof t.low_confidence_count === "number"
    ? t.low_confidence_count
    : items.filter((it) => it.lowConf).length;

  const frag = tpl("tpl-merged");
  pick(frag, "headerMeta").textContent =
    `${t.wav_count || 0} wavs · ${(t.segments || []).length} seg · took ${fmtMs(t.transcribe_ms)} · model ${t.model || "?"}`;

  buildSpkBar(frag, speakers, speakingByName, aliases);
  buildMetaStrip(pick(frag, "metaStrip"), t, lowCount);

  const linesHost = pick(frag, "lines");
  for (const it of items) linesHost.appendChild(buildLine(it, speakers, aliases));

  if (t.suppressed_count > 0 && Array.isArray(t.suppressed)) {
    buildAudit(pick(frag, "auditWrapper"), t, showAudit);
  }
  return frag;
}
