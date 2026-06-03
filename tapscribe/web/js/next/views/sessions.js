// @ts-check
// Stages · Sessions (GLOBAL · all sessions). A dense, scannable, manageable
// list of EVERY session on disk — the spine's session <select> doesn't scale
// past a handful, so this is the place to find/manage one when there are many.
// Pure /api/state data (the same Session[] the spine reads), no mock.
//
//   - a filter/search box narrows the list by label, session id, or date so
//     it stays usable at 50+ sessions;
//   - a dense table, newest first, one row per session: label (or
//     fmtSessionLabel) + the raw id as a dim sub, a status chip (● live for the
//     current session, else archived), WAV count, stripped?, transcript status
//     (merged / "N segs" / not run), and total size where the WAV list reports
//     it. The currently-focused session's row carries the amber left-edge the
//     spine uses for its active item.
//   - Row actions: Open (onSelectSession → focuses + routes into the session,
//     same as the spine picker), Rename (inline editable label, debounced
//     optimistic PUT /api/session-meta/{session} {label}, mirrors people.js),
//     and Delete audio (DELETE /api/sessions/{session}/audio, behind a
//     confirm() — destructive, reclaims disk; refused by the backend on the
//     CURRENT session and when a job is in flight, so it's disabled there).
//
// The list is rendered through renderRegion (templates.js): it holds the
// search box + the rename inputs, so the 500ms poll must not clobber an open
// edit — renderRegion skips the swap while a control inside the host is
// focused, and the caller-supplied signature skips the rebuild when nothing
// the list shows has changed. The header (with its live counts) repaints every
// tick; only the interactive list region is guarded.

import { tpl, pick, renderRegion } from "../../templates.js";
import { putJson, del } from "../../api.js";
import { fmtBytes, fmtSessionLabel } from "../../formatters.js";
import { header, strong, inline } from "../shell.js";

/**
 * Sum of a session's original WAV sizes — the only size signal /api/state
 * carries (per-WAV `size`; there's no session-level total or duration field,
 * so we derive what we can and show "—" when there are no files to sum).
 * @param {import('../../types.js').Session} s
 */
function totalBytes(s) {
  let n = 0;
  for (const f of (s.files || [])) n += f.size || 0;
  return n;
}

/**
 * Transcript status descriptor for a session: the merged transcript wins
 * (segment count, suppressed badge), else "not run".
 * @param {import('../../types.js').Session} s
 * @returns {{ text: string, tone: "good"|"warn"|"mute" }}
 */
function txStatus(s) {
  const tx = s.session_transcript;
  if (!tx) return { text: "not run", tone: "mute" };
  const segs = (tx.segments || []).length;
  const suppressed = tx.suppressed_count || 0;
  if (suppressed) return { text: `${segs} seg · ${suppressed} supp`, tone: "warn" };
  return { text: `merged · ${segs} seg`, tone: "good" };
}

/**
 * @param {{
 *   metaFor: (s: import('../../types.js').Session) => import('../../types.js').EffectiveMeta,
 *   onSelectSession: (id: string) => void,
 *   afterMutate: () => void,
 * }} ctx
 * @returns {{ node: DocumentFragment, update: (j: import('../../types.js').AppState, session: import('../../types.js').Session | null) => void }}
 */
export function build(ctx) {
  const { metaFor, onSelectSession, afterMutate } = ctx;
  const frag = tpl("tpl-next-view-sessions");

  const headHost = pick(frag, "head");
  const listHost = pick(frag, "listHost");

  // ---- View-local state -----------------------------------------------------
  /** The focused session id (highlighted row); kept in step with `update`. */
  let focusedId = "";
  /** Current filter text (lower-cased). Persists across poll ticks because the
   * <input> lives inside the renderRegion host and is read on rebuild. */
  let filter = "";
  /** Optimistic local label overlay, per session id, so a save + re-poll round
   * trip doesn't clear the field the operator just typed (mirrors people.js). */
  /** @type {Map<string, string>} */
  const localLabels = new Map();
  /** Debounce timers per session id (debounced PUT, like the alias editor). */
  /** @type {Map<string, ReturnType<typeof setTimeout>>} */
  const saveTimers = new Map();

  /** Effective label for a session = local overlay (if any) else server meta. */
  /** @param {import('../../types.js').Session} s */
  const labelFor = (s) => {
    const local = localLabels.get(s.session);
    if (local != null) return local;
    return metaFor(s).label;
  };

  /** Debounced PUT /api/session-meta/{session} with just the { label }. The
   * server merges partial meta, so aliases/prompt/hotwords are preserved. */
  /** @param {string} sid @param {HTMLElement} statusEl */
  const persistLabel = (sid, statusEl) => {
    clearTimeout(saveTimers.get(sid));
    saveTimers.set(sid, setTimeout(async () => {
      saveTimers.delete(sid);
      const label = localLabels.get(sid);
      if (label == null) return;
      statusEl.textContent = "saving…";
      try {
        await putJson(`/api/session-meta/${encodeURIComponent(sid)}`, { label });
        if (statusEl.textContent === "saving…") {
          statusEl.textContent = "saved";
          setTimeout(() => { if (statusEl.textContent === "saved") statusEl.textContent = ""; }, 1400);
        }
      } catch (e) {
        statusEl.textContent = `failed: ${String(e).replace(/^Error:\s*/, "")}`;
      } finally {
        afterMutate();
      }
    }, 600));
  };

  /** Delete a session's audio (WAVs + stripped/ + per-WAV caches). Destructive,
   * so behind a confirm(); the backend keeps the merged transcript + meta. */
  /** @param {import('../../types.js').Session} s */
  const deleteAudio = async (s) => {
    const name = labelFor(s) || fmtSessionLabel(s.session) || s.session;
    if (!confirm(
      `Delete all audio for "${name}"?\n\n` +
      "Removes the original WAVs, the stripped/ folder, and per-WAV transcript " +
      "caches to reclaim disk. The merged transcript and labels are kept. " +
      "This can't be undone.",
    )) return;
    try {
      await del(`/api/sessions/${encodeURIComponent(s.session)}/audio`);
    } catch (e) {
      alert(`Delete audio failed: ${String(e).replace(/^Error:\s*/, "")}`);
      return;
    } finally {
      afterMutate();
    }
  };

  // ---- Row -----------------------------------------------------------------

  /** @param {import('../../types.js').Session} s */
  const sessionRow = (s) => {
    const node = tpl("tpl-next-sessrow");
    const row = /** @type {HTMLElement} */ (node.firstElementChild);
    if (s.session === focusedId) row.classList.add("is-focused");

    // Label + raw id sub.
    const label = labelFor(s) || fmtSessionLabel(s.session) || s.session;
    pick(node, "label").textContent = label;
    const idEl = pick(node, "id");
    idEl.textContent = s.session;
    idEl.title = s.session;

    // Status chip — ● live for the current (recording) session, else archived.
    const status = pick(node, "status");
    if (s.is_current) {
      status.textContent = "● live";
      status.classList.add("is-live");
    } else {
      status.textContent = "archived";
      status.classList.add("is-archived");
    }

    // WAV count.
    const wavs = s.wav_count || 0;
    pick(node, "wavs").textContent = wavs ? `${wavs}` : "—";

    // Stripped? — ✓ when a stripped/ folder exists, else —.
    const strip = pick(node, "stripped");
    if (s.stripped) { strip.textContent = "✓"; strip.classList.add("is-yes"); }
    else { strip.textContent = "—"; strip.classList.add("is-no"); }

    // Transcript status.
    const tx = txStatus(s);
    const txEl = pick(node, "tx");
    txEl.textContent = tx.text;
    txEl.classList.add(`tone-${tx.tone}`);

    // Size — sum of original WAV sizes (the only size signal we have).
    const bytes = totalBytes(s);
    pick(node, "size").textContent = wavs ? fmtBytes(bytes) : "—";

    // ---- Rename (inline editable label) ----
    const nameInput = /** @type {HTMLInputElement} */ (pick(node, "rename"));
    nameInput.value = labelFor(s);
    nameInput.placeholder = fmtSessionLabel(s.session) || s.session;
    const renameStatus = pick(node, "renameStatus");
    nameInput.addEventListener("input", () => {
      localLabels.set(s.session, nameInput.value);
      // keep the row's display label in step with the typed value
      pick(node, "label").textContent = nameInput.value || fmtSessionLabel(s.session) || s.session;
      persistLabel(s.session, renameStatus);
    });

    // ---- Open (focus + route into the session) ----
    const openBtn = /** @type {HTMLButtonElement} */ (pick(node, "open"));
    openBtn.addEventListener("click", () => onSelectSession(s.session));

    // ---- Delete audio ----
    const delBtn = /** @type {HTMLButtonElement} */ (pick(node, "del"));
    // The backend refuses the CURRENT session (rotate first) and any session
    // with no WAVs to delete — disable + explain rather than fail on click.
    if (s.is_current) {
      delBtn.disabled = true;
      delBtn.title = "can't delete audio from the current session — rotate to a new one first";
    } else if (!wavs) {
      delBtn.disabled = true;
      delBtn.title = "no audio to delete";
    } else {
      delBtn.addEventListener("click", () => deleteAudio(s));
    }

    return node;
  };

  /**
   * Does a session match the current filter? Matches against the effective
   * label, the raw session id, and the human date (fmtSessionLabel).
   * @param {import('../../types.js').Session} s
   */
  const matches = (s) => {
    if (!filter) return true;
    const hay = [
      labelFor(s),
      s.session,
      fmtSessionLabel(s.session),
    ].join(" ").toLowerCase();
    return hay.includes(filter);
  };

  /**
   * Build the whole list region (search box + table) — only invoked by
   * renderRegion when it actually swaps, so a skipped tick never builds.
   * @param {import('../../types.js').Session[]} sessions
   */
  const buildList = (sessions) => {
    const region = tpl("tpl-next-sesslist");

    // Search box. Re-wire its listener each rebuild (a fresh node every swap);
    // renderRegion guards the swap while it's focused, so an in-progress query
    // is never interrupted mid-keystroke.
    const search = /** @type {HTMLInputElement} */ (pick(region, "search"));
    search.value = filter;
    search.addEventListener("input", () => {
      filter = search.value.trim().toLowerCase();
      // Re-render just the list region with the new filter. force:true so the
      // focused search box doesn't make renderRegion skip its own update.
      renderRegion(listHost, () => buildList(sessions), { force: true });
      // Keep focus + caret at the end after the swap.
      const next = /** @type {HTMLInputElement | null} */ (listHost.querySelector('[data-slot="search"]'));
      if (next) { next.focus(); next.setSelectionRange(next.value.length, next.value.length); }
    });

    const shown = sessions.filter(matches);
    pick(region, "shownCount").textContent =
      filter ? `${shown.length} of ${sessions.length}` : `${sessions.length} total`;

    const body = pick(region, "rows");
    if (!sessions.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "No sessions yet — start recording to see them here.";
      body.replaceChildren(empty);
    } else if (!shown.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = `No sessions match “${filter}”.`;
      body.replaceChildren(empty);
    } else {
      const list = document.createDocumentFragment();
      for (const s of shown) list.appendChild(sessionRow(s));
      body.replaceChildren(list);
    }
    return region;
  };

  /** A signature of everything the list region shows, so renderRegion only
   * rebuilds when the data (not just the tick) changes — and never while a
   * control inside it is focused. */
  /** @param {import('../../types.js').Session[]} sessions */
  const listSig = (sessions) => [
    focusedId,
    filter,
    sessions.map((s) =>
      `${s.session}=${labelFor(s)}/${s.wav_count || 0}/${s.is_current ? 1 : 0}` +
      `/${s.stripped ? 1 : 0}/${s.session_transcript ? (s.session_transcript.segments || []).length : -1}` +
      `/${totalBytes(s)}/${!!s.progress}`,
    ).join("§"),
  ].join("‖");

  // ---- Per-tick update ------------------------------------------------------

  /**
   * @param {import('../../types.js').AppState} j
   * @param {import('../../types.js').Session | null} sess
   */
  const update = (j, sess) => {
    focusedId = sess?.session || "";
    // Newest first — session ids are ISO-ish timestamps, so a string sort
    // descending is chronological (same ordering the spine picker uses).
    const sessions = [...(j.sessions || [])].sort((a, b) => (a.session < b.session ? 1 : -1));
    const total = sessions.length;
    const transcribed = sessions.filter((s) => s.session_transcript).length;

    // Header repaints every tick (cheap, no controls) — real live counts.
    header(headHost, {
      eyebrow: "Global · Sessions",
      title: "Sessions",
      sub: total
        ? inline(strong(`${total}`), ` session${total === 1 ? "" : "s"} · `, strong(`${transcribed}`), " transcribed")
        : "no sessions yet — start recording to populate this list",
    });

    // The list region holds the search box + rename inputs → renderRegion so
    // the poll never clobbers an open edit; signature so it only rebuilds when
    // the data actually changes.
    renderRegion(listHost, () => buildList(sessions), { sig: listSig(sessions) });
  };

  return { node: frag, update };
}
