// @ts-check
// gate-allow: signal-listener — handlers attach to nodes this view builds and owns; an evicted or rebuilt view drops the whole subtree with its listeners (no document/window targets here). Revisit if views gain a mount AbortSignal.
// Stages · Sessions (GLOBAL · all sessions). A dense, scannable, manageable
// list of EVERY session on disk — the spine's session <select> doesn't scale
// past a handful, so this is the place to find/manage one when there are many.
// Pure /api/state data (the same Session[] the spine reads), no mock.
//
//   - a filter/search box narrows the list by label, session id, or date so
//     it stays usable at 50+ sessions; when that local filter yields zero
//     matches, the same box falls over to a server-side cross-session
//     transcript-content search (GET /api/search, #315) and the rows body
//     shows snippet-preview hits instead;
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
// The list is a KEYED LIST rendered through `renderList` (#312): the region
// chrome (search box, column header, placeholder sibling) mounts exactly once,
// and rows reconcile per tick — content ticks mutate cells via fillRow, gated by
// the seam on each row's `itemSig`, while structural flips (is_current,
// has-WAVs, absorb-target set) are folded into the row KEY and recreate just
// that row. It passes NO list-level sig, deliberately: see rowSig's docstring.
// The 500ms poll therefore never clobbers an open edit, and none of that
// discipline lives here — the seam holds a row whose control is focused, holds
// the whole render when a focused row would be REMOVED, and defers on a text
// selection, each without advancing the gate it skipped (ADR-0004). The header
// (with its live counts) repaints every tick; the prune button lives in the
// static panel head, wired once.

import { tpl, pick, mount, renderList, deferIfSelectionInside } from "../../templates.js";
import { postJson, del, getJson, errText } from "../../api.js";
import { fmtBytes, fmtSessionLabel } from "../../formatters.js";
import { header, strong, inline, newestFirst } from "../shell.js";
import {
  editSessionLabel,
  forgetSessionLabel,
  pendingOr,
  sessionLabelFor,
} from "../session-labels.js";

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
      alert(`Prune empty failed: ${errText(e)}`);
    } finally {
      pruneBtn.disabled = false;
      afterMutate();
    }
  });

  // ---- View-local state -----------------------------------------------------
  /** The focused session id (highlighted row); kept in step with `update`. */
  let focusedId = "";
  /** Current filter text (lower-cased). The search <input> is built once with
   * the region chrome and never rebuilt, so it persists trivially. */
  let filter = "";
  // Renames go through next/session-labels.js (#355) — it owns the pending
  // edit, the debounced PUT and the sweep, shared with the spine's rename card.
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
  /** Query waiting on the search debounce timer ("" = none). */
  let pendingSearch = "";
  /** @type {ReturnType<typeof setTimeout> | undefined} */
  let searchTimer;
  /** Last fireSearch failed — show "unavailable, retrying" instead of "searching…". */
  let searchFailed = false;

  /** Effective label for a session = local overlay (if any) else server meta. */
  /** @param {import('../../types.js').Session} s */
  const labelFor = (s) => pendingOr(s.session, metaFor(s).label);


  /** Fire a transcript-search query when the local filter yields no results.
    * Results are cached per query string. */
  const fireSearch = async (/** @type {string} */ q) => {
    pendingSearch = "";
    if (q !== filter) return; // filter moved on while the debounce timer ran
    lastSearchQuery = q;
    lastSearchResults = null;
    searchFailed = false;
    try {
      const data = await getJson(`/api/search?q=${encodeURIComponent(q)}`);
      if (lastSearchQuery !== q) return; // filter moved on during the fetch — this result is stale
      lastSearchResults = Array.isArray(data) ? data : [];
      syncRows(); // paint the hits now instead of waiting out the poll tick
    } catch {
      if (lastSearchQuery !== q) return; // a newer query owns the state
      // Transient /api/search failure (network, 5xx): forget the query so the
      // next tick re-fires it, and flag the failure so the counts line says
      // "unavailable — retrying" instead of a forever-"searching…" wedge.
      lastSearchQuery = "";
      searchFailed = true;
    }
  };

  /** Debounce fireSearch so per-keystroke syncRows calls don't each hit
    * GET /api/search (a full-corpus transcript scan server-side). Same debounce
    * shape as the shared field saver, but single-slot: one query at a time. */
  const scheduleSearch = (/** @type {string} */ q) => {
    pendingSearch = q;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => fireSearch(q), 250);
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
      alert(`Delete audio failed: ${errText(e)}`);
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
      alert(`Delete session failed: ${errText(e)}`);
      return;
    } finally {
      // Drop any optimistic label / pending save for the gone session so a
      // debounced PUT can't 404 after the folder's deleted.
      forgetSessionLabel(s.session);
      afterMutate();
    }
  };

  /** Trigger the end-of-meeting pipeline for a session.
   * @param {import('../../types.js').Session} s */
  const processSession = async (s) => {
    try {
      await postJson(`/api/sessions/${encodeURIComponent(s.session)}/pipeline`);
    } catch (e) {
      alert(`Process failed: ${errText(e)}`);
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
      alert(`Absorb failed: ${errText(e)}`);
      return;
    } finally {
      // The source folder is gone — forget its optimistic label + pending
      // save. Deliberately NO onSelectSession(target) here: that would route
      // into the target's Transcript view, yanking the operator out of the
      // Sessions list mid-management (absorbing several sessions in a row is
      // the normal flow). The row's "open" button is the explicit way in.
      forgetSessionLabel(source.session);
      afterMutate();
    }
  };

  // ---- Row -----------------------------------------------------------------

  /**
   * @param {import('../../types.js').Session} s
   * @param {import('../../types.js').Session[]} archived — ALL archived
   *   sessions (shared per-tick array; the picker skips this row itself).
   *   A row can absorb into any OTHER archived session.
   */
  const sessionRow = (s, archived) => {
    const node = tpl("tpl-next-sessrow");
    const row = /** @type {HTMLElement} */ (node.firstElementChild);
    row.dataset.sid = s.session; // stable per-row hook (e2e + debugging)
    if (s.session === focusedId) row.classList.add("is-focused");

    // Label + raw id sub. Capture the label element ONCE here: `node` is the
    // template fragment, which is drained when the row is appended to the DOM,
    // so re-`pick`ing it later (e.g. from the rename input handler) would throw
    // "template slot not found" — the bug that silently broke inline rename.
    const label = displayName(s);
    const labelEl = pick(node, "label");
    labelEl.textContent = label;
    const idEl = pick(node, "id");
    idEl.textContent = s.session;
    idEl.title = s.session;

    // Status chip — ● live for the current (recording) session, else archived.
    // is_current is folded into the reconcile KEY, so it's static per node.
    const status = pick(node, "status");
    if (s.is_current) {
      status.textContent = "● live";
      status.classList.add("is-live");
    } else {
      status.textContent = "archived";
      status.classList.add("is-archived");
    }

    const wavs = s.wav_count || 0;

    // ---- Rename (inline editable label) ----
    const nameInput = /** @type {HTMLInputElement} */ (pick(node, "rename"));
    const renameStatus = pick(node, "renameStatus");
    // Stamp the cell with the session it reports on so a settling save can
    // re-resolve the LIVE one (session-labels.js's statusCellsFor) instead of
    // writing into this node, which is detached once the row is rebuilt or
    // filtered out.
    renameStatus.dataset.statusSid = s.session;
    nameInput.addEventListener("input", () => {
      editSessionLabel(s.session, nameInput.value);
      // keep the row's display label in step with the typed value (use the
      // captured element — `node` is drained once the row is in the DOM)
      labelEl.textContent = nameInput.value || fmtSessionLabel(s.session) || s.session;
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
    // While the select is focused (dropdown open), renderList holds this whole
    // row's update, so its options are left alone until blur. The backend
    // refuses the CURRENT session as a source, so we drop the picker entirely on
    // the current row (and when there is no other session to absorb into).
    const absorbSel = /** @type {HTMLSelectElement} */ (pick(node, "absorb"));
    if (s.is_current || archived.length <= 1) {
      // No target: the current session can't be a source, and an archived
      // row needs at least one OTHER archived session to fold into.
      absorbSel.remove();
    } else {
      // Options are filled (and label-refreshed) by fillRow below.
      absorbSel.addEventListener("change", () => {
        const targetId = absorbSel.value;
        if (!targetId) return;
        // Reset + blur before firing so a refused merge doesn't pin the select
        // to the failed choice, and so the seam's per-row hold doesn't keep
        // holding back the post-merge option refresh.
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

    // Mutable content (label, counts, tx status, size, absorb options…) is
    // owned by fillRow, which renderList runs for us — on a freshly created row
    // as well as on the per-tick update path — so this builds the shell only.
    return node;
  };

  /**
   * This list has NO cheap list-level stamp — every row's content (label, bytes,
   * tx status, progress) changes independently and no server-side digest covers
   * them, so building one would itself be the O(rows) walk a gate is meant to
   * skip. Hence the per-row gate: `renderList` compares this signature and runs
   * `fillRow` only for rows whose content actually moved.
   * @param {import('../../types.js').Session} s
   * @param {string} targetsSig — per-tick content signature of `archived`
   *   (ids · labels · wav counts — ids too, so a target-set change with
   *   identical labels still repaints the option VALUES).
   */
  const rowSig = (s, targetsSig) => [
    sessionLabelFor(s),
    s.session === focusedId ? 1 : 0,
    s.wav_count || 0,
    s.stripped ? 1 : 0,
    s.session_transcript ? s.session_transcript.segment_count || 0 : -1,
    totalBytes(s),
    s.progress ? 1 : 0,
    targetsSig,
  ].join("§");

  /**
   * Refresh a row's mutable cells IN PLACE — `renderList`'s `update`, run both on
   * a freshly created row and on the per-tick path. Structural bits (is_current,
   * has-WAVs, the absorb-target id set) are folded into the reconcile KEY, so a
   * flip recreates the row via sessionRow (its wiring differs); this touches only
   * content.
   *
   * NO focus guards and no signature bookkeeping of their own: the seam holds a
   * whole row whose control is focused (deferring without stamping its sig, so
   * the held write lands on the first tick after blur) and stamps the sig only
   * when this ran to completion. That is the ADR-0004 trap this view used to own
   * by hand and got wrong — the sig was advanced ABOVE the guards, so the skipped
   * update was stranded forever: the next tick recomputed the identical sig and
   * early-returned, the row kept a stale label, and a later keystroke persisted
   * the stale value back over an external rename.
   * @param {HTMLElement} row
   * @param {import('../../types.js').Session} s
   * @param {import('../../types.js').Session[]} archived — shared per-tick
   *   array of all archived sessions; the options loop skips this row itself.
   */
  const fillRow = (row, s, archived) => {
    row.classList.toggle("is-focused", s.session === focusedId);

    const nameInput = /** @type {HTMLInputElement} */ (row.querySelector('[data-slot="rename"]'));
    if (nameInput) {
      nameInput.value = labelFor(s);
      nameInput.placeholder = fmtSessionLabel(s.session) || s.session;
      const labelEl = row.querySelector('[data-slot="label"]');
      if (labelEl) labelEl.textContent = displayName(s);
    }

    const wavs = s.wav_count || 0;
    pick(row, "wavs").textContent = wavs ? `${wavs}` : "—";
    const strip = pick(row, "stripped");
    strip.textContent = s.stripped ? "✓" : "—";
    strip.classList.toggle("is-yes", !!s.stripped);
    strip.classList.toggle("is-no", !s.stripped);
    const tx = txStatus(s);
    const txEl = pick(row, "tx");
    txEl.textContent = tx.text;
    txEl.className = `sesstx mono tone-${tx.tone}`;
    pick(row, "size").textContent = wavs ? fmtBytes(totalBytes(s)) : "—";

    const absorbSel = /** @type {HTMLSelectElement | null} */ (row.querySelector('[data-slot="absorb"]'));
    if (absorbSel) {
      while (absorbSel.options.length > 1) absorbSel.remove(1);
      for (const t of archived) {
        if (t.session === s.session) continue; // a row can't absorb into itself
        const lbl = displayName(t);
        absorbSel.add(new Option(`${lbl} (${t.wav_count || 0}w)`, t.session));
      }
    }
  };

  /**
   * Does a session match the current filter? Matches against the effective
   * label, the raw session id, and the human date (fmtSessionLabel).
   * @param {import('../../types.js').Session} s
   */
  const matches = (s) => {
    if (!filter) return true;
    // Purely a filter: keeping a row the operator is typing in is `renderList`'s
    // removal hold, not this predicate's business — the seam defers the whole
    // render when a focused row's key would leave the list, so the node (and the
    // focus in it) survives until blur.
    const hay = [
      sessionLabelFor(s), // per-tick filter — same value as labelFor, no metaFor allocation
      s.session,
      fmtSessionLabel(s.session),
    ].join(" ").toLowerCase();
    return hay.includes(filter);
  };

  /**
   * Build the list region CHROME once — search box, column header, the
   * placeholder sibling, and the (empty) rows host. Rows are reconciled into
   * it in place by syncRows, so this never rebuilds: the search box is wired
   * exactly once and is never swapped out from under the operator (the old
   * whole-region rebuild needed a force+refocus hack for exactly that).
   */
  const buildRegion = () => {
    const region = tpl("tpl-next-sesslist");
    const search = /** @type {HTMLInputElement} */ (pick(region, "search"));
    search.value = filter;
    search.addEventListener("input", () => {
      filter = search.value.trim().toLowerCase();
      syncRows(); // in place — this input is untouched, focus + caret keep themselves
    });
    return region;
  };

  /**
   * Reconcile the rows INTO THE LIVE REGION in place (no host swap): the
   * shown-count, the empty/filtered placeholder (a hidden-toggled SIBLING of
   * the rows host — renderList owns the host's children outright), and the keyed
   * row list. Safe per tick AND from the filter handler. Content ticks (bytes,
   * tx status, labels) mutate cells via fillRow; structural flips (is_current,
   * has-WAVs, the absorb-target set) change the KEY and recreate just that row.
   * Every hold is the seam's: a text selection inside the list defers, a focused
   * control holds ITS OWN ROW's update (coarser than a per-field guard — see
   * fillRow), and a focused row that would be removed defers the whole render
   * (ADR-0004).
   *
   * When the local (label/id/date) filter yields zero matches, the rows host
   * falls over to cross-session transcript search instead (#315): that's a
   * COLD, discrete mode switch (not a per-tick content update), so it swaps
   * the body directly rather than through reconcileList's keyed hot path.
   * On the way BACK from search mode the normal path clears the body first:
   * reconcileList only removes nodes it created (keyed in its WeakMap), so
   * the keyless search nodes would otherwise dangle below the real rows.
   */
  const syncRows = () => {
    const body = /** @type {HTMLElement | null} */ (listHost.querySelector('[data-slot="rows"]'));
    if (!body) return; // region not mounted yet — update() mounts it first
    const sessions = lastSessions;
    const shown = sessions.filter(matches);
    const counts = listHost.querySelector('[data-slot="shownCount"]');
    const ph = /** @type {HTMLElement | null} */ (listHost.querySelector('[data-slot="rowsEmpty"]'));

    if (!filter && (lastSearchQuery || pendingSearch)) {
      // Leaving search mode — drop the cached result so a later, identical
      // query re-fires against current data instead of replaying a stale hit,
      // and cancel any still-debouncing query so it can't fire after the fact.
      clearTimeout(searchTimer);
      pendingSearch = "";
      lastSearchResults = null;
      lastSearchQuery = "";
      searchFailed = false;
    }

    if (filter && !shown.length) {
      if (ph) ph.hidden = true;
      if (lastSearchQuery !== filter && pendingSearch !== filter) scheduleSearch(filter);
      if (counts) {
        if (lastSearchResults === null) {
          counts.textContent = searchFailed
            ? `${sessions.length} total · transcript search unavailable — retrying…`
            : `${sessions.length} total · searching transcripts…`;
        } else if (!lastSearchResults.length) {
          counts.textContent = "0 search results";
        } else {
          counts.textContent =
            `${lastSearchResults.length} session${lastSearchResults.length === 1 ? "" : "s"} in transcript`;
        }
      }

      if (deferIfSelectionInside(body)) return;

      if (lastSearchResults === null) {
        body.replaceChildren(); // static-render — cold search-mode transition, see docstring
      } else if (!lastSearchResults.length) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = `No sessions match "${filter}".`;
        body.replaceChildren(empty); // static-render — cold search-mode transition, see docstring
      } else {
        // Render search hits with snippet previews. Template + textContent
        // (NOT innerHTML) — hit.label/hit.session are operator-controlled and
        // must never be parsed as HTML.
        const frag = document.createDocumentFragment();
        for (const hit of lastSearchResults) {
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
        body.replaceChildren(frag); // static-render — cold search-mode transition, see docstring
      }
      return;
    }

    if (counts) {
      counts.textContent = filter ? `${shown.length} of ${sessions.length}` : `${sessions.length} total`;
    }
    if (ph) {
      ph.hidden = !!shown.length;
      ph.textContent = "No sessions yet — start recording to see them here.";
    }

    // The search branch above raw-swaps keyless nodes (search-hit rows,
    // snippets, the no-match placeholder) into the body. renderList's reconcile
    // only tracks and removes nodes it created itself, so returning from search
    // mode must clear those foreign nodes first or they dangle below the real
    // rows forever. Normal rows all carry data-sid (sessionRow), so
    // "no [data-sid] child" ⇔ the body holds only foreign nodes (or nothing).
    // This stays HERE rather than in the seam: it exists because THIS view has a
    // second, cold rendering mode, which no other keyed list has — one adapter is
    // a hypothetical seam.
    if (!body.querySelector("[data-sid]")) body.replaceChildren(); // gate-allow: raw-swap — clears the search branch's keyless nodes so renderList owns the host

    // Absorb targets: archived sessions only — the current (recording) one is
    // never a merge endpoint, and a row can't absorb into itself. Computed
    // over the FULL list (not just the filtered `shown`) so a filtered-out
    // session is still a valid target. Only the picker's EXISTENCE is
    // structural (sessionRow drops the <select> when there are no targets) →
    // a has-targets bit in the key; the target ids/labels/counts are content
    // → refreshed in place by fillRow. Keying on the full target-id set would
    // re-key EVERY row whenever any session is added/archived/deleted,
    // tearing a focused rename input out mid-edit (ADR-0004).
    const archived = sessions.filter((s) => !s.is_current);
    // ONE shared target-content signature per tick — a per-row exclude-self
    // copy would be O(rows × targets) allocation churn every poll. Including
    // a row's own entry in its sig is harmless (its own label/wav-count terms
    // already re-fill it); the options loop skips self when painting. The
    // key's has-targets bit is arithmetic, not an allocation: a non-current
    // row has targets iff some OTHER archived session exists.
    const targetsSig = archived.map((t) => `${t.session}·${sessionLabelFor(t)}·${t.wav_count || 0}`).join(",");
    // No list-level `sig` — see rowSig's docstring: this list has no cheap
    // aggregate stamp, so it gates per row instead. `auditRows` is ON because a
    // row's entire content comes from sessionRow + fillRow, making a probe row a
    // sound comparison.
    renderList(body, shown, {
      key: (s) => `${s.session}·c${s.is_current ? 1 : 0}·w${(s.wav_count || 0) > 0 ? 1 : 0}·t${!s.is_current && archived.length > 1 ? 1 : 0}`,
      create: (s) => /** @type {HTMLElement} */ (sessionRow(s, archived).firstElementChild),
      update: (row, s) => fillRow(/** @type {HTMLElement} */ (row), s, archived),
      itemSig: (s) => rowSig(s, targetsSig),
      auditRows: true,
    });
  };

  // ---- Per-tick update ------------------------------------------------------

  /**
   * @param {import('../../types.js').AppState} j
   * @param {import('../../types.js').Session | null} sess
   */
  const update = (j, sess) => {
    focusedId = sess?.session || "";
    // Newest first (shared shell.js comparator — same ordering as the spine picker).
    const sessions = [...(j.sessions || [])].sort(newestFirst);
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

    // Chrome mounts once (static-render — never rebuilt); rows (or, in search
    // mode, search hits — see syncRows) reconcile in place every tick.
    if (!listHost.firstElementChild) mount(listHost, buildRegion());
    syncRows();
  };

  return { node: frag, update };
}
