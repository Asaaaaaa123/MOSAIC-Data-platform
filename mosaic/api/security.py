"""Request guards, security headers and secret scrubbing for the local API.

Mosaic has no user accounts: it is meant to run on 127.0.0.1 for one researcher.
Two browser-borne attacks still matter for such a server, because it spends the
user's API credits and exposes their audit log:

* DNS rebinding - a malicious page re-points its own hostname at 127.0.0.1.
  Blocked by checking the ``Host`` header against an allow-list when the server
  is bound to a loopback address.
* Cross-site request forgery - a page POSTs to ``http://127.0.0.1:8000/api/chat``.
  Blocked by rejecting state-changing requests whose ``Origin`` (or
  ``Sec-Fetch-Site``) shows they came from another site.

Every response also carries a strict Content-Security-Policy so the web UI can
only load its own scripts and styles, even if upstream data were to slip markup
past the client-side escaping.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable
from urllib.parse import urlsplit

from ..config import Settings

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)

SECURITY_HEADERS: list[tuple[bytes, bytes]] = [
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
]


def hostname_of(host_header: str) -> str:
    """``"localhost:8000"`` -> ``"localhost"``; ``"[::1]:8000"`` -> ``"::1"``."""
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end > 0 else host
    if host.count(":") == 1:
        return host.split(":", 1)[0]
    return host


def default_allowed_hosts(bind_host: str) -> set[str] | None:
    """Host-header allow-list for a bind address (``None`` = accept any host)."""
    if hostname_of(bind_host) in LOOPBACK_HOSTS or bind_host in LOOPBACK_HOSTS:
        # "testserver" is the Host that Starlette's TestClient sends.
        return LOOPBACK_HOSTS | {"testserver"}
    return None


class SecurityMiddleware:
    """Pure-ASGI middleware (safe for streaming responses)."""

    def __init__(self, app: Any, *, allowed_hosts: Iterable[str] | None, allowed_origins: Iterable[str] = ()):
        self.app = app
        self.allowed_hosts = None if allowed_hosts is None else {h.lower() for h in allowed_hosts}
        self.allowed_origins = {o.rstrip("/").lower() for o in allowed_origins}

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        host = headers.get("host", "")
        if self.allowed_hosts is not None and "*" not in self.allowed_hosts:
            if hostname_of(host) not in self.allowed_hosts:
                await _reject(send, 400, "invalid Host header")
                return
        if scope.get("method", "GET").upper() not in SAFE_METHODS:
            origin = headers.get("origin")
            if origin is not None:
                if not self._origin_ok(origin, host):
                    await _reject(send, 403, "cross-origin request blocked")
                    return
            elif headers.get("sec-fetch-site") == "cross-site":
                await _reject(send, 403, "cross-site request blocked")
                return

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                current = list(message.get("headers", []))
                present = {k.lower() for k, _ in current}
                current.extend((k, v) for k, v in SECURITY_HEADERS if k not in present)
                message = {**message, "headers": current}
            await send(message)

        await self.app(scope, receive, send_with_headers)

    def _origin_ok(self, origin: str, host: str) -> bool:
        origin = origin.strip().rstrip("/").lower()
        if origin in self.allowed_origins:
            return True
        if origin in ("", "null"):
            return False
        return urlsplit(origin).netloc == host.strip().lower()


async def _reject(send: Any, status: int, message: str) -> None:
    body = json.dumps({"error": message, "status": status}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
            + SECURITY_HEADERS,
        }
    )
    await send({"type": "http.response.body", "body": body})


_SECRET_NAME = re.compile(r"(key|token|secret|password|insttoken)$", re.I)


class SecretScrubber:
    """Replaces configured secret values in free text (error messages) with ``***``.

    Upstream errors are already redacted by :mod:`mosaic.http`; this is a second
    line of defence for messages that reach API responses. Only long values are
    scrubbed so short public credentials (DrugCentral's published password) do
    not mangle ordinary words.
    """

    MIN_LENGTH = 12

    def __init__(self, settings: Settings, extra_names: Iterable[str] = ()):
        values: set[str] = set()
        for name in type(settings).model_fields:
            if _SECRET_NAME.search(name):
                value = getattr(settings, name, None)
                if isinstance(value, str) and len(value) >= self.MIN_LENGTH:
                    values.add(value)
        for name in extra_names:
            if _SECRET_NAME.search(name):
                value = settings.env(name)
                if value and len(value) >= self.MIN_LENGTH:
                    values.add(value)
        self._values = sorted(values, key=len, reverse=True)

    def __call__(self, text: Any) -> Any:
        if not isinstance(text, str) or not self._values:
            return text
        for value in self._values:
            if value in text:
                text = text.replace(value, "***")
        return text
