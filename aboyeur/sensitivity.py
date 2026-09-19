"""Sensitivity classifier for user messages.

Runs after a turn ends, on the user's own resolved LLM (BYOK or hosted),
and stores a label + category list on chat_messages — never a summary,
never the content. The admin console redacts anything flagged
`sensitive` or `unknown` (unknown = the classifier failed, treated as
sensitive on purpose).

This is a UX/ops safeguard so an operator reading transcripts for support
doesn't stumble into a client's diagnosis. It is NOT a compliance control:
it is best-effort, model-dependent, and the ToS forbids PHI regardless.
See docs/privacy-posture.md.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time

from openai import OpenAI

logger = logging.getLogger("aboyeur")

CATEGORIES = (
    "diagnosis", "medication", "mental_health", "pregnancy", "disability",
    "lab", "third_party_health", "none",
)

SYSTEM_PROMPT = "You are a data-classification filter. Reply with JSON only."

USER_TEMPLATE = (
    "Classify this message. sensitive=true if it contains a medical diagnosis, "
    "medication, mental-health condition, pregnancy, disability, lab results, or "
    "identifies a third person by full name together with any health detail. "
    "Categories from: [diagnosis, medication, mental_health, pregnancy, disability, "
    "lab, third_party_health, none].\n"
    '{"sensitive": bool, "categories": [...]}\n'
    "Message: <<<{content}>>>"
)

MAX_TOKENS = 60
# Long messages are truncated for the classifier; the tail of a 4k-char
# paste is rarely where the diagnosis is, and the prompt cost is ours.
MAX_CONTENT_CHARS = 4000

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_reply(text: str) -> tuple[bool, list[str]] | None:
    """Strict parse of the model's JSON. None on anything malformed."""
    if not text:
        return None
    m = _JSON_RE.search(text)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("sensitive"), bool):
        return None
    cats = data.get("categories")
    if not isinstance(cats, list):
        return None
    clean = [c for c in cats if isinstance(c, str) and c in CATEGORIES and c != "none"]
    sensitive = data["sensitive"] or bool(clean)
    return sensitive, sorted(set(clean))


def classify_text(client: OpenAI, model: str, content: str,
                  timeout: float = 20.0) -> tuple[str, list[str], tuple[int, int] | None]:
    """('none' | 'sensitive' | 'unknown', categories, usage). Never raises."""
    prompt = USER_TEMPLATE.replace("{content}", content[:MAX_CONTENT_CHARS])
    try:
        res = client.with_options(timeout=timeout, max_retries=0).chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": prompt}],
            max_tokens=MAX_TOKENS,
            temperature=0,
        )
    except Exception as e:
        logger.info("sensitivity classifier call failed: %s", type(e).__name__)
        return "unknown", [], None
    usage = None
    if getattr(res, "usage", None) is not None:
        usage = (res.usage.prompt_tokens or 0, res.usage.completion_tokens or 0)
    text = ""
    try:
        text = res.choices[0].message.content or ""
    except (IndexError, AttributeError):
        pass
    parsed = parse_reply(text)
    if parsed is None:
        return "unknown", [], usage
    sensitive, cats = parsed
    return ("sensitive" if sensitive else "none"), cats, usage


def store(conn: sqlite3.Connection, message_id: int, label: str, categories: list[str]) -> None:
    conn.execute(
        "UPDATE chat_messages SET sensitivity = ?, sensitivity_json = ? WHERE id = ?",
        (label, json.dumps(categories), message_id),
    )
    conn.commit()


def mark_unknown(conn: sqlite3.Connection, message_id: int) -> None:
    store(conn, message_id, "unknown", [])


def unclassified_user_messages(conn: sqlite3.Connection, session_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, content FROM chat_messages WHERE session_id = ? AND role = 'user' "
        "AND sensitivity IS NULL ORDER BY id",
        (session_id,),
    )]


def is_redacted_label(label: str | None) -> bool:
    return label in ("sensitive", "unknown")


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Phones: 7+ digits with common separators, guarded to avoid eating ids.
_PHONE_RE = re.compile(r"(?<![\w/])(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}(?!\w)")
_KEY_RE = re.compile(r"\b(?:sk|rk|pk|whsec|re|sk-ant|sk-proj)[-_][A-Za-z0-9_\-]{12,}\b")
_BEARER_RE = re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._\-]+")


def mask_pii(text: str | None) -> str | None:
    """Emails, phone numbers, API keys and bearer tokens -> placeholders.
    Used on unflagged admin transcript lines and the log tail."""
    if not text:
        return text
    text = _BEARER_RE.sub(r"\1[redacted]", text)
    text = _KEY_RE.sub("[key]", text)
    text = _EMAIL_RE.sub("[email]", text)
    text = _PHONE_RE.sub("[phone]", text)
    return text


def since_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
