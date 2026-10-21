"""API error type and the SourceError -> HTTP status mapping."""

from __future__ import annotations

from typing import Any

from ..http import NotConfiguredError, SourceError


class ApiError(Exception):
    """Raised by endpoints; rendered as ``{"error": message, "status": code, ...extra}``."""

    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {"error": self.message, "status": self.status, **self.extra}


def classify_source_error(exc: SourceError) -> tuple[int, str]:
    """Map a connector failure to ``(http_status, kind)``.

    * 503 ``not_configured``  - the source is missing an API key / credentials
    * 404 ``unknown_source``  - no connector with that name
    * 400 ``unknown_tool`` / ``invalid_params`` - the caller sent a bad request
    * 502 ``timeout`` / ``connector_error`` / ``upstream_error`` - the source failed
    """
    if isinstance(exc, NotConfiguredError):
        return 503, "not_configured"
    message = exc.message or ""
    if message.startswith("unknown source"):
        return 404, "unknown_source"
    if message.startswith("unknown tool"):
        return 400, "unknown_tool"
    if message.startswith("invalid parameters"):
        return 400, "invalid_params"
    if "timed out" in message:
        return 502, "timeout"
    if message.startswith("internal connector error"):
        return 502, "connector_error"
    return 502, "upstream_error"


def source_api_error(exc: SourceError, scrub: Any = None) -> ApiError:
    status, kind = classify_source_error(exc)
    message = str(exc)
    if scrub is not None:
        message = scrub(message)
    return ApiError(
        status,
        message,
        source=exc.source,
        kind=kind,
        upstream_status=exc.status,
        retryable=bool(exc.retryable),
    )
