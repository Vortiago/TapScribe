"""API adapter — summarize via an OpenAI-compatible chat-completions endpoint.

Taps #85 into the summarizer seam: POST to {base_url}/chat/completions with
{"model", "messages", "max_tokens?}, response choices[0].message.content. The
shape Ollama serves at /v1, and any OpenAI-compatible proxy (Groq, Anthropic's
gateway, etc.). Transport is stdlib urllib — no new dependency.

Security: base_url is operator-controlled config validated to http(s) at write
time (same trust level as the command source's template). api_key is persisted
but NEVER returned to any GET or state poll — only key_set boolean is exposed.

testability seam mirrors LocalSummarizer.generate_fn: an injectable post_fn so
tests drive the adapter with no real network."""

from __future__ import annotations

import json as _json
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from .base import (
    DEFAULT_SUMMARY_PROMPT,
    SummarizerError,
    SummarizerFailed,
    SummarizerUnavailable,
    SummaryResult,
)

# Type alias for the HTTP POST seam. Production builds a urllib-based default;
# tests inject a stub that records the request + returns a canned response.
ApiPostFn = Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]]

# Per-summarize transport timeout.
_DEFAULT_TIMEOUT_S = 30.0


def _http_post_json(
    url: str, headers: dict[str, str], body: dict[str, Any], *, timeout_s: float
) -> dict[str, Any]:
    """Default ApiPostFn: POST `body` as JSON to `url`, return the parsed JSON
    response. urllib (stdlib) keeps this dependency-free.

    The scheme is HARD-ENFORCED here, at the urlopen boundary, not trusted from
    upstream: `write_summarizer_config` validates the STORED base_url to http(s)
    at write time, but the per-generate `base_url` override on
    POST /api/sessions/{s}/summarize bypasses that path entirely. Re-checking at
    the call site closes urlopen's file://-and-custom-scheme vector (the B310 /
    S310 finding) regardless of how the URL arrived."""
    if not url.startswith(("http://", "https://")):
        raise SummarizerFailed(f"refusing to call non-http(s) summarizer url: {url!r}")
    data = _json.dumps(body).encode("utf-8")
    req = urllib_request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib_request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310  # nosec B310 — scheme hard-enforced to http(s) immediately above
            return _json.loads(resp.read().decode("utf-8"))
    except urllib_error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace").strip()[:200]
        raise SummarizerFailed(f"api endpoint {url} returned HTTP {e.code}: {detail}") from e
    except (urllib_error.URLError, OSError) as e:
        raise SummarizerFailed(f"could not reach api endpoint {url}: {e}") from e


class ApiSummarizer:
    """Summarize via an OpenAI-compatible chat-completions endpoint.

    POSTs to {base_url}/chat/completions with a system + user message carrying
    the prompt and transcript. Extracts choices[0].message.content as the
    summary text."""

    source = "api"

    def __init__(
        self,
        *,
        base_url: str,
        model: str = "",
        api_key: str = "",
        max_tokens: int | None = None,
        timeout_s: float | None = None,
        post_fn: ApiPostFn | None = None,
    ) -> None:
        if not base_url.strip():
            raise SummarizerUnavailable(
                "the api summarizer source needs a base URL (e.g. http://host:11434/v1)"
            )
        self._base_url = base_url.strip()
        self.model = model
        self._api_key = api_key or ""
        self._max_tokens = max_tokens
        self._timeout_s = _DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s
        # Default seam binds the per-call timeout into the urllib helper so it
        # matches the 3-arg ApiPostFn shape an injected stub uses.
        self._post_fn: ApiPostFn = post_fn or partial(_http_post_json, timeout_s=self._timeout_s)

    def summarize(self, transcript: str, *, prompt: str) -> SummaryResult:
        instruction = (prompt or "").strip() or DEFAULT_SUMMARY_PROMPT
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a meeting-summarisation assistant. Read the transcript and "
                    "produce a clear, well-structured summary. Output only the summary."
                ),
            },
            {
                "role": "user",
                "content": f"{instruction}\n\n--- TRANSCRIPT ---\n{transcript}",
            },
        ]

        url = self._base_url.rstrip("/") + "/chat/completions"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        body: dict[str, Any] = {"model": self.model, "messages": messages}
        if self._max_tokens is not None:
            body["max_tokens"] = self._max_tokens

        started = datetime.now(UTC)
        try:
            data = self._post_fn(url, headers, body)
        except SummarizerError:
            raise
        except Exception as e:
            raise SummarizerFailed(f"api endpoint {url} request failed: {e}") from e

        took_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        try:
            content = data["choices"][0]["message"]["content"]
            summary = (content or "").strip()
        except (KeyError, IndexError, TypeError) as e:
            raise SummarizerFailed(f"api endpoint {url} returned an unexpected response shape") from e

        if not summary:
            raise SummarizerFailed("the api endpoint returned an empty summary")

        return SummaryResult(
            summary=summary,
            source=self.source,
            prompt=prompt,
            model=self.model,
            took_ms=took_ms,
            created_at=started.isoformat(),
        )
