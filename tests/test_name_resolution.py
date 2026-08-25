"""Name resolution — the server-side join that turns Identities + the People
Registry + per-session overrides into (a) a per-session speaker-key → display
name map and (b) the cross-session People view rows (ADR-0009).

Resolution precedence: per-session Override (session_meta.aliases) > Person name
> bridge/roster default > slug fallback. Rosterless old sessions resolve by slug
(via their retained aliases) — no regression.
"""

from __future__ import annotations

from tapscribe.name_resolution import (
    attach_people_view,
    build_people_view,
    known_names,
    resolve_session_names,
    session_occurrences,
)
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


# ---- known_names (the summarizer's known-people hint input) -----------------


def test_known_names_lists_participants_first_then_registry() -> None:
    # Alice is in THIS session (resolved to her canonical registry name); Bob is
    # a name the registry learned from a different meeting and isn't present here.
    roster = {"alice-1": _entry("Alice", slug="Alice")}
    reg = _reg(
        [
            {"id": "p1", "name": "Alice Havso", "identities": ["alice-1"]},
            {"id": "p2", "name": "Bob Distant", "identities": ["bob-x"]},
        ]
    )
    assert known_names(roster=roster, aliases={}, registry=reg) == ["Alice Havso", "Bob Distant"]


def test_known_names_resolves_the_lossy_slug_to_the_canonical_name() -> None:
    # The transcript only carries the slug 'Alice'; the hint must surface the
    # canonical 'Alice Havso' so the model can correct it.
    roster = {"alice-1": _entry("Alice", slug="Alice")}
    reg = _reg([{"id": "p1", "name": "Alice Havso", "identities": ["alice-1"]}])
    assert known_names(roster=roster, aliases={}, registry=reg) == ["Alice Havso"]


def test_known_names_dedupes_case_insensitively() -> None:
    roster = {"alice-1": _entry("Alice", slug="Alice")}
    reg = _reg(
        [
            {"id": "p1", "name": "Alice Havso", "identities": ["alice-1"]},
            {"id": "p2", "name": "alice havso", "identities": ["dup-x"]},
        ]
    )
    # The participant spelling leads and the case-variant duplicate is dropped.
    assert known_names(roster=roster, aliases={}, registry=reg) == ["Alice Havso"]


def test_known_names_drops_unnamed_registry_people() -> None:
    # An auto-bound, still-unnamed Person contributes no hint (a blank name is
    # useless as a spelling correction).
    reg = _reg([{"id": "p1", "name": "", "identities": ["ghost-1"]}])
    assert known_names(roster={}, aliases={}, registry=reg) == []


def test_known_names_honours_per_session_alias_override() -> None:
    roster = {"alice-1": _entry("Alice", slug="Alice")}
    reg = _reg([{"id": "p1", "name": "Alice Havso", "identities": ["alice-1"]}])
    names = known_names(roster=roster, aliases={"Alice": "Guest A"}, registry=reg)
    assert names[0] == "Guest A"  # the override wins, same precedence as the dashboard


def test_known_names_caps_registry_tail_keeping_participants() -> None:
    roster = {"alice-1": _entry("Alice", slug="Alice")}
    people = [{"id": "p1", "name": "Alice Havso", "identities": ["alice-1"]}]
    people += [{"id": f"p{i}", "name": f"Person {i}", "identities": [f"id-{i}"]} for i in range(2, 20)]
    names = known_names(roster=roster, aliases={}, registry=_reg(people), limit=3)
    assert len(names) == 3
    assert names[0] == "Alice Havso"  # the participant leads the list and is never trimmed


def test_known_names_never_trims_participants_even_over_cap() -> None:
    # Four participants in THIS session with a cap of 2: every participant must
    # survive (the cap bounds only the registry tail), and no registry-only name
    # gets in ahead of a participant.
    roster = {f"id-{c}": _entry(c, slug=c) for c in ("Ann", "Bob", "Cy", "Dee")}
    people = [
        {"id": f"p{c}", "name": f"{c} Full", "identities": [f"id-{c}"]} for c in ("Ann", "Bob", "Cy", "Dee")
    ]
    people.append({"id": "pZ", "name": "Zoe Tail", "identities": ["zoe-x"]})  # registry-only
    names = known_names(roster=roster, aliases={}, registry=_reg(people), limit=2)
    assert set(names) == {"Ann Full", "Bob Full", "Cy Full", "Dee Full"}
    assert "Zoe Tail" not in names  # tail trimmed; participants (over the cap) are not


def test_known_names_sorts_participants_for_reproducible_output() -> None:
    # resolve_session_names returns a hash-ordered set; known_names sorts the
    # participants so the hint (and its trimmed tail) is stable run-to-run.
    roster = {"id-z": _entry("Zoe", slug="Zoe"), "id-a": _entry("Ann", slug="Ann")}
    reg = _reg(
        [
            {"id": "p1", "name": "Zoe Z", "identities": ["id-z"]},
            {"id": "p2", "name": "Ann A", "identities": ["id-a"]},
        ]
    )
    assert known_names(roster=roster, aliases={}, registry=reg) == ["Ann A", "Zoe Z"]


# ---- Voice keys must never reach the registry (ADR-0021) -------------------


def test_backfill_skips_voice_keys() -> None:
    """`attach_people_mutation` runs on every /api/state poll and PERSISTS an
    auto-bound Person per unknown occurrence. A `slug#<voice>` key reaching the
    backfill would mint a blank Person twice a second, forever."""
    session = {
        "roster": {"tray-sysaudio-001": _entry("System audio", slug="sysaudio")},
        "speakers": ["sysaudio#A", "sysaudio#B"],
    }

    occ = session_occurrences(session)

    assert set(occ) == {"tray-sysaudio-001"}


def test_backfill_still_covers_a_rosterless_plain_slug() -> None:
    """The guard must not disable ADR-0009's F1 backfill for old recordings."""
    occ = session_occurrences({"roster": {}, "speakers": ["Alice_Andersen"]})

    assert set(occ) == {"Alice_Andersen"}


# ---- Voice -> Person resolution (ADR-0021) ---------------------------------

_SYS = "tray-sysaudio-001"
_ROSTER = {_SYS: _entry("System audio", slug="sysaudio")}
_KEYS = ["sysaudio#A", "sysaudio#B"]


def _resolve(*, aliases=None, voices=None, voice_runs=None, people=None):
    return resolve_session_names(
        roster=_ROSTER,
        aliases=aliases or {},
        registry=_reg(people or [{"id": "p1", "name": "Alice Andersen", "identities": []}]),
        voices=voices or {},
        voice_runs=voice_runs or {},
        speaker_keys=_KEYS,
    )


def test_mapped_voice_resolves_to_its_person() -> None:
    names = _resolve(voices={f"{_SYS}#A": {"person_id": "p1", "run_id": "r1"}})

    assert names["sysaudio#A"] == "Alice Andersen"


def test_a_voice_mapped_to_an_auto_bound_person_takes_that_person_s_roster_name() -> None:
    """Every auto-bound Person carries a BLANK `name`, and the People view — which
    is what fills the operator's picker — shows its roster name instead. Reading
    the raw field would put `Speaker A` on a Voice that view counts as named."""
    names = _resolve(
        voices={f"{_SYS}#A": {"person_id": "p9", "run_id": "r1"}},
        people=[{"id": "p9", "name": "", "identities": [_SYS]}],
    )

    assert names["sysaudio#A"] == "System audio"


def test_a_mapped_person_this_session_cannot_name_still_reads_as_the_placeholder() -> None:
    """The floor is `Speaker A`, never the raw Identity token: a Person auto-bound
    in an EARLIER meeting has no entry in this session's roster, and a
    `tray-macbook-a1b2…:` transcript line would also reach the summarizer's hint,
    which only filters the placeholder."""
    names = _resolve(
        voices={f"{_SYS}#A": {"person_id": "p9", "run_id": "r1"}},
        people=[{"id": "p9", "name": "", "identities": ["tray-other-box-999"]}],
    )

    assert names["sysaudio#A"] == "Speaker A"


def test_unmapped_voice_renders_a_readable_speaker_label() -> None:
    """Not the raw `sysaudio#A` — the operator has to recognise the row to map it."""
    assert _resolve()["sysaudio#B"] == "Speaker B"


def test_an_unmapped_voice_is_not_a_known_name_for_the_summarizer() -> None:
    """`Speaker A` is a placeholder for the operator to click, not a spelling the
    model should correct a transcribed name toward."""
    hint = known_names(
        roster=_ROSTER,
        aliases={},
        registry=_reg([{"id": "p1", "name": "Alice Andersen", "identities": []}]),
        speaker_keys=_KEYS,
    )

    assert not [n for n in hint if n.startswith("Speaker ")]


def test_override_on_a_voice_key_beats_the_person() -> None:
    names = _resolve(
        aliases={"sysaudio#A": "Chair"},
        voices={f"{_SYS}#A": {"person_id": "p1", "run_id": "r1"}},
    )

    assert names["sysaudio#A"] == "Chair"


def test_override_on_the_bare_slug_does_not_fan_out_to_its_voices() -> None:
    names = _resolve(aliases={"sysaudio": "The room"})

    assert names["sysaudio"] == "The room"
    assert names["sysaudio#A"] == "Speaker A"


def test_a_mapping_stamped_with_a_superseded_run_is_not_applied() -> None:
    """Carrying it over would put a named human on whatever the NEW run calls A."""
    names = _resolve(
        voices={f"{_SYS}#A": {"person_id": "p1", "run_id": "old"}},
        voice_runs={_SYS: "new"},
    )

    assert names["sysaudio#A"] == "Speaker A"


def test_a_mapping_matching_the_current_run_is_applied() -> None:
    names = _resolve(
        voices={f"{_SYS}#A": {"person_id": "p1", "run_id": "r1"}},
        voice_runs={_SYS: "r1"},
    )

    assert names["sysaudio#A"] == "Alice Andersen"


def test_attach_people_view_feeds_voice_keys_from_the_TRANSCRIPT_not_the_wav_slugs() -> None:
    """`session["speakers"]` is WAV-filename slugs, which `safe_name` makes
    `#`-free — a voice key can never appear there. Reading that list left the
    whole voice branch dead on the /api/state path with every unit test still
    green, because they all passed `speaker_keys` by hand."""
    reg = _reg([{"id": "p1", "name": "Alice Andersen", "identities": []}])
    session = {
        "roster": _ROSTER,
        "speakers": ["sysaudio"],  # WAV slugs: no `#`, ever
        "session_transcript": {"speakers": ["sysaudio#A"]},
        "session_meta": {"voices": {f"{_SYS}#A": {"person_id": "p1", "run_id": "r1"}}},
        "voice_runs": {_SYS: "r1"},
    }

    attach_people_view([session], reg, [session_occurrences(session)], live_identities=set())

    assert session["names"]["sysaudio#A"] == "Alice Andersen"


def test_attach_people_view_survives_a_session_with_no_transcript() -> None:
    """The synthetic current-session entry carries `session_transcript: None`."""
    session = {"roster": _ROSTER, "speakers": [], "session_transcript": None, "session_meta": {}}

    attach_people_view([session], _reg([]), [session_occurrences(session)], live_identities=set())

    # Resolves normally (the roster default), and no voice keys to resolve.
    assert session["names"] == {"sysaudio": "System audio"}


def test_an_ambiguous_slug_is_not_named_through_either_person() -> None:
    """Two taps under one display name: the roster default still applies, but
    neither identity's Person may claim the slug (#440)."""
    roster = {
        "tray-a": _entry("System audio", slug="sysaudio"),
        "tray-b": _entry("System audio", slug="sysaudio"),
    }
    reg = _reg([{"id": "p1", "name": "Machine A", "identities": ["tray-a"]}])

    names = resolve_session_names(roster=roster, aliases={}, registry=reg)

    assert names["sysaudio"] == "System audio", "the shared roster name, not a Person"


# ---- build_people_view · Voice-mapped Persons (ADR-0021) -------------------


def _mapped_session(sid: str, *, key: str, person_id: str, run_id: str, current: str) -> dict:
    """A diarized session with one Voice→Person mapping on its meta."""
    return {
        "session": sid,
        "roster": {"sysaudio": _entry("Them", slug="Them")},
        "session_meta": {"voices": {key: {"person_id": person_id, "run_id": run_id}}},
        "voice_runs": {"sysaudio": current},
    }


def test_a_person_reached_only_by_a_voice_pointer_counts_its_session() -> None:
    """Mapping a Voice by typing a name creates a Person with NO Identity, so
    the identity join finds nothing for it — and the People stage would show the
    operator's brand-new Person against zero sessions."""
    reg = _reg([{"id": "p1", "name": "Dana", "identities": []}])

    rows = build_people_view(
        sessions=[_mapped_session("s1", key="sysaudio#A", person_id="p1", run_id="r1", current="r1")],
        registry=reg,
        live_identities=set(),
    )

    dana = next(r for r in rows if r["id"] == "p1")
    assert dana["sessions"] == ["s1"]
    assert dana["recorded"] is True


def test_a_mapping_from_a_superseded_run_counts_no_session() -> None:
    """It is not applied to the transcript either — counting it would tell the
    operator a Person appears in a meeting where their name shows nowhere."""
    reg = _reg([{"id": "p1", "name": "Dana", "identities": []}])

    rows = build_people_view(
        sessions=[_mapped_session("s1", key="sysaudio#A", person_id="p1", run_id="old", current="r2")],
        registry=reg,
        live_identities=set(),
    )

    assert next(r for r in rows if r["id"] == "p1")["sessions"] == []


def test_a_voice_session_merges_with_the_identity_join() -> None:
    """A Person can be reached both ways — an Identity in one meeting, a mapped
    Voice in another — and the count is the union, not either half."""
    reg = _reg([{"id": "p1", "name": "Dana", "identities": ["mic-dana"]}])
    sessions = [
        {"session": "s1", "roster": {"mic-dana": _entry("Dana", slug="Dana")}},
        _mapped_session("s2", key="sysaudio#A", person_id="p1", run_id="r1", current="r1"),
    ]

    rows = build_people_view(sessions=sessions, registry=reg, live_identities=set())

    assert next(r for r in rows if r["id"] == "p1")["sessions"] == ["s1", "s2"]
