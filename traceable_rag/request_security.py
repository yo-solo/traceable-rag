from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_HOSTNAME = re.compile(
    r"(?=.{1,253}\Z)"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z",
    re.IGNORECASE,
)


def normalize_trusted_hosts(value: str | Iterable[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    candidates = value.split(",") if isinstance(value, str) else value
    normalized: list[str] = []
    for candidate in candidates:
        host = str(candidate).strip().rstrip(".").casefold()
        if not host:
            continue
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not _HOSTNAME.fullmatch(host):
                raise ValueError("trusted hosts must contain exact IP addresses or DNS names")
        if host not in normalized:
            normalized.append(host)
    if not normalized:
        raise ValueError("trusted hosts cannot be empty")
    return tuple(normalized)


class BrowserRequestBoundaryMiddleware:
    """Reject DNS rebinding and browser cross-origin write side effects."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        trusted_hosts: Iterable[str] = (),
    ) -> None:
        self.app = app
        self.trusted_hosts = (
            frozenset(normalize_trusted_hosts(tuple(trusted_hosts)))
            if trusted_hosts
            else frozenset()
        )

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = _headers(scope)
        host_header = headers.get("host")
        host = _parse_host_header(host_header)
        send_with_security_headers = _security_header_sender(send)

        if self.trusted_hosts and (
            host is None or host[0] not in self.trusted_hosts
        ):
            await _reject(
                scope,
                receive,
                send_with_security_headers,
                status_code=400,
                code="untrusted_host",
                message="Request Host is not allowed for this deployment.",
            )
            return

        if scope.get("method", "GET").upper() not in _SAFE_METHODS:
            if not _browser_write_is_same_origin(
                scope=scope,
                headers=headers,
                request_host=host,
            ):
                await _reject(
                    scope,
                    receive,
                    send_with_security_headers,
                    status_code=403,
                    code="cross_origin_write_forbidden",
                    message="Browser write requests must be same-origin.",
                )
                return

        await self.app(scope, receive, send_with_security_headers)


def _headers(scope: Scope) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw_name, raw_value in scope.get("headers", []):
        name = raw_name.decode("latin-1").casefold()
        if name not in headers:
            headers[name] = raw_value.decode("latin-1").strip()
    return headers


def _parse_host_header(value: str | None) -> tuple[str, int | None] | None:
    if value is None or not value or any(ord(character) < 32 for character in value):
        return None
    try:
        parsed = urlsplit(f"//{value}")
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or not parsed.hostname
    ):
        return None
    return parsed.hostname.rstrip(".").casefold(), port


def _browser_write_is_same_origin(
    *,
    scope: Scope,
    headers: dict[str, str],
    request_host: tuple[str, int | None] | None,
) -> bool:
    origin = headers.get("origin")
    if origin is not None:
        if origin.casefold() == "null":
            return False
        return _url_matches_request(origin, scope=scope, request_host=request_host)

    referer = headers.get("referer")
    if referer is not None:
        return _url_matches_request(referer, scope=scope, request_host=request_host)

    fetch_site = headers.get("sec-fetch-site", "").casefold()
    if fetch_site in {"cross-site", "same-site"}:
        return False
    return True


def _url_matches_request(
    value: str,
    *,
    scope: Scope,
    request_host: tuple[str, int | None] | None,
) -> bool:
    if request_host is None:
        return False
    try:
        parsed = urlsplit(value)
        parsed_port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.hostname
    ):
        return False
    request_scheme = str(scope.get("scheme", "http")).casefold()
    if parsed.scheme.casefold() != request_scheme:
        return False
    request_hostname, request_port = request_host
    return (
        parsed.hostname.rstrip(".").casefold() == request_hostname
        and _effective_port(parsed.scheme, parsed_port)
        == _effective_port(request_scheme, request_port)
    )


def _effective_port(scheme: str, port: int | None) -> int | None:
    if port is not None:
        return port
    if scheme.casefold() == "http":
        return 80
    if scheme.casefold() == "https":
        return 443
    return None


def _security_header_sender(send: Send) -> Send:
    async def send_with_headers(message: Message) -> None:
        if message["type"] == "http.response.start":
            headers = list(message.get("headers", []))
            existing = {name.lower() for name, _value in headers}
            additions = (
                (b"content-security-policy", b"frame-ancestors 'none'"),
                (b"referrer-policy", b"no-referrer"),
                (b"x-content-type-options", b"nosniff"),
                (b"x-frame-options", b"DENY"),
            )
            for name, value in additions:
                if name not in existing:
                    headers.append((name, value))
            message = {**message, "headers": headers}
        await send(message)

    return send_with_headers


async def _reject(
    scope: Scope,
    receive: Receive,
    send: Send,
    *,
    status_code: int,
    code: str,
    message: str,
) -> None:
    response = JSONResponse(
        status_code=status_code,
        content={"detail": {"code": code, "message": message}},
    )
    await response(scope, receive, send)
