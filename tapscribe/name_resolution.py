"""Name resolution — the server-side join that turns Identities + the People
Registry + per-session overrides into the names the dashboard renders
(ADR-0009; CONTEXT.md: Person · Identity · Roster · People Registry).

Two products, both computed at `/api/state` build time so the frontend keeps
consuming a plain name-map (the Interaction-hold render path is untouched):

  * `resolve_session_names` — per session, a `speaker-key (slug) → display name`
    map. The merged transcript keys segments by the slug; this resolves each
    slug through: per-session **Override** (`session_meta.aliases`) > **Person**
    name (slug → Identity via the roster → registry) > bridge/roster **default**
    name > (unset → frontend renders the raw slug). Rosterless old sessions have
    no slug→Identity link, so they resolve purely via their retained aliases —
    no regression.

  * `build_people_view` — the cross-session People view: one row per Person with
    its chosen-or-default name, member Identities, the sessions it appears in,
    recorded/live source, and whether it's currently live.

`attach_people` is the thin I/O orchestrator the route handler calls: it syncs
the registry against every roster (auto-bind), persists only if that added a new
Identity, resolves each session's names in place, and returns the view rows.
"""

from __future__ import annotations

from typing import Any

from .people import PeopleRegistry

# Cap on the known-people hint injected into a summarize (see `known_names`). A
# large registry would bloat the prompt and dilute the signal; this bounds the
# registry TAIL only — this session's own participants are always included (a
# meeting with more than this many named speakers is not expected, and its
# transcript dwarfs the hint anyway).
DEFAULT_KNOWN_NAMES_LIMIT = 60


def session_occurrences(session: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every Identity occurrence in a session, as `{identity: entry}`.

    Primary source is the session's Roster (full bridge identities). On top of
    that, a best-effort backfill (ADR-0009 F1): any recorded speaker slug
    (`session['speakers']`) NOT already covered by a roster entry becomes a
    slug-keyed occurrence — so old, rosterless recordings still surface in the
    registry. A new recording carries both a roster entry (slug="Alice") and a
    WAV with that slug, so it's covered and never double-counted; only a WAV
    with no matching roster slug backfills, and it joins its full-identity
    counterpart via a manual merge."""
    occ = dict(session.get("roster") or {})
    covered = {e.get("slug") for e in occ.values() if e.get("slug")}
    for slug in session.get("speakers") or []:
        if slug and slug not in covered and slug not in occ:
            occ[slug] = {"name": slug.replace("_", " "), "source": "recorded", "slug": slug, "wavs": []}
    return occ


def resolve_session_names(
    *,
    roster: dict[str, dict[str, Any]],
    aliases: dict[str, str],
    registry: PeopleRegistry,
) -> dict[str, str]:
    """`speaker-key (slug) → display name` for one session. See module docstring
    for the precedence. Keys that resolve to nothing are omitted so the frontend
    falls back to the raw slug."""
    slug_to_identity: dict[str, str] = {}
    default_by_slug: dict[str, str] = {}
    for identity, entry in roster.items():
        slug = entry.get("slug")
        if slug:
            slug_to_identity.setdefault(slug, identity)
            if entry.get("name"):
                default_by_slug.setdefault(slug, entry["name"])

    names: dict[str, str] = {}
    for key in set(aliases) | set(slug_to_identity):
        override = aliases.get(key)
        if override:
            names[key] = override
            continue
        identity = slug_to_identity.get(key)
        person_name = registry.name_for_identity(identity) if identity else None
        if person_name:
            names[key] = person_name
            continue
        default = default_by_slug.get(key)
        if default:
            names[key] = default
    return names


def known_names(
    *,
    roster: dict[str, dict[str, Any]],
    aliases: dict[str, str],
    registry: PeopleRegistry,
    limit: int = DEFAULT_KNOWN_NAMES_LIMIT,
) -> list[str]:
    """The ordered, deduped display names to hint a summarizer against
    mis-transcribed names (the `tapscribe.summarizers.build_names_hint` input).

    This session's resolved participant names come FIRST — they're the highest
    signal, mapping the transcript's lossy speaker slugs to canonical names — and
    are ALWAYS included; then the `limit` budget is filled with OTHER named
    Persons the registry has learned across previous meetings (useful for people
    *mentioned* but not present), so the cap only ever trims that tail. Case-
    insensitive dedup; blank names dropped. Participants are sorted so the hint is
    reproducible run-to-run (`resolve_session_names` returns a hash-ordered set,
    which would otherwise make the trimmed tail and dedup winner non-deterministic).

    Pure: the caller supplies the session's roster + aliases + the loaded
    registry — the same inputs `resolve_session_names` takes — so this is unit-
    testable without disk. `known_names_for_session` in `sessions` is the I/O
    wrapper that reads those inputs for a session id."""
    out: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        name = (name or "").strip()
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            out.append(name)

    # Participants first, always included, deterministically ordered.
    for name in sorted(resolve_session_names(roster=roster, aliases=aliases, registry=registry).values()):
        _add(name)
    # Fill the remaining budget with the registry tail; the cap trims only here.
    for person in registry.as_list():
        if limit and len(out) >= limit:
            break
        if person.get("name"):
            _add(person["name"])
    return out


def _default_name(identities: list[str], roster_names: dict[str, str]) -> str:
    """The fallback display for an unnamed Person: the first non-empty bridge
    name across its Identities, else the (first) Identity token itself."""
    for identity in identities:
        if roster_names.get(identity):
            return roster_names[identity]
    return identities[0] if identities else ""


def build_people_view(
    *,
    sessions: list[dict[str, Any]],
    registry: PeopleRegistry,
    live_identities: set[str],
) -> list[dict[str, Any]]:
    """One row per Person, aggregated across every session's roster. `sessions`
    entries need only `{"session": id, "roster": {...}}`. `registry` must
    already be synced against these rosters (so every Identity has a Person)."""
    # Identity → sessions it appears in, whether any occurrence was recorded,
    # and a default bridge name — one pass over all rosters.
    ident_sessions: dict[str, set[str]] = {}
    ident_recorded: dict[str, bool] = {}
    roster_names: dict[str, str] = {}
    for s in sessions:
        sid = s.get("session", "")
        for identity, entry in session_occurrences(s).items():
            ident_sessions.setdefault(identity, set()).add(sid)
            if entry.get("source") == "recorded":
                ident_recorded[identity] = True
            if entry.get("name") and identity not in roster_names:
                roster_names[identity] = entry["name"]

    rows: list[dict[str, Any]] = []
    for person in registry.as_list():
        idents = person["identities"]
        sess: set[str] = set()
        recorded = False
        for identity in idents:
            sess |= ident_sessions.get(identity, set())
            recorded = recorded or ident_recorded.get(identity, False)
        rows.append(
            {
                "id": person["id"],
                "name": person["name"] or _default_name(idents, roster_names),
                "named": bool(person["name"]),
                "identities": list(idents),
                "sessions": sorted(sess),
                "session_count": len(sess),
                "recorded": recorded,
                "live": any(i in live_identities for i in idents),
            }
        )
    rows.sort(key=lambda r: (-r["session_count"], r["name"].lower()))
    return rows


def attach_people(
    sessions: list[dict[str, Any]],
    *,
    live_identities: set[str],
) -> list[dict[str, Any]]:
    """Sync the registry against every roster + live Identity (auto-bind),
    persist only on a real change, resolve each session's `names` in place, and
    return the People view rows. Synchronous (no `await`) so the load → sync →
    save runs atomically under the event loop."""
    registry = PeopleRegistry.load()
    occs = [session_occurrences(s) for s in sessions]
    all_idents: set[str] = set(live_identities)
    for occ in occs:
        all_idents.update(occ)
    if registry.sync(all_idents):
        registry.save()
    for s, occ in zip(sessions, occs, strict=True):
        s["names"] = resolve_session_names(
            roster=occ,
            aliases=(s.get("session_meta") or {}).get("aliases") or {},
            registry=registry,
        )
    people = build_people_view(sessions=sessions, registry=registry, live_identities=live_identities)
    # The roster is server-side join input only — the dashboard renders from the
    # resolved `names` map and the `people` view, never the raw roster. Drop it
    # so /api/state doesn't broadcast full bridge identities (a disclosure) and
    # re-ship them O(rosters) every ~0.5s poll (the bloat files[]/the merged
    # transcript were already slimmed out to avoid).
    for s in sessions:
        s.pop("roster", None)
    return people
