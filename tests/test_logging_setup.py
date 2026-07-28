"""Coverage for `tapscribe/logging_setup.py` (#236) — the `--log-json`
path, imported inside the lifespan (`tapscribe/lifespan.py`) but never
exercised: no test constructed a
`JsonFormatter` or called `install_json_logging()` before this file, so a
broken field name or a formatter that silently drops handlers shipped
with the rest of the suite green.

Seams under test:
  * `JsonFormatter.format()` — pure function of a `logging.LogRecord`,
    the cheapest and most direct seam.
  * `install_json_logging()` — mutates handler formatters on a handful of
    named loggers; asserted by inspecting `logger.handlers[i].formatter`.
"""

from __future__ import annotations

import json
import logging

from tapscribe.logging_setup import JsonFormatter, install_json_logging


def _make_record(
    *,
    level: int = logging.INFO,
    msg: str = "hello %s",
    args: tuple = ("world",),
    logger_name: str = "tapscribe.test",
    extra: dict | None = None,
    exc_info=None,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name=logger_name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=exc_info,
    )
    for k, v in (extra or {}).items():
        setattr(record, k, v)
    return record


def test_format_emits_valid_json_with_core_fields() -> None:
    record = _make_record()
    line = JsonFormatter().format(record)
    payload = json.loads(line)

    assert payload["level"] == "INFO"
    assert payload["logger"] == "tapscribe.test"
    assert payload["msg"] == "hello world"  # %-args interpolated via getMessage()
    assert "ts" in payload


def test_format_ts_is_iso8601_utc() -> None:
    from datetime import datetime

    record = _make_record()
    payload = json.loads(JsonFormatter().format(record))

    # Must round-trip through fromisoformat and carry a UTC offset.
    parsed = datetime.fromisoformat(payload["ts"])
    assert parsed.utcoffset() is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_format_includes_extra_fields() -> None:
    record = _make_record(extra={"session": "abc123", "count": 4})
    payload = json.loads(JsonFormatter().format(record))

    assert payload["session"] == "abc123"
    assert payload["count"] == 4


def test_format_excludes_reserved_logrecord_keys() -> None:
    """Standard LogRecord bookkeeping attributes (filename, lineno,
    process, ...) must not leak into the payload as if they were
    caller-supplied `extra=` fields."""
    record = _make_record()
    payload = json.loads(JsonFormatter().format(record))

    for reserved in ("filename", "lineno", "process", "module", "pathname", "thread"):
        assert reserved not in payload


def test_format_includes_exc_info_as_formatted_traceback() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _make_record(exc_info=sys.exc_info())

    payload = json.loads(JsonFormatter().format(record))

    assert "exc_info" in payload
    assert "ValueError: boom" in payload["exc_info"]


def test_format_omits_exc_info_key_when_no_exception() -> None:
    record = _make_record()
    payload = json.loads(JsonFormatter().format(record))

    assert "exc_info" not in payload


def test_format_non_json_serializable_extra_falls_back_to_str() -> None:
    """`extra=` may carry an arbitrary object (e.g. a Path); the formatter
    must not raise — `json.dumps(..., default=str)` is the documented
    fallback."""
    from pathlib import Path

    record = _make_record(extra={"path": Path("/tmp/x")})
    payload = json.loads(JsonFormatter().format(record))

    assert payload["path"] == str(Path("/tmp/x"))


def test_install_json_logging_replaces_handler_formatters_on_target_loggers() -> None:
    targets = [
        logging.getLogger(),
        logging.getLogger("uvicorn"),
        logging.getLogger("uvicorn.access"),
        logging.getLogger("uvicorn.error"),
        logging.getLogger("tapscribe"),
    ]
    handlers = [logging.StreamHandler() for _ in targets]
    originals = []
    for logger, handler in zip(targets, handlers, strict=True):
        originals.append(list(logger.handlers))
        logger.handlers = [handler]

    try:
        install_json_logging()
        for handler in handlers:
            assert isinstance(handler.formatter, JsonFormatter)
    finally:
        for logger, original in zip(targets, originals, strict=True):
            logger.handlers = original


def test_install_json_logging_is_idempotent() -> None:
    """Calling twice re-applies the same formatter type without raising
    or duplicating handlers."""
    logger = logging.getLogger("tapscribe")
    handler = logging.StreamHandler()
    original = list(logger.handlers)
    logger.handlers = [handler]

    try:
        install_json_logging()
        install_json_logging()
        assert logger.handlers == [handler]
        assert isinstance(handler.formatter, JsonFormatter)
    finally:
        logger.handlers = original


def test_install_json_logging_leaves_untargeted_loggers_alone() -> None:
    """A logger that isn't one of the five named targets keeps its
    existing formatter untouched."""
    other = logging.getLogger("tapscribe.test.untouched")
    handler = logging.StreamHandler()
    plain_formatter = logging.Formatter("%(message)s")
    handler.setFormatter(plain_formatter)
    original = list(other.handlers)
    other.handlers = [handler]

    try:
        install_json_logging()
        assert handler.formatter is plain_formatter
    finally:
        other.handlers = original
