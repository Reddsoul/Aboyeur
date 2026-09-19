"""The one constant the ToS gate keys on.

Bump LEGAL_VERSION when terms or privacy change materially (and add a row
to legal/CHANGELOG.md). Any user whose users.tos_version is older sees the
blocking "updated terms" modal until they POST /api/account/accept-tos.
"""

from __future__ import annotations

LEGAL_VERSION = "2026-09-19"

# Documents served at /legal/<slug>; the files live in legal/<slug>.md.
LEGAL_DOCS = ("terms", "privacy", "disclaimer", "refunds", "acceptable-use")


def needs_acceptance(stored: str | None) -> bool:
    """ISO-date versions compare lexically; None (never accepted) needs it."""
    return not stored or stored < LEGAL_VERSION
