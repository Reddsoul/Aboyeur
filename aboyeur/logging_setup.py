"""Structured JSON logs for the backend.

One line per record: {ts, level, logger, msg, ...extras}. Chat content is
never logged anywhere in this codebase (grep `logger.*content` — the rule is
enforced by review, not by this formatter), and the formatter itself scrubs
anything that looks like an email, phone number or API key in the message
as a second net.
"""

from __future__ import annotations

import json
import logging
import time

from aboyeur.sensitivity import mask_pii


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": mask_pii(record.getMessage()),
        }
        if record.exc_info:
            payload["exc"] = mask_pii(self.formatException(record.exc_info))
        for key in ("user_id", "session_id", "event", "latency_ms"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(payload, ensure_ascii=False)


def configure(level: int = logging.INFO, json_logs: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter() if json_logs
        else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root.addHandler(handler)
    # uvicorn's access log stays on its own format (path + status only, no
    # bodies); its error logger is routed through ours.
    for name in ("uvicorn.error",):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
