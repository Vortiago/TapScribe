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
//     Delete audio (DELETE /api/sessions/{session}/audio, behind a confirm() —
//     destructive, reclaims disk; refused by the backend on the CURRENT session
//     and when a job is in flight, so it's disabled there), Absorb into… (a
//     <select> of OTHER archived sessions; POST /api/sessions/{target}/absorb
//     {source} folds THIS row's session into the picked target, then deletes
//     THIS folder — hidden on the current session, which the server refuses as
//     a source), and Delete (DELETE /api/sessions/{session} — the WHOLE folder:
//     audio AND merged transcript AND meta; refused on the current session, so
//     disabled there). Ports the classic dashboard's session management.
//   - Toolbar action (in the static panel head, wired once): Prune empty
//     (POST /api/sessions/prune-empty — deletes every session with 0 WAVs, no
//     merged transcript, no label; skips the current one), surfacing the count.
//
// The list is rendered through renderRegion (templates.js): it holds the
// search box + the rename inputs + the per-row absorb <select>, so the 500ms
// poll must not clobber an open edit — renderRegion skips the swap while a
// control inside the host is focused, and the caller-supplied signature skips
// the rebuild when nothing the list shows has changed. The header (with its
// live counts) repaints every tick; only the interactive list region is
// guarded. The prune button lives OUTSIDE the guarded region (static head).

import { tpl, pick, renderRegion } from "../../templates.js";
import { putJson, postJson, del, getJson } from "../../api.js";
import { fmtBytes, fmtSessionLabel } from "../../formatters.js";
import { header, strong, inline } from "../shell.js";

/**
 * A session's total original-WAV bytes. Precomputed server-side as
 * `total_bytes` — /api/state no longer ships the per-WAV files[] array (a huge
 * session re-shipped + re-parsed it every poll), so the listing reads the
 * aggregate instead of summing files itself.
 * @param {import('../../types.js').Session} s
 */
function totalBytes(s) {
  return s.total_bytes || 0;
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
  // Slim marker — segment count is a scalar field now, not segments.length.
  const segs = tx.segment_count || 0;
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

  // ---- Prune empty (view-level toolbar action) ------------------------------
  // Lives in the STATIC panel head (part of `frag`, never touched by the poll),
  // so its listener is wired exactly once here. POST /api/sessions/prune-empty
  // deletes every session with 0 WAVs, no merged transcript, and no label
  // (skips the current one); we surface the count the way classic did.
  const pruneBtn = /** @type {HTMLButtonElement} */ (pick(frag, "prune"));
  const pruneStatus = pick(frag, "pruneStatus");
  pruneBtn.addEventListener("click", async () => {
    if (!confirm(
      "Delete every session that has 0 WAVs, no merged transcript, and no " +
      "label?\n\nThe current session is always kept. This can't be undone.",
    )) return;
    pruneBtn.disabled = true;
    pruneStatus.textContent = "pruning…";
    try {
      const r = /** @type {{ count?: number }} */ (await postJson("/api/sessions/prune-empty"));
      const n = r.count || 0;
      pruneStatus.textContent = `removed ${n} empty session${n === 1 ? "" : "s"}`;
      setTimeout(() => {
        if (pruneStatus.textContent?.startsWith("removed")) pruneStatus.textContent = "";
      }, 2600);
    } catch (e) {
      pruneStatus.textContent = "";
      alert(`Prune empty failed: ${String(e).replace(/^Error:\s*/, "")}`);
    } finally {
      pruneBtn.disabled = false;
      afterMutate();
    }
  });

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
  /** The sessions array from the most recent tick — the absorb confirm() reads
    * it to resolve the target's display label. Kept in step with `update`. */
  /** @type {import('../../types.js').Session[]} */
  let lastSessions = [];
  /** Cached transcript-search results for the current non-empty filter;
    * null = not yet fetched or filter is empty. */
  /** @type {import('../../types.js').SearchHit[] | null} */
  let lastSearchResults = null;
  /** The filter value that produced `lastSearchResults`. */
  let lastSearchQuery = "";

  /** Effective label for a session = local overlay (if any) else server meta. */
  /** @param {import('../../types.js').Session} s */
  const labelFor = (s) => {
    const local = localLabels.get(s.session);
    if (local != null) return local;
    return metaFor(s).label;
  };

  /** Fire a transcript-search query when the local filter yields no results.
    * Results are cached per query string. */
  const fireSearch = async (/** @type {string} */ q) => {
    lastSearchQuery = q;
    lastSearchResults = null;
    try {
      const data = await getJson(`/api/search?q=${encodeURIComponent(q)}`);
      if (lastSearchQuery !== q) return; // filter moved on during the fetch — this result is stale
      lastSearchResults = Array.isArray(data) ? data : [];
    } catch { /* search unavailable — fall back to "searching…" */ }
  };

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

  /** Human label for a session id, for confirm() copy. */
  /** @param {import('../../types.js').Session} s */
  const displayName = (s) => labelFor(s) || fmtSessionLabel(s.session) || s.session;

  /** Delete a session's audio (WAVs + stripped/ + per-WAV caches). Destructive,
   * so behind a confirm(); the backend keeps the merged transcript + meta. */
  /** @param {import('../../types.js').Session} s */
  const deleteAudio = async (s) => {
    if (!confirm(
      `Delete all audio for "${displayName(s)}"?\n\n` +
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

  /** Delete a WHOLE session — folder, audio AND merged transcript AND meta.
   * The classic UI's "Delete" action: maximally destructive, so the confirm()
   * spells out that BOTH audio and transcript go. The backend refuses the
   * current session, so this is only offered (button enabled) on archived ones.
   * @param {import('../../types.js').Session} s */
  const deleteSession = async (s) => {
    const wavs = s.wav_count || 0;
    const tail = wavs
      ? ` and its ${wavs} WAV${wavs === 1 ? "" : "s"}`
      : "";
    if (!confirm(
      `Delete the ENTIRE session "${displayName(s)}"${tail}?\n\n` +
      "This removes the whole folder from disk — the audio AND the merged " +
      "transcript AND the label/aliases. This can't be undone.\n\n" +
      `(${s.session})`,
    )) return;
    try {
      await del(`/api/sessions/${encodeURIComponent(s.session)}`);
    } catch (e) {
      alert(`Delete session failed: ${String(e).replace(/^Error:\s*/, "")}`);
      return;
    } finally {
      // Drop any optimistic label / pending save for the gone session so a
      // debounced PUT can't 404 after the folder's deleted.
      clearTimeout(saveTimers.get(s.session));
      saveTimers.delete(s.session);
      localLabels.delete(s.session);
      afterMutate();
    }
  };

  /** Trigger the end-of-meeting pipeline for a session.
   * @param {import('../../types.js').Session} s */
  const processSession = async (s) => {
    try {
      await postJson(`/api/sessions/${encodeURIComponent(s.session)}/pipeline`);
    } catch (e) {
      alert(`Process failed: ${String(e).replace(/^Error:\s*/, "")}`);
      return;
    } finally {
      afterMutate();
    }
  };

  /** Absorb (fold) the SOURCE session into the TARGET: source WAVs + sidecars
   * move into target, source aliases fill gaps in target's, target's merged
   * transcript is cleared (now stale against the fuller WAV set), and the
   * SOURCE folder is deleted. Mirrors the classic absorb flow.
   * POST /api/sessions/{target}/absorb with body { source }.
   * @param {import('../../types.js').Session} source
   * @param {string} targetId */
  const absorbInto = async (source, targetId) => {
    if (!targetId || targetId === source.session) return;
    const sessions = lastSessions;
    const target = sessions.find((x) => x.session === targetId);
    const targetName = target ? displayName(target) : targetId;
    const wavs = source.wav_count || 0;
    if (!confirm(
      `Move all ${wavs} WAV${wavs === 1 ? "" : "s"} from "${displayName(source)}" ` +
      `into "${targetName}"?\n\n` +
      "The source folder will be deleted. The target's merged transcript (if " +
      "any) is cleared so you can re-run it on the combined audio. Target " +
      "speaker aliases are kept; source aliases fill in any names the target " +
      "doesn't already have. This can't be undone.",
    )) return;
    try {
      await postJson(`/api/sessions/${encodeURIComponent(targetId)}/absorb`, { source: source.session });
    } catch (e) {
      alert(`Absorb failed: ${String(e).replace(/^Error:\s*/, "")}`);
      return;
    } finally {
      // The source folder is gone — forget its optimistic label + pending
      // save. Deliberately NO onSelectSession(target) here: that would route
      // into the target's Transcript view, yanking the operator out of the
      // Sessions list mid-management (absorbing several sessions in a row is
      // the normal flow). The row's "open" button is the explicit way in.
      clearTimeout(saveTimers.get(source.session));
      saveTimers.delete(source.session);
      localLabels.delete(source.session);
      afterMutate();
    }
  };

  // ---- Row -----------------------------------------------------------------

  /**
   * @param {import('../../types.js').Session} s
   * @param {import('../../types.js').Session[]} absorbTargets — archived
   *   sessions this row could be folded INTO (excludes this row + the current
   *   session). Empty → the absorb picker is hidden.
   */
  const sessionRow = (s, absorbTargets) => {
    const node = tpl("tpl-next-sessrow");
    const row = /** @type {HTMLElement} */ (node.firstElementChild);
    row.dataset.sid = s.session; // stable per-row hook (e2e + debugging)
    if (s.session === focusedId) row.classList.add("is-focused");

    // Label + raw id sub. Capture the label element ONCE here: `node` is the
    // template fragment, which is drained when the row is appended to the DOM,
    // so re-`pick`ing it later (e.g. from the rename input handler) would throw
    // "template slot not found" — the bug that silently broke inline rename.
    const label = labelFor(s) || fmtSessionLabel(s.session) || s.session;
    const labelEl = pick(node, "label");
    labelEl.textContent = label;
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
      // keep the row's display label in step with the typed value (use the
      // captured element — `node` is drained once the row is in the DOM)
      labelEl.textContent = nameInput.value || fmtSessionLabel(s.session) || s.session;
      persistLabel(s.session, renameStatus);
    });

    // ---- Open (focus + route into the session) ----
    const openBtn = /** @type {HTMLButtonElement} */ (pick(node, "open"));
    openBtn.addEventListener("click", () => onSelectSession(s.session));

    // ---- Process (strip → transcribe → summarize pipeline) ----
    const procBtn = /** @type {HTMLButtonElement} */ (pick(node, "process"));
    if (s.is_current) {
      procBtn.disabled = true;
      procBtn.title = "can't process the current (live) session — archive it first";
    } else {
      procBtn.title = "run the end-of-meeting pipeline: strip silence, transcribe, and summarize";
      procBtn.addEventListener("click", () => processSession(s));
    }

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

    // ---- Absorb into… (fold THIS session, as source, into a target) ----
    // The select lives inside the renderRegion host, so the poll's focus guard
    // protects it while open. The backend refuses the CURRENT session as a
    // source, so we drop the picker entirely on the current row (and when there
    // is no other session to absorb into).
    const absorbSel = /** @type {HTMLSelectElement} */ (pick(node, "absorb"));
    if (s.is_current || !absorbTargets.length) {
      absorbSel.remove();
    } else {
      for (const t of absorbTargets) {
        const lbl = labelFor(t) || fmtSessionLabel(t.session) || t.session;
        absorbSel.add(new Option(`${lbl} (${t.wav_count || 0}w)`, t.session));
      }
      absorbSel.addEventListener("change", () => {
        const targetId = absorbSel.value;
        if (!targetId) return;
        // Reset + blur before firing so a refused merge doesn't pin the select
        // to the failed choice, and so the post-merge re-render isn't blocked by
        // the renderRegion focus guard.
        absorbSel.value = "";
        absorbSel.blur();
        absorbInto(s, targetId);
      });
    }

    // ---- Delete WHOLE session (audio + transcript + meta) ----
    const delSessBtn = /** @type {HTMLButtonElement} */ (pick(node, "delSession"));
    // The backend refuses the CURRENT session — disable + explain on it.
    if (s.is_current) {
      delSessBtn.disabled = true;
      delSessBtn.title = "can't delete the current session — rotate to a new one first";
    } else {
      delSessBtn.title = "delete the whole session — audio AND transcript";
      delSessBtn.addEventListener("click", () => deleteSession(s));
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
      // No local (label/id/date) matches — show the cross-session transcript
      // search instead, into the SAME rows body (the two are mutually exclusive,
      // so they share one container).
      if (lastSearchResults === null) {
        // Still waiting for the server-side search to return.
        pick(region, "shownCount").textContent =
          `${sessions.length} total · searching transcripts…`;
        body.replaceChildren();
      } else if (lastSearchResults.length === 0) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = `No sessions match "search" for "${filter}".`;
        body.replaceChildren(empty);
        pick(region, "shownCount").textContent = `0 search results`;
      } else {
        // Render search hits with snippet previews.
        const frag = document.createDocumentFragment();
        for (const hit of lastSearchResults) {
          // Template + textContent (NOT innerHTML) — hit.label/hit.session are
          // operator-controlled and must never be parsed as HTML.
          const node = tpl("tpl-next-searchrow");
          const row = /** @type {HTMLElement} */ (pick(node, "row"));
          row.style.cursor = "pointer";
          pick(node, "label").textContent = hit.label || hit.session;
          pick(node, "id").textContent = hit.session;
          pick(node, "count").textContent = String(hit.count);
          row.addEventListener("click", () => onSelectSession(hit.session));
          frag.appendChild(node);
          const snippetEl = document.createElement("div");
          snippetEl.className = "sessrow__snippet mono dim";
          snippetEl.textContent = hit.snippet;
          frag.appendChild(snippetEl);
        }
        body.replaceChildren(frag);
        pick(region, "shownCount").textContent =
          `${lastSearchResults.length} session${lastSearchResults.length === 1 ? "" : "s"} in transcript`;
      }
    } else {
      // Absorb targets: archived sessions only — the current (recording) one is
      // never a merge endpoint (classic kept it out of the picker entirely),
      // and a row can't absorb into itself. Computed once over the FULL list
      // (not just the filtered `shown`) so a filtered-out session is still a
      // valid target.
      const archived = sessions.filter((s) => !s.is_current);
      const list = document.createDocumentFragment();
      for (const s of shown) {
        const targets = archived.filter((t) => t.session !== s.session);
        list.appendChild(sessionRow(s, targets));
      }
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
    // Search state MUST be in the sig: an async search result arriving flips
    // `lastSearchResults` null→array without changing filter/sessions, so without
    // this the sig-gated renderRegion never repaints and the rows stay "searching…".
    lastSearchQuery,
    lastSearchResults === null ? "…" : lastSearchResults.map((h) => `${h.session}:${h.count}`).join(","),
    sessions.map((s) =>
      `${s.session}=${labelFor(s)}/${s.wav_count || 0}/${s.is_current ? 1 : 0}` +
      `/${s.stripped ? 1 : 0}/${s.session_transcript ? (s.session_transcript.segment_count || 0) : -1}` +
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
    lastSessions = sessions;
    const total = sessions.length;
    const transcribed = sessions.filter((s) => s.session_transcript).length;

    // Header repaints every tick (cheap, no controls) — real live counts.
    header(headHost, {
      eyebrow: "Global · Sessions",
      title: "Sessions",
      sub: total
        ? {
            sig: `${total}§${transcribed}`,
            build: () =>
              inline(strong(`${total}`), ` session${total === 1 ? "" : "s"} · `, strong(`${transcribed}`), " transcribed"),
          }
        : "no sessions yet — start recording to populate this list",
    });

    // Reset search state when the filter becomes empty.
    if (!filter) {
      lastSearchResults = null;
      lastSearchQuery = "";
    }

    // When local filter yields no results for a non-empty query, fire a
    // server-side transcript search. Results are cached per query string
    // so a poll tick that re-evaluates the same filter reuses them.
    if (filter && lastSearchQuery !== filter && sessions.filter(matches).length === 0) {
      fireSearch(filter);
    }

    // The list region holds the search box + rename inputs → renderRegion so
    // the poll never clobbers an open edit; signature so it only rebuilds when
    // the data actually changes.
    renderRegion(listHost, () => buildList(sessions), { sig: listSig(sessions) });
  };

  return { node: frag, update };
}
