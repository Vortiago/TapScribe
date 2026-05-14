"""Hallucination filter — parse the rules file and match segment text.

Whisper was trained on a lot of YouTube data and likes to emit phrases like
"Thank you for watching" or "Subtitles by the Amara.org community" on
near-silent audio. This module is the post-decode net that drops those.

See `config/hallucinations.txt` for the human-editable rules.
"""

from __future__ import annotations

import re
from typing import Any

from . import config
from .text import normalise_for_exact, read_text_file


def parse_rules() -> list[dict[str, Any]]:
    """Parse hallucinations.txt into a list of compiled match rules.

    Each rule has shape:
      {"raw": "<line>", "kind": "substr"|"regex"|"exact", "matcher": <compiled>}

    Prefixes (case-insensitive on the prefix itself):
      re:    line is a regex (compiled with IGNORECASE)
      exact: line matches only if the segment, after punctuation/whitespace
             stripping, equals this string (case-insensitively)
      <none> case-insensitive substring match

    Lines starting with `#` and blank lines are ignored.
    """
    raw = read_text_file(config.HALLUCINATIONS_FILE)
    rules: list[dict[str, Any]] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        lower = s.lower()
        if lower.startswith("re:"):
            pat = s[3:].strip()
            try:
                rules.append({"raw": s, "kind": "regex", "matcher": re.compile(pat, re.IGNORECASE)})
            except re.error:
                # Bad regex: skip silently rather than break a job.
                continue
        elif lower.startswith("exact:"):
            target = s[6:].strip()
            rules.append({"raw": s, "kind": "exact", "matcher": normalise_for_exact(target)})
        else:
            rules.append({"raw": s, "kind": "substr", "matcher": s.lower()})
    return rules


def match(text: str, rules: list[dict[str, Any]]) -> str | None:
    """Return the matching rule's raw string if `text` matches any rule,
    else None. First-match-wins."""
    t = (text or "").strip()
    if not t:
        return None
    t_lower = t.lower()
    t_exact = normalise_for_exact(t)
    for r in rules:
        kind = r["kind"]
        if kind == "regex":
            if r["matcher"].search(t):
                return r["raw"]
        elif kind == "exact":
            if r["matcher"] == t_exact:
                return r["raw"]
        else:  # substr
            if r["matcher"] in t_lower:
                return r["raw"]
    return None
