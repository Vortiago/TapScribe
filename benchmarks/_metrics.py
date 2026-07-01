"""Shared scoring for the multi-language benchmarks: word error rate and
≥4-char content-word recall. Recall is robust to the da/no near-identity (shared
words match either way; the discriminating ones move the number); WER is the
standard ASR metric. Both lowercase + strip punctuation first."""

from __future__ import annotations

import re

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def content_words(text: str, *, min_len: int = 4) -> set[str]:
    return {m.group(0).lower() for m in _WORD.finditer(text) if len(m.group(0)) >= min_len}


def recall(reference: str, hypothesis: str) -> float:
    ref = content_words(reference)
    if not ref:
        return 0.0
    return round(len(ref & content_words(hypothesis)) / len(ref), 3)


def wer(reference: str, hypothesis: str) -> float:
    r = _PUNCT.sub("", reference.lower()).split()
    h = _PUNCT.sub("", hypothesis.lower()).split()
    if not r:
        return 0.0 if not h else 1.0
    prev = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        cur = [i]
        for j, hw in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rw != hw)))
        prev = cur
    return round(prev[-1] / len(r), 3)
