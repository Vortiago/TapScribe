// @ts-check
// gate-allow: signal-listener — handlers attach to nodes this view builds and owns; an evicted or rebuilt view drops the whole subtree with its listeners (no document/window targets here). Revisit if views gain a mount AbortSignal.
// Stages · People (GLOBAL · Registry) — the canonical cross-session Person
// model (ADR-0009; CONTEXT.md: Person · Identity · Roster · People Registry).
//
// ONE editable registry, rendered straight from /api/state's `people` rows
// (one per Person, aggregated server-side from every session's roster + the
// live identities — never empty, since every device Identity auto-binds to a
// Person default-named from the bridge). The view does no client-side joining;
// it renders rows and mutates them through /api/people:
//
//   · rename   — PUT  /api/people/{id} {name}  (name once → propagates to every
//                 session's transcript via the server-resolved name map)
//   · merge    — POST /api/people/merge {survivor, absorbed}
//   · detach   — POST /api/people/{id}/detach {identity}
//
// Selecting a session in the spine only HIGHLIGHTS the people present in it
// (`is-here`); it never swaps the list out — the registry is global, so the
// "it changes per session" complaint is gone. The whole list renders through
// renderRegionModel, so an in-progress name edit / open merge picker / mid-copy
// selection holds the swap (Interaction hold; templates.js).

import { tpl, pick, renderRegionModel } from "../../templates.js";
import { putJson, postJson, errText } from "../../api.js";
import { speakerIndex } from "../../speakers.js";
import { header, strong, inline, sessionLabel } from "../shell.js";
import { statusTarget } from "../../save-status.js";
import { createFieldSaver, createOverlay } from "../field-saver.js";

/** spk palette index → the avatar class suffix next.css `.av.spk-N` paints. */
/** @param {number} spk */
const spkClass = (spk) => `spk-${((spk % 5) + 5) % 5}`;

/** First two letters of a name, upper-cased, for an avatar chip. */
/** @param {string} s */
const initials = (s) => (s || "?").trim().slice(0, 2).toUpperCase() || "?";

/**
 * One row of the People Region model: every value the rows render from.
 * @typedef {{
 *   id: string,
 *   named: boolean,
 *   name: string,
 *   pending: string | null,
 *   identities: string[],
 *   sessions: string[],
 *   live: boolean,
 *   recorded: boolean,
 * }} PeopleRow
 * @typedef {{ here: string, people: PeopleRow[] }} PeopleModel
 */

/**
 * The People view's Region model (templates.js `renderRegionModel`): its
 * serialisation IS the swap gate, so it lists exactly what the rows read.
 * `pendingOf` is the pending-rename lookup, taken as a function so the
 * derivation stays pure.
 * @param {import('../../types.js').Person[]} people
 * @param {string} here The focused session id, "" when none.
 * @param {(id: string) => string | undefined} pendingOf
 * @returns {PeopleModel}
 */
export function peopleModel(people, here, pendingOf) {
  return {
    here,
    people: people.map((p) => ({
      id: p.id,
      named: p.named,
      name: p.name,
      pending: pendingOf(p.id) ?? null,
      identities: p.identities,
      sessions: p.sessions,
      live: p.live,
      recorded: p.recorded,
    })),
  };
}

/** The value the input shows absent a local edit: the chosen name, or "" for
 * an unnamed Person (its default then surfaces as the placeholder). Also the
 * baseline the catch-up sweep compares a pending edit against.
 * @param {{ named: boolean, name: string }} p */
const serverName = (p) => (p.named ? p.name : "");

/** A Person's default label: its name, else its first identity. "" when it has
 * neither, so each caller adds its own last fallback.
 * @param {{ name: string, identities: string[] }} p */
const defaultLabel = (p) => p.name || p.identities[0] || "";

/**
 * What one registry row displays, read from its model row alone.
 * @param {PeopleRow} row
 * @param {string} here The focused session id, "" when none.
 */
export function pregRowView(row, here) {
  const n = row.sessions.length;
  return {
    // `here` guard first: with no focused session, a "" in `sessions` must
    // not highlight the row.
    isHere: here !== "" && row.sessions.includes(here),
    shown: row.pending ?? serverName(row),
    fallback: defaultLabel(row),
    placeholder: defaultLabel(row) || "name…",
    count: `${n} session${n === 1 ? "" : "s"}`,
    src: row.live ? "● live" : row.recorded ? "recorded" : "—",
    srcClass: row.live ? "is-live" : "is-recorded",
  };
}

/**
 * @param {{ afterMutate: () => void }} ctx
 * @returns {{ node: DocumentFragment, update: (j: import('../../types.js').AppState, session: import('../../types.js').Session | null) => void }}
 */
export function build(ctx) {
  const { afterMutate } = ctx;
  const frag = tpl("tpl-next-view-people");

  const headHost = pick(frag, "head");
  const hint = pick(frag, "hint");
  const peopleHost = pick(frag, "people");

  // Pending renames, so a save + re-poll round trip doesn't clear the field the
  // operator just typed. Per-view (unlike session labels, shared by two editors)
  // because this view is the only place a Person is renamed — but the same
  // overlay contract, so the catch-up sweep comes with it (#355).
  const pendingNames = createOverlay({
    idOf: (/** @type {import('../../types.js').Person} */ p) => p.id,
    baselineFor: serverName,
  });

  /** Where a row's status/error text goes. Resolved per write, not captured:
   * renderRegion can rebuild the row between a PUT starting and settling, and a
   * captured cell is detached by then (see `statusTarget`). The ONE writer for a
   * row's status cell — the rename save and the merge/detach failures below both
   * go through it.
   * @param {string} pid */
  const rowStatus = (pid) =>
    statusTarget(() => peopleHost.querySelectorAll(`[data-status-pid="${CSS.escape(pid)}"]`));

  /** Debounced PUT /api/people/{id} {name} — the shared saver (#355), the same
   * overlay + debounce + saving/saved/failed lifecycle the session renames use. */
  const saver = createFieldSaver({
    overlay: pendingNames,
    put: (pid, name) => putJson(`/api/people/${encodeURIComponent(pid)}`, { name }),
    afterSave: afterMutate,
  });
  /** @param {string} pid */
  const persist = (pid) => saver.save(pid, rowStatus(pid));

  /**
   * One registry row, rendered from its model row and `speakerIndex`, which
   * is stable per id. Reading other live state here reopens the drift the
   * Region model closes. Event handlers may write live state.
   * @param {PeopleRow} row
   * @param {PeopleRow[]} all Every model row, for the merge options.
   * @param {string} here The focused session id, "" when none.
   */
  const pregRow = (row, all, here) => {
    const view = pregRowView(row, here);
    const node = tpl("tpl-next-pregrow");
    const rowEl = pick(node, "row");
    if (view.isHere) rowEl.classList.add("is-here");

    const av = pick(node, "av");
    av.classList.add(spkClass(speakerIndex(row.id)));
    /** Avatar initials = current field text, else the Person's default.
     * @param {string} v */
    const avatarText = (v) => initials(v || view.fallback || "?");
    av.textContent = avatarText(view.shown);

    const name = /** @type {HTMLInputElement} */ (pick(node, "name"));
    name.value = view.shown;
    name.placeholder = view.placeholder;
    name.addEventListener("input", () => {
      pendingNames.set(row.id, name.value);
      av.textContent = avatarText(name.value);
      persist(row.id);
    });

    const status = pick(node, "status");
    status.dataset.statusPid = row.id;
    /** Surface a merge/detach failure in the row's status cell, mirroring the
     * rename path — never swallow it. @param {() => Promise<unknown>} req */
    const mutate = async (req) => {
      try {
        await req();
      } catch (e) {
        rowStatus(row.id).set(`failed: ${errText(e)}`);
      } finally {
        afterMutate();
      }
    };

    const src = pick(node, "src");
    src.textContent = view.src;
    src.classList.add(view.srcClass);

    const count = pick(node, "count");
    count.textContent = view.count;
    count.title = row.sessions.join(", ");

    // Device identity token(s) — each detachable when the Person owns more than
    // one (detaching a sole identity would be a no-op, so no ✕ then).
    const ids = pick(node, "ids");
    for (const identity of [...row.identities].sort()) {
      const chip = tpl("tpl-next-idchip");
      const tok = pick(chip, "tok");
      tok.textContent = identity;
      tok.title = identity;
      const detach = pick(chip, "detach");
      if (row.identities.length > 1) {
        detach.addEventListener("click", () =>
          mutate(() => postJson(`/api/people/${encodeURIComponent(row.id)}/detach`, { identity })),
        );
      } else {
        detach.remove();
      }
      ids.appendChild(chip);
    }

    // "Merge into…" — fold THIS Person (absorbed) into the chosen one (survivor).
    const merge = /** @type {HTMLSelectElement} */ (pick(node, "merge"));
    const opt0 = document.createElement("option");
    opt0.value = "";
    opt0.textContent = "Merge into…";
    merge.appendChild(opt0);
    for (const other of all) {
      if (other.id === row.id) continue;
      const o = document.createElement("option");
      o.value = other.id;
      o.textContent = defaultLabel(other) || other.id;
      merge.appendChild(o);
    }
    merge.addEventListener("change", () => {
      const survivor = merge.value;
      if (!survivor) return;
      // This Person is about to stop existing, so drop any pending rename for it
      // — the saver re-reads the overlay at timer-fire, so forgetting IS the
      // cancellation, and a queued PUT would otherwise 404 against a dead id and
      // report the failure into a row that has already been removed.
      pendingNames.forget(row.id);
      mutate(() => postJson("/api/people/merge", { survivor, absorbed: row.id }));
    });

    return node;
  };

  /** The region's build: one row per model row (see `pregRow`).
   * @param {PeopleModel} model @returns {Node} */
  const buildPeopleRows = (model) => {
    if (!model.people.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "No one recorded yet — start a meeting and speakers appear here automatically.";
      return empty;
    }
    const list = document.createDocumentFragment();
    for (const row of model.people) list.appendChild(pregRow(row, model.people, model.here));
    return list;
  };

  /**
   * @param {import('../../types.js').AppState} j
   * @param {import('../../types.js').Session | null} sess
   */
  const update = (j, sess) => {
    header(headHost, {
      eyebrow: "Global · Registry",
      title: "People",
      sub: sess
        ? {
            // Sig mirrors the FULL rendered text (prefix included), like the old
            // textContent key did — a bare-label sig could equal the sess-null
            // fallback string below and wrongly skip the rebuild on sess → null.
            sig: `highlighting people in ${sessionLabel(sess)}`,
            build: () => inline("highlighting people in ", strong(sessionLabel(sess))),
          }
        : "everyone you've recorded, across every session",
    });

    const people = j.people || [];
    // Retire pending renames the server has caught up to (the overlay's own
    // sweep — its doc carries the why).
    pendingNames.sweep(people);
    const liveN = people.filter((p) => p.live).length;
    hint.textContent = `${people.length} ${people.length === 1 ? "person" : "people"}${liveN ? ` · ${liveN} live` : ""}`;

    // Region model: the serialisation IS the sig, so nothing the rows read can
    // go stale unlisted (templates.js renderRegionModel). The sweep above runs
    // before the model reads the overlay, so a caught-up rename never reaches
    // the sig.
    renderRegionModel(
      peopleHost,
      peopleModel(people, sess?.session || "", (id) => pendingNames.get(id)),
      buildPeopleRows,
    );
  };

  return { node: frag, update };
}
