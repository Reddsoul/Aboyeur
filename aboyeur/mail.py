"""Transactional email via Resend's REST API (httpx, no SDK).

One entry point: send(to, template, ctx). Templates live in
aboyeur/templates/<name>.txt and .html and use string.Template `$var`
placeholders — no logic, no HTML escaping surprises (values are escaped for
the HTML variant here). With no RESEND_API_KEY the message is logged instead
of sent, which is what dev and the test suite want; the log line carries the
link so a developer can click it, but never a password or an API key.
"""

from __future__ import annotations

import html
import logging
import os
from pathlib import Path
from string import Template

import httpx

logger = logging.getLogger("aboyeur")

_TEMPLATES = Path(__file__).resolve().parent / "templates"
_RESEND_URL = "https://api.resend.com/emails"

SUBJECTS = {
    "verify": "Confirm your Aboyeur email",
    "reset": "Reset your Aboyeur password",
    "low_balance": "Your Aboyeur credit is running low",
    "deleted": "Your Aboyeur account is scheduled for deletion",
}


class MailError(Exception):
    pass


def _render(template: str, ctx: dict, kind: str) -> str:
    path = _TEMPLATES / f"{template}.{kind}"
    if not path.exists():
        raise MailError(f"no template {template}.{kind}")
    values = {k: (html.escape(str(v)) if kind == "html" else str(v)) for k, v in ctx.items()}
    return Template(path.read_text(encoding="utf-8")).safe_substitute(values)


def send(to: str, template: str, ctx: dict, *, api_key: str | None = None,
         sender: str | None = None) -> dict:
    """Render and send. Returns {"sent": bool, "id": str | None}. Never
    raises on a delivery failure — callers already did the important write
    (the token row) and a bounced email is a support case, not a 500."""
    api_key = api_key if api_key is not None else os.environ.get("RESEND_API_KEY")
    sender = sender or os.environ.get("EMAIL_FROM") or "Aboyeur <no-reply@localhost>"
    subject = SUBJECTS.get(template, "Aboyeur")
    text = _render(template, ctx, "txt")
    body_html = _render(template, ctx, "html")
    if not api_key:
        # Dev: the link is the useful part. ctx never carries secrets beyond
        # the single-use token URL, which is the thing being delivered.
        logger.info("mail (not sent, no RESEND_API_KEY) to=%s template=%s link=%s",
                    to, template, ctx.get("link", "-"))
        return {"sent": False, "id": None}
    try:
        res = httpx.post(
            _RESEND_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"from": sender, "to": [to], "subject": subject, "text": text, "html": body_html},
            timeout=10,
        )
        res.raise_for_status()
        return {"sent": True, "id": (res.json() or {}).get("id")}
    except httpx.HTTPError as e:
        logger.warning("mail send failed template=%s to=%s: %s", template, to, type(e).__name__)
        return {"sent": False, "id": None}
