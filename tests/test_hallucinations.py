"""Tests for the hallucination filter — parser + matcher."""

from __future__ import annotations

from tapscribe import hallucinations


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
