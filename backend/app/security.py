"""SSRF guard, robots.txt checks and admin auth."""
import ipaddress
import secrets
import socket
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from fastapi import Header, HTTPException

from .config import get_settings


def is_public_http_url(url: str) -> bool:
    """Reject non-http(s) schemes and hosts that resolve to private, loopback, link-local or reserved IPs."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    if parts.username or parts.password:
        return False
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return True


_robots_cache: dict[str, tuple[float, RobotFileParser | None]] = {}


def robots_allows(url: str, client: httpx.Client) -> bool:
    """True if robots.txt permits our user agent to fetch url. Cached 6h per host. Fails closed on 401/403."""
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    now = time.time()
    cached = _robots_cache.get(base)
    if not cached or now - cached[0] > 6 * 3600:
        rp = RobotFileParser()
        try:
            r = client.get(base + "/robots.txt", timeout=10)
            if r.status_code in (401, 403):
                rp = None
            elif r.status_code >= 400:
                rp.parse([])  # no robots.txt -> allowed
            else:
                rp.parse(r.text.splitlines())
        except httpx.HTTPError:
            rp = None
        _robots_cache[base] = (now, rp)
        cached = _robots_cache[base]
    rp = cached[1]
    if rp is None:
        return False
    return rp.can_fetch(get_settings().user_agent, url)


def require_admin(authorization: str = Header(default="")) -> None:
    token = get_settings().admin_token
    supplied = authorization.removeprefix("Bearer ").strip()
    if not token or token == "change-me":
        raise HTTPException(503, "Set ADMIN_TOKEN in the environment to enable the admin API")
    if not secrets.compare_digest(supplied, token):
        raise HTTPException(401, "Invalid admin token")
