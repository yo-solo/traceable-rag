import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from traceable_rag.web_app import create_app
from traceable_rag.store import DEFAULT_KNOWLEDGE_BASE_ID


INDEX_MARKER = "traceable-rag-spa-root"


def test_web_app_uses_runtime_path_defaults_from_dotenv(tmp_path, monkeypatch):
    database_path = tmp_path / "runtime" / "traceable.db"
    managed_root = tmp_path / "runtime" / "managed"
    settings_path = tmp_path / "runtime" / "settings.json"
    provider_store_path = tmp_path / "runtime" / "providers.db"
    master_key_file = tmp_path / "runtime" / "provider-master-key"
    static_dir = tmp_path / "runtime" / "dist"
    static_dir.mkdir(parents=True)
    master_key_file.write_bytes(b"k" * 32)
    (static_dir / "index.html").write_text(INDEX_MARKER, encoding="utf-8")
    (tmp_path / ".env").write_text(
        "\n".join(
            (
                f"TRACEABLE_RAG_DATABASE_PATH={database_path}",
                f"TRACEABLE_RAG_MANAGED_STORAGE_ROOT={managed_root}",
                f"TRACEABLE_RAG_SETTINGS_PATH={settings_path}",
                f"TRACEABLE_RAG_STATIC_DIR={static_dir}",
                f"TRACEABLE_RAG_PROVIDER_STORE_PATH={provider_store_path}",
                f"TRACEABLE_RAG_MASTER_KEY_FILE={master_key_file}",
                "MINERU_TOKEN=configured-for-test",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    client = TestClient(create_app(runtime_root=tmp_path))

    assert client.get("/api/health").status_code == 200
    assert client.patch("/api/settings", json={"default_top_k": 6}).status_code == 200
    upload = client.post(
        f"/api/knowledge-bases/{DEFAULT_KNOWLEDGE_BASE_ID}/documents/upload",
        files={"file": ("runtime.docx", b"docx", "application/octet-stream")},
    )
    assert upload.status_code == 201
    assert client.get("/").text == INDEX_MARKER
    assert database_path.is_file()
    assert settings_path.is_file()
    assert list(managed_root.rglob("*.docx"))


def test_custom_web_app_paths_override_injected_runtime_config(tmp_path):
    runtime_root = tmp_path / "runtime-root"
    runtime_root.mkdir()
    runtime_static = runtime_root / "runtime-dist"
    runtime_static.mkdir()
    (runtime_static / "index.html").write_text("runtime-static", encoding="utf-8")
    (runtime_root / ".env").write_text(
        "\n".join(
            (
                f"TRACEABLE_RAG_DATABASE_PATH={runtime_root / 'runtime.db'}",
                f"TRACEABLE_RAG_MANAGED_STORAGE_ROOT={runtime_root / 'managed'}",
                f"TRACEABLE_RAG_SETTINGS_PATH={runtime_root / 'settings.json'}",
                f"TRACEABLE_RAG_STATIC_DIR={runtime_static}",
            )
        ),
        encoding="utf-8",
    )
    custom_static = tmp_path / "custom-dist"
    custom_static.mkdir()
    (custom_static / "index.html").write_text(INDEX_MARKER, encoding="utf-8")
    custom_db = tmp_path / "custom.db"
    custom_settings = tmp_path / "custom-settings.json"

    client = TestClient(
        create_app(
            db_path=custom_db,
            managed_root=tmp_path / "custom-managed",
            settings_path=custom_settings,
            static_dir=custom_static,
            runtime_root=runtime_root,
        )
    )

    assert client.get("/api/health").status_code == 200
    assert client.patch("/api/settings", json={"default_top_k": 6}).status_code == 200
    assert client.get("/").text == INDEX_MARKER
    assert custom_db.is_file()
    assert custom_settings.is_file()
    assert not (runtime_root / "runtime.db").exists()


def test_explicit_web_app_paths_ignore_unrelated_invalid_cwd_dotenv(
    tmp_path, monkeypatch
):
    bad_cwd = tmp_path / "bad-cwd"
    bad_cwd.mkdir()
    (bad_cwd / ".env").write_bytes(b"SECRET=\xff\n")
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(INDEX_MARKER, encoding="utf-8")
    monkeypatch.chdir(bad_cwd)

    app = create_app(
        db_path=tmp_path / "explicit.db",
        managed_root=tmp_path / "managed",
        settings_path=tmp_path / "settings.json",
        static_dir=static_dir,
    )

    assert TestClient(app).get("/api/health").status_code == 200


def test_loopback_deployment_rejects_dns_rebinding_and_cross_origin_writes(tmp_path):
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(INDEX_MARKER, encoding="utf-8")
    app = create_app(
        db_path=tmp_path / "request-boundary.db",
        managed_root=tmp_path / "managed",
        settings_path=tmp_path / "settings.json",
        static_dir=static_dir,
        start_processing_worker=False,
        trusted_hosts=("127.0.0.1", "localhost"),
    )
    client = TestClient(app)

    rebinding = client.post(
        "/api/knowledge-bases",
        headers={
            "Host": "attacker.example:8000",
            "Origin": "http://attacker.example:8000",
        },
        json={"name": "rebound"},
    )
    assert rebinding.status_code == 400
    assert rebinding.json()["detail"]["code"] == "untrusted_host"

    cross_origin = client.post(
        "/api/knowledge-bases",
        headers={
            "Host": "127.0.0.1:8000",
            "Origin": "http://attacker.example:8000",
        },
        json={"name": "cross-origin"},
    )
    assert cross_origin.status_code == 403
    assert cross_origin.json()["detail"]["code"] == "cross_origin_write_forbidden"

    hostile_referer = client.post(
        "/api/knowledge-bases",
        headers={
            "Host": "127.0.0.1:8000",
            "Referer": "http://attacker.example/form",
        },
        json={"name": "hostile-referer"},
    )
    assert hostile_referer.status_code == 403

    hostile_fetch_metadata = client.post(
        "/api/knowledge-bases",
        headers={
            "Host": "127.0.0.1:8000",
            "Sec-Fetch-Site": "same-site",
        },
        json={"name": "hostile-fetch-metadata"},
    )
    assert hostile_fetch_metadata.status_code == 403

    cross_origin_upload = client.post(
        f"/api/knowledge-bases/{DEFAULT_KNOWLEDGE_BASE_ID}/documents/upload",
        headers={
            "Host": "127.0.0.1:8000",
            "Origin": "http://attacker.example:8000",
        },
        files={"file": ("hostile.txt", b"blocked", "text/plain")},
    )
    assert cross_origin_upload.status_code == 403
    assert not list((tmp_path / "managed").rglob("hostile.txt"))

    same_origin = client.post(
        "/api/knowledge-bases",
        headers={
            "Host": "127.0.0.1:8000",
            "Origin": "http://127.0.0.1:8000",
        },
        json={"name": "same-origin"},
    )
    assert same_origin.status_code == 201

    cli_request = client.post(
        "/api/knowledge-bases",
        headers={"Host": "localhost:8000"},
        json={"name": "agent-cli"},
    )
    assert cli_request.status_code == 201

    response = client.get(
        "/api/knowledge-bases",
        headers={"Host": "127.0.0.1:8000"},
    )
    assert response.status_code == 200
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["content-security-policy"] == "frame-ancestors 'none'"
    names = {item["name"] for item in response.json()["knowledge_bases"]}
    assert "rebound" not in names
    assert "cross-origin" not in names
    assert "hostile-referer" not in names
    assert "hostile-fetch-metadata" not in names
    assert {"same-origin", "agent-cli"} <= names


def test_web_app_module_import_isolated_from_bad_dotenv_but_default_entry_fails_closed(
    tmp_path,
):
    bad_cwd = tmp_path / "bad-cwd"
    bad_cwd.mkdir()
    (bad_cwd / ".env").write_bytes(b"SECRET=do-not-leak-\xff\n")
    package_root = Path(__file__).parents[1]
    script = f"""
from pathlib import Path
from traceable_rag.runtime_config import RuntimeConfigError
from traceable_rag.web_app import app, create_app
explicit = create_app(
    db_path=Path({str(tmp_path / 'explicit.db')!r}),
    managed_root=Path({str(tmp_path / 'managed')!r}),
    settings_path=Path({str(tmp_path / 'settings.json')!r}),
    static_dir=Path({str(tmp_path / 'dist')!r}),
)
assert explicit.openapi()['info']['title'] == 'Traceable RAG Retrieval API'
try:
    app.openapi()
except RuntimeConfigError as exc:
    assert 'must be UTF-8' in str(exc)
    assert 'do-not-leak' not in str(exc)
else:
    raise AssertionError('default app entry must fail closed')
print('isolated-import-ok')
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(package_root)

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=bad_cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "isolated-import-ok\n"
    assert "do-not-leak" not in result.stderr


@pytest.fixture
def web_client(tmp_path: Path) -> TestClient:
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(
        f"<!doctype html><html><body><div id='{INDEX_MARKER}'></div></body></html>",
        encoding="utf-8",
    )
    app = create_app(
        db_path=tmp_path / "traceable_v2.db",
        static_dir=static_dir,
        managed_root=tmp_path / "managed",
    )
    return TestClient(app)


@pytest.mark.parametrize(
    "route",
    [
        "/knowledge-bases",
        "/knowledge-bases/kb-1",
        "/knowledge-bases/kb-1/documents/doc-1",
        "/retrieval",
        "/sources/src_example",
        "/settings",
    ],
)
def test_formal_spa_routes_support_direct_refresh(
    web_client: TestClient, route: str
) -> None:
    response = web_client.get(route)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert INDEX_MARKER in response.text


def test_unknown_spa_route_is_left_for_the_client_router(
    web_client: TestClient,
) -> None:
    response = web_client.get("/missing-page")

    assert response.status_code == 200
    assert INDEX_MARKER in response.text


@pytest.mark.parametrize(
    "route",
    [
        "/missing.js",
        "/favicon.ico",
        "/styles/missing.css",
    ],
)
def test_missing_static_resources_do_not_fall_back_to_spa(
    web_client: TestClient, route: str
) -> None:
    response = web_client.get(route, headers={"accept": "text/html"})

    assert response.status_code == 404
    assert INDEX_MARKER not in response.text


def test_non_html_request_does_not_fall_back_to_spa(web_client: TestClient) -> None:
    response = web_client.get(
        "/missing-page", headers={"accept": "application/json"}
    )

    assert response.status_code == 404
    assert INDEX_MARKER not in response.text


def test_non_get_request_does_not_fall_back_to_spa(web_client: TestClient) -> None:
    response = web_client.post("/missing-page")

    assert response.status_code == 405
    assert INDEX_MARKER not in response.text


def test_path_traversal_does_not_fall_back_to_spa(web_client: TestClient) -> None:
    response = web_client.get(
        "/%2e%2e/secret.txt", headers={"accept": "text/html"}
    )

    assert response.status_code == 404
    assert INDEX_MARKER not in response.text


def test_api_404_is_not_swallowed_by_spa_fallback(web_client: TestClient) -> None:
    response = web_client.get("/api/not-a-real-endpoint")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert INDEX_MARKER not in response.text


@pytest.mark.parametrize("method", ["HEAD", "POST", "OPTIONS"])
def test_legal_api_wrong_methods_keep_standard_405_and_allow_semantics(
    web_client: TestClient, method: str
) -> None:
    response = web_client.request(method, "/api/health")

    assert response.status_code == 405
    assert "GET" in response.headers["allow"]


def test_legacy_tombstones_are_precise_instead_of_a_generic_api_catch_all(
    web_client: TestClient,
) -> None:
    route_paths = {getattr(route, "path", None) for route in web_client.app.routes}

    assert "/api/{full_path:path}" not in route_paths
    assert "/api/documents" in route_paths
    assert "/api/documents/{full_path:path}" in route_paths
    assert "/api/retrieval/search" in route_paths
    assert "/api/rag/ask" in route_paths
    assert "/api/agent/investigate" in route_paths


@pytest.mark.parametrize(
    "route",
    [
        "/api/documents",
        "/api/documents/doc-a/blocks",
        "/api/retrieval/search",
        "/api/rag/ask",
        "/api/agent/investigate",
        "/kb/documents",
        "/kb/documents/doc-a",
    ],
)
def test_legacy_tombstones_return_404_for_head_requests(
    web_client: TestClient, route: str
) -> None:
    response = web_client.head(route)

    assert response.status_code == 404
    assert INDEX_MARKER not in response.text


@pytest.mark.parametrize(
    ("method", "route"),
    [
        ("GET", "/api/documents"),
        ("POST", "/api/documents/upload"),
        ("GET", "/api/documents/doc-a"),
        ("GET", "/api/documents/doc-a/file"),
        ("GET", "/api/documents/doc-a/blocks"),
        ("POST", "/api/documents/doc-a/parse"),
        ("POST", "/api/documents/doc-a/index"),
        ("POST", "/api/retrieval/search"),
        ("POST", "/api/rag/ask"),
        ("POST", "/api/agent/investigate"),
        ("GET", "/kb/documents"),
        ("GET", "/kb/documents/doc-a"),
    ],
)
def test_legacy_http_and_knowledge_base_routes_are_not_compatible(
    web_client: TestClient, method: str, route: str
) -> None:
    response = web_client.request(method, route)

    assert response.status_code == 404
    assert INDEX_MARKER not in response.text
