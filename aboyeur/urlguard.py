"""Server-side reachability policy for user-supplied LLM endpoints.

A BYOK base_url is a URL the *server* connects to on the user's behalf, so an
unrestricted one is an SSRF primitive: point it at a sibling container,
the cloud metadata service or the admin API and read the error body back
through /api/llm-settings/test. `check_llm_url` refuses anything that
isn't a public https origin.

Private/loopback targets are legitimate in development and in the
Tailscale beta (a LiteLLM on the DGX at 100.x). They're allowed when
COOKIE_SECURE is off (plain-http local dev) or when ALLOW_PRIVATE_LLM_URLS
is set explicitly; the hosted deploy has neither. Admin-managed providers
(llm_providers) are trusted input and don't go through this.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlsplit

from aboyeur.config import env_flag


class UnsafeUrlError(ValueError):
    pass


def private_urls_allowed() -> bool:
    if env_flag("ALLOW_PRIVATE_LLM_URLS", False):
        return True
    return not env_flag("COOKIE_SECURE", True)


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # is_global excludes private, loopback, link-local (169.254/16 — cloud
    # metadata), CGNAT (100.64/10 — Tailscale), ULA, multicast, reserved.
    return ip.is_global and not ip.is_multicast


def check_llm_url(url: str, *, resolve: bool = True) -> str:
    """Return the normalised URL or raise UnsafeUrlError.

    resolve=True also does a DNS lookup and checks every address (the
    save/test path). resolve=False is the cheap per-turn re-check: scheme,
    literal IPs and obvious hostnames only, no network I/O.
    """
    url = (url or "").strip().rstrip("/")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not parts.scheme or not host:
        raise UnsafeUrlError("base_url must be a full URL like https://api.example.com/v1")
    if parts.username or parts.password:
        raise UnsafeUrlError("base_url must not embed credentials")
    if parts.scheme not in ("http", "https"):
        raise UnsafeUrlError("base_url must use http or https")

    if private_urls_allowed():
        return url

    if parts.scheme != "https":
        raise UnsafeUrlError("base_url must use https")
    if "." not in host or host.endswith(".local") or host.endswith(".internal"):
        raise UnsafeUrlError("base_url must be a public hostname")

    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public_ip(literal):
            raise UnsafeUrlError("base_url must not point at a private or internal address")
        return url

    if resolve:
        try:
            infos = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
        except socket.gaierror as e:
            raise UnsafeUrlError(f"base_url host does not resolve: {e}") from e
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if not _is_public_ip(ip):
                raise UnsafeUrlError("base_url resolves to a private or internal address")
    return url
