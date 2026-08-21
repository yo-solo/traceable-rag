from __future__ import annotations

from fastapi.testclient import TestClient

from traceable_rag.readiness import liveness_payload, readiness_payload
from traceable_rag.store import EvidenceStore
from traceable_rag.web_app import create_app


class Worker:
    def __init__(self, *, alive: bool, fatal_error_type: str | None = None):
        self.is_alive = alive
        self.fatal_error_type = fatal_error_type


def test_liveness_does_not_depend_on_database_or_providers():
    payload = liveness_payload()

    assert payload["ok"] is True
    assert payload["status"] == "live"
    assert payload["version"]


def test_readiness_passes_for_writable_store_and_live_worker(tmp_path):
    store = EvidenceStore(tmp_path / "runtime.db")

    ready, payload = readiness_payload(
        store_factory=lambda: store,
        managed_root=tmp_path / "managed",
        worker=Worker(alive=True),
        worker_required=True,
        minimum_free_bytes=0,
        provider_infrastructure_check=lambda: True,
        normalization_version="current",
    )

    assert ready is True
    assert payload["status"] == "ready"
    assert all(check["ok"] for check in payload["checks"])


def test_readiness_reports_dead_worker_without_exposing_exception_detail(tmp_path):
    store = EvidenceStore(tmp_path / "runtime.db")

    ready, payload = readiness_payload(
        store_factory=lambda: store,
        managed_root=tmp_path / "managed",
        worker=Worker(alive=False, fatal_error_type="PrivateTokenError"),
        worker_required=True,
        minimum_free_bytes=0,
        normalization_version="current",
    )

    assert ready is False
    worker = next(check for check in payload["checks"] if check["name"] == "worker")
    assert worker == {
        "name": "worker",
        "ok": False,
        "message": "文档处理 Worker 未运行。",
    }
    assert "PrivateTokenError" not in str(payload)


def test_readiness_blocks_while_content_upgrade_is_pending(tmp_path):
    store = EvidenceStore(tmp_path / "runtime.db")
    with store.connect() as connection:
        connection.execute(
            """
            insert into documents (
                id, knowledge_base_id, original_filename, display_name,
                display_name_key, file_hash, source_type, processing_status,
                parser_provider, normalization_version
            ) values (
                'document', 'default', 'document.pdf', 'Document',
                'document', 'hash', 'pdf', 'ready', 'mineru', 'old'
            )
            """
        )
        connection.execute(
            """
            insert into processing_jobs (
                id, knowledge_base_id, document_id, operation,
                status, stage, attempt
            ) values ('job', 'default', 'document',
                      'normalize_upgrade:next', 'queued', 'queued', 1)
            """
        )

    ready, payload = readiness_payload(
        store_factory=lambda: store,
        managed_root=tmp_path / "managed",
        worker=None,
        worker_required=False,
        minimum_free_bytes=0,
        normalization_version="next",
    )

    assert ready is False
    upgrade = next(
        check for check in payload["checks"] if check["name"] == "content_upgrade"
    )
    assert upgrade["ok"] is False
    assert "1" in upgrade["message"]


def test_readiness_blocks_when_latest_content_upgrade_failed(tmp_path):
    store = EvidenceStore(tmp_path / "runtime.db")
    with store.connect() as connection:
        connection.execute(
            """
            insert into documents (
                id, knowledge_base_id, original_filename, display_name,
                display_name_key, file_hash, source_type, processing_status,
                parser_provider, normalization_version
            ) values (
                'document', 'default', 'document.pdf', 'Document',
                'document', 'hash', 'pdf', 'ready', 'mineru', 'old'
            )
            """
        )
        connection.execute(
            """
            insert into processing_jobs (
                id, knowledge_base_id, document_id, operation,
                status, stage, attempt, retryable
            ) values ('job', 'default', 'document',
                      'normalize_upgrade:next', 'failed', 'normalize', 1, 0)
            """
        )

    ready, payload = readiness_payload(
        store_factory=lambda: store,
        managed_root=tmp_path / "managed",
        worker=None,
        worker_required=False,
        minimum_free_bytes=0,
        normalization_version="next",
    )

    assert ready is False
    upgrade = next(
        check for check in payload["checks"] if check["name"] == "content_upgrade"
    )
    assert upgrade["ok"] is False
    assert "失败" in upgrade["message"]


def test_readiness_blocks_when_provider_infrastructure_is_unavailable(tmp_path):
    store = EvidenceStore(tmp_path / "runtime.db")

    ready, payload = readiness_payload(
        store_factory=lambda: store,
        managed_root=tmp_path / "managed",
        worker=None,
        worker_required=False,
        minimum_free_bytes=0,
        provider_infrastructure_check=lambda: False,
        normalization_version="current",
    )

    assert ready is False
    provider = next(
        check
        for check in payload["checks"]
        if check["name"] == "provider_infrastructure"
    )
    assert provider["ok"] is False
    assert "主密钥" in provider["message"]


def test_readyz_requires_deployment_provider_secrets_and_store(tmp_path):
    admin_token = tmp_path / "admin-token"
    admin_token.write_text("a" * 64, encoding="utf-8")
    missing_key = tmp_path / "missing-master-key"
    app = create_app(
        db_path=tmp_path / "runtime.db",
        managed_root=tmp_path / "managed",
        provider_store_path=tmp_path / "providers.db",
        provider_master_key_file=missing_key,
        provider_admin_token_file=admin_token,
        trusted_hosts=("testserver",),
        start_processing_worker=False,
    )

    unavailable = TestClient(app).get("/readyz")

    assert unavailable.status_code == 503
    provider = next(
        check
        for check in unavailable.json()["checks"]
        if check["name"] == "provider_infrastructure"
    )
    assert provider["ok"] is False

    missing_key.write_bytes(b"k" * 32)
    available = TestClient(app).get("/readyz")

    assert available.status_code == 200
    assert available.json()["ok"] is True
