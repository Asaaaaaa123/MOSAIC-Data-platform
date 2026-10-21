"""Shared async HTTP client used by every REST/GraphQL connector.

Provides per-source rate limiting, retries with backoff (429 / 5xx / network),
a SQLite response cache, and secret redaction so API keys never reach logs,
audit rows or tool output.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit, urlunsplit

import httpx

from . import __version__
from .cache import ResponseCache
from .config import Settings

log = logging.getLogger("mosaic.http")

# Query-string parameter names whose values are always redacted.
SECRET_PARAM_NAMES = {
    "api_key", "apikey", "api-key", "key", "token", "access_token", "insttoken",
    "x-apikey", "x-api-key", "password", "email", "mailto", "auth", "authorization",
}

# Header names containing one of these fragments carry credentials (Authorization,
# X-ApiKey, X-ELS-APIKey, X-ELS-Insttoken, x-api-key ...).
SECRET_HEADER_HINTS = ("key", "token", "auth", "secret", "password", "cookie")
# Settings fields / environment variables whose values are credentials.
_SECRET_SETTING = re.compile(r"(key|token|secret|password|insttoken)$", re.I)
_SECRET_ENV_NAMES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ZENODO_ACCESS_TOKEN", "SYNAPSE_AUTH_TOKEN")
# Shorter values are not scrubbed from free text (they would mangle ordinary words,
# e.g. DrugCentral's published password "dosage"); e-mail addresses always are.
MIN_SECRET_LENGTH = 8

RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}

ParamsType = Mapping[str, Any] | Sequence[tuple[str, Any]] | None


class SourceError(Exception):
    """An upstream source failed. ``message`` is safe to show to users and Claude."""

    def __init__(
        self,
        source: str,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        url: str | None = None,
    ):
        super().__init__(message)
        self.source = source
        self.message = message
        self.status = status
        self.retryable = retryable
        self.url = url

    def __str__(self) -> str:
        prefix = f"[{self.source}]"
        if self.status:
            prefix += f" HTTP {self.status}"
        return f"{prefix} {self.message}"


class NotConfiguredError(SourceError):
    """The connector is missing required settings (API key, credentials)."""


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    content: bytes
    url: str  # redacted
    from_cache: bool = False
    elapsed_ms: float = 0.0
    final_url: str | None = None  # redacted URL after redirects (None on cache hits)
    # Removes credentials from error text built from the body (set by HttpClient).
    scrub: Callable[[str], str] | None = field(default=None, repr=False, compare=False)

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.content)
        except json.JSONDecodeError as exc:
            snippet = self.text[:200].replace("\n", " ")
            if self.scrub is not None:
                snippet = self.scrub(snippet)
            raise ValueError(f"expected JSON from {self.url}, got: {snippet!r}") from exc


class _TokenBucket:
    """Async token bucket: ``rate`` requests/second with a burst of ``capacity``."""

    def __init__(self, rate: float, capacity: float | None = None):
        self.rate = max(rate, 0.01)
        self.capacity = capacity if capacity is not None else max(1.0, self.rate)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


def redact_url(url: str, params: ParamsType = None, secret_params: Iterable[str] = ()) -> str:
    """Return ``url`` (+ ``params``) as a string with secret values replaced by ***."""
    secrets = SECRET_PARAM_NAMES | {s.lower() for s in secret_params}
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if params:
        items = params.items() if isinstance(params, Mapping) else params
        query += [(k, "" if v is None else str(v)) for k, v in items]
    query = [(k, "***" if k.lower() in secrets else v) for k, v in query]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, safe="*,:"), parts.fragment))


def _is_secret_value(value: Any) -> bool:
    return isinstance(value, str) and (len(value) >= MIN_SECRET_LENGTH or ("@" in value and len(value) >= 3))


def settings_secret_values(settings: Settings) -> set[str]:
    """Credential values configured in ``settings`` (API keys, tokens, contact e-mail)."""
    values: set[str] = set()
    for name in type(settings).model_fields:
        if _SECRET_SETTING.search(name) or name == "biopha_contact_email":
            value = getattr(settings, name, None)
            if _is_secret_value(value):
                values.add(value)
    env = getattr(settings, "env", None)
    if callable(env):
        for name in _SECRET_ENV_NAMES:
            try:
                value = env(name)
            except Exception:  # noqa: BLE001 - best effort
                value = None
            if _is_secret_value(value):
                values.add(value)
    return values


def request_secret_values(
    params: Iterable[tuple[str, str]] | None,
    headers: Mapping[str, str] | None,
    secret_params: Iterable[str] = (),
) -> set[str]:
    """Credential values sent with one request (secret query params and auth headers)."""
    names = SECRET_PARAM_NAMES | {s.lower() for s in secret_params}
    values = {v for k, v in (params or []) if k.lower() in names and _is_secret_value(v)}
    for name, value in (headers or {}).items():
        if not isinstance(value, str) or not any(hint in name.lower() for hint in SECRET_HEADER_HINTS):
            continue
        for candidate in (value, value.split(" ", 1)[-1]):  # "Bearer <token>"
            if _is_secret_value(candidate.strip()):
                values.add(candidate.strip())
    return values


def scrub_secrets(text: str, secrets: Iterable[str]) -> str:
    """Replace every secret value (raw or URL-encoded) in ``text`` with ``***``."""
    if not text:
        return text
    variants: set[str] = set()
    for value in secrets:
        if value:
            variants.update({value, quote(value, safe=""), quote_plus(value, safe="")})
    for variant in sorted(variants, key=len, reverse=True):
        if variant in text:
            text = text.replace(variant, "***")
    return text


def _clean_params(params: ParamsType) -> list[tuple[str, str]] | None:
    if params is None:
        return None
    items = params.items() if isinstance(params, Mapping) else params
    out: list[tuple[str, str]] = []
    for key, value in items:
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        out.append((key, str(value)))
    return out


class HttpClient:
    def __init__(
        self,
        settings: Settings,
        cache: ResponseCache | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings = settings
        self.cache = cache
        contact = settings.biopha_contact_email
        agent = f"Mosaic/{__version__} (biomedical research platform)"
        if contact:
            agent += f" mailto:{contact}"
        self._client = httpx.AsyncClient(
            timeout=settings.biopha_http_timeout,
            follow_redirects=True,
            headers={"User-Agent": agent, "Accept": "application/json"},
            transport=transport,
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        )
        self._buckets: dict[str, _TokenBucket] = {}
        self._secrets = settings_secret_values(settings)

    def scrub(self, text: str, extra: Iterable[str] = ()) -> str:
        """Remove configured credentials (and ``extra`` values) from ``text``."""
        return scrub_secrets(text, self._secrets | set(extra))

    def set_rate(self, source: str, rate: float, capacity: float | None = None) -> None:
        self._buckets[source] = _TokenBucket(rate, capacity)

    def _bucket(self, source: str, rate: float | None) -> _TokenBucket:
        bucket = self._buckets.get(source)
        if bucket is None:
            bucket = _TokenBucket(rate or 5.0)
            self._buckets[source] = bucket
        return bucket

    @staticmethod
    def _cache_key(method: str, url: str, params: list[tuple[str, str]] | None, body: bytes | None) -> str:
        h = hashlib.sha256()
        h.update(method.upper().encode())
        h.update(url.encode())
        h.update(json.dumps(sorted(params or [])).encode())
        if body:
            h.update(body)
        return h.hexdigest()

    async def request(
        self,
        source: str,
        method: str,
        url: str,
        *,
        params: ParamsType = None,
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        data: Any = None,
        rate: float | None = None,
        cache_ttl: int | None = None,
        timeout: float | None = None,
        secret_params: Iterable[str] = (),
        retries: int = 3,
        allow_status: Iterable[int] = (),
        validate: Callable[[HttpResponse], None] | None = None,
    ) -> HttpResponse:
        """Perform a request. Raises :class:`SourceError` on failure.

        ``cache_ttl``: seconds to cache. ``None`` = settings default for GET,
        no caching for other methods. ``0`` disables caching for this call.
        ``allow_status``: non-2xx statuses returned instead of raised (e.g. 404).
        ``validate``: called on every 2xx response before it is cached; raise
        :class:`SourceError` to reject it (e.g. an HTML CAPTCHA page served with 200),
        so bad payloads never enter the cache.
        """
        method = method.upper()
        clean = _clean_params(params)
        body: bytes | None = None
        if json_body is not None:
            body = json.dumps(json_body, sort_keys=True).encode()
        elif isinstance(data, (bytes, str)):
            body = data.encode() if isinstance(data, str) else data
        elif data is not None:
            body = json.dumps(data, sort_keys=True, default=str).encode()
        safe_url = redact_url(url, clean, secret_params)
        allowed = set(allow_status)
        sent_secrets = request_secret_values(clean, headers, secret_params)

        def scrub(text: str) -> str:
            return self.scrub(text, sent_secrets)

        ttl = cache_ttl
        if ttl is None:
            ttl = self.cache.default_ttl if (self.cache and method == "GET") else 0
        use_cache = bool(self.cache and self.cache.enabled and ttl > 0)
        key = self._cache_key(method, url, clean, body) if use_cache else ""
        if use_cache:
            hit = self.cache.get(key)
            if hit is not None:
                return HttpResponse(hit.status, hit.headers, hit.content, safe_url, from_cache=True, scrub=scrub)

        bucket = self._bucket(source, rate)
        attempt = 0
        while True:
            attempt += 1
            await bucket.acquire()
            started = time.perf_counter()
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=clean,
                    headers=dict(headers or {}),
                    json=json_body,
                    data=data if json_body is None else None,
                    timeout=timeout or self.settings.biopha_http_timeout,
                )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                if attempt <= retries:
                    await asyncio.sleep(min(2 ** attempt, 20))
                    continue
                raise SourceError(
                    source, scrub(f"network error: {type(exc).__name__}: {exc}"), retryable=True, url=safe_url
                ) from exc
            elapsed = (time.perf_counter() - started) * 1000
            status = response.status_code

            if status in RETRY_STATUSES and attempt <= retries:
                await asyncio.sleep(self._retry_delay(response, attempt))
                continue

            content = response.content
            resp_headers = {k.lower(): v for k, v in response.headers.items()}
            if 200 <= status < 300 or status in allowed:
                result = HttpResponse(
                    status,
                    resp_headers,
                    content,
                    safe_url,
                    elapsed_ms=elapsed,
                    final_url=redact_url(str(response.url), None, secret_params),
                    scrub=scrub,
                )
                if 200 <= status < 300:
                    if validate is not None:
                        validate(result)
                    if use_cache:
                        keep = {
                            k: v
                            for k, v in resp_headers.items()
                            if k in ("content-type", "link") or k.startswith(("x-total", "x-wp-total"))
                        }
                        self.cache.set(key, source, status, keep, content, ttl)
                return result

            # Scrub before truncating so a key cut at the 300-char boundary cannot leak a prefix.
            snippet = scrub(content.decode("utf-8", errors="replace")[:4096])[:300].replace("\n", " ").strip()
            raise SourceError(
                source,
                f"{method} {safe_url} failed: {snippet or response.reason_phrase}",
                status=status,
                retryable=status in RETRY_STATUSES,
                url=safe_url,
            )

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), 30.0)
            except ValueError:
                pass
        return min(1.5 * 2 ** (attempt - 1), 20.0)

    async def get_json(self, source: str, url: str, **kwargs: Any) -> Any:
        return (await self.request(source, "GET", url, **kwargs)).json()

    async def post_json(self, source: str, url: str, json_body: Any = None, **kwargs: Any) -> Any:
        return (await self.request(source, "POST", url, json_body=json_body, **kwargs)).json()

    async def graphql(
        self,
        source: str,
        url: str,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        cache_ttl: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """POST a GraphQL query; returns ``data``. GraphQL errors raise SourceError."""
        ttl = (self.cache.default_ttl if self.cache else 0) if cache_ttl is None else cache_ttl
        payload = await self.post_json(
            source, url, {"query": query, "variables": variables or {}}, cache_ttl=ttl, **kwargs
        )
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors and not payload.get("data"):
            message = "; ".join(str(e.get("message", e)) for e in errors[:3])
            raise SourceError(source, self.scrub(f"GraphQL error: {message}"))
        return payload.get("data") or {}

    async def aclose(self) -> None:
        await self._client.aclose()


@dataclass
class Page:
    """Helper for offset pagination bookkeeping in connectors."""

    offset: int = 0
    limit: int = 10
    extra: dict[str, Any] = field(default_factory=dict)

    def next_cursor(self, total: int | None, returned: int) -> str | None:
        nxt = self.offset + returned
        if returned == 0 or (total is not None and nxt >= total):
            return None
        return str(nxt)
