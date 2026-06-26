"""Name resolution — the server-side join that turns Identities + the People
Registry + per-session overrides into (a) a per-session speaker-key → display
name map and (b) the cross-session People view rows (ADR-0009).

Resolution precedence: per-session Override (session_meta.aliases) > Person name
> bridge/roster default > slug fallback. Rosterless old sessions resolve by slug
(via their retained aliases) — no regression.
"""

from __future__ import annotations

from tapscribe.name_resolution import build_people_view, resolve_session_names, session_occurrences
from tapscribe.people import PeopleRegistry


def _reg(people: list[dict]) -> PeopleRegistry:
    return PeopleRegistry(people)


# A roster as read_roster returns it: {identity: {name, source, slug, wavs}}.
def _entry(name: str, *, slug: str = "", source: str = "recorded", wavs=None) -> dict:
    return {"name": name, "source": source, "slug": slug, "wavs": wavs or []}


# ---- resolve_session_names -------------------------------------------------


def test_person_name_propagates_to_the_transcript_slug() -> None:
    roster = {"alice-9f2c": _entry("Alice", slug="Alice")}
    reg = _reg([{"id": "p1", "name": "Alice Havso", "identities": ["alice-9f2c"]}])
    names = resolve_session_names(roster=roster, aliases={}, registry=reg)
    # The transcript keys segments by the slug; resolution maps slug → Person name.
    assert names["Alice"] == "Alice Havso"


def test_override_beats_person_name() -> None:
    roster = {"alice-9f2c": _entry("Alice", slug="Alice")}
    reg = _reg([{"id": "p1", "name": "Alice Havso", "identities": ["alice-9f2c"]}])
    names = resolve_session_names(roster=roster, aliases={"Alice": "Guest A"}, registry=reg)
    assert names["Alice"] == "Guest A"


def test_falls_back_to_roster_default_when_person_unnamed() -> None:
    roster = {"bob-1": _entry("Bob from Bridge", slug="Bob")}
    reg = _reg([{"id": "p1", "name": "", "identities": ["bob-1"]}])  # auto-bound, unnamed
    names = resolve_session_names(roster=roster, aliases={}, registry=reg)
    assert names["Bob"] == "Bob from Bridge"


def test_old_session_with_no_roster_still_honours_its_alias() -> None:
    # No roster (pre-feature session); the retained slug-keyed alias still wins.
    names = resolve_session_names(roster={}, aliases={"Carol_Old": "Carol"}, registry=_reg([]))
    assert names["Carol_Old"] == "Carol"


# ---- build_people_view -----------------------------------------------------


def test_view_aggregates_sessions_and_sources() -> None:
    sessions = [
        {"session": "s1", "roster": {"alice": _entry("Alice", slug="Alice")}},
        {
            "session": "s2",
            "roster": {
                "alice": _entry("Alice", slug="Alice"),
                "bob": _entry("Bob", slug="Bob", source="live", wavs=[]),
            },
        },
    ]
    reg = _reg([])
    reg.sync(["alice", "bob"])
    rows = build_people_view(sessions=sessions, registry=reg, live_identities=set())
    by_ident = {tuple(r["identities"]): r for r in rows}
    alice = by_ident[("alice",)]
    assert sorted(alice["sessions"]) == ["s1", "s2"]
    assert alice["session_count"] == 2
    assert alice["recorded"] is True
    # Unnamed Person uses its roster default name for display.
    assert alice["name"] == "Alice"
    assert alice["named"] is False
    bob = by_ident[("bob",)]
    assert bob["recorded"] is False  # live-only in s2


def test_view_marks_currently_live_identities() -> None:
    sessions = [{"session": "s1", "roster": {"alice": _entry("Alice", slug="Alice")}}]
    reg = _reg([])
    reg.sync(["alice"])
    rows = build_people_view(sessions=sessions, registry=reg, live_identities={"alice"})
    assert rows[0]["live"] is True


def test_view_uses_chosen_name_and_merges_identities_into_one_row() -> None:
    sessions = [
        {"session": "s1", "roster": {"alice-laptop": _entry("Alice", slug="Alice")}},
        {"session": "s2", "roster": {"alice-office": _entry("Alice O", slug="AliceO")}},
    ]
    reg = _reg([{"id": "p1", "name": "Alice Havso", "identities": ["alice-laptop", "alice-office"]}])
    rows = build_people_view(sessions=sessions, registry=reg, live_identities=set())
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "Alice Havso"
    assert row["named"] is True
    assert sorted(row["identities"]) == ["alice-laptop", "alice-office"]
    assert sorted(row["sessions"]) == ["s1", "s2"]


def test_old_rosterless_session_backfills_people_from_wav_slugs() -> None:
    # No roster (pre-feature recording), but the WAV slugs are known — they
    # must still surface as People, keyed on the slug (best-effort backfill).
    session = {"session": "old", "roster": {}, "speakers": ["Old_Speaker", "Them"]}
    occ = session_occurrences(session)
    assert set(occ) == {"Old_Speaker", "Them"}
    assert occ["Old_Speaker"]["name"] == "Old Speaker"  # underscores humanised
    reg = _reg([])
    reg.sync(occ)
    rows = build_people_view(sessions=[session], registry=reg, live_identities=set())
    assert {r["name"] for r in rows} == {"Old Speaker", "Them"}


def test_roster_entry_is_not_double_counted_by_its_own_wav_slug() -> None:
    # A new recording carries BOTH a roster entry (slug="Alice") and a WAV whose
    # slug is "Alice" — it must be ONE occurrence, not two.
    session = {
        "session": "s1",
        "roster": {"alice-9f": _entry("Alice", slug="Alice")},
        "speakers": ["Alice"],
    }
    occ = session_occurrences(session)
    assert set(occ) == {"alice-9f"}


def test_view_sorted_by_session_count_desc_then_name() -> None:
    sessions = [
        {"session": "s1", "roster": {"a": _entry("A", slug="A")}},
        {"session": "s2", "roster": {"a": _entry("A", slug="A"), "b": _entry("Zoe", slug="Zoe")}},
    ]
    reg = _reg([])
    reg.sync(["a", "b"])
    rows = build_people_view(sessions=sessions, registry=reg, live_identities=set())
    assert rows[0]["identities"] == ["a"]  # 2 sessions, first
