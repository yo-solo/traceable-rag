from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


API_WRITE_REQUEST_MAX_BYTES = 1024 * 1024
_API_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class ApiWriteRequestBodyLimitMiddleware:
    """Reject oversized API writes before framework parsing or route handling."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int = API_WRITE_REQUEST_MAX_BYTES,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not _is_limited_api_write(scope):
            await self.app(scope, receive, send)
            return
        if _declared_body_exceeds_limit(scope, self.max_bytes):
            await self._reject(scope, receive, send)
            return

        buffered: list[Message] = []
        total = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    await self._reject(scope, receive, send)
                    return
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                break

        async def replay_receive() -> Message:
            if buffered:
                return buffered.pop(0)
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content={
                "detail": {
                    "code": "request_body_too_large",
                    "message": "API write request body exceeds the 1 MiB limit.",
                }
            },
        )
        await response(scope, receive, send)


def _is_limited_api_write(scope: Scope) -> bool:
    if scope["type"] != "http" or scope.get("method") not in _API_WRITE_METHODS:
        return False
    path = scope.get("path", "")
    if path != "/api" and not path.startswith("/api/"):
        return False
    return not _is_managed_document_upload(scope.get("method"), path)


def _is_managed_document_upload(method: str | None, path: str) -> bool:
    parts = path.split("/")
    return (
        method == "POST"
        and len(parts) == 6
        and parts[1] == "api"
        and parts[2] == "knowledge-bases"
        and bool(parts[3])
        and parts[4] == "documents"
        and parts[5] == "upload"
    )


def _declared_body_exceeds_limit(scope: Scope, max_bytes: int) -> bool:
    for name, value in scope.get("headers", []):
        if name.lower() != b"content-length":
            continue
        try:
            declared_bytes = int(value)
        except (TypeError, ValueError):
            continue
        if declared_bytes > max_bytes:
            return True
    return False
