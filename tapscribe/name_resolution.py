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

from collections.abc import Iterable, Mapping
from typing import Any

# Aliased: `voices` is ALSO the name of the operator's Voice->Person mapping,
# which two functions below take as a parameter. Reaching the module by
# attribute is what makes a test monkeypatch propagate; the alias is only
# there so a future edit inside those functions cannot silently bind the
# parameter instead.
import tapscribe.voices as voice_store

from .people import PeopleRegistry
from .roster import slug_owners
from .text import is_voice_key, split_voice_key, voice_key

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
        # `is_voice_key` is defence in depth: these are WAV-filename slugs, which
        # `safe_name` already makes `#`-free. It matters because this runs on
        # every /api/state poll and PERSISTS what it syncs, so one voice key
        # reaching it would mint a blank Person twice a second (ADR-0021).
        if not slug or is_voice_key(slug) or slug in covered or slug in occ:
            continue
        occ[slug] = {"name": slug.replace("_", " "), "source": "recorded", "slug": slug, "wavs": []}
    return occ


def resolve_session_names(
    *,
    roster: dict[str, dict[str, Any]],
    aliases: dict[str, str],
    registry: PeopleRegistry,
    voices: Mapping[str, Any] | None = None,
    voice_runs: Mapping[str, str] | None = None,
    speaker_keys: Iterable[str] = (),
) -> dict[str, str]:
    """`speaker-key (slug) → display name` for one session. See module docstring
    for the precedence. Keys that resolve to nothing are omitted so the frontend
    falls back to the raw slug.

    `slug#<voice>` keys resolve through the session's own `voices` map
    (ADR-0021): `voices` is the operator mapping off `session-meta.json`
    (`identity#<voice> → {person_id, run_id}`) and `voice_runs` is each
    identity's CURRENT `run_id` from the sidecar. They must agree, or the
    mapping predates a re-diarization and would put a named human on whatever
    the new run happens to call `A`. A sidecar that names NO run for an identity
    (deleted, torn, or never diarized) leaves its mappings applied: nothing has
    superseded them, and the transcript keys they name came from the run they
    were stamped against.

    Two ways this pass departs from the omit rule above, both deliberate: a
    voice key ALWAYS resolves — to `Speaker <label>` when nothing maps it, since
    the operator has to recognise the row to map it — and it is keyed off the
    transcript's own speaker list rather than the roster.
    """
    # An ambiguous slug resolves to NO identity, so it can't be named through a
    # Person (#440). Its roster display name still applies — that is what the two
    # taps share, and it is why the slug collides in the first place.
    owners = slug_owners(roster)
    slug_to_identity = {slug: next(iter(ids)) for slug, ids in owners.items() if len(ids) == 1}
    default_by_slug: dict[str, str] = {}
    for entry in roster.values():
        if (slug := entry.get("slug")) and entry.get("name"):
            default_by_slug.setdefault(slug, entry["name"])

    names: dict[str, str] = {}
    # Every slug, not just the unambiguous ones: an ambiguous slug still takes
    # its roster default below, it just resolves to no identity and so to no
    # Person.
    for key in set(aliases) | set(owners):
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

    # Voice keys are in NEITHER set above, so they get their own pass over the
    # transcript's own speaker keys.
    voices = voices or {}
    voice_runs = voice_runs or {}
    roster_names = {identity: entry.get("name") or "" for identity, entry in roster.items()}
    for key in speaker_keys:
        if key in names or not is_voice_key(key):
            continue
        slug, label = split_voice_key(key)
        identity = slug_to_identity.get(slug)
        mapping = voices.get(voice_key(identity, label)) if identity else None
        person = None
        if _mapping_applies(mapping, voice_runs.get(identity or "")):
            person = registry.get(mapping["person_id"])
        # The Person's DISPLAY name — what the operator picked from. Every
        # auto-bound Person carries a blank `name` and the People view shows its
        # roster name instead, so reading the raw field here would leave
        # `Speaker A` on a Voice that view counts against that Person.
        names[key] = _person_display(person, roster_names) or unmapped_voice_name(label)
    return names


def unmapped_voice_name(label: str | None) -> str:
    """What a Voice nobody has mapped reads as. A placeholder the operator has to
    recognise to map — never a human, so `known_names` keeps it out of the
    summarizer's known-people hint."""
    return f"Speaker {label}"


def _person_display(person: dict[str, Any] | None, roster_names: dict[str, str]) -> str:
    """A Person's display name, or "" when this session cannot supply one.

    The first two rungs of `_default_name`, never its third: that one falls back
    to the raw Identity token, and `roster_names` here is THIS session's roster
    where `build_people_view` pools every session's. A Person auto-bound in an
    earlier meeting is therefore routinely absent, and returning the token would
    put `tray-macbook-a1b2…:` on the transcript line — and into the summarizer's
    known-people hint, which only filters the `Speaker <label>` placeholder.
    """
    if not person:
        return ""
    return person["name"] or _roster_name(person["identities"], roster_names)


def known_names(
    *,
    roster: dict[str, dict[str, Any]],
    aliases: dict[str, str],
    registry: PeopleRegistry,
    limit: int = DEFAULT_KNOWN_NAMES_LIMIT,
    voices: Mapping[str, Any] | None = None,
    voice_runs: Mapping[str, str] | None = None,
    speaker_keys: Iterable[str] = (),
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
    wrapper that reads those inputs for a session id.

    A caller that ALSO needs the resolved map itself resolves once and calls
    `known_names_from` — the summarize path does, since resolving twice over one
    session's files can hand the hint and the rendered transcript two different
    snapshots."""
    return known_names_from(
        resolve_session_names(
            roster=roster,
            aliases=aliases,
            registry=registry,
            voices=voices,
            voice_runs=voice_runs,
            speaker_keys=speaker_keys,
        ),
        registry=registry,
        limit=limit,
    )


def known_names_from(
    resolved: Mapping[str, str],
    *,
    registry: PeopleRegistry,
    limit: int = DEFAULT_KNOWN_NAMES_LIMIT,
) -> list[str]:
    """`known_names` over an ALREADY-resolved `speaker key -> display name` map."""
    out: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        name = (name or "").strip()
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            out.append(name)

    # An unmapped Voice resolves to `Speaker A` so the operator can recognise the
    # row and map it. That is a placeholder, not a human: the hint exists to give
    # the model canonical spellings for names it mis-heard, and participants are
    # never trimmed, so letting them in would both invite `Speaker A` into the
    # prose and eat the budget the registry tail is supposed to fill.
    for name in sorted(
        name
        for key, name in resolved.items()
        if not (is_voice_key(key) and name == unmapped_voice_name(split_voice_key(key)[1]))
    ):
        _add(name)
    # Fill the remaining budget with the registry tail; the cap trims only here.
    for person in registry.as_list():
        if limit and len(out) >= limit:
            break
        if person.get("name"):
            _add(person["name"])
    return out


def _roster_name(identities: list[str], roster_names: dict[str, str]) -> str:
    """The first non-empty bridge name across `identities`, else "". The rung
    `_default_name` and `_person_display` share; they differ only in their
    floor, so the rung itself has one spelling."""
    return next((roster_names[i] for i in identities if roster_names.get(i)), "")


def _default_name(identities: list[str], roster_names: dict[str, str]) -> str:
    """The fallback display for an unnamed Person: the first non-empty bridge
    name across its Identities, else the (first) Identity token itself."""
    return _roster_name(identities, roster_names) or (identities[0] if identities else "")


def _mapping_applies(mapped: Any, current_run: str | None) -> bool:
    """ADR-0021's rule, in one place: a Voice→Person mapping counts only while
    its stamp matches the identity's CURRENT diarization run.

    A sidecar that names NO run for the identity (deleted, torn, or never
    diarized) leaves its mappings applied — nothing has superseded them.

    Both readers cross it — `resolve_session_names` off the transcript's
    `slug#<voice>` keys, `_sessions_by_voice_pointer` off the meta's
    `identity#<voice>` ones — because a drift between those two key spaces means
    the People view counts a Person in a meeting whose transcript never names
    them.
    """
    if not isinstance(mapped, dict) or not mapped.get("person_id"):
        return False
    return not current_run or mapped.get("run_id") == current_run


def _sessions_by_voice_pointer(sessions: list[dict[str, Any]]) -> dict[str, set[str]]:
    """`person_id → sessions reached through a Voice mapping`.

    Only mappings the transcript actually applies count: one stamped with a
    superseded `run_id` names nobody in that meeting, so counting it would tell
    the operator a Person appears where their name shows nowhere.
    """
    out: dict[str, set[str]] = {}
    for s in sessions:
        sid = s.get("session", "")
        runs = s.get("voice_runs") or {}
        for key, mapped in ((s.get("session_meta") or {}).get("voices") or {}).items():
            identity, label = split_voice_key(key)
            if label and _mapping_applies(mapped, runs.get(identity)):
                out.setdefault(mapped["person_id"], set()).add(sid)
    return out


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

    voice_sessions = _sessions_by_voice_pointer(sessions)

    rows: list[dict[str, Any]] = []
    for person in registry.as_list():
        idents = person["identities"]
        # A Person mapped from a Voice may own NO Identity (ADR-0021), so the
        # identity join alone would show the operator's brand-new Person against
        # zero sessions. A Voice is recorded audio by construction.
        sess: set[str] = set(voice_sessions.get(person["id"], ()))
        recorded = bool(sess)
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


def attach_people_mutation(
    sessions: list[dict[str, Any]],
    *,
    live_identities: set[str],
) -> tuple[PeopleRegistry, list[dict[str, Any]]]:
    """Load registry, compute occurrences, sync, save if changed.
    Returns (registry, per-session occurrence maps).
    MUST run on the event loop (serialised with /api/people mutations)."""
    registry = PeopleRegistry.load()
    occs = [session_occurrences(s) for s in sessions]
    all_idents: set[str] = set(live_identities)
    for occ in occs:
        all_idents.update(occ)
    if registry.sync(all_idents):
        registry.save()
    return registry, occs


def attach_people_view(
    sessions: list[dict[str, Any]],
    registry: PeopleRegistry,
    occs: list[dict[str, Any]],
    live_identities: set[str],
) -> list[dict[str, Any]]:
    """Resolve session names, build people view rows, strip rosters.
    Pure (no I/O) — safe to run on a worker thread."""
    for s, occ in zip(sessions, occs, strict=True):
        meta = s.get("session_meta") or {}
        s["names"] = resolve_session_names(
            roster=occ,
            aliases=meta.get("aliases") or {},
            registry=registry,
            voices=meta.get("voices") or {},
            voice_runs=s.get("voice_runs") or {},
            # The TRANSCRIPT's keys, not `s["speakers"]` — that is the WAV
            # filename slugs, which `safe_name` makes `#`-free, so no voice key
            # could ever appear there.
            speaker_keys=(s.get("session_transcript") or {}).get("speakers") or [],
        )
    people = build_people_view(sessions=sessions, registry=registry, live_identities=live_identities)
    for s in sessions:
        # Both are join inputs consumed above, not payload: shipping them would
        # put the whole roster and every run stamp in each poll body. The runs
        # leave one projection behind — the stamp the Transcript stage keys its
        # lazy Voices body on, which is the only way it learns a diarize landed.
        s["voices_sig"] = voice_store.voices_sig(s.get("voice_runs") or {})
        s.pop("roster", None)
        s.pop("voice_runs", None)
    return people


def attach_people(
    sessions: list[dict[str, Any]],
    *,
    live_identities: set[str],
) -> list[dict[str, Any]]:
    registry, occs = attach_people_mutation(sessions, live_identities=live_identities)
    return attach_people_view(sessions, registry, occs, live_identities)
