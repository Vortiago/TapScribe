"""JSON logging setup — opt-in via `--log-json` on `python -m tapscribe`.

Default behaviour stays plaintext (uvicorn's own formatter); flipping the
flag swaps every handler's formatter on the root + uvicorn loggers to
emit one JSON line per record. Useful when piping into a structured log
collector (journalctl -o json, vector, fluent-bit) without parsing
uvicorn's freeform output.

Schema per line: `{ts, level, logger, msg, ...extras}` where `extras`
covers anything the caller attaches via `logger.<level>(..., extra={...})`
and any exception info if present. Stable enough for an alerting rule;
no nested objects in the top level.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

_RESERVED_LOGRECORD_KEYS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record. `extra={...}` keys passed to
    the logger surface as top-level fields alongside `ts`/`level`/...
    Exception info, if any, lands under `exc_info` as the formatted
    traceback string — same content as the plaintext formatter, just
    inside the JSON envelope."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=UTC).isoformat()
        payload: dict[str, object] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED_LOGRECORD_KEYS or key.startswith("_"):
                continue
            if key in payload:
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def install_json_logging() -> None:
    """Replace every handler's formatter on the root logger and uvicorn's
    three loggers with `JsonFormatter`. Idempotent — calling twice just
    re-applies the same formatter instance.

    Must run AFTER uvicorn.run() has installed its own handlers via
    dictConfig (otherwise our formatter is dropped). The simplest
    placement is uvicorn's `log_config=` hook, but a post-start swap
    works for one-shot processes too — we set it up in main() and the
    lifespan handler re-applies after the dictConfig pass."""
    formatter = JsonFormatter()
    targets = [
        logging.getLogger(),
        logging.getLogger("uvicorn"),
        logging.getLogger("uvicorn.access"),
        logging.getLogger("uvicorn.error"),
        logging.getLogger("tapscribe"),
    ]
    for logger in targets:
        for handler in logger.handlers:
            handler.setFormatter(formatter)
