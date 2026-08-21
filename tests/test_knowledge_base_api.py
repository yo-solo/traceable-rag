import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from inspect import signature
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from traceable_rag.chunking import BlockWindowChunker
from traceable_rag.document_processing import DocumentProcessingService
from traceable_rag.indexing import build_index_records
from traceable_rag.models import Block
from traceable_rag.store import DEFAULT_KNOWLEDGE_BASE_ID, EvidenceStore
from traceable_rag.web_app import _parse_multipart_upload, create_app


def test_list_knowledge_bases_returns_initialized_default(tmp_path):
    client = TestClient(
        create_app(
            db_path=tmp_path / "traceable.db",
            managed_root=tmp_path / "managed",
        )
    )

    response = client.get("/api/knowledge-bases")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["knowledge_bases"]] == [
        DEFAULT_KNOWLEDGE_BASE_ID
    ]


def test_knowledge_base_payloads_include_server_aggregated_document_counts(
    tmp_path, monkeypatch
):
    client, store, service = make_client(tmp_path)
    knowledge_base = create_knowledge_base(client)
    zero_counts = {
        "document_count": 0,
        "ready_document_count": 0,
        "processing_document_count": 0,
        "failed_document_count": 0,
    }
    assert {key: knowledge_base[key] for key in zero_counts} == zero_counts

    statuses = (
        "ready",
        "uploading",
        "parsing",
        "indexing",
        "failed",
        "uploaded",
        "parsed",
    )
    for index, status in enumerate(statuses):
        document = service.delegate.register_upload_bytes(
            knowledge_base["id"],
            f"document-{index}.pdf",
            f"content-{index}".encode(),
            display_name=f"文档 {index}",
        )
        store.upsert_document(replace(document, processing_status=status))

    def forbid_per_knowledge_base_document_query(*args, **kwargs):
        raise AssertionError("knowledge-base summaries must not call list_documents")

    monkeypatch.setattr(
        EvidenceStore,
        "list_documents",
        forbid_per_knowledge_base_document_query,
    )
    expected_counts = {
        "document_count": 7,
        "ready_document_count": 1,
        "processing_document_count": 3,
        "failed_document_count": 1,
    }

    listed = client.get("/api/knowledge-bases")
    fetched = client.get(f"/api/knowledge-bases/{knowledge_base['id']}")
    patched = client.patch(
        f"/api/knowledge-bases/{knowledge_base['id']}",
        json={"description": "更新后的说明"},
    )

    assert listed.status_code == 200
    listed_by_id = {
        item["id"]: item for item in listed.json()["knowledge_bases"]
    }
    assert {
        key: listed_by_id[DEFAULT_KNOWLEDGE_BASE_ID][key] for key in zero_counts
    } == zero_counts
    assert {
        key: listed_by_id[knowledge_base["id"]][key] for key in expected_counts
    } == expected_counts
    assert {
        key: fetched.json()["knowledge_base"][key] for key in expected_counts
    } == expected_counts
    assert {
        key: patched.json()["knowledge_base"][key] for key in expected_counts
    } == expected_counts


def test_document_list_uses_aggregate_graph_stats_instead_of_per_document_lists(
    tmp_path, monkeypatch
):
    client, _, _ = make_client(tmp_path)
    knowledge_base = create_knowledge_base(client)
    processed = upload_document(client, knowledge_base["id"])
    assert process_next_job(client).status == "succeeded"
    pending_response = client.post(
        f"/api/knowledge-bases/{knowledge_base['id']}/documents/upload",
        files={"file": ("pending.pdf", b"pending", "application/pdf")},
        data={"display_name": "待处理文档"},
    )
    assert pending_response.status_code == 201
    pending = pending_response.json()["document"]

    def forbid_per_document_graph_query(*args, **kwargs):
        raise AssertionError("document list must not load graph rows per document")

    for method_name in (
        "list_blocks",
        "list_assets",
        "list_chunks",
        "list_index_records",
    ):
        monkeypatch.setattr(
            EvidenceStore,
            method_name,
            forbid_per_document_graph_query,
        )

    response = client.get(
        f"/api/knowledge-bases/{knowledge_base['id']}/documents"
    )

    assert response.status_code == 200
    documents = {item["id"]: item for item in response.json()["documents"]}
    assert documents[processed["id"]]["stats"] == {
        "blocks": 1,
        "assets": 0,
        "chunks": 1,
        "index_records": 1,
        "embedding_records": 0,
    }
    assert documents[processed["id"]]["embedding_record_count"] == 0
    assert documents[processed["id"]]["embedding_ready"] is False
    assert documents[processed["id"]]["embedding_status"] == "fallback_keyword"
    assert "关键词" in documents[processed["id"]]["embedding_warning"]
    assert documents[pending["id"]]["stats"] == {
        "blocks": 0,
        "assets": 0,
        "chunks": 0,
        "index_records": 0,
        "embedding_records": 0,
    }
    assert documents[pending["id"]]["embedding_record_count"] == 0
    assert documents[pending["id"]]["embedding_ready"] is False
    assert documents[pending["id"]]["embedding_status"] == "not_indexed"
    assert documents[pending["id"]]["embedding_warning"] is None


def test_document_embedding_ready_requires_all_nonempty_valid_record_vectors(tmp_path):
    client, store, _ = make_client(tmp_path)
    knowledge_base = create_knowledge_base(client)
    document = upload_document(client, knowledge_base["id"])
    assert process_next_job(client).status == "succeeded"
    records = store.list_index_records(knowledge_base["id"], document["id"])
    store.replace_index_records(
        knowledge_base["id"],
        document["id"],
        [
            replace(
                records[0],
                vector=[0.2, 0.8],
                embedding_provider="fake",
                embedding_model="fake-model",
                embedding_dimension=2,
            )
        ],
    )

    response = client.get(
        f"/api/knowledge-bases/{knowledge_base['id']}/documents/{document['id']}"
    )

    assert response.status_code == 200
    payload = response.json()["document"]
    assert payload["embedding_record_count"] == 1
    assert payload["embedding_ready"] is True
    assert payload["embedding_status"] == "ready"
    assert payload["embedding_warning"] is None


def test_document_embedding_ready_rejects_mixed_vector_dimensions(tmp_path):
    client, store, _ = make_client(tmp_path)
    knowledge_base = create_knowledge_base(client)
    document = upload_document(client, knowledge_base["id"])
    assert process_next_job(client).status == "succeeded"
    chunk = store.list_chunks(knowledge_base["id"], document["id"])[0]
    record = store.list_index_records(knowledge_base["id"], document["id"])[0]
    second_chunk = replace(chunk, id=f"{chunk.id}-second", chunk_idx=1)
    store.replace_chunks(
        knowledge_base["id"], document["id"], [chunk, second_chunk]
    )
    store.replace_index_records(
        knowledge_base["id"],
        document["id"],
        [
            replace(
                record,
                vector=[0.2, 0.8],
                embedding_provider="fake",
                embedding_model="fake-model",
                embedding_dimension=2,
            ),
            replace(
                record,
                id=f"{record.id}-second",
                evidence_id=f"{record.evidence_id}-second",
                chunk_id=second_chunk.id,
                vector=[0.1, 0.2, 0.7],
                embedding_provider="fake",
                embedding_model="fake-model",
                embedding_dimension=3,
            ),
        ],
    )

    response = client.get(
        f"/api/knowledge-bases/{knowledge_base['id']}/documents/{document['id']}"
    )

    assert response.status_code == 200
    payload = response.json()["document"]
    assert payload["embedding_record_count"] == 2
    assert payload["embedding_ready"] is False
    assert payload["embedding_status"] == "fallback_keyword"
    assert "关键词" in payload["embedding_warning"]
    assert set(payload["stats"]) == {
        "blocks",
        "assets",
        "chunks",
        "index_records",
        "embedding_records",
    }


@pytest.mark.parametrize(
    ("vector_json", "vector", "expected_count", "expected_ready"),
    [
        ("[1e999, 1.0]", None, 0, False),
        (None, [0.0, 0.0], 0, False),
        (None, [1e308, -1e308], 1, True),
    ],
    ids=["sqlite-infinity", "zero-norm", "extreme-finite"],
)
def test_document_embedding_status_requires_finite_nonzero_vectors(
    tmp_path, vector_json, vector, expected_count, expected_ready
):
    client, store, _ = make_client(tmp_path)
    knowledge_base = create_knowledge_base(client)
    document = upload_document(client, knowledge_base["id"])
    assert process_next_job(client).status == "succeeded"
    record = store.list_index_records(knowledge_base["id"], document["id"])[0]
    if vector_json is not None:
        with store.connect() as conn:
            conn.execute(
                """
                update index_records
                set vector_json = ?, embedding_provider = ?, embedding_model = ?,
                    embedding_dimension = ?
                where id = ?
                """,
                (vector_json, "fake", "fake-model", 2, record.id),
            )
    else:
        store.replace_index_records(
            knowledge_base["id"],
            document["id"],
            [
                replace(
                    record,
                    vector=vector,
                    embedding_provider="fake",
                    embedding_model="fake-model",
                    embedding_dimension=2,
                )
            ],
        )

    response = client.get(
        f"/api/knowledge-bases/{knowledge_base['id']}/documents/{document['id']}"
    )

    assert response.status_code == 200
    payload = response.json()["document"]
    assert payload["embedding_record_count"] == expected_count
    assert payload["embedding_ready"] is expected_ready


def test_web_app_defaults_to_v3_database_path():
    parameters = signature(create_app).parameters
    assert parameters["db_path"].default == "storage/traceable_v3.db"
    assert parameters["managed_root"].default == "storage/managed"
    assert parameters["settings_path"].default == "storage/settings.json"
    assert parameters["static_dir"].default == "web/dist"


class SpyProcessingService:
    def __init__(self, store: EvidenceStore, managed_root: Path, *, produce_index=True):
        self.store = store
        self.managed_root = managed_root
        self.delegate = DocumentProcessingService(store, managed_root)
        self.register_calls: list[tuple[str, str]] = []
        self.process_calls: list[tuple[str, str]] = []
        self.delete_knowledge_base_calls: list[str] = []
        self.update_knowledge_base_calls: list[tuple[str, str | None, str | None]] = []
        self.rename_calls: list[tuple[str, str, str]] = []
        self.produce_index = produce_index

    def mineru_configured(self):
        return True

    def register_upload(self, knowledge_base_id, source_file, **kwargs):
        self.register_calls.append((knowledge_base_id, Path(source_file).name))
        return self.delegate.register_upload(knowledge_base_id, source_file, **kwargs)

    def register_upload_bytes(self, knowledge_base_id, filename, content, **kwargs):
        self.register_calls.append((knowledge_base_id, filename))
        return self.delegate.register_upload_bytes(
            knowledge_base_id, filename, content, **kwargs
        )

    def process_document(self, knowledge_base_id, document_id, **kwargs):
        self.delegate._ensure_knowledge_base_recovery_clear(knowledge_base_id)
        self.process_calls.append((knowledge_base_id, document_id))
        document = self.store.get_document(knowledge_base_id, document_id)
        assert document is not None
        preview = self.managed_root / f"{document_id}-preview.pdf"
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"%PDF-managed-preview")
        block = Block(
            id=f"{document_id}:block-1",
            document_id=document_id,
            page_idx=0,
            block_idx=0,
            block_type="paragraph",
            text="受管流程生成的正文",
            bbox=[10, 20, 30, 40],
            page_size=[1000, 1400],
        )
        ready = replace(
            document,
            preview_file_path=str(preview),
            page_count=1,
            processing_status="ready",
        )
        chunks = BlockWindowChunker().chunk(document_id, [block]) if self.produce_index else []
        records = build_index_records(chunks) if self.produce_index else []
        self.store.replace_document_graph(
            knowledge_base_id,
            ready,
            [block],
            [],
            chunks,
            records,
        )
        return ready

    def ensure_knowledge_base_ready(self, knowledge_base_id):
        self.delegate.ensure_knowledge_base_ready(knowledge_base_id)

    def delete_document(self, knowledge_base_id, document_id):
        return self.delegate.delete_document(knowledge_base_id, document_id)

    def delete_knowledge_base(self, knowledge_base_id, *, confirm_name=None):
        self.delete_knowledge_base_calls.append(knowledge_base_id)
        return self.delegate.delete_knowledge_base(
            knowledge_base_id, confirm_name=confirm_name
        )

    def update_knowledge_base(
        self, knowledge_base_id, *, name=None, description=None
    ):
        self.update_knowledge_base_calls.append(
            (knowledge_base_id, name, description)
        )
        return self.delegate.update_knowledge_base(
            knowledge_base_id, name=name, description=description
        )

    def rename_document(self, knowledge_base_id, document_id, display_name):
        self.rename_calls.append((knowledge_base_id, document_id, display_name))
        return self.delegate.rename_document(
            knowledge_base_id, document_id, display_name
        )


def make_client(tmp_path, *, produce_index=True):
    db_path = tmp_path / "traceable.db"
    managed_root = tmp_path / "managed"
    store = EvidenceStore(db_path)
    service = SpyProcessingService(store, managed_root, produce_index=produce_index)
    app = create_app(
        db_path=db_path,
        managed_root=managed_root,
        document_processing_service=service,
        start_processing_worker=False,
    )
    return TestClient(app), store, service


def create_knowledge_base(client: TestClient, name: str = "编程示例资料库") -> dict:
    response = client.post(
        "/api/knowledge-bases",
        json={"name": name, "description": "公开示例资料"},
    )
    assert response.status_code == 201
    return response.json()["knowledge_base"]


def upload_document(client: TestClient, kb_id: str, filename: str = "inheritance-examples.docx") -> dict:
    response = client.post(
        f"/api/knowledge-bases/{kb_id}/documents/upload",
        files={"file": (filename, b"office-content", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        data={"display_name": "Python 继承示例"},
    )
    assert response.status_code == 201
    return response.json()["document"]


def test_upload_rejects_before_persistence_when_document_processing_is_not_configured(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("MINERU_TOKEN", raising=False)
    monkeypatch.delenv("MINERU_API_KEY", raising=False)
    db_path = tmp_path / "traceable.db"
    client = TestClient(
        create_app(
            db_path=db_path,
            managed_root=tmp_path / "managed",
            runtime_root=tmp_path,
            start_processing_worker=False,
        )
    )

    response = client.post(
        f"/api/knowledge-bases/{DEFAULT_KNOWLEDGE_BASE_ID}/documents/upload",
        files={"file": ("待处理.docx", b"office-content")},
        data={"display_name": "待处理"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "document_processing_not_configured",
        "message": "文档解析服务尚未配置。请在运行状态中确认服务配置后再上传。",
    }
    assert EvidenceStore(db_path).list_documents(DEFAULT_KNOWLEDGE_BASE_ID) == []


def process_next_job(client: TestClient) -> dict:
    worker = client.app.state.processing_worker
    job = worker.store.claim_next_processing_job()
    assert job is not None
    worker._process(job)
    completed = worker.store.get_processing_job(job.id)
    assert completed is not None
    return completed


def test_knowledge_base_crud_validates_names_conflicts_and_delete_confirmation(tmp_path):
    client, _, service = make_client(tmp_path)
    created = create_knowledge_base(client)

    assert client.get(f"/api/knowledge-bases/{created['id']}").json()["knowledge_base"]["name"] == "编程示例资料库"
    duplicate = client.post("/api/knowledge-bases", json={"name": "编程示例资料库"})
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "knowledge_base_name_conflict"
    assert client.post("/api/knowledge-bases", json={"name": "   "}).status_code == 422

    renamed = client.patch(
        f"/api/knowledge-bases/{created['id']}",
        json={"name": "编程与就业资料库"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["knowledge_base"]["name"] == "编程与就业资料库"
    occupied = create_knowledge_base(client, "已占用资料库")
    rename_conflict = client.patch(
        f"/api/knowledge-bases/{created['id']}",
        json={"name": occupied["name"]},
    )
    assert rename_conflict.status_code == 409
    assert rename_conflict.json()["detail"]["code"] == "knowledge_base_name_conflict"

    upload_document(client, created["id"])
    missing_confirmation = client.request(
        "DELETE", f"/api/knowledge-bases/{created['id']}"
    )
    assert missing_confirmation.status_code == 409
    assert missing_confirmation.json()["detail"]["code"] == "knowledge_base_delete_confirmation_required"
    wrong_confirmation = client.request(
        "DELETE",
        f"/api/knowledge-bases/{created['id']}",
        json={"confirm_name": "编程示例资料库"},
    )
    assert wrong_confirmation.status_code == 409
    deleted = client.request(
        "DELETE",
        f"/api/knowledge-bases/{created['id']}",
        json={"confirm_name": "编程与就业资料库"},
    )
    assert deleted.status_code == 200
    assert service.delete_knowledge_base_calls == [created["id"]] * 3
    assert client.get(f"/api/knowledge-bases/{created['id']}").status_code == 404


def test_document_crud_uses_managed_service_and_enforces_knowledge_base_scope(tmp_path):
    client, _, service = make_client(tmp_path)
    first_kb = create_knowledge_base(client, "第一资料库")
    second_kb = create_knowledge_base(client, "第二资料库")

    uploaded = upload_document(client, first_kb["id"])

    assert service.register_calls == [(first_kb["id"], "inheritance-examples.docx")]
    assert uploaded["original_filename"] == "inheritance-examples.docx"
    assert uploaded["display_name"] == "Python 继承示例"
    duplicate_name = client.post(
        f"/api/knowledge-bases/{first_kb['id']}/documents/upload",
        files={"file": ("another.docx", b"different", "application/octet-stream")},
        data={"display_name": "Python 继承示例"},
    )
    assert duplicate_name.status_code == 409
    assert duplicate_name.json()["detail"]["code"] == "document_name_conflict"
    listed = client.get(f"/api/knowledge-bases/{first_kb['id']}/documents")
    assert [item["id"] for item in listed.json()["documents"]] == [uploaded["id"]]
    assert client.get(
        f"/api/knowledge-bases/{second_kb['id']}/documents/{uploaded['id']}"
    ).status_code == 404
    assert client.post(
        f"/api/knowledge-bases/{second_kb['id']}/documents/{uploaded['id']}/parse"
    ).status_code == 404
    assert client.post(
        f"/api/knowledge-bases/{second_kb['id']}/documents/{uploaded['id']}/index"
    ).status_code == 404
    assert service.process_calls == []

    renamed = client.patch(
        f"/api/knowledge-bases/{first_kb['id']}/documents/{uploaded['id']}",
        json={"display_name": "新版继承示例"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["document"]["display_name"] == "新版继承示例"
    assert service.rename_calls == [
        (first_kb["id"], uploaded["id"], "新版继承示例")
    ]
    assert client.patch(
        f"/api/knowledge-bases/{first_kb['id']}/documents/{uploaded['id']}",
        json={"display_name": "  "},
    ).status_code == 422

    legacy_parse = client.post(
        f"/api/knowledge-bases/{first_kb['id']}/documents/{uploaded['id']}/parse",
        json={"result_dir": "/tmp/legacy-mineru-result"},
    )
    assert legacy_parse.status_code == 422
    assert process_next_job(client).status == "succeeded"
    assert service.process_calls == [(first_kb["id"], uploaded["id"])]
    parsed = client.post(
        f"/api/knowledge-bases/{first_kb['id']}/documents/{uploaded['id']}/parse"
    )
    assert parsed.status_code == 202
    assert parsed.json()["document"]["processing_status"] == "queued"
    assert parsed.json()["job"]["operation"] == "parse"
    assert process_next_job(client).status == "succeeded"
    indexed = client.post(
        f"/api/knowledge-bases/{first_kb['id']}/documents/{uploaded['id']}/index"
    )
    assert indexed.status_code == 202
    assert indexed.json()["operation"] == "index"
    assert indexed.json()["job"]["operation"] == "index"
    assert process_next_job(client).status == "succeeded"
    assert service.process_calls == [
        (first_kb["id"], uploaded["id"]),
        (first_kb["id"], uploaded["id"]),
        (first_kb["id"], uploaded["id"]),
    ]
    preview = client.get(
        f"/api/knowledge-bases/{first_kb['id']}/documents/{uploaded['id']}/file"
    )
    assert preview.status_code == 200
    assert preview.content == b"%PDF-managed-preview"
    blocks = client.get(
        f"/api/knowledge-bases/{first_kb['id']}/documents/{uploaded['id']}/blocks"
    ).json()
    assert blocks["blocks"][0]["text"] == "受管流程生成的正文"
    assert blocks["pages"] == [
        {
            "page_idx": 0,
            "human_page_number": 1,
            "page_size": [1000, 1400],
            "block_count": 1,
        }
    ]

    deleted = client.delete(
        f"/api/knowledge-bases/{first_kb['id']}/documents/{uploaded['id']}"
    )
    assert deleted.status_code == 200
    assert client.get(
        f"/api/knowledge-bases/{first_kb['id']}/documents/{uploaded['id']}"
    ).status_code == 404


def test_async_processing_can_complete_without_index_records(tmp_path):
    client, _, _ = make_client(tmp_path, produce_index=False)
    knowledge_base = create_knowledge_base(client)
    document = upload_document(client, knowledge_base["id"])
    assert process_next_job(client).status == "succeeded"

    response = client.get(
        f"/api/knowledge-bases/{knowledge_base['id']}/documents/{document['id']}"
    )

    assert response.status_code == 200
    assert response.json()["document"]["processing_status"] == "ready"
    assert response.json()["document"]["stats"]["index_records"] == 0


def test_document_response_does_not_expose_internal_processing_error_paths(tmp_path):
    client, store, _ = make_client(tmp_path)
    knowledge_base = create_knowledge_base(client)
    uploaded = upload_document(client, knowledge_base["id"])
    document = store.get_document(knowledge_base["id"], uploaded["id"])
    assert document is not None
    store.upsert_document(
        replace(
            document,
            processing_status="failed",
            processing_error="convert failed at /private/managed/secret/source.docx",
        )
    )

    response = client.get(
        f"/api/knowledge-bases/{knowledge_base['id']}/documents/{uploaded['id']}"
    )

    assert response.status_code == 200
    assert response.json()["document"]["processing_error"] == "Document processing failed."
    assert "/private/managed" not in response.text


def test_nonempty_knowledge_base_delete_cleanup_failure_is_non_2xx_and_restores_state(
    tmp_path, monkeypatch
):
    client, store, service = make_client(tmp_path)
    knowledge_base = create_knowledge_base(client)
    uploaded = upload_document(client, knowledge_base["id"])
    document = store.get_document(knowledge_base["id"], uploaded["id"])
    assert document is not None
    managed_source = Path(document.source_file_path)
    original_remove = service.delegate._safe_remove_tree

    def fail_cleanup_probe(path):
        if Path(path).name == ".cleanup-probe":
            raise OSError("private cleanup path must not leak")
        return original_remove(path)

    monkeypatch.setattr(service.delegate, "_safe_remove_tree", fail_cleanup_probe)

    response = client.request(
        "DELETE",
        f"/api/knowledge-bases/{knowledge_base['id']}",
        json={"confirm_name": knowledge_base["name"]},
    )

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "knowledge_base_delete_failed"
    assert "private cleanup" not in response.text
    assert client.get(f"/api/knowledge-bases/{knowledge_base['id']}").status_code == 200
    assert client.get(
        f"/api/knowledge-bases/{knowledge_base['id']}/documents/{uploaded['id']}"
    ).status_code == 200
    assert managed_source.read_bytes() == b"office-content"


def test_upload_rejects_request_body_over_configured_limit(tmp_path):
    db_path = tmp_path / "traceable.db"
    managed_root = tmp_path / "managed"
    store = EvidenceStore(db_path)
    service = SpyProcessingService(store, managed_root)
    client = TestClient(
        create_app(
            db_path=db_path,
            managed_root=managed_root,
            document_processing_service=service,
            max_upload_bytes=8,
        )
    )

    response = client.post(
        f"/api/knowledge-bases/{DEFAULT_KNOWLEDGE_BASE_ID}/documents/upload",
        content=b"123456789",
        headers={"x-filename": "too-large.pdf", "content-type": "application/pdf"},
    )

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "upload_too_large"
    assert store.list_documents(DEFAULT_KNOWLEDGE_BASE_ID) == []


def test_upload_returns_document_busy_during_real_knowledge_base_lock_contention(
    tmp_path,
):
    client, _, service = make_client(tmp_path)

    with service.delegate._knowledge_base_lock(DEFAULT_KNOWLEDGE_BASE_ID):
        response = client.post(
            f"/api/knowledge-bases/{DEFAULT_KNOWLEDGE_BASE_ID}/documents/upload",
            content=b"content",
            headers={"x-filename": "busy.pdf", "content-type": "application/pdf"},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "document_busy"


def test_knowledge_base_patch_uses_same_exclusive_lock(tmp_path):
    client, store, service = make_client(tmp_path)
    knowledge_base = create_knowledge_base(client)

    with service.delegate._knowledge_base_lock(knowledge_base["id"]):
        response = client.patch(
            f"/api/knowledge-bases/{knowledge_base['id']}",
            json={"name": "锁内新名称"},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "document_busy"
    assert store.get_knowledge_base(knowledge_base["id"]).name == knowledge_base["name"]


def test_empty_kb_delete_rechecks_after_late_upload_without_orphaning_file(
    tmp_path, monkeypatch
):
    client, store, service = make_client(tmp_path)
    knowledge_base = create_knowledge_base(client, "竞态资料库")
    source = tmp_path / "late.docx"
    source.write_bytes(b"late upload")
    delete_selected = threading.Event()
    upload_done = threading.Event()
    uploaded = {}
    original_store_delete = EvidenceStore.delete_knowledge_base
    original_service_delete = service.delete_knowledge_base

    def wait_for_late_upload_then_store_delete(current, knowledge_base_id):
        if knowledge_base_id == knowledge_base["id"] and not upload_done.is_set():
            delete_selected.set()
            assert upload_done.wait(timeout=5)
        return original_store_delete(current, knowledge_base_id)

    def wait_for_late_upload_then_service_delete(
        knowledge_base_id, *, confirm_name=None
    ):
        if knowledge_base_id == knowledge_base["id"]:
            delete_selected.set()
            assert upload_done.wait(timeout=5)
        return original_service_delete(
            knowledge_base_id, confirm_name=confirm_name
        )

    monkeypatch.setattr(
        EvidenceStore, "delete_knowledge_base", wait_for_late_upload_then_store_delete
    )
    monkeypatch.setattr(
        service, "delete_knowledge_base", wait_for_late_upload_then_service_delete
    )

    def upload_late_document():
        assert delete_selected.wait(timeout=5)
        document = service.delegate.register_upload(
            knowledge_base["id"], source, document_id="late-doc"
        )
        uploaded["document"] = document
        upload_done.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        upload_future = executor.submit(upload_late_document)
        response = client.request(
            "DELETE", f"/api/knowledge-bases/{knowledge_base['id']}"
        )
        upload_future.result(timeout=5)

    document = uploaded["document"]
    assert response.status_code == 409
    assert (
        response.json()["detail"]["code"]
        == "knowledge_base_delete_confirmation_required"
    )
    assert store.get_knowledge_base(knowledge_base["id"]) is not None
    assert store.get_document(knowledge_base["id"], document.id) is not None
    assert Path(document.source_file_path).read_bytes() == b"late upload"


def test_recovery_blocked_kb_mutations_return_stable_503(tmp_path):
    client, _, service = make_client(tmp_path)
    uploaded = upload_document(client, DEFAULT_KNOWLEDGE_BASE_ID)
    assert process_next_job(client).status == "succeeded"
    service.delegate._blocked_knowledge_base_ids = {DEFAULT_KNOWLEDGE_BASE_ID}

    responses = [
        client.post(
            f"/api/knowledge-bases/{DEFAULT_KNOWLEDGE_BASE_ID}/documents/upload",
            content=b"new",
            headers={"x-filename": "new.pdf", "content-type": "application/pdf"},
        ),
        client.patch(
            f"/api/knowledge-bases/{DEFAULT_KNOWLEDGE_BASE_ID}",
            json={"description": "blocked"},
        ),
        client.patch(
            f"/api/knowledge-bases/{DEFAULT_KNOWLEDGE_BASE_ID}/documents/{uploaded['id']}",
            json={"display_name": "blocked"},
        ),
        client.post(
            f"/api/knowledge-bases/{DEFAULT_KNOWLEDGE_BASE_ID}/documents/{uploaded['id']}/parse"
        ),
        client.delete(
            f"/api/knowledge-bases/{DEFAULT_KNOWLEDGE_BASE_ID}/documents/{uploaded['id']}"
        ),
        client.request(
            "DELETE",
            f"/api/knowledge-bases/{DEFAULT_KNOWLEDGE_BASE_ID}",
            json={"confirm_name": "默认知识库"},
        ),
    ]

    assert [response.status_code for response in responses] == [503] * len(responses)
    assert {
        response.json()["detail"]["code"] for response in responses
    } == {"knowledge_base_recovery_blocked"}


def test_multipart_upload_preserves_binary_tail_and_boundary_similar_bytes(tmp_path):
    client, store, _ = make_client(tmp_path)
    knowledge_base = create_knowledge_base(client)
    content = b"\x00binary--tail\r\n--almost-a-boundary\r\n"

    response = client.post(
        f"/api/knowledge-bases/{knowledge_base['id']}/documents/upload",
        files={"file": ("binary.pdf", content, "application/pdf")},
        data={"display_name": "二进制样本"},
    )

    assert response.status_code == 201
    document_id = response.json()["document"]["id"]
    document = store.get_document(knowledge_base["id"], document_id)
    assert document is not None
    assert Path(document.source_file_path).read_bytes() == content


def test_multipart_rejects_non_ascii_and_overlong_boundaries_as_invalid(tmp_path):
    cases = (
        (b"body", "multipart/form-data; boundary=边界"),
        (
            b"--" + b"a" * 71 + b"--\r\n",
            "multipart/form-data; boundary=" + "a" * 71,
        ),
    )

    for body, content_type in cases:
        with pytest.raises(HTTPException) as error:
            _parse_multipart_upload(body, content_type)
        assert error.value.status_code == 400
        assert error.value.detail["code"] == "invalid_multipart"
