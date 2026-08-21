from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from tests.test_knowledge_base_api import make_client
from traceable_rag.request_limits import API_WRITE_REQUEST_MAX_BYTES
from traceable_rag.store import DEFAULT_KNOWLEDGE_BASE_ID
from traceable_rag.web_app import create_app


def _call_asgi_json(
    app,
    *,
    path: str,
    chunks: list[bytes],
    content_length: int | None,
    content_type: bytes | None = b"application/json",
) -> tuple[int, dict, int]:
    headers = []
    if content_type is not None:
        headers.append((b"content-type", content_type))
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    messages = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]
    sent: list[dict] = []
    receive_calls = 0

    async def receive() -> dict:
        nonlocal receive_calls
        receive_calls += 1
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))
    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return start["status"], json.loads(body), receive_calls


@pytest.mark.parametrize(
    "content_type",
    [None, b"text/plain", b"multipart/form-data; boundary=fake"],
)
def test_api_write_rejects_oversized_content_length_regardless_of_content_type(
    tmp_path, content_type
):
    app = create_app(
        db_path=tmp_path / "traceable.db",
        managed_root=tmp_path / "managed",
        settings_path=tmp_path / "settings.json",
    )
    sensitive_marker = "secret-body-marker"
    body = b"x" * API_WRITE_REQUEST_MAX_BYTES + sensitive_marker.encode()

    status, payload, receive_calls = _call_asgi_json(
        app,
        path="/api/retrieval",
        chunks=[body],
        content_length=len(body),
        content_type=content_type,
    )

    assert status == 413
    assert payload["detail"]["code"] == "request_body_too_large"
    assert isinstance(payload["detail"]["message"], str)
    assert sensitive_marker not in json.dumps(payload)
    assert receive_calls == 0


@pytest.mark.parametrize("content_length", [None, 16])
@pytest.mark.parametrize("content_type", [None, b"text/plain"])
def test_api_write_rejects_chunked_body_when_length_or_content_type_is_untrusted(
    tmp_path, content_length, content_type
):
    app = create_app(
        db_path=tmp_path / "traceable.db",
        managed_root=tmp_path / "managed",
        settings_path=tmp_path / "settings.json",
    )
    sensitive_marker = "secret-body-marker"
    body = (
        b'{"query":"'
        + b"x" * API_WRITE_REQUEST_MAX_BYTES
        + sensitive_marker.encode()
        + b'","knowledge_base_ids":["default"]}'
    )

    status, payload, _ = _call_asgi_json(
        app,
        path="/api/retrieval",
        chunks=[body[:600_000], body[600_000:1_000_000], body[1_000_000:]],
        content_length=content_length,
        content_type=content_type,
    )

    assert status == 413
    assert payload["detail"]["code"] == "request_body_too_large"
    assert sensitive_marker not in json.dumps(payload)


def test_api_write_request_limit_preserves_normal_json_routes(tmp_path):
    client = TestClient(
        create_app(
            db_path=tmp_path / "traceable.db",
            managed_root=tmp_path / "managed",
            settings_path=tmp_path / "settings.json",
        )
    )
    scoped_query = {
        "query": "normal",
        "knowledge_base_ids": [DEFAULT_KNOWLEDGE_BASE_ID],
    }

    assert client.post("/api/retrieval", json=scoped_query).status_code == 409
    assert client.post(
        "/api/knowledge-bases",
        json={"name": "normal-write"},
    ).status_code == 201
    assert client.patch("/api/settings", json={"default_top_k": 6}).status_code == 200


def test_api_write_request_limit_does_not_apply_to_managed_uploads(tmp_path):
    client, _, service = make_client(tmp_path)

    response = client.post(
        f"/api/knowledge-bases/{DEFAULT_KNOWLEDGE_BASE_ID}/documents/upload",
        files={
            "file": (
                "large-but-allowed.pdf",
                b"x" * (API_WRITE_REQUEST_MAX_BYTES + 1),
                "application/pdf",
            )
        },
    )

    assert response.status_code == 201
    assert service.register_calls == [
        (DEFAULT_KNOWLEDGE_BASE_ID, "large-but-allowed.pdf")
    ]
