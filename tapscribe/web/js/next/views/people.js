// @ts-check
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
// renderRegion, so an in-progress name edit / open merge picker / mid-copy
// selection holds the swap (Interaction hold; templates.js).

import { tpl, pick, renderRegion } from "../../templates.js";
import { putJson, postJson } from "../../api.js";
import { speakerIndex } from "../../speakers.js";
import { header, strong, inline } from "../shell.js";

/** spk palette index → the avatar class suffix next.css `.av.spk-N` paints. */
/** @param {number} spk */
const spkClass = (spk) => `spk-${((spk % 5) + 5) % 5}`;

/** First two letters of a name, upper-cased, for an avatar chip. */
/** @param {string} s */
const initials = (s) => (s || "?").trim().slice(0, 2).toUpperCase() || "?";

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

  // Optimistic local name overlay, per person id, so a rename's save + re-poll
  // round trip doesn't clear the field the operator just typed.
  /** @type {Map<string, string>} */
  const localNames = new Map();
  /** @type {Map<string, ReturnType<typeof setTimeout>>} */
  const saveTimers = new Map();

  /** The name to SHOW in the input: local overlay > chosen name > "" (so an
   * unnamed Person shows its default only as the placeholder, inviting a name). */
  /** The value the input shows absent a local edit: the chosen name, or "" for
   * an unnamed Person (its default then surfaces as the placeholder). Also the
   * baseline update() compares the overlay against — one source for the rule. */
  /** @param {import('../../types.js').Person} p */
  const serverName = (p) => (p.named ? p.name : "");
  /** @param {import('../../types.js').Person} p */
  const inputValue = (p) => {
    const local = localNames.get(p.id);
    return local !== undefined ? local : serverName(p);
  };

  /** Debounced PUT /api/people/{id} {name}. */
  /** @param {string} pid */
  const persist = (pid) => {
    clearTimeout(saveTimers.get(pid));
    saveTimers.set(pid, setTimeout(async () => {
      saveTimers.delete(pid);
      const name = localNames.get(pid);
      if (name === undefined) return;
      const statusEls = peopleHost.querySelectorAll(`[data-status-pid="${CSS.escape(pid)}"]`);
      for (const el of statusEls) if (el instanceof HTMLElement) el.textContent = "saving…";
      try {
        await putJson(`/api/people/${encodeURIComponent(pid)}`, { name });
        for (const el of statusEls) {
          if (el instanceof HTMLElement && el.textContent === "saving…") {
            el.textContent = "saved";
            setTimeout(() => { if (el.textContent === "saved") el.textContent = ""; }, 1400);
          }
        }
      } catch (e) {
        for (const el of statusEls) {
          if (el instanceof HTMLElement) el.textContent = `failed: ${String(e).replace(/^Error:\s*/, "")}`;
        }
      } finally {
        afterMutate();
      }
    }, 600));
  };

  /**
   * @param {import('../../types.js').Person} p
   * @param {import('../../types.js').Person[]} all
   * @param {import('../../types.js').Session | null} sess
   */
  const pregRow = (p, all, sess) => {
    const node = tpl("tpl-next-pregrow");
    const row = pick(node, "row");
    if (sess && p.sessions.includes(sess.session)) row.classList.add("is-here");

    const av = pick(node, "av");
    av.classList.add(spkClass(speakerIndex(p.id)));
    /** Avatar initials = current field text, else the Person's default.
     * @param {string} v */
    const avatarText = (v) => initials(v || p.name || p.identities[0] || "?");
    av.textContent = avatarText(inputValue(p));

    const name = /** @type {HTMLInputElement} */ (pick(node, "name"));
    name.value = inputValue(p);
    name.placeholder = p.name || p.identities[0] || "name…";
    name.addEventListener("input", () => {
      localNames.set(p.id, name.value);
      av.textContent = avatarText(name.value);
      persist(p.id);
    });

    const status = pick(node, "status");
    status.dataset.statusPid = p.id;
    /** Surface a merge/detach failure in the row's status cell, mirroring the
     * rename path — never swallow it. @param {() => Promise<unknown>} req */
    const mutate = async (req) => {
      try {
        await req();
      } catch (e) {
        status.textContent = `failed: ${String(e).replace(/^Error:\s*/, "")}`;
      } finally {
        afterMutate();
      }
    };

    const src = pick(node, "src");
    src.textContent = p.live ? "● live" : p.recorded ? "recorded" : "—";
    src.classList.add(p.live ? "is-live" : "is-recorded");

    const count = pick(node, "count");
    const n = p.session_count;
    count.textContent = `${n} session${n === 1 ? "" : "s"}`;
    count.title = p.sessions.join(", ");

    // Device identity token(s) — each detachable when the Person owns more than
    // one (detaching a sole identity would be a no-op, so no ✕ then).
    const ids = pick(node, "ids");
    for (const identity of [...p.identities].sort()) {
      const chip = tpl("tpl-next-idchip");
      const tok = pick(chip, "tok");
      tok.textContent = identity;
      tok.title = identity;
      const detach = pick(chip, "detach");
      if (p.identities.length > 1) {
        detach.addEventListener("click", () =>
          mutate(() => postJson(`/api/people/${encodeURIComponent(p.id)}/detach`, { identity })),
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
      if (other.id === p.id) continue;
      const o = document.createElement("option");
      o.value = other.id;
      o.textContent = other.name || other.identities[0] || other.id;
      merge.appendChild(o);
    }
    merge.addEventListener("change", () => {
      const survivor = merge.value;
      if (!survivor) return;
      mutate(() => postJson("/api/people/merge", { survivor, absorbed: p.id }));
    });

    return node;
  };

  /** Signature so the list only rebuilds on a real change — and skips while a
   * name input / merge picker is focused or a selection is mid-copy. Must list
   * EVERY value the build closure reads, or the region goes stale (CLAUDE.md
   * sig-drift). That's: each person's SERVER name (other rows' merge-picker
   * options mirror it — a rename must reflow them), the local edit overlay
   * (this row's input value + avatar initials), the identities + the sessions
   * SET (the `is-here` highlight and the count tooltip read the array, not just
   * its length), and live/recorded. The focused row's own input is additionally
   * held by renderRegion's focus guard. */
  /** @param {import('../../types.js').Person[]} people @param {string} here */
  const sig = (people, here) =>
    here + "§" + people
      .map((p) =>
        `${p.id}:${p.named ? 1 : 0}:${p.name}:${localNames.get(p.id) ?? ""}:`
        + `${p.identities.join(",")}:${p.sessions.join(",")}:${p.live ? 1 : 0}:${p.recorded ? 1 : 0}`)
      .join("|");

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
            sig: `highlighting people in ${sess.session_meta?.label || sess.session}`,
            build: () => inline("highlighting people in ", strong(sess.session_meta?.label || sess.session)),
          }
        : "everyone you've recorded, across every session",
    });

    const people = j.people || [];
    // Drop a local name overlay once the server has caught up to it — otherwise
    // a stale overlay would mask a later external change to that name.
    for (const p of people) {
      if (localNames.get(p.id) === serverName(p)) localNames.delete(p.id);
    }
    const liveN = people.filter((p) => p.live).length;
    hint.textContent = `${people.length} ${people.length === 1 ? "person" : "people"}${liveN ? ` · ${liveN} live` : ""}`;

    renderRegion(peopleHost, () => {
      if (!people.length) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "No one recorded yet — start a meeting and speakers appear here automatically.";
        return empty;
      }
      const list = document.createDocumentFragment();
      for (const p of people) list.appendChild(pregRow(p, people, sess));
      return list;
    }, { sig: sig(people, sess?.session || "") });
  };

  return { node: frag, update };
}
