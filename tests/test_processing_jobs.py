from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from pypdf import PdfWriter
from fastapi.testclient import TestClient

from traceable_rag.document_processing import (
    DocumentProcessingService,
    MinerUConfigurationError,
    NormalizationSourceUnavailableError,
)
from traceable_rag.mineru_import import NORMALIZATION_VERSION
from traceable_rag.models import Document
from traceable_rag.processing_jobs import (
    ProcessingWorker,
    classify_processing_error,
    enqueue_document_processing,
    normalization_upgrade_operation,
    processing_error_diagnostic,
)
from traceable_rag.store import DEFAULT_KNOWLEDGE_BASE_ID, EvidenceStore
from traceable_rag.web_app import create_app


def make_document(store: EvidenceStore, tmp_path: Path, document_id: str) -> Document:
    source = tmp_path / f"{document_id}.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with source.open("wb") as target:
        writer.write(target)
    document = Document(
        id=document_id,
        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
        original_filename=source.name,
        display_name=document_id,
        file_hash=document_id,
        source_type="upload",
        source_file_path=str(source),
        preview_file_path=None,
        mineru_result_dir=None,
        page_count=0,
        processing_status="uploaded",
        processing_error=None,
        parser_provider="mineru",
    )
    store.upsert_document(document)
    return document


def wait_for(predicate, timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition was not reached")


def test_worker_processes_persistent_jobs_one_at_a_time(tmp_path):
    store = EvidenceStore(tmp_path / "runtime.db")
    make_document(store, tmp_path, "doc-1")
    make_document(store, tmp_path, "doc-2")
    active = 0
    max_active = 0
    order: list[str] = []
    lock = threading.Lock()

    class Service:
        def process_document(self, knowledge_base_id, document_id, *, progress, **_kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
                order.append(document_id)
            progress("page_check")
            time.sleep(0.04)
            document = store.get_document(knowledge_base_id, document_id)
            assert document is not None
            store.upsert_document(replace(document, processing_status="ready"))
            with lock:
                active -= 1

    first = enqueue_document_processing(store, DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")
    second = enqueue_document_processing(store, DEFAULT_KNOWLEDGE_BASE_ID, "doc-2")
    worker = ProcessingWorker(store, Service(), poll_interval=0.01)  # type: ignore[arg-type]
    worker.start()
    worker.notify()
    try:
        wait_for(lambda: store.get_processing_job(second.id).status == "succeeded")  # type: ignore[union-attr]
    finally:
        worker.stop()

    assert order == ["doc-1", "doc-2"]
    assert max_active == 1
    assert store.get_processing_job(first.id).stage == "complete"  # type: ignore[union-attr]


def test_worker_reports_liveness_and_fatal_loop_failure_without_secret_details(
    caplog,
):
    class FailingStore:
        def recover_processing_jobs(self):
            return 0

        def claim_next_processing_job(self):
            raise RuntimeError("private provider token must not enter readiness")

    class Service:
        pass

    worker = ProcessingWorker(
        FailingStore(), Service(), poll_interval=0.01  # type: ignore[arg-type]
    )
    with caplog.at_level(logging.ERROR, logger="traceable_rag.processing_jobs"):
        worker.start()
        wait_for(lambda: not worker.is_alive)

    assert worker.fatal_error_type == "RuntimeError"
    assert "exception_type=RuntimeError" in caplog.text
    assert "private provider token" not in caplog.text


def test_worker_recovers_interrupted_running_job(tmp_path):
    store = EvidenceStore(tmp_path / "runtime.db")
    make_document(store, tmp_path, "doc-1")
    job = enqueue_document_processing(store, DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")
    claimed = store.claim_next_processing_job()
    assert claimed and claimed.status == "running"

    assert store.recover_processing_jobs() == 1
    recovered = store.get_processing_job(job.id)
    assert recovered and recovered.status == "queued" and recovered.stage == "queued"


def test_normalization_upgrade_uses_rebuild_without_changing_user_document_status(tmp_path):
    assert NORMALIZATION_VERSION == "mineru-content.v4"
    store = EvidenceStore(tmp_path / "runtime.db")
    original = make_document(store, tmp_path, "doc-upgrade")
    store.upsert_document(replace(original, processing_status="ready"))
    calls: list[tuple[str, str]] = []

    class Service:
        def process_document(self, *_args, **_kwargs):
            raise AssertionError("content upgrade must not invoke external processing")

        def rebuild_document_content(
            self, knowledge_base_id, document_id, *, progress
        ):
            calls.append((knowledge_base_id, document_id))
            progress("normalizing")
            progress("indexing")
            progress("persist")

    job = enqueue_document_processing(
        store,
        DEFAULT_KNOWLEDGE_BASE_ID,
        "doc-upgrade",
        operation=normalization_upgrade_operation(NORMALIZATION_VERSION),
    )
    assert store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-upgrade").processing_status == "ready"
    worker = ProcessingWorker(store, Service())  # type: ignore[arg-type]
    claimed = store.claim_next_processing_job()
    assert claimed is not None

    worker._process(claimed)

    assert calls == [(DEFAULT_KNOWLEDGE_BASE_ID, "doc-upgrade")]
    assert store.get_processing_job(job.id).status == "succeeded"  # type: ignore[union-attr]
    assert store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-upgrade").processing_status == "ready"


def test_failed_normalization_upgrade_preserves_status_and_is_not_requeued(tmp_path):
    store = EvidenceStore(tmp_path / "runtime.db")
    original = make_document(store, tmp_path, "doc-upgrade-failed")
    original = replace(
        original,
        processing_status="ready",
        mineru_result_dir=str(tmp_path / "retained-result"),
    )
    store.upsert_document(original)
    original = store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, original.id)
    assert original is not None

    class Service:
        def rebuild_document_content(self, *_args, **_kwargs):
            raise ValueError("private retained result is malformed")

    job = enqueue_document_processing(
        store,
        DEFAULT_KNOWLEDGE_BASE_ID,
        original.id,
        operation=normalization_upgrade_operation(NORMALIZATION_VERSION),
    )
    worker = ProcessingWorker(store, Service())  # type: ignore[arg-type]
    claimed = store.claim_next_processing_job()
    assert claimed is not None

    worker._process(claimed)

    failed = store.get_processing_job(job.id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.error_code == "normalization_failed"
    assert "private" not in (failed.error_message or "")
    assert store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, original.id) == original
    assert store.list_documents_needing_normalization(NORMALIZATION_VERSION) == []


def test_retryable_normalization_upgrade_failure_can_be_requeued(tmp_path):
    store = EvidenceStore(tmp_path / "runtime.db")
    document = replace(
        make_document(store, tmp_path, "doc-upgrade-retryable"),
        processing_status="ready",
        mineru_result_dir=str(tmp_path / "retained-result"),
    )
    store.upsert_document(document)
    operation = normalization_upgrade_operation(NORMALIZATION_VERSION)
    first = enqueue_document_processing(
        store,
        DEFAULT_KNOWLEDGE_BASE_ID,
        document.id,
        operation=operation,
    )
    claimed = store.claim_next_processing_job()
    assert claimed is not None
    store.update_processing_job(
        first.id,
        status="failed",
        stage="persist",
        error_code="storage_failed",
        error_message="temporary storage failure",
        retryable=True,
        finished=True,
    )

    candidates = store.list_documents_needing_normalization(NORMALIZATION_VERSION)
    assert [candidate.id for candidate in candidates] == [document.id]

    second = enqueue_document_processing(
        store,
        DEFAULT_KNOWLEDGE_BASE_ID,
        document.id,
        operation=operation,
    )
    assert second.attempt == 2


def test_startup_enqueues_each_content_upgrade_at_most_once(tmp_path, monkeypatch):
    db_path = tmp_path / "runtime.db"
    store = EvidenceStore(db_path)
    document = make_document(store, tmp_path, "startup-upgrade")
    document = replace(
        document,
        processing_status="ready",
        mineru_result_dir=str(tmp_path / "retained-result"),
    )
    store.upsert_document(document)
    monkeypatch.setattr(ProcessingWorker, "start", lambda self: None)

    first_app = create_app(
        db_path=db_path,
        managed_root=tmp_path / "managed",
        start_processing_worker=True,
    )
    with TestClient(first_app) as client:
        response = client.get(
            f"/api/knowledge-bases/{DEFAULT_KNOWLEDGE_BASE_ID}/documents"
        )
        assert response.status_code == 200
        assert response.json()["documents"][0]["latest_job"]["operation"] == (
            "normalize_upgrade"
        )

    jobs = store.list_processing_jobs(DEFAULT_KNOWLEDGE_BASE_ID)
    assert len(jobs) == 1
    assert jobs[0].operation == normalization_upgrade_operation(
        NORMALIZATION_VERSION
    )
    assert store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, document.id).processing_status == "ready"
    store.update_processing_job(
        jobs[0].id,
        status="failed",
        stage="normalizing",
        error_code="normalization_failed",
        error_message="failed",
        retryable=False,
        finished=True,
    )

    second_app = create_app(
        db_path=db_path,
        managed_root=tmp_path / "managed",
        start_processing_worker=True,
    )
    with TestClient(second_app):
        pass

    assert len(store.list_processing_jobs(DEFAULT_KNOWLEDGE_BASE_ID)) == 1


def test_official_mineru_error_code_mapping_is_safe():
    from traceable_rag.mineru_client import MinerUAPIError

    code, message, retryable = classify_processing_error(
        MinerUAPIError(-60009, "private signed url https://secret.example"),
        "mineru_submit",
    )

    assert code == "mineru_queue_full"
    assert retryable is True
    assert "secret.example" not in message


def test_mineru_provider_diagnostic_keeps_response_reason_but_redacts_secrets():
    from traceable_rag.mineru_client import MinerUAPIError

    diagnostic = processing_error_diagnostic(
        MinerUAPIError(
            401,
            "token=private https://secret.example/request 凭据已过期",
        )
    )

    assert diagnostic["provider_code"] == "401"
    assert "凭据已过期" in str(diagnostic["provider_response"])
    assert "private" not in str(diagnostic)
    assert "secret.example" not in str(diagnostic)


@pytest.mark.parametrize(
    ("provider_code", "expected_code", "retryable"),
    [
        (401, "mineru_auth_failed", False),
        (429, "mineru_rate_limited", True),
        (-60006, "page_limit_exceeded", False),
        (-60015, "mineru_conversion_failed", False),
        (-60018, "mineru_daily_limit_reached", True),
        (-60022, "mineru_web_read_failed", True),
        (-69999, "mineru_rejected", False),
    ],
)
def test_official_mineru_errors_map_to_stable_public_categories(
    provider_code, expected_code, retryable
):
    from traceable_rag.mineru_client import MinerUAPIError

    code, message, actual_retryable = classify_processing_error(
        MinerUAPIError(provider_code, "token=secret signed_url=https://secret.example"),
        "mineru_submit",
    )

    assert code == expected_code
    assert actual_retryable is retryable
    assert "secret" not in message


@pytest.mark.parametrize(
    ("stage", "expected_code", "retryable"),
    [
        ("converting", "conversion_failed", False),
        ("page_check", "pdf_inspection_failed", False),
        ("mineru_submit", "mineru_upload_failed", True),
        ("mineru_download", "mineru_result_download_failed", True),
        ("normalizing", "normalization_failed", False),
        ("indexing", "indexing_failed", True),
        ("persist", "storage_failed", True),
        ("unknown", "unexpected_processing_error", True),
    ],
)
def test_processing_stages_map_to_stable_safe_errors(stage, expected_code, retryable):
    code, message, actual_retryable = classify_processing_error(
        RuntimeError("private path /private/customer/token-secret"), stage
    )

    assert code == expected_code
    assert actual_retryable is retryable
    assert "/private/customer" not in message


def test_mineru_timeout_has_a_dedicated_retryable_error():
    code, message, retryable = classify_processing_error(
        TimeoutError("batch secret timed out"), "mineru_poll"
    )

    assert code == "mineru_timeout"
    assert retryable is True
    assert "secret" not in message


def test_missing_retained_result_requires_manual_reprocessing():
    code, message, retryable = classify_processing_error(
        NormalizationSourceUnavailableError("private missing path"), "normalizing"
    )

    assert code == "normalization_source_unavailable"
    assert "手动重新处理" in message
    assert "private" not in message
    assert retryable is False


def test_missing_mineru_configuration_is_classified_at_submit_stage(
    tmp_path, monkeypatch, caplog
):
    import traceable_rag.document_processing as document_processing_module

    store = EvidenceStore(tmp_path / "runtime.db")
    source = tmp_path / "source.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with source.open("wb") as target:
        writer.write(target)
    service = DocumentProcessingService(store, tmp_path / "managed")
    uploaded = service.register_upload(
        DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-no-mineru"
    )
    monkeypatch.setattr(
        document_processing_module,
        "_mineru_client_from_environment",
        lambda **_kwargs: (_ for _ in ()).throw(
            MinerUConfigurationError("private missing token detail")
        ),
    )
    job = enqueue_document_processing(
        store, DEFAULT_KNOWLEDGE_BASE_ID, uploaded.id
    )
    worker = ProcessingWorker(store, service)

    claimed = store.claim_next_processing_job()
    assert claimed is not None
    with caplog.at_level(logging.ERROR, logger="traceable_rag.processing_jobs"):
        worker._process(claimed)

    failed = store.get_processing_job(job.id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.stage == "mineru_submit"
    assert failed.error_code == "document_processing_not_configured"
    assert failed.retryable is False
    assert "解析服务尚未配置" in (failed.error_message or "")
    assert "private" not in (failed.error_message or "")
    assert f"diagnostic_id={job.id}" in caplog.text
    assert '"reason":"missing_configuration"' in caplog.text
    assert "private missing token detail" not in caplog.text


def test_page_limit_preflight_retains_preview_without_calling_mineru(tmp_path):
    store = EvidenceStore(tmp_path / "runtime.db")
    source = tmp_path / "source.docx"
    source.write_bytes(b"source")

    class Converter:
        def convert(self, _source, output):
            output.parent.mkdir(parents=True, exist_ok=True)
            writer = PdfWriter()
            for _ in range(201):
                writer.add_blank_page(width=100, height=100)
            with output.open("wb") as target:
                writer.write(target)
            return output

    class MinerU:
        def parse_local_file(self, *_args, **_kwargs):
            raise AssertionError("MinerU must not be called for an oversized PDF")

    service = DocumentProcessingService(
        store,
        tmp_path / "managed",
        converter=Converter(),
        mineru_client=MinerU(),
    )
    uploaded = service.register_upload(DEFAULT_KNOWLEDGE_BASE_ID, source)

    try:
        service.process_document(
            DEFAULT_KNOWLEDGE_BASE_ID, uploaded.id, max_pages=200
        )
    except Exception as exc:
        code, message, retryable = classify_processing_error(exc, "page_check")
    else:
        raise AssertionError("page limit must fail processing")

    failed = store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, uploaded.id)
    assert failed is not None
    assert failed.processing_status == "failed"
    assert failed.page_count == 201
    assert failed.preview_file_path and Path(failed.preview_file_path).is_file()
    assert code == "page_limit_exceeded"
    assert "201" in message
    assert retryable is False


def test_upload_returns_queued_job_without_running_processing_inline(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MINERU_TOKEN", "configured-for-test")
    master_key_file = tmp_path / "provider-master-key"
    master_key_file.write_bytes(b"k" * 32)
    app = create_app(
        db_path=tmp_path / "runtime.db",
        managed_root=tmp_path / "managed",
        provider_store_path=tmp_path / "providers.db",
        provider_master_key_file=master_key_file,
        start_processing_worker=False,
    )
    client = TestClient(app)

    response = client.post(
        "/api/knowledge-bases/default/documents/upload",
        files={"file": ("source.pdf", b"%PDF-1.4\n", "application/pdf")},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["document"]["processing_status"] == "queued"
    assert payload["job"]["status"] == "queued"
    assert payload["job"]["queue_position"] == 1
    listed = client.get("/api/knowledge-bases/default/processing-jobs").json()
    assert [job["id"] for job in listed["jobs"]] == [payload["job"]["id"]]
    duplicate_retry = client.post(
        f"/api/knowledge-bases/default/documents/{payload['document']['id']}/parse"
    )
    assert duplicate_retry.status_code == 409
    assert duplicate_retry.json()["detail"]["code"] == "processing_job_active"
