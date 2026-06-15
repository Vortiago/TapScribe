"""Direct tests for ApiSummarizer — driven by a stub post_fn (no real network).

Covers request shape (URL, headers, body), response mapping, empty-base_url → Unavailable,
transport errors via SummarizerFailed, and the Authorization header on/off behaviour."""

from __future__ import annotations

import pytest

from tapscribe.summarizers.api import ApiSummarizer
from tapscribe.summarizers.base import (
    DEFAULT_SUMMARY_PROMPT,
    SummarizerFailed,
    SummarizerUnavailable,
    SummaryResult,
)


def _make_stub(rec: list[tuple]) -> dict:
    """Return a canned response; records the call in `rec`."""

    def stub(url: str, headers: dict[str, str], body: dict) -> dict:
        rec.append((url, headers, body))
        return {"choices": [{"message": {"content": "SUMMARY TEXT"}}]}

    return stub


class TestApiSummarizerConstruction:
    def test_empty_base_url_raises_unavailable(self):
        for blank in ("", "   "):
            with pytest.raises(SummarizerUnavailable) as ei:
                ApiSummarizer(base_url=blank)
            assert "base url" in str(ei.value).lower()


class TestApiSummarizerRequestShape:
    def test_url_ends_with_chat_completions(self):
        rec: list[tuple] = []
        s = ApiSummarizer(base_url="http://h:11434/v1", model="qwen", post_fn=_make_stub(rec))
        s.summarize("hello world", prompt="sum it up")
        assert rec[0][0] == "http://h:11434/v1/chat/completions"

    def test_url_strips_trailing_slash(self):
        rec: list[tuple] = []
        s = ApiSummarizer(base_url="http://h:11434/v1/", model="m", post_fn=_make_stub(rec))
        s.summarize("t", prompt="p")
        assert rec[0][0] == "http://h:11434/v1/chat/completions"

    def test_body_carries_model_and_messages(self):
        rec: list[tuple] = []
        s = ApiSummarizer(base_url="http://x/v1", model="qwen", post_fn=_make_stub(rec))
        s.summarize("transcript text", prompt="custom prompt")
        _, _, body = rec[0]
        assert body["model"] == "qwen"
        assert len(body["messages"]) == 2
        assert body["messages"][0]["role"] == "system"
        assert body["messages"][1]["role"] == "user"
        assert "transcript text" in body["messages"][1]["content"]
        assert "custom prompt" in body["messages"][1]["content"]

    def test_empty_prompt_uses_default(self):
        rec: list[tuple] = []
        s = ApiSummarizer(base_url="http://x/v1", model="m", post_fn=_make_stub(rec))
        s.summarize("t", prompt="")
        _, _, body = rec[0]
        assert DEFAULT_SUMMARY_PROMPT in body["messages"][1]["content"]

    def test_max_tokens_included_when_set(self):
        rec: list[tuple] = []
        s = ApiSummarizer(base_url="http://x/v1", model="m", max_tokens=2048, post_fn=_make_stub(rec))
        s.summarize("t", prompt="p")
        _, _, body = rec[0]
        assert body["max_tokens"] == 2048

    def test_max_tokens_omitted_when_none(self):
        rec: list[tuple] = []
        s = ApiSummarizer(base_url="http://x/v1", model="m", max_tokens=None, post_fn=_make_stub(rec))
        s.summarize("t", prompt="p")
        _, _, body = rec[0]
        assert "max_tokens" not in body


class TestApiSummarizerAuth:
    def test_authorized_when_key_set(self):
        rec: list[tuple] = []
        s = ApiSummarizer(base_url="http://x/v1", model="m", api_key="s3cret", post_fn=_make_stub(rec))
        s.summarize("t", prompt="p")
        _, headers, _ = rec[0]
        assert headers["Authorization"] == "Bearer s3cret"

    def test_no_authorization_when_key_empty(self):
        rec: list[tuple] = []
        s = ApiSummarizer(base_url="http://x/v1", model="m", api_key="", post_fn=_make_stub(rec))
        s.summarize("t", prompt="p")
        _, headers, _ = rec[0]
        assert "Authorization" not in headers


class TestApiSummarizerResponseMapping:
    def test_returns_summary_result(self):
        rec: list[tuple] = []
        stub_resp = {"choices": [{"message": {"content": "THE SUMMARY"}}]}

        def _stub(url, headers, body):
            rec.append((url, headers, body))
            return stub_resp

        s = ApiSummarizer(base_url="http://x/v1", model="mymodel", post_fn=_stub)
        result = s.summarize("some text", prompt="p")
        assert isinstance(result, SummaryResult)
        assert result.summary == "THE SUMMARY"
        assert result.source == "api"
        assert result.model == "mymodel"
        assert result.prompt == "p"
        assert result.took_ms >= 0

    def test_empty_content_raises_failed(self):
        def _stub(url, headers, body):
            return {"choices": [{"message": {"content": ""}}]}

        s = ApiSummarizer(base_url="http://x/v1", model="m", post_fn=_stub)
        with pytest.raises(SummarizerFailed, match="empty summary"):
            s.summarize("t", prompt="p")

    def test_missing_content_raises_failed(self):
        def _stub(url, headers, body):
            return {"choices": [{"message": {}}]}

        s = ApiSummarizer(base_url="http://x/v1", model="m", post_fn=_stub)
        with pytest.raises(SummarizerFailed, match="unexpected response shape"):
            s.summarize("t", prompt="p")

    def test_post_fn_raising_summarizer_failed_propagates(self):
        def _stub(url, headers, body):
            raise SummarizerFailed("could not reach api endpoint http://x/v1/chat/completions: unreachable")

        s = ApiSummarizer(base_url="http://x/v1", model="m", post_fn=_stub)
        with pytest.raises(SummarizerFailed, match="unreachable"):
            s.summarize("t", prompt="p")

    def test_post_fn_exception_is_wrapped_as_failed(self):
        def _stub(url, headers, body):
            raise RuntimeError("something exploded")

        s = ApiSummarizer(base_url="http://x/v1", model="m", post_fn=_stub)
        with pytest.raises(SummarizerFailed):
            s.summarize("t", prompt="p")


class TestApiSummarizerDefaultTransport:
    """The DEFAULT post_fn (no stub injected) is the real urllib path — every
    other test mocks the seam, so this is the only coverage that the default
    binds the per-call timeout into `_http_post_json` (a 3-arg ApiPostFn). It
    regressed once: the default was `_http_post_json` bare, which has a required
    `timeout_s` kw-only arg → TypeError on the first real call. urlopen is
    monkeypatched so no socket is opened."""

    def test_default_post_fn_binds_timeout_and_maps_response(self, monkeypatch):
        captured: dict = {}

        class _FakeResp:
            def __init__(self, payload: bytes):
                self._payload = payload

            def read(self):
                return self._payload

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _fake_urlopen(req, timeout=None):
            captured["timeout"] = timeout
            captured["url"] = req.full_url
            captured["body"] = req.data
            return _FakeResp(b'{"choices": [{"message": {"content": "OK SUMMARY"}}]}')

        monkeypatch.setattr("tapscribe.summarizers.api.urllib_request.urlopen", _fake_urlopen)
        # No post_fn → exercises the real default seam + _http_post_json.
        s = ApiSummarizer(base_url="http://x/v1", model="m", timeout_s=12.5)
        res = s.summarize("transcript", prompt="p")
        assert isinstance(res, SummaryResult)
        assert res.summary == "OK SUMMARY"
        assert captured["timeout"] == 12.5  # the bound per-call timeout reached urlopen
        assert captured["url"].endswith("/chat/completions")

    @pytest.mark.parametrize("bad_base", ["file:///etc/passwd", "ftp://host/x", "gopher://h", "x/v1"])
    def test_non_http_scheme_rejected_before_urlopen(self, monkeypatch, bad_base):
        """The urlopen boundary HARD-ENFORCES http(s): a non-http(s) base_url
        (which can arrive via the per-generate body override, bypassing the
        write-time config validation) is refused WITHOUT opening anything —
        closing urlopen's file:// / custom-scheme vector (B310/S310)."""
        called = False

        def _boom_urlopen(req, timeout=None):
            nonlocal called
            called = True
            raise AssertionError("urlopen must NOT be reached for a non-http(s) scheme")

        monkeypatch.setattr("tapscribe.summarizers.api.urllib_request.urlopen", _boom_urlopen)
        s = ApiSummarizer(base_url=bad_base, model="m")  # default transport (no stub)
        with pytest.raises(SummarizerFailed, match="non-http"):
            s.summarize("t", prompt="p")
        assert called is False
