"""Tests for the hallucination filter — parser, matcher, and pipeline apply."""

from __future__ import annotations

from tapscribe import hallucinations
from tapscribe.transcribers.base import TranscriptionResult, TranscriptionSegment


def _result_with(segments: tuple[TranscriptionSegment, ...]) -> TranscriptionResult:
    return TranscriptionResult(
        transcriber="fake",
        backend="fake-backend",
        device="test",
        model="fake-model",
        language="en",
        language_probability=1.0,
        duration=segments[-1].end if segments else 0.0,
        text=" ".join(s.text for s in segments),
        segments=segments,
        initial_prompt_used="",
        hotwords_used="",
        quality_settings={},
    )


def _write_rules(path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_substring_rule_matches_case_insensitive(tmp_config_dir):
    _write_rules(tmp_config_dir / "hallucinations.txt", "amara.org\n")
    rules = hallucinations.parse_rules()
    assert hallucinations.match("Subtitles by the Amara.org community", rules) == "amara.org"


def test_substring_rule_does_not_match_unrelated(tmp_config_dir):
    _write_rules(tmp_config_dir / "hallucinations.txt", "amara.org\n")
    rules = hallucinations.parse_rules()
    assert hallucinations.match("Today's standup ran long.", rules) is None


def test_exact_rule_matches_only_whole_segment(tmp_config_dir):
    _write_rules(tmp_config_dir / "hallucinations.txt", "exact:thank you for watching\n")
    rules = hallucinations.parse_rules()
    assert hallucinations.match("Thank you for watching.", rules) == "exact:thank you for watching"
    # Embedded inside a longer real sentence — should NOT match.
    assert hallucinations.match("thank you for watching this demo.", rules) is None


def test_exact_rule_strips_terminal_punctuation(tmp_config_dir):
    _write_rules(tmp_config_dir / "hallucinations.txt", "exact:please subscribe\n")
    rules = hallucinations.parse_rules()
    assert hallucinations.match(" Please subscribe!!", rules) == "exact:please subscribe"


def test_regex_rule_is_case_insensitive(tmp_config_dir):
    _write_rules(
        tmp_config_dir / "hallucinations.txt",
        r"re:\bsubscribe to (my|this|our) channel\b" + "\n",
    )
    rules = hallucinations.parse_rules()
    matched = hallucinations.match("Subscribe to MY channel for more videos", rules)
    assert matched is not None
    assert matched.startswith("re:")


def test_blank_lines_and_comments_ignored(tmp_config_dir):
    _write_rules(tmp_config_dir / "hallucinations.txt", "\n# comment\namara.org\n\n")
    rules = hallucinations.parse_rules()
    assert [r["raw"] for r in rules] == ["amara.org"]


def test_bad_regex_silently_skipped(tmp_config_dir):
    # An unclosed group is invalid — the parser should drop the rule without
    # crashing the job.
    _write_rules(tmp_config_dir / "hallucinations.txt", "re:(unclosed\namara.org\n")
    rules = hallucinations.parse_rules()
    assert [r["raw"] for r in rules] == ["amara.org"]


def test_empty_re_pattern_skipped_at_runtime_not_match_all(tmp_config_dir):
    """FOOTGUN: a bare `re:` line (empty pattern) reaching the file by hand-edit
    used to `re.compile("")` into a MATCH-ALL rule that suppressed EVERY segment,
    silently emptying the transcript. The write guard blocks the PUT path, but a
    legacy/hand-edited file bypasses it — so the runtime parser must drop the
    empty `re:` too. The load-bearing assertion is that it is NOT a match-all."""
    _write_rules(tmp_config_dir / "hallucinations.txt", "re:\nkeep this\n")
    rules = hallucinations.parse_rules()
    assert [r["raw"] for r in rules] == ["keep this"]  # the empty re: is dropped
    assert hallucinations.match("literally any transcript segment", rules) is None


def test_empty_exact_pattern_skipped_at_runtime(tmp_config_dir):
    """Same degenerate class for `exact:`: a bare `exact:` normalises to "" and
    would match every punctuation-only ("noise") segment — a match-many. The
    runtime parser drops it, mirroring the empty-`re:` handling."""
    _write_rules(tmp_config_dir / "hallucinations.txt", "exact:\nkeep this\n")
    rules = hallucinations.parse_rules()
    assert [r["raw"] for r in rules] == ["keep this"]  # the empty exact: is dropped
    assert hallucinations.match("...", rules) is None  # a punctuation-only segment


def test_catastrophic_backtracking_pattern_rejected(tmp_config_dir):
    """A `(a+)+$` rule against a long no-match input takes seconds on Python's
    backtracking engine — long enough to wedge a transcribe job. The ReDoS
    guard must drop the rule at parse time rather than let it compile."""
    _write_rules(
        tmp_config_dir / "hallucinations.txt",
        "re:(a+)+$\namara.org\n",
    )
    rules = hallucinations.parse_rules()
    assert [r["raw"] for r in rules] == ["amara.org"]


def test_oversized_regex_pattern_rejected(tmp_config_dir):
    """A 300-char regex pattern is well over what any real hallucination rule
    needs. Reject as a precaution; the operator can split into multiple rules
    if they really need more."""
    huge = "re:" + ("foo|" * 100)  # > 256 chars
    _write_rules(tmp_config_dir / "hallucinations.txt", huge + "\namara.org\n")
    rules = hallucinations.parse_rules()
    assert [r["raw"] for r in rules] == ["amara.org"]


def test_safe_regex_with_nested_groups_is_accepted(tmp_config_dir):
    """The ReDoS guard must not over-fire — a `(foo|bar)+` is safe (no
    nested unbounded quantifier on the same body) and is exactly the
    shape of the real rule in `config/hallucinations.example.txt`."""
    _write_rules(
        tmp_config_dir / "hallucinations.txt",
        r"re:(subscribe|like) to (my|our|this) channel" + "\n",
    )
    rules = hallucinations.parse_rules()
    assert len(rules) == 1
    assert hallucinations.match("Subscribe to my channel for more", rules) is not None


def test_regex_is_safe_helper_returns_false_for_nested_unbounded():
    """Direct test of the guard helper — all four canonical nested-quantifier
    shapes are rejected."""
    from tapscribe.hallucinations import _regex_is_safe

    for bad in ["(a+)+", "(a+)*", "(a*)+", "(a*)*"]:
        assert not _regex_is_safe(bad), f"expected guard to reject {bad!r}"
    # Non-nested unbounded is fine.
    assert _regex_is_safe(r"(a|b)+")
    assert _regex_is_safe(r"a+b+")


def test_regex_guard_rejects_the_brace_form_of_nested_unbounded():
    """`{n,}` is as unbounded as `+`, and backtracks identically.

    The guard matched only `[+*]\\)[+*]`, so `(a{1,}){1,}$` passed both
    `regex_rule_ok` (the PUT /api/config/hallucinations validator) and the
    runtime parser. Measured on a no-match input it doubles cleanly — 18 chars
    0.03 s, 20 -> 0.14, 22 -> 0.59, 24 -> 2.18 — so a ~40-char transcript
    segment wedges the transcribe job, and `match()` runs once per segment with
    no timeout. That is exactly what the guard's comment says it prevents.
    """
    from tapscribe.hallucinations import _regex_is_safe, regex_rule_ok

    for bad in [r"(a{1,}){1,}", r"(a+){1,}", r"(a{1,})+", r"(a{2,})*", r"(a*){5,}"]:
        assert not _regex_is_safe(bad), f"expected guard to reject {bad!r}"
        assert not regex_rule_ok(bad + "$"), f"the write-time validator must reject {bad!r} too"

    # BOUNDED braces are not the hazard and must stay usable — real rules use
    # them (e.g. a repeated-phrase filter).
    assert _regex_is_safe(r"(ab){3}")
    assert _regex_is_safe(r"(ab){2,3}")
    assert regex_rule_ok(r"^(ha){3,8}$")


def test_match_returns_none_for_empty_text(tmp_config_dir):
    _write_rules(tmp_config_dir / "hallucinations.txt", "amara.org\n")
    rules = hallucinations.parse_rules()
    assert hallucinations.match("", rules) is None
    assert hallucinations.match("   ", rules) is None


def test_first_match_wins(tmp_config_dir):
    _write_rules(
        tmp_config_dir / "hallucinations.txt",
        "amara.org\nsubtitles by the amara\n",
    )
    rules = hallucinations.parse_rules()
    # Both match this segment — the first rule (amara.org) should be returned.
    matched = hallucinations.match("Subtitles by the Amara.org team", rules)
    assert matched == "amara.org"


def test_missing_file_returns_no_rules(tmp_config_dir):
    rules = hallucinations.parse_rules()
    assert rules == []


# ---------------------------------------------------------------------------
# hallucinations.apply — pipeline post-processor
# ---------------------------------------------------------------------------


def test_apply_with_empty_rules_returns_same_segments():
    seg = TranscriptionSegment(start=0.0, end=1.0, text="hello world")
    result = _result_with((seg,))
    out = hallucinations.apply(result, rules=[])
    assert out.segments == (seg,)
    assert out.suppressed_hallucinations == ()


def test_apply_moves_matching_segment_to_suppressed_with_rule_annotated(tmp_config_dir):
    (tmp_config_dir / "hallucinations.txt").write_text("amara.org\n", encoding="utf-8")
    rules = hallucinations.parse_rules()
    keep = TranscriptionSegment(start=0.0, end=1.0, text="welcome to the meeting")
    drop = TranscriptionSegment(start=2.0, end=3.0, text="subtitles by amara.org")
    result = _result_with((keep, drop))
    out = hallucinations.apply(result, rules=rules)
    assert out.segments == (keep,)
    assert len(out.suppressed_hallucinations) == 1
    sup = out.suppressed_hallucinations[0]
    assert sup.text == "subtitles by amara.org"
    assert sup.matched_rule == "amara.org"


def test_apply_preserves_segment_order(tmp_config_dir):
    (tmp_config_dir / "hallucinations.txt").write_text("exact:thank you\n", encoding="utf-8")
    rules = hallucinations.parse_rules()
    s1 = TranscriptionSegment(start=0.0, end=1.0, text="first")
    s2 = TranscriptionSegment(start=1.0, end=2.0, text="thank you")
    s3 = TranscriptionSegment(start=2.0, end=3.0, text="second")
    s4 = TranscriptionSegment(start=3.0, end=4.0, text="thank you")
    result = _result_with((s1, s2, s3, s4))
    out = hallucinations.apply(result, rules=rules)
    assert tuple(s.text for s in out.segments) == ("first", "second")
    assert tuple(s.text for s in out.suppressed_hallucinations) == ("thank you", "thank you")


def test_apply_returns_a_new_result_not_mutating_input():
    seg = TranscriptionSegment(start=0.0, end=1.0, text="hello")
    result = _result_with((seg,))
    out = hallucinations.apply(result, rules=[])
    assert out is not result  # new instance
    # Original untouched (verified by frozen + identity check above)
    assert result.segments == (seg,)


def test_apply_carries_forward_existing_suppressed_list(tmp_config_dir):
    """If a result somehow already has suppressed entries (e.g. from a
    chained earlier filter), apply should append new ones rather than
    drop the existing."""
    (tmp_config_dir / "hallucinations.txt").write_text("hit the bell\n", encoding="utf-8")
    rules = hallucinations.parse_rules()
    earlier = TranscriptionSegment(start=0.0, end=1.0, text="redacted-by-prior-step", matched_rule="prior")
    keep = TranscriptionSegment(start=1.0, end=2.0, text="hello there")
    drop = TranscriptionSegment(start=2.0, end=3.0, text="hit the bell")
    result = TranscriptionResult(
        transcriber="fake",
        backend="fake-backend",
        device="test",
        model="fake-model",
        language="en",
        language_probability=1.0,
        duration=3.0,
        text="hello there hit the bell",
        segments=(keep, drop),
        initial_prompt_used="",
        hotwords_used="",
        quality_settings={},
        suppressed_hallucinations=(earlier,),
    )
    out = hallucinations.apply(result, rules=rules)
    assert out.segments == (keep,)
    assert out.suppressed_hallucinations[0] == earlier  # preserved
    assert out.suppressed_hallucinations[1].matched_rule == "hit the bell"
