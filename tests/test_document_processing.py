from __future__ import annotations

import ctypes
import json
import logging
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader, PdfWriter

import traceable_rag.document_processing as document_processing_module
import traceable_rag.web_app as web_app_module
from traceable_rag.cli import DEFAULT_DB, DEMO_DOCUMENT_FILENAMES, seed_demo_documents
from traceable_rag.document_processing import (
    DocumentAlreadyExistsError,
    DocumentBusyError,
    DocumentProcessingService,
    DocumentSourceMismatchError,
    LibreOfficeConverter,
    LocalDocumentIndexer,
)
from traceable_rag.indexing import build_index_records
from traceable_rag.mineru_client import MinerUParseResult, mineru_api_key_from_doc
from traceable_rag.models import Block, Document
from traceable_rag.mineru_import import NORMALIZATION_VERSION
from traceable_rag.store import DEFAULT_KNOWLEDGE_BASE_ID, EvidenceStore
from traceable_rag.web_app import create_app


class RecordingStore(EvidenceStore):
    def __init__(self, db_path):
        self.statuses: list[str] = []
        self.persist_error: Exception | None = None
        super().__init__(db_path)

    def upsert_document(self, document: Document) -> None:
        super().upsert_document(document)
        self.statuses.append(document.processing_status)
    def replace_document_graph(self, knowledge_base_id, document, blocks, assets, chunks, records):
        if self.persist_error:
            raise self.persist_error
        super().replace_document_graph(
            knowledge_base_id, document, blocks, assets, chunks, records
        )
        self.statuses.append(document.processing_status)


class SimulatedProcessCrash(BaseException):
    pass


def wait_for_document_status(
    client: TestClient,
    knowledge_base_id: str,
    document_id: str,
    expected_status: str,
    *,
    timeout: float = 3,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(
            f"/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}"
        )
        assert response.status_code == 200
        document = response.json()["document"]
        if document["processing_status"] == expected_status:
            return document
        time.sleep(0.02)
    raise AssertionError(
        f"document {document_id} did not reach status {expected_status!r}"
    )


class FakeKernel32:
    def __init__(
        self,
        *,
        create_result=123,
        flush_result=1,
        close_result=1,
    ):
        self.create_result = create_result
        self.flush_result = flush_result
        self.close_result = close_result
        self.create_calls = []
        self.flush_calls = []
        self.close_calls = []

    def CreateFileW(self, *args):
        self.create_calls.append(args)
        return self.create_result

    def FlushFileBuffers(self, handle):
        self.flush_calls.append(handle)
        return self.flush_result

    def CloseHandle(self, handle):
        self.close_calls.append(handle)
        return self.close_result


class FakeConverter:
    def __init__(
        self,
        store: EvidenceStore,
        *,
        error: Exception | None = None,
        document_id: str = "doc-1",
    ):
        self.store = store
        self.error = error
        self.document_id = document_id
        self.calls: list[tuple[Path, Path]] = []

    def convert(self, source_file: Path, output_pdf: Path) -> Path:
        document = self.store.get_document(
            DEFAULT_KNOWLEDGE_BASE_ID, self.document_id
        )
        assert document and document.processing_status == "parsing"
        if self.error:
            raise self.error
        output_pdf.parent.mkdir(parents=True, exist_ok=True)
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        with output_pdf.open("wb") as target:
            writer.write(target)
        self.calls.append((source_file, output_pdf))
        return output_pdf


class FakeMinerUClient:
    def __init__(
        self,
        store: EvidenceStore,
        *,
        error: Exception | None = None,
        document_id: str = "doc-1",
    ):
        self.store = store
        self.error = error
        self.document_id = document_id
        self.calls: list[Path] = []
        self.options: list[dict] = []
        self.generation = 0

    def parse_local_file(self, file_path: Path, output_dir: Path, **kwargs) -> MinerUParseResult:
        document = self.store.get_document(
            DEFAULT_KNOWLEDGE_BASE_ID, self.document_id
        )
        assert document and document.processing_status == "parsing"
        if self.error:
            raise self.error
        self.generation += 1
        result_dir = output_dir / "result"
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "content_list.json").write_text(
            json.dumps(
                [
                    {
                        "type": "text",
                        "text": f"generation-{self.generation}",
                        "page_idx": 0,
                        "bbox": [1, 2, 30, 40],
                        "page_size": [100, 200],
                    }
                ]
            ),
            encoding="utf-8",
        )
        origin = result_dir / "converted_origin.pdf"
        if file_path.suffix.lower() in {".jpg", ".jpeg", ".png"}:
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            with origin.open("wb") as target:
                writer.write(target)
        else:
            origin.write_bytes(file_path.read_bytes())
        self.calls.append(file_path)
        self.options.append(kwargs)
        return MinerUParseResult(
            batch_id="batch-1",
            file_name=file_path.name,
            state="done",
            output_dir=output_dir,
            zip_path=None,
            full_zip_url=None,
            raw_result={},
        )


class FakeIndexer:
    def __init__(
        self,
        store: EvidenceStore,
        *,
        error: Exception | None = None,
        record_count: int = 1,
    ):
        self.store = store
        self.error = error
        self.record_count = record_count
        self.calls = 0

    def build(self, document_id, blocks):
        document = self.store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, document_id)
        assert document and document.processing_status == "indexing"
        if self.error:
            raise self.error
        from traceable_rag.chunking import BlockWindowChunker

        self.calls += 1
        chunks = BlockWindowChunker().chunk(document_id, blocks)
        if self.record_count > 1:
            source = chunks[0]
            chunks = [
                replace(
                    source,
                    id=f"{source.id}:copy:{index}",
                    chunk_idx=index,
                    indexing_text=f"{source.indexing_text} copy-{index}",
                )
                for index in range(self.record_count)
            ]
        return chunks, build_index_records(chunks)


class FakeEmbeddingProvider:
    provider_name = "fake-embedding"
    model_name = "fake-model"

    def __init__(self, vectors=None, *, error: Exception | None = None):
        self.vectors = vectors or [[0.25, 0.75]]
        self.error = error
        self.calls: list[list[str]] = []

    def embed_texts(self, texts):
        self.calls.append(list(texts))
        if self.error:
            raise self.error
        return self.vectors


def make_service(
    tmp_path,
    *,
    converter_error=None,
    mineru_error=None,
    indexer_error=None,
    record_count=1,
    embedding_provider=None,
):
    store = RecordingStore(tmp_path / "traceable_v2.db")
    return store, DocumentProcessingService(
        store,
        tmp_path / "managed",
        converter=FakeConverter(store, error=converter_error),
        mineru_client=FakeMinerUClient(store, error=mineru_error),
        indexer=FakeIndexer(
            store,
            error=indexer_error,
            record_count=record_count,
        ),
        embedding_provider=embedding_provider,
    )


def test_docx_processing_preserves_business_source_and_uses_mineru_pdf_for_viewer(tmp_path):
    store, service = make_service(tmp_path)
    source = tmp_path / "public-sample.docx"
    source.write_bytes(b"docx-source")

    uploaded = service.register_upload(
        DEFAULT_KNOWLEDGE_BASE_ID,
        source,
        display_name="Public sample",
        document_id="doc-1",
    )
    ready = service.process_document(DEFAULT_KNOWLEDGE_BASE_ID, uploaded.id)

    assert store.statuses == [
        "uploaded",
        "parsing",
        "parsing",
        "parsed",
        "indexing",
        "ready",
    ]
    assert ready.original_filename == source.name
    assert ready.display_name == "Public sample"
    assert ready.source_file_path and ready.source_file_path.endswith(".docx")
    assert ready.preview_file_path and ready.preview_file_path.endswith("_origin.pdf")
    assert Path(ready.source_file_path).read_bytes() == b"docx-source"
    assert Path(ready.preview_file_path).read_bytes().startswith(b"%PDF")
    assert ready.processing_error is None
    assert service.mineru_client.calls[0].suffix == ".pdf"
    assert service.mineru_client.options[0]["model_version"] == "vlm"
    assert service.mineru_client.options[0]["language"] == "ch"
    assert len(store.list_chunks(DEFAULT_KNOWLEDGE_BASE_ID, ready.id)) == 1
    assert len(store.list_index_records(DEFAULT_KNOWLEDGE_BASE_ID, ready.id)) == 1


@pytest.mark.parametrize(
    ("suffix", "image_format"),
    [(".jpg", "JPEG"), (".jpeg", "JPEG"), (".png", "PNG")],
)
def test_image_processing_submits_native_file_and_keeps_pdf_viewer_preview(
    tmp_path, monkeypatch, suffix, image_format
):
    store = RecordingStore(tmp_path / "traceable_v2.db")
    mineru = FakeMinerUClient(store)
    service = DocumentProcessingService(
        store,
        tmp_path / "managed",
        converter=LibreOfficeConverter(executable="soffice"),
        mineru_client=mineru,
        indexer=FakeIndexer(store),
        embedding_provider=FakeEmbeddingProvider(),
    )
    source = tmp_path / f"教材页面{suffix}"
    if image_format == "PNG":
        image = Image.new("RGBA", (160, 90), (40, 90, 180, 128))
    else:
        image = Image.new("RGB", (160, 90), (40, 90, 180))
    image.save(source, format=image_format)
    monkeypatch.setattr(
        document_processing_module.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "native image processing must not invoke LibreOffice"
        ),
    )

    uploaded = service.register_upload(
        DEFAULT_KNOWLEDGE_BASE_ID,
        source,
        document_id="doc-1",
    )
    ready = service.process_document(DEFAULT_KNOWLEDGE_BASE_ID, uploaded.id)

    assert mineru.calls[0].suffix == suffix
    assert mineru.calls[0].read_bytes() == source.read_bytes()
    assert Path(ready.source_file_path or "").suffix == suffix
    assert ready.page_count == 1
    assert ready.preview_file_path is not None
    assert len(PdfReader(ready.preview_file_path).pages) == 1
    assert ready.processing_status == "ready"


def test_image_preview_conversion_rejects_mismatched_file_content(tmp_path):
    source = tmp_path / "伪装图片.jpg"
    Image.new("RGB", (64, 64), "white").save(source, format="PNG")

    with pytest.raises(ValueError, match="valid static JPG, JPEG, or PNG"):
        LibreOfficeConverter(executable="soffice").convert(
            source, tmp_path / "preview.pdf"
        )


def test_processing_automatically_embeds_every_index_record(tmp_path):
    provider = FakeEmbeddingProvider(vectors=[[0.25, 0.75]])
    store, service = make_service(tmp_path, embedding_provider=provider)
    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    service.register_upload(DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1")

    ready = service.process_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")

    records = store.list_index_records(DEFAULT_KNOWLEDGE_BASE_ID, ready.id)
    assert provider.calls == [[records[0].indexing_text]]
    assert records[0].vector == [0.25, 0.75]
    assert records[0].embedding_provider == "fake-embedding"
    assert records[0].embedding_model == "fake-model"
    assert records[0].embedding_dimension == 2


def test_local_indexer_builds_table_aware_records_from_parsed_blocks():
    rows = "".join(
        f"<tr><td>Item {index}</td><td>{'measurement ' * 85}</td></tr>"
        for index in range(4)
    )
    blocks = [
        Block("title", "doc-1", 0, 0, "title", "Results"),
        Block(
            "table",
            "doc-1",
            0,
            1,
            "table",
            html=(
                "<table><tr><th>Item</th><th>Measurement</th></tr>"
                f"{rows}</table>"
            ),
            metadata={"caption": "Observed values"},
        ),
    ]

    chunks, records = LocalDocumentIndexer().build("doc-1", blocks)

    assert len(chunks) == 1
    assert records[0].chunk_id == chunks[0].id
    assert records[0].indexing_text.startswith("Results\nObserved values")
    row_groups = [record for record in records if "Table columns:" in record.indexing_text]
    assert len(row_groups) >= 2
    assert all(record.chunk_id == chunks[0].id for record in row_groups)
    assert all(record.block_ids == chunks[0].block_ids for record in row_groups)


def test_processing_resolves_embedding_provider_from_runtime_config_when_not_injected(
    tmp_path, monkeypatch
):
    provider = FakeEmbeddingProvider(vectors=[[0.4, 0.6]])
    resolver_calls = []

    def resolve_provider(api_doc_path, *, runtime_root):
        resolver_calls.append((Path(api_doc_path), Path(runtime_root)))
        return provider

    monkeypatch.setattr(
        document_processing_module,
        "provider_from_api_doc",
        resolve_provider,
    )
    store = RecordingStore(tmp_path / "traceable_v2.db")
    service = DocumentProcessingService(
        store,
        tmp_path / "managed",
        converter=FakeConverter(store),
        mineru_client=FakeMinerUClient(store),
        indexer=FakeIndexer(store),
        embedding_api_doc_path=tmp_path / "provider.txt",
        runtime_root=tmp_path,
    )
    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    service.register_upload(DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1")

    ready = service.process_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")

    records = store.list_index_records(DEFAULT_KNOWLEDGE_BASE_ID, ready.id)
    assert resolver_calls == [(tmp_path / "provider.txt", tmp_path)]
    assert records[0].vector == [0.4, 0.6]


def test_runtime_configured_app_wires_dotenv_embedding_provider_into_processing(
    tmp_path, monkeypatch
):
    secret = "sk-runtime-wiring-secret"
    (tmp_path / ".env").write_text(
        "\n".join(
            (
                "OPENAI_EMBEDDING_BASE_URL=https://embedding.example/v1",
                f"OPENAI_EMBEDDING_API_KEY={secret}",
                "OPENAI_EMBEDDING_MODEL=runtime-model",
                "TRACEABLE_RAG_DATABASE_PATH=runtime.db",
                "TRACEABLE_RAG_MANAGED_STORAGE_ROOT=managed",
                "TRACEABLE_RAG_SETTINGS_PATH=settings.json",
                "TRACEABLE_RAG_STATIC_DIR=static",
            )
        ),
        encoding="utf-8",
    )
    for name in (
        "OPENAI_EMBEDDING_BASE_URL",
        "TRACEABLE_RAG_EMBEDDING_BASE_URL",
        "OPENAI_EMBEDDING_API_KEY",
        "TRACEABLE_RAG_EMBEDDING_API_KEY",
        "OPENAI_EMBEDDING_MODEL",
        "TRACEABLE_RAG_EMBEDDING_MODEL",
        "TRACEABLE_RAG_DATABASE_PATH",
        "TRACEABLE_RAG_MANAGED_STORAGE_ROOT",
        "TRACEABLE_RAG_SETTINGS_PATH",
        "TRACEABLE_RAG_STATIC_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    generated_ids = iter(uuid.UUID(int=value) for value in range(1, 20))
    monkeypatch.setattr(
        document_processing_module.uuid, "uuid4", lambda: next(generated_ids)
    )
    document_id = str(uuid.UUID(int=1))
    provider = FakeEmbeddingProvider(vectors=[[0.4, 0.6]])
    resolved_runtime = []

    def provider_factory(registry):
        resolved_runtime.append(registry._current_runtime())
        return provider

    real_processing_service = DocumentProcessingService

    def processing_service_factory(store, managed_root, **kwargs):
        return real_processing_service(
            store,
            managed_root,
            converter=FakeConverter(store, document_id=document_id),
            mineru_client=FakeMinerUClient(store, document_id=document_id),
            indexer=FakeIndexer(store),
            embedding_provider=kwargs.get("embedding_provider"),
            runtime_root=kwargs.get("runtime_root"),
        )

    monkeypatch.setattr(
        web_app_module.ProviderRegistry, "embedding_provider", provider_factory
    )
    monkeypatch.setattr(
        web_app_module, "DocumentProcessingService", processing_service_factory
    )
    with TestClient(web_app_module.RuntimeConfiguredApp()) as client:
        uploaded = client.post(
            f"/api/knowledge-bases/{DEFAULT_KNOWLEDGE_BASE_ID}/documents/upload",
            files={"file": ("source.docx", b"docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        )
        processed_document = wait_for_document_status(
            client, DEFAULT_KNOWLEDGE_BASE_ID, document_id, "ready"
        )

    assert uploaded.status_code == 201
    assert processed_document["embedding_ready"] is True
    records = EvidenceStore(tmp_path / "runtime.db").list_index_records(
        DEFAULT_KNOWLEDGE_BASE_ID, document_id
    )
    assert records[0].vector == [0.4, 0.6]
    assert provider.calls == [[records[0].indexing_text]]
    assert resolved_runtime
    assert resolved_runtime[0].embedding_base_url == "https://embedding.example/v1"
    assert resolved_runtime[0].embedding_api_key == secret
    assert resolved_runtime[0].embedding_model == "runtime-model"
    assert secret not in uploaded.text
    assert secret not in json.dumps(processed_document)


def test_runtime_provider_resolution_failure_keeps_document_keyword_searchable_without_secret(
    tmp_path, monkeypatch, caplog
):
    secret = "sk-runtime-resolver-secret"
    (tmp_path / ".env").write_text(
        "\n".join(
            (
                "OPENAI_EMBEDDING_BASE_URL=https://embedding.example/v1",
                f"OPENAI_EMBEDDING_API_KEY={secret}",
                "OPENAI_EMBEDDING_MODEL=runtime-model",
                "TRACEABLE_RAG_DATABASE_PATH=runtime.db",
                "TRACEABLE_RAG_MANAGED_STORAGE_ROOT=managed",
                "TRACEABLE_RAG_SETTINGS_PATH=settings.json",
                "TRACEABLE_RAG_STATIC_DIR=static",
            )
        ),
        encoding="utf-8",
    )
    for name in (
        "OPENAI_EMBEDDING_BASE_URL",
        "TRACEABLE_RAG_EMBEDDING_BASE_URL",
        "OPENAI_EMBEDDING_API_KEY",
        "TRACEABLE_RAG_EMBEDDING_API_KEY",
        "OPENAI_EMBEDDING_MODEL",
        "TRACEABLE_RAG_EMBEDDING_MODEL",
        "TRACEABLE_RAG_DATABASE_PATH",
        "TRACEABLE_RAG_MANAGED_STORAGE_ROOT",
        "TRACEABLE_RAG_SETTINGS_PATH",
        "TRACEABLE_RAG_STATIC_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    generated_ids = iter(uuid.UUID(int=value) for value in range(1, 20))
    monkeypatch.setattr(
        document_processing_module.uuid, "uuid4", lambda: next(generated_ids)
    )
    document_id = str(uuid.UUID(int=1))

    def fail_provider_resolution(*_args, **_kwargs):
        raise RuntimeError(f"provider construction failed: {secret}")

    real_processing_service = DocumentProcessingService

    def processing_service_factory(store, managed_root, **kwargs):
        return real_processing_service(
            store,
            managed_root,
            converter=FakeConverter(store, document_id=document_id),
            mineru_client=FakeMinerUClient(store, document_id=document_id),
            indexer=FakeIndexer(store),
            embedding_provider=kwargs.get("embedding_provider"),
            runtime_root=kwargs.get("runtime_root"),
        )

    monkeypatch.setattr(
        web_app_module.ProviderRegistry,
        "embedding_provider",
        fail_provider_resolution,
    )
    monkeypatch.setattr(
        document_processing_module, "provider_from_api_doc", fail_provider_resolution
    )
    monkeypatch.setattr(
        web_app_module, "DocumentProcessingService", processing_service_factory
    )
    with caplog.at_level(
        logging.WARNING, logger="traceable_rag.document_processing"
    ), TestClient(web_app_module.RuntimeConfiguredApp()) as client:
        uploaded = client.post(
            f"/api/knowledge-bases/{DEFAULT_KNOWLEDGE_BASE_ID}/documents/upload",
            files={"file": ("source.docx", b"docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        )
        processed_document = wait_for_document_status(
            client, DEFAULT_KNOWLEDGE_BASE_ID, document_id, "ready"
        )
        searched = client.post(
            "/api/retrieval",
            json={
                "query": "generation-1",
                "knowledge_base_ids": [DEFAULT_KNOWLEDGE_BASE_ID],
                "top_k": 5,
                "mode": "keyword",
            },
        )

    assert uploaded.status_code == 201
    assert processed_document["processing_status"] == "ready"
    assert processed_document["embedding_ready"] is False
    assert searched.status_code == 200
    assert searched.json()["pipeline_used"] == "keyword"
    assert searched.json()["results"]
    records = EvidenceStore(tmp_path / "runtime.db").list_index_records(
        DEFAULT_KNOWLEDGE_BASE_ID, document_id
    )
    assert records[0].vector is None
    assert "回退" in caplog.text
    assert secret not in caplog.text
    assert secret not in uploaded.text
    assert secret not in json.dumps(processed_document)
    assert secret not in searched.text


def test_processing_embedding_failure_keeps_ready_keyword_index_and_safe_log(
    tmp_path, caplog
):
    secret = "sk-secret-upstream-body"
    provider = FakeEmbeddingProvider(error=RuntimeError(f"provider failed: {secret}"))
    store, service = make_service(tmp_path, embedding_provider=provider)
    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    service.register_upload(DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1")

    with caplog.at_level(logging.WARNING, logger="traceable_rag.document_processing"):
        ready = service.process_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")

    records = store.list_index_records(DEFAULT_KNOWLEDGE_BASE_ID, ready.id)
    assert ready.processing_status == "ready"
    assert ready.processing_error is None
    assert len(records) == 1
    assert records[0].vector is None
    assert records[0].embedding_provider is None
    assert "回退" in caplog.text
    assert secret not in caplog.text


@pytest.mark.parametrize(
    "vectors",
    [
        [[0.1, 0.9]],
        [[0.1, 0.9], [0.2, 0.3, 0.5]],
        [[0.1, math.nan], [0.2, 0.8]],
        [[0.1, math.inf], [0.2, 0.8]],
    ],
    ids=["wrong-count", "mixed-dimension", "nan", "infinity"],
)
def test_processing_invalid_embedding_batch_falls_back_atomically(
    tmp_path, vectors
):
    provider = FakeEmbeddingProvider(vectors=vectors)
    store, service = make_service(
        tmp_path,
        record_count=2,
        embedding_provider=provider,
    )
    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    service.register_upload(DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1")

    ready = service.process_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")

    records = store.list_index_records(DEFAULT_KNOWLEDGE_BASE_ID, ready.id)
    assert ready.processing_status == "ready"
    assert len(records) == 2
    assert all(record.vector is None for record in records)
    assert all(record.embedding_provider is None for record in records)


@pytest.mark.parametrize(
    ("failure_stage", "kwargs"),
    [
        ("convert", {"converter_error": RuntimeError("libreoffice unavailable")}),
        ("mineru", {"mineru_error": RuntimeError("mineru failed")}),
        ("index", {"indexer_error": RuntimeError("index failed")}),
    ],
)
def test_processing_failure_is_persisted_and_readable(tmp_path, failure_stage, kwargs):
    store, service = make_service(tmp_path, **kwargs)
    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    service.register_upload(DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1")

    with pytest.raises(RuntimeError):
        service.process_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")

    failed = store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")
    assert failed is not None
    assert failed.processing_status == "failed"
    assert failure_stage in (failed.processing_error or "")


def test_processing_failure_does_not_persist_signed_url(tmp_path):
    signed_url = "https://signed.example/private-secret"
    store, service = make_service(
        tmp_path,
        mineru_error=RuntimeError(f"MinerU upload failed at {signed_url}"),
    )
    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    service.register_upload(DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1")

    with pytest.raises(RuntimeError):
        service.process_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")

    failed = store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")
    assert failed is not None
    assert failed.processing_error == "mineru: operation failed"
    assert signed_url not in failed.processing_error


def test_retry_replaces_old_derived_data_without_duplicates(tmp_path):
    store, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    service.register_upload(DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1")

    first_ready = service.process_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")
    first_preview = Path(first_ready.preview_file_path)
    second_ready = service.process_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")

    blocks = store.list_blocks(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")
    assert [block.text for block in blocks] == ["generation-2"]
    assert len(store.list_chunks(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")) == 1
    assert len(store.list_index_records(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")) == 1
    assert not first_preview.exists()
    assert Path(second_ready.preview_file_path).is_file()
    assert ".staging" not in second_ready.preview_file_path
    run_roots = list(service.managed_root.rglob("runs"))
    assert sum(len(list(root.iterdir())) for root in run_roots) == 1


def test_post_commit_cleanup_failure_keeps_ready_graph_and_returns_success(
    tmp_path, monkeypatch, caplog
):
    store, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    service.register_upload(DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1")
    service.process_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")
    previous_runs = set(service.managed_root.rglob("runs/*"))
    sensitive_path = "/private/managed/customer-a/old-run"

    def fail_cleanup(_document_dir, _active_run):
        raise OSError(f"cleanup failed for {sensitive_path}")

    monkeypatch.setattr(service, "_cleanup_previous_runs", fail_cleanup)
    with caplog.at_level(
        logging.WARNING, logger="traceable_rag.document_processing"
    ), TestClient(
        create_app(
            db_path=store.db_path,
            managed_root=service.managed_root,
            document_processing_service=service,
        )
    ) as client:
        response = client.post(
            f"/api/knowledge-bases/{DEFAULT_KNOWLEDGE_BASE_ID}"
            "/documents/doc-1/index"
        )
        wait_for_document_status(
            client, DEFAULT_KNOWLEDGE_BASE_ID, "doc-1", "ready"
        )

    ready = store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")
    assert ready is not None
    assert ready.processing_status == "ready"
    assert ready.processing_error is None
    assert [
        block.text
        for block in store.list_blocks(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")
    ] == ["generation-2"]
    retained_runs = set(service.managed_root.rglob("runs/*"))
    assert previous_runs < retained_runs
    assert Path(ready.preview_file_path).parents[2] in retained_runs
    assert response.status_code == 202
    assert response.json()["job"]["status"] == "queued"
    assert "previous managed document runs" in caplog.text.lower()
    assert sensitive_path not in caplog.text


def test_mineru_retry_failure_preserves_previous_ready_evidence_and_preview(tmp_path):
    store, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    service.register_upload(DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1")
    ready = service.process_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")
    old_preview = Path(ready.preview_file_path)
    old_blocks = store.list_blocks(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")
    old_chunks = store.list_chunks(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")
    old_records = store.list_index_records(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")
    service.mineru_client.error = RuntimeError("retry mineru failed")

    with pytest.raises(RuntimeError, match="retry mineru failed"):
        service.process_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")

    failed = store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")
    assert failed is not None
    assert failed.processing_status == "failed"
    assert failed.preview_file_path == str(old_preview)
    assert old_preview.is_file()
    assert ".staging" not in failed.preview_file_path
    assert failed.mineru_result_dir and ".staging" not in failed.mineru_result_dir
    assert store.list_blocks(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") == old_blocks
    assert store.list_chunks(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") == old_chunks
    assert store.list_index_records(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") == old_records
    assert list(service.managed_root.rglob(".staging")) == []


def test_index_retry_failure_preserves_previous_ready_graph_and_cleans_new_run(tmp_path):
    store, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    service.register_upload(DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1")
    ready = service.process_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")
    old_preview = Path(ready.preview_file_path)
    old_graph = (
        store.list_blocks(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1"),
        store.list_assets(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1"),
        store.list_chunks(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1"),
        store.list_index_records(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1"),
    )
    old_runs = sorted(service.managed_root.rglob("runs/*"))
    service.indexer.error = RuntimeError("retry index failed")

    with pytest.raises(RuntimeError, match="retry index failed"):
        service.process_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")

    failed = store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")
    assert failed and failed.processing_status == "failed"
    assert failed.preview_file_path == str(old_preview)
    assert old_preview.is_file()
    assert sorted(service.managed_root.rglob("runs/*")) == old_runs
    assert store.list_blocks(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") == old_graph[0]
    assert store.list_assets(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") == old_graph[1]
    assert store.list_chunks(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") == old_graph[2]
    assert store.list_index_records(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") == old_graph[3]


def test_atomic_persist_failure_preserves_previous_ready_graph_and_cleans_new_run(tmp_path):
    store, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    service.register_upload(DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1")
    ready = service.process_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")
    old_preview = Path(ready.preview_file_path)
    old_graph = (
        store.list_blocks(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1"),
        store.list_assets(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1"),
        store.list_chunks(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1"),
        store.list_index_records(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1"),
    )
    old_runs = sorted(service.managed_root.rglob("runs/*"))
    store.persist_error = RuntimeError("atomic persist failed")

    with pytest.raises(RuntimeError, match="atomic persist failed"):
        service.process_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")

    failed = store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")
    assert failed and failed.processing_status == "failed"
    assert failed.preview_file_path == str(old_preview)
    assert old_preview.is_file()
    assert sorted(service.managed_root.rglob("runs/*")) == old_runs
    assert store.list_blocks(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") == old_graph[0]
    assert store.list_assets(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") == old_graph[1]
    assert store.list_chunks(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") == old_graph[2]
    assert store.list_index_records(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") == old_graph[3]


def test_same_document_process_lock_is_nonblocking_across_threads(tmp_path):
    store, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    service.register_upload(DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1")
    entered = threading.Event()
    release = threading.Event()
    original_parse = service.mineru_client.parse_local_file

    def blocking_parse(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_parse(*args, **kwargs)

    service.mineru_client.parse_local_file = blocking_parse
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            service.process_document, DEFAULT_KNOWLEDGE_BASE_ID, "doc-1"
        )
        assert entered.wait(timeout=5)
        second = executor.submit(
            service.process_document, DEFAULT_KNOWLEDGE_BASE_ID, "doc-1"
        )
        with pytest.raises(DocumentBusyError):
            second.result(timeout=5)
        release.set()
        assert first.result(timeout=5).processing_status == "ready"


def test_services_with_same_database_and_different_managed_roots_share_lock(tmp_path):
    store, first_service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    first_service.register_upload(
        DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1"
    )
    second_store = EvidenceStore(store.db_path)
    second_service = DocumentProcessingService(second_store, tmp_path / "other-managed")
    entered = threading.Event()
    release = threading.Event()
    original_parse = first_service.mineru_client.parse_local_file

    def blocking_parse(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_parse(*args, **kwargs)

    first_service.mineru_client.parse_local_file = blocking_parse
    with ThreadPoolExecutor(max_workers=2) as executor:
        processing = executor.submit(
            first_service.process_document, DEFAULT_KNOWLEDGE_BASE_ID, "doc-1"
        )
        assert entered.wait(timeout=5)
        with pytest.raises(DocumentBusyError):
            second_service.process_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")
        release.set()
        assert processing.result(timeout=5).processing_status == "ready"

    assert first_service.lock_root == second_service.lock_root


def test_processing_one_document_does_not_block_uploading_another(tmp_path):
    store, service = make_service(tmp_path)
    first_source = tmp_path / "first.docx"
    second_source = tmp_path / "second.docx"
    first_source.write_bytes(b"first")
    second_source.write_bytes(b"second")
    service.register_upload(
        DEFAULT_KNOWLEDGE_BASE_ID, first_source, document_id="doc-1"
    )
    entered = threading.Event()
    release = threading.Event()
    original_parse = service.mineru_client.parse_local_file

    def blocking_parse(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_parse(*args, **kwargs)

    service.mineru_client.parse_local_file = blocking_parse
    with ThreadPoolExecutor(max_workers=2) as executor:
        processing = executor.submit(
            service.process_document, DEFAULT_KNOWLEDGE_BASE_ID, "doc-1"
        )
        assert entered.wait(timeout=5)
        second = service.register_upload(
            DEFAULT_KNOWLEDGE_BASE_ID, second_source, document_id="doc-2"
        )
        release.set()
        assert processing.result(timeout=5).processing_status == "ready"

    assert second.id == "doc-2"
    assert store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-2") is not None


def test_knowledge_base_lock_blocks_concurrent_upload_before_document_lock(tmp_path):
    _, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"source")

    with service._knowledge_base_lock(DEFAULT_KNOWLEDGE_BASE_ID):
        with pytest.raises(DocumentBusyError):
            service.register_upload(
                DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1"
            )

    assert service.store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") is None


def test_knowledge_base_lock_is_shared_across_processes(tmp_path):
    store, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"source")
    script = """
import sys
from traceable_rag.document_processing import DocumentProcessingService
from traceable_rag.store import EvidenceStore
service = DocumentProcessingService(EvidenceStore(sys.argv[1]), sys.argv[2])
with service._knowledge_base_lock('default'):
    print('locked', flush=True)
    sys.stdin.read(1)
"""
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(store.db_path),
            str(service.managed_root),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(DocumentBusyError):
            service.register_upload(
                DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1"
            )
    finally:
        child.stdin.write("x")
        child.stdin.flush()
        child.wait(timeout=5)
    assert child.returncode == 0, child.stderr.read()
    assert store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") is None


def test_same_document_lock_is_nonblocking_across_processes(tmp_path):
    store, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    service.register_upload(DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1")
    script = """
import sys
from traceable_rag.document_processing import DocumentProcessingService
from traceable_rag.store import EvidenceStore
service = DocumentProcessingService(EvidenceStore(sys.argv[1]), sys.argv[2])
with service._document_lock('default', 'doc-1'):
    print('locked', flush=True)
    sys.stdin.read(1)
"""
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(store.db_path),
            str(service.managed_root),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(DocumentBusyError):
            service.process_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")
    finally:
        child.stdin.write("x")
        child.stdin.flush()
        child.wait(timeout=5)
    assert child.returncode == 0, child.stderr.read()


def test_delete_uses_same_lock_and_cannot_be_undone_by_inflight_processing(tmp_path):
    store, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    service.register_upload(DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1")
    entered = threading.Event()
    release = threading.Event()
    original_parse = service.mineru_client.parse_local_file

    def blocking_parse(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_parse(*args, **kwargs)

    service.mineru_client.parse_local_file = blocking_parse
    with ThreadPoolExecutor(max_workers=2) as executor:
        processing = executor.submit(
            service.process_document, DEFAULT_KNOWLEDGE_BASE_ID, "doc-1"
        )
        assert entered.wait(timeout=5)
        with pytest.raises(DocumentBusyError):
            service.delete_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")
        release.set()
        processing.result(timeout=5)

    assert service.delete_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") is True
    assert store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") is None


def test_rename_holds_kb_then_document_lock_and_cannot_race_delete(tmp_path, monkeypatch):
    store, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"source")
    uploaded = service.register_upload(
        DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1"
    )
    entered = threading.Event()
    release = threading.Event()
    original_update = store.update_document_display_name

    def blocking_update(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_update(*args, **kwargs)

    monkeypatch.setattr(store, "update_document_display_name", blocking_update)
    with ThreadPoolExecutor(max_workers=2) as executor:
        renaming = executor.submit(
            service.rename_document,
            DEFAULT_KNOWLEDGE_BASE_ID,
            uploaded.id,
            "新名称",
        )
        assert entered.wait(timeout=5)
        with pytest.raises(DocumentBusyError):
            service.delete_document(DEFAULT_KNOWLEDGE_BASE_ID, uploaded.id)
        release.set()
        renamed = renaming.result(timeout=5)

    assert renamed.display_name == "新名称"
    assert renamed.processing_status == uploaded.processing_status
    assert renamed.source_file_path == uploaded.source_file_path


def test_delete_only_removes_deterministic_managed_document_directory(tmp_path):
    store, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    document = service.register_upload(
        DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1"
    )
    managed_file = Path(document.source_file_path)
    outside = tmp_path / "must-survive"
    outside.mkdir()
    (outside / "data.json").write_text("safe", encoding="utf-8")
    store.upsert_document(replace(document, mineru_result_dir=str(outside)))

    assert service.delete_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") is True

    assert not managed_file.exists()
    assert (outside / "data.json").read_text(encoding="utf-8") == "safe"
    assert store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") is None


def test_duplicate_upload_cleans_new_file_without_touching_existing_document(tmp_path):
    store, service = make_service(tmp_path)
    source = tmp_path / "same.docx"
    source.write_bytes(b"same-content")
    existing = service.register_upload(
        DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1"
    )
    existing_path = Path(existing.source_file_path)
    before = {
        path
        for path in service.managed_root.rglob("*")
        if service.lock_root not in path.parents
    }

    with pytest.raises(sqlite3.IntegrityError):
        service.register_upload(
            DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-duplicate"
        )

    assert existing_path.read_bytes() == b"same-content"
    after = {
        path
        for path in service.managed_root.rglob("*")
        if service.lock_root not in path.parents
    }
    assert after == before


def test_register_upload_bytes_rejects_symlinked_incoming_without_external_write(tmp_path):
    store, service = make_service(tmp_path)
    outside = tmp_path / "outside-incoming"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("safe", encoding="utf-8")
    incoming = service.managed_root / ".incoming"
    incoming.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        service.register_upload_bytes(
            DEFAULT_KNOWLEDGE_BASE_ID,
            "source.docx",
            b"source",
            document_id="doc-1",
        )

    assert sentinel.read_text(encoding="utf-8") == "safe"
    assert sorted(path.name for path in outside.iterdir()) == ["keep.txt"]
    assert store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") is None


def test_register_upload_rejects_existing_document_id_without_changing_ready_graph(tmp_path):
    store, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"first")
    service.register_upload(DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1")
    ready = service.process_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")
    old_source = Path(ready.source_file_path)
    old_graph = (
        store.list_blocks(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1"),
        store.list_assets(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1"),
        store.list_chunks(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1"),
        store.list_index_records(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1"),
    )
    replacement = tmp_path / "replacement.docx"
    replacement.write_bytes(b"replacement")

    with pytest.raises(DocumentAlreadyExistsError):
        service.register_upload(
            DEFAULT_KNOWLEDGE_BASE_ID, replacement, document_id="doc-1"
        )

    assert store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") == ready
    assert old_source.read_bytes() == b"first"
    assert store.list_blocks(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") == old_graph[0]
    assert store.list_assets(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") == old_graph[1]
    assert store.list_chunks(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") == old_graph[2]
    assert store.list_index_records(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") == old_graph[3]


def test_register_upload_rejects_document_id_that_exists_in_another_knowledge_base(tmp_path):
    store, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    service.register_upload(DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="shared-id")
    from traceable_rag.models import KnowledgeBase

    store.create_knowledge_base(KnowledgeBase(id="other", name="Other"))

    with pytest.raises(DocumentAlreadyExistsError):
        service.register_upload("other", source, document_id="shared-id")


def test_ensure_uploaded_reuses_matching_document_and_rejects_source_mismatch(tmp_path):
    store, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"same")
    uploaded = service.register_upload(
        DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1"
    )
    before_files = set(service.managed_root.rglob("*"))

    ensured = service.ensure_uploaded(
        DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1"
    )

    assert ensured.id == uploaded.id
    assert ensured.source_file_path == uploaded.source_file_path
    assert ensured.file_hash == uploaded.file_hash
    assert set(service.managed_root.rglob("*")) == before_files

    renamed = tmp_path / "renamed.docx"
    renamed.write_bytes(b"same")
    with pytest.raises(DocumentSourceMismatchError, match="filename"):
        service.ensure_uploaded(
            DEFAULT_KNOWLEDGE_BASE_ID, renamed, document_id="doc-1"
        )

    source.write_bytes(b"changed")
    with pytest.raises(DocumentSourceMismatchError, match="hash"):
        service.ensure_uploaded(
            DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1"
        )


def test_ensure_uploaded_allows_failed_processing_to_retry_same_source(tmp_path):
    store, service = make_service(tmp_path, mineru_error=RuntimeError("temporary"))
    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    uploaded = service.ensure_uploaded(
        DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1"
    )
    with pytest.raises(RuntimeError, match="temporary"):
        service.process_document(DEFAULT_KNOWLEDGE_BASE_ID, uploaded.id)
    service.mineru_client.error = None

    ensured = service.ensure_uploaded(
        DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1"
    )
    ready = service.process_document(DEFAULT_KNOWLEDGE_BASE_ID, ensured.id)

    assert ready.processing_status == "ready"


def test_delete_unlinks_managed_top_level_symlink_without_following_it(tmp_path):
    store, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    document = service.register_upload(
        DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1"
    )
    document_dir = Path(document.source_file_path).parents[1]
    outside = tmp_path / "outside-tree"
    outside.mkdir()
    protected = outside / "protected.txt"
    protected.write_text("keep", encoding="utf-8")
    shutil.rmtree(document_dir)
    document_dir.symlink_to(outside, target_is_directory=True)

    assert service.delete_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") is True

    assert not document_dir.exists()
    assert protected.read_text(encoding="utf-8") == "keep"


def test_delete_restores_managed_directory_when_database_delete_fails(tmp_path, monkeypatch):
    store, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    document = service.register_upload(
        DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1"
    )
    managed_source = Path(document.source_file_path)
    monkeypatch.setattr(
        store,
        "delete_document",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("database delete failed")),
    )

    with pytest.raises(RuntimeError, match="database delete failed"):
        service.delete_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")

    assert store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") is not None
    assert managed_source.read_bytes() == b"docx"
    trash_root = service.managed_root / ".trash"
    assert not trash_root.exists() or list(trash_root.iterdir()) == []


def test_delete_does_not_touch_database_when_managed_rename_fails(tmp_path, monkeypatch):
    store, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    document = service.register_upload(
        DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1"
    )
    managed_source = Path(document.source_file_path)
    document_dir = managed_source.parents[1]
    original_rename = Path.rename

    def fail_document_rename(path, target):
        if path == document_dir:
            raise OSError("managed rename failed")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_document_rename)

    with pytest.raises(OSError, match="managed rename failed"):
        service.delete_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1")

    assert store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") is not None
    assert managed_source.read_bytes() == b"docx"


def test_delete_reports_success_when_trash_cleanup_fails(tmp_path, monkeypatch):
    store, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    document = service.register_upload(
        DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1"
    )
    document_dir = Path(document.source_file_path).parents[1]
    original_remove = service._safe_remove_tree

    def fail_trash_cleanup(path):
        if ".trash" in Path(path).parts:
            raise OSError("trash cleanup failed")
        return original_remove(path)

    monkeypatch.setattr(service, "_safe_remove_tree", fail_trash_cleanup)

    assert service.delete_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") is True

    assert store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") is None
    assert not document_dir.exists()
    trash_entries = list((service.managed_root / ".trash").iterdir())
    assert len(trash_entries) == 1


def test_delete_knowledge_base_keeps_all_documents_when_kb_root_move_fails(
    tmp_path, monkeypatch
):
    store, service = make_service(tmp_path)
    first_source = tmp_path / "first.docx"
    second_source = tmp_path / "second.docx"
    first_source.write_bytes(b"first")
    second_source.write_bytes(b"second")
    first = service.register_upload(
        DEFAULT_KNOWLEDGE_BASE_ID, first_source, document_id="doc-a"
    )
    second = service.register_upload(
        DEFAULT_KNOWLEDGE_BASE_ID, second_source, document_id="doc-b"
    )
    first_managed = Path(first.source_file_path)
    second_managed = Path(second.source_file_path)
    knowledge_base_dir = service._knowledge_base_dir(DEFAULT_KNOWLEDGE_BASE_ID)
    original_rename = Path.rename

    def fail_knowledge_base_move(path, target):
        if path == knowledge_base_dir:
            raise OSError("knowledge base move failed")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_knowledge_base_move)

    with pytest.raises(OSError, match="knowledge base move failed"):
        service.delete_knowledge_base(
            DEFAULT_KNOWLEDGE_BASE_ID, confirm_name="默认知识库"
        )

    assert store.get_knowledge_base(DEFAULT_KNOWLEDGE_BASE_ID) is not None
    assert store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-a") is not None
    assert store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-b") is not None
    assert first_managed.read_bytes() == b"first"
    assert second_managed.read_bytes() == b"second"


def test_delete_knowledge_base_rolls_back_before_commit_when_cleanup_probe_fails(
    tmp_path, monkeypatch
):
    store, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"source")
    document = service.register_upload(
        DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1"
    )
    managed_source = Path(document.source_file_path)
    original_remove = service._safe_remove_tree

    def fail_cleanup_probe(path):
        if Path(path).name == ".cleanup-probe":
            raise OSError("cleanup probe failed")
        return original_remove(path)

    monkeypatch.setattr(service, "_safe_remove_tree", fail_cleanup_probe)

    with pytest.raises(OSError, match="cleanup probe failed"):
        service.delete_knowledge_base(
            DEFAULT_KNOWLEDGE_BASE_ID, confirm_name="默认知识库"
        )

    assert store.get_knowledge_base(DEFAULT_KNOWLEDGE_BASE_ID) is not None
    assert store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") is not None
    assert managed_source.read_bytes() == b"source"
    assert list(service.recovery_manifests_root.glob("*.json")) == []
    assert list(service.recovery_tombstones_root.glob("*")) == []


def test_delete_knowledge_base_keeps_manifest_after_post_commit_cleanup_failure(
    tmp_path, monkeypatch
):
    store, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"source")
    document = service.register_upload(
        DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1"
    )
    managed_source = Path(document.source_file_path)
    original_remove = service._safe_remove_tree

    def remove_then_fail(path):
        if Path(path).parent == service.recovery_tombstones_root:
            original_remove(path)
            raise OSError("cleanup failed after committed removal")
        return original_remove(path)

    monkeypatch.setattr(service, "_safe_remove_tree", remove_then_fail)

    with pytest.raises(OSError, match="committed removal"):
        service.delete_knowledge_base(
            DEFAULT_KNOWLEDGE_BASE_ID, confirm_name="默认知识库"
        )

    assert store.get_knowledge_base(DEFAULT_KNOWLEDGE_BASE_ID) is None
    assert not managed_source.exists()
    assert list(service.recovery_manifests_root.glob("*.json"))

    recovered = DocumentProcessingService(store, service.managed_root)

    assert list(recovered.recovery_manifests_root.glob("*.json")) == []
    assert list(recovered.recovery_tombstones_root.glob("*")) == []


def test_delete_knowledge_base_cleans_partial_manifest_when_manifest_creation_fails(
    tmp_path, monkeypatch
):
    store, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"source")
    document = service.register_upload(
        DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1"
    )
    managed_source = Path(document.source_file_path)
    def dump_then_fail(value, output, **kwargs):
        output.write('{"partial":')
        output.flush()
        raise OSError("manifest creation failed after write")

    monkeypatch.setattr(json, "dump", dump_then_fail)

    with pytest.raises(OSError, match="manifest creation failed after write"):
        service.delete_knowledge_base(
            DEFAULT_KNOWLEDGE_BASE_ID, confirm_name="默认知识库"
        )

    assert store.get_knowledge_base(DEFAULT_KNOWLEDGE_BASE_ID) is not None
    assert store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-1") is not None
    assert managed_source.read_bytes() == b"source"
    assert list(service.recovery_manifests_root.iterdir()) == []
    assert list(service.recovery_tombstones_root.iterdir()) == []


def test_pending_kb_delete_before_database_commit_is_restored_on_service_restart(
    tmp_path, monkeypatch
):
    store, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"source")
    document = service.register_upload(
        DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1"
    )
    managed_source = Path(document.source_file_path)

    monkeypatch.setattr(
        service,
        "_commit_knowledge_base_delete",
        lambda _kb_id: (_ for _ in ()).throw(SimulatedProcessCrash()),
        raising=False,
    )

    with pytest.raises(SimulatedProcessCrash):
        service.delete_knowledge_base(
            DEFAULT_KNOWLEDGE_BASE_ID, confirm_name="默认知识库"
        )

    assert store.get_knowledge_base(DEFAULT_KNOWLEDGE_BASE_ID) is not None
    assert not managed_source.exists()
    manifests_root = service.managed_root / ".recovery" / "manifests"
    assert list(manifests_root.glob("*.json"))

    recovered = DocumentProcessingService(store, service.managed_root)

    assert managed_source.read_bytes() == b"source"
    assert list(manifests_root.glob("*.json")) == []
    assert list((recovered.managed_root / ".recovery" / "tombstones").glob("*")) == []


def test_pending_kb_delete_after_database_commit_is_finalized_on_service_restart(
    tmp_path, monkeypatch
):
    store, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"source")
    document = service.register_upload(
        DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1"
    )
    managed_source = Path(document.source_file_path)

    monkeypatch.setattr(
        service,
        "_finalize_knowledge_base_delete",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SimulatedProcessCrash()),
        raising=False,
    )

    with pytest.raises(SimulatedProcessCrash):
        service.delete_knowledge_base(
            DEFAULT_KNOWLEDGE_BASE_ID, confirm_name="默认知识库"
        )

    assert store.get_knowledge_base(DEFAULT_KNOWLEDGE_BASE_ID) is None
    assert not managed_source.exists()
    manifests_root = service.managed_root / ".recovery" / "manifests"
    assert list(manifests_root.glob("*.json"))

    recovered = DocumentProcessingService(store, service.managed_root)

    assert store.get_knowledge_base(DEFAULT_KNOWLEDGE_BASE_ID) is None
    assert list(manifests_root.glob("*.json")) == []
    assert list((recovered.managed_root / ".recovery" / "tombstones").glob("*")) == []


def test_failed_restart_recovery_preserves_manifest_and_tombstone_for_retry(
    tmp_path, monkeypatch
):
    store, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"source")
    document = service.register_upload(
        DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1"
    )
    managed_source = Path(document.source_file_path)
    monkeypatch.setattr(
        service,
        "_commit_knowledge_base_delete",
        lambda _kb_id: (_ for _ in ()).throw(SimulatedProcessCrash()),
        raising=False,
    )
    with pytest.raises(SimulatedProcessCrash):
        service.delete_knowledge_base(
            DEFAULT_KNOWLEDGE_BASE_ID, confirm_name="默认知识库"
        )

    manifests_root = service.managed_root / ".recovery" / "manifests"
    tombstones_root = service.managed_root / ".recovery" / "tombstones"
    manifest_paths = list(manifests_root.glob("*.json"))
    tombstone_paths = list(tombstones_root.glob("*"))
    assert manifest_paths and tombstone_paths

    original_rename = Path.rename

    def fail_restore_rename(path, target):
        if tombstones_root in path.parents:
            raise OSError("restore rename failed")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_restore_rename)
    DocumentProcessingService(store, service.managed_root)

    assert list(manifests_root.glob("*.json")) == manifest_paths
    assert list(tombstones_root.glob("*")) == tombstone_paths
    assert not managed_source.exists()


def test_failed_restart_recovery_blocks_all_kb_mutations_without_split_roots(
    tmp_path, monkeypatch
):
    store, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    new_source = tmp_path / "new.docx"
    source.write_bytes(b"source")
    new_source.write_bytes(b"new")
    document = service.register_upload(
        DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1"
    )
    active_root = service._knowledge_base_dir(DEFAULT_KNOWLEDGE_BASE_ID)
    monkeypatch.setattr(
        service,
        "_commit_knowledge_base_delete",
        lambda _kb_id: (_ for _ in ()).throw(SimulatedProcessCrash()),
    )
    with pytest.raises(SimulatedProcessCrash):
        service.delete_knowledge_base(
            DEFAULT_KNOWLEDGE_BASE_ID, confirm_name="默认知识库"
        )

    tombstones_root = service.recovery_tombstones_root
    original_rename = Path.rename

    def fail_restore_rename(path, target):
        if tombstones_root in path.parents:
            raise OSError("restore rename failed")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_restore_rename)
    blocked = DocumentProcessingService(store, service.managed_root)

    operations = (
        lambda: blocked.register_upload(
            DEFAULT_KNOWLEDGE_BASE_ID, new_source, document_id="doc-new"
        ),
        lambda: blocked.register_upload_bytes(
            DEFAULT_KNOWLEDGE_BASE_ID,
            "new-bytes.docx",
            b"new bytes",
            document_id="doc-new-bytes",
        ),
        lambda: blocked.ensure_uploaded(
            DEFAULT_KNOWLEDGE_BASE_ID, new_source, document_id="doc-ensure"
        ),
        lambda: blocked.process_document(DEFAULT_KNOWLEDGE_BASE_ID, document.id),
        lambda: blocked.rename_document(
            DEFAULT_KNOWLEDGE_BASE_ID, document.id, "blocked rename"
        ),
        lambda: blocked.delete_document(DEFAULT_KNOWLEDGE_BASE_ID, document.id),
        lambda: blocked.update_knowledge_base(
            DEFAULT_KNOWLEDGE_BASE_ID, description="blocked update"
        ),
        lambda: blocked.delete_knowledge_base(
            DEFAULT_KNOWLEDGE_BASE_ID, confirm_name="默认知识库"
        ),
    )
    for operation in operations:
        with pytest.raises(RuntimeError, match="recovery"):
            operation()

    assert not active_root.exists()
    assert len(list(tombstones_root.glob("*/payload"))) == 1
    assert store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, document.id) is not None
    assert store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, "doc-new") is None


def test_kb_delete_fsyncs_recovery_transitions_in_durable_order(
    tmp_path, monkeypatch
):
    store, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"source")
    service.register_upload(
        DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1"
    )
    fsync_calls: list[Path] = []
    monkeypatch.setattr(
        service,
        "_fsync_directory",
        lambda path: fsync_calls.append(Path(path)),
        raising=False,
    )

    assert service.delete_knowledge_base(
        DEFAULT_KNOWLEDGE_BASE_ID, confirm_name="默认知识库"
    ) is True

    tombstone_operation_roots = [
        path for path in fsync_calls if path.parent == service.recovery_tombstones_root
    ]
    assert tombstone_operation_roots
    operation_root = tombstone_operation_roots[0]
    assert fsync_calls.index(service.recovery_manifests_root) < fsync_calls.index(
        service.recovery_tombstones_root
    )
    assert fsync_calls.index(service.recovery_tombstones_root) < fsync_calls.index(
        service.managed_root
    )
    assert fsync_calls.index(service.managed_root) < fsync_calls.index(operation_root)
    assert fsync_calls[-2:] == [
        service.recovery_tombstones_root,
        service.recovery_manifests_root,
    ]


def test_kb_delete_directory_fsync_failure_preserves_restart_recovery(
    tmp_path, monkeypatch
):
    store, service = make_service(tmp_path)
    source = tmp_path / "source.docx"
    source.write_bytes(b"source")
    document = service.register_upload(
        DEFAULT_KNOWLEDGE_BASE_ID, source, document_id="doc-1"
    )
    managed_source = Path(document.source_file_path)

    def fail_managed_root_fsync(path):
        if Path(path) == service.managed_root:
            raise OSError("directory fsync unsupported")

    monkeypatch.setattr(
        service, "_fsync_directory", fail_managed_root_fsync, raising=False
    )

    with pytest.raises(OSError, match="directory fsync unsupported"):
        service.delete_knowledge_base(
            DEFAULT_KNOWLEDGE_BASE_ID, confirm_name="默认知识库"
        )

    assert store.get_knowledge_base(DEFAULT_KNOWLEDGE_BASE_ID) is not None
    assert managed_source.read_bytes() == b"source"
    assert list(service.recovery_manifests_root.glob("*.json"))
    with pytest.raises(RuntimeError, match="recovery"):
        service.rename_document(
            DEFAULT_KNOWLEDGE_BASE_ID, document.id, "must stay blocked"
        )

    recovered = DocumentProcessingService(store, service.managed_root)

    assert managed_source.read_bytes() == b"source"
    assert list(recovered.recovery_manifests_root.glob("*.json")) == []
    assert list(recovered.recovery_tombstones_root.glob("*")) == []


def test_windows_directory_fsync_uses_backup_semantics_and_flushes_and_closes():
    kernel32 = FakeKernel32()

    document_processing_module._fsync_windows_directory(
        Path(r"C:\managed"),
        kernel32=kernel32,
        get_last_error=lambda: 0,
    )

    assert len(kernel32.create_calls) == 1
    (
        path,
        desired_access,
        share_mode,
        security,
        creation_disposition,
        flags,
        template,
    ) = kernel32.create_calls[0]
    assert path == r"C:\managed"
    assert desired_access == 0x80000000 | 0x40000000
    assert share_mode == 0x00000001 | 0x00000002 | 0x00000004
    assert security is None
    assert creation_disposition == 3
    assert flags & 0x02000000
    assert template is None
    assert kernel32.flush_calls == [123]
    assert kernel32.close_calls == [123]


@pytest.mark.parametrize(
    ("kernel32", "expected_close_calls"),
    (
        (
            FakeKernel32(create_result=ctypes.c_void_p(-1).value),
            [],
        ),
        (FakeKernel32(flush_result=0), [123]),
        (FakeKernel32(close_result=0), [123]),
    ),
)
def test_windows_directory_fsync_converts_failures_to_oserror(
    kernel32, expected_close_calls
):
    errors = iter((5, 6, 7))

    with pytest.raises(OSError, match="Windows directory"):
        document_processing_module._fsync_windows_directory(
            Path(r"C:\managed"),
            kernel32=kernel32,
            get_last_error=lambda: next(errors),
        )

    assert kernel32.close_calls == expected_close_calls


def test_kb_delete_recovery_never_follows_absolute_or_relative_document_symlinks(
    tmp_path, monkeypatch
):
    store, service = make_service(tmp_path)
    first_source = tmp_path / "first.docx"
    second_source = tmp_path / "second.docx"
    first_source.write_bytes(b"first")
    second_source.write_bytes(b"second")
    first = service.register_upload(
        DEFAULT_KNOWLEDGE_BASE_ID, first_source, document_id="doc-a"
    )
    second = service.register_upload(
        DEFAULT_KNOWLEDGE_BASE_ID, second_source, document_id="doc-b"
    )
    first_dir = Path(first.source_file_path).parents[1]
    second_dir = Path(second.source_file_path).parents[1]
    absolute_outside = tmp_path / "absolute-outside"
    relative_outside = tmp_path / "relative-outside"
    absolute_outside.mkdir()
    relative_outside.mkdir()
    (absolute_outside / "keep.txt").write_text("absolute", encoding="utf-8")
    (relative_outside / "keep.txt").write_text("relative", encoding="utf-8")
    shutil.rmtree(first_dir)
    shutil.rmtree(second_dir)
    first_dir.symlink_to(absolute_outside, target_is_directory=True)
    second_dir.symlink_to(
        os.path.relpath(relative_outside, second_dir.parent), target_is_directory=True
    )
    monkeypatch.setattr(
        service,
        "_commit_knowledge_base_delete",
        lambda _kb_id: (_ for _ in ()).throw(SimulatedProcessCrash()),
    )

    with pytest.raises(SimulatedProcessCrash):
        service.delete_knowledge_base(
            DEFAULT_KNOWLEDGE_BASE_ID, confirm_name="默认知识库"
        )

    recovered = DocumentProcessingService(store, service.managed_root)

    assert first_dir.is_symlink()
    assert second_dir.is_symlink()
    assert (absolute_outside / "keep.txt").read_text(encoding="utf-8") == "absolute"
    assert (relative_outside / "keep.txt").read_text(encoding="utf-8") == "relative"

    assert recovered.delete_knowledge_base(
        DEFAULT_KNOWLEDGE_BASE_ID, confirm_name="默认知识库"
    ) is True
    assert (absolute_outside / "keep.txt").read_text(encoding="utf-8") == "absolute"
    assert (relative_outside / "keep.txt").read_text(encoding="utf-8") == "relative"


def test_libreoffice_converter_uses_and_cleans_run_local_profile(tmp_path, monkeypatch):
    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    output = tmp_path / "run" / "preview" / "converted.pdf"
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        generated = output.parent / "source.pdf"
        generated.write_bytes(b"pdf")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    converter = LibreOfficeConverter(executable="soffice")

    assert converter.convert(source, output) == output

    profile_arg = next(
        item for item in captured["command"] if item.startswith("-env:UserInstallation=")
    )
    profile_path = Path(profile_arg.split("=", 1)[1].removeprefix("file://"))
    assert output.read_bytes() == b"pdf"
    assert not profile_path.exists()


def test_libreoffice_converter_normalizes_embedded_cjk_font_references_without_mutating_source(
    tmp_path, monkeypatch
):
    source = tmp_path / "业务报告.docx"
    document_xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:rPr>
    <w:rFonts w:ascii="方正楷体_GBK" w:hAnsi="方正楷体_GBK"
      w:eastAsia="方正黑体_GBK" w:cs="仿宋_GB2312"/>
  </w:rPr><w:t>清能互联使用方正楷体_GBK</w:t></w:r></w:p></w:body>
</w:document>""".encode()
    font_table_xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:fonts xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:font w:name="方正楷体_GBK"><w:embedRegular r:id="rId1"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/></w:font>
</w:fonts>""".encode()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/fontTable.xml", font_table_xml)
        archive.writestr("word/fonts/font1.odttf", b"embedded-font")
    original_bytes = source.read_bytes()
    output = tmp_path / "preview" / "converted.pdf"
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        conversion_source = Path(command[-1])
        captured["conversion_source"] = conversion_source
        assert conversion_source != source
        assert conversion_source.name.startswith(".libreoffice-source-")
        with zipfile.ZipFile(conversion_source) as archive:
            converted_document = archive.read("word/document.xml")
            assert converted_document.count(b"Noto Serif CJK SC") == 3
            assert converted_document.count(b"Noto Sans CJK SC") == 1
            assert "清能互联使用方正楷体_GBK".encode() in converted_document
            assert archive.read("word/fontTable.xml") == font_table_xml
            assert archive.read("word/fonts/font1.odttf") == b"embedded-font"
        generated = output.parent / f"{conversion_source.stem}.pdf"
        generated.write_bytes(b"stable-pdf")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    converted = LibreOfficeConverter(executable="soffice").convert(source, output)

    assert converted == output
    assert output.read_bytes() == b"stable-pdf"
    assert source.read_bytes() == original_bytes
    conversion_source = captured["conversion_source"]
    assert isinstance(conversion_source, Path)
    assert not conversion_source.exists()
    assert not list(output.parent.glob(".libreoffice-source-*.docx"))


def test_libreoffice_converter_keeps_docx_without_mapped_cjk_fonts_as_is(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(
            "word/document.xml",
            b'<w:document xmlns:w="urn:test"><w:rFonts w:ascii="Arial"/></w:document>',
        )
    output = tmp_path / "preview" / "converted.pdf"
    captured = {}

    def fake_run(command, **kwargs):
        captured["source"] = Path(command[-1])
        (output.parent / "source.pdf").write_bytes(b"pdf")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    LibreOfficeConverter(executable="soffice").convert(source, output)

    assert captured["source"] == source
    assert not list(output.parent.glob(".libreoffice-source-*.docx"))


def test_libreoffice_converter_cleans_normalized_docx_when_conversion_fails(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(
            "word/document.xml",
            """<w:document xmlns:w="urn:test">
<w:rFonts w:eastAsia="方正小标宋_GBK"/><w:t>分产业分析</w:t>
</w:document>""".encode(),
        )
    output = tmp_path / "preview" / "converted.pdf"

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1, "", "conversion failed"
        ),
    )

    with pytest.raises(RuntimeError, match="LibreOffice conversion failed"):
        LibreOfficeConverter(executable="soffice").convert(source, output)

    assert source.exists()
    assert not list(output.parent.glob(".libreoffice-source-*.docx"))
    assert not list(output.parent.glob(".libreoffice-profile-*"))


def test_seed_demo_uses_exact_four_public_vidore_documents(tmp_path):
    assert DEMO_DOCUMENT_FILENAMES == (
        "openstax-introduction-to-computer-science-p357.pdf",
        "openstax-introduction-to-python-programming-p334-335.pdf",
        "eu-joint-employment-report-2025-p97.pdf",
        "eu-future-oriented-occupations-p19.pdf",
    )
    source_dir = tmp_path / "examples"
    source_dir.mkdir()
    for filename in DEMO_DOCUMENT_FILENAMES:
        (source_dir / filename).write_bytes(b"pdf")

    class FakeService:
        def __init__(self):
            self.registered = []
            self.processed = []

        def ensure_uploaded(self, knowledge_base_id, source_file, **kwargs):
            self.registered.append((knowledge_base_id, Path(source_file).name, kwargs))
            return type("Uploaded", (), {"id": Path(source_file).stem})()

        def process_document(self, knowledge_base_id, document_id):
            self.processed.append((knowledge_base_id, document_id))
            return type("Ready", (), {"id": document_id})()

    service = FakeService()
    seeded = seed_demo_documents(service, source_dir, DEFAULT_KNOWLEDGE_BASE_ID)
    seeded_again = seed_demo_documents(
        service, source_dir, DEFAULT_KNOWLEDGE_BASE_ID
    )

    assert [item[1] for item in service.registered] == list(
        DEMO_DOCUMENT_FILENAMES
    ) * 2
    assert [item[0] for item in service.registered] == [
        DEFAULT_KNOWLEDGE_BASE_ID
    ] * 8
    assert len(service.processed) == 8
    assert len(seeded) == 4
    assert len(seeded_again) == 4


def test_cli_defaults_to_v3_database_and_mineru_token_is_supported(tmp_path, monkeypatch):
    monkeypatch.delenv("MINERU_API_KEY", raising=False)
    monkeypatch.setenv("MINERU_TOKEN", "test-token")

    assert DEFAULT_DB.name == "traceable_v3.db"
    assert mineru_api_key_from_doc(tmp_path / "missing-api.txt") == "test-token"


def test_managed_processing_reads_mineru_token_from_dotenv(tmp_path, monkeypatch):
    from traceable_rag.document_processing import _mineru_client_from_environment

    monkeypatch.delenv("MINERU_TOKEN", raising=False)
    monkeypatch.delenv("MINERU_API_KEY", raising=False)
    (tmp_path / ".env").write_text("MINERU_TOKEN=dotenv-token\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    client = _mineru_client_from_environment()

    assert client.api_key == "dotenv-token"


def test_seed_demo_default_does_not_read_api_doc(tmp_path, monkeypatch):
    import traceable_rag.cli as cli

    monkeypatch.setenv("MINERU_TOKEN", "env-token")
    monkeypatch.setattr(
        cli,
        "mineru_api_key_from_doc",
        lambda _path: pytest.fail("default seed-demo must not read an API doc"),
    )
    captured = {}
    monkeypatch.setattr(cli, "MinerUClient", lambda key: captured.setdefault("client", key))
    monkeypatch.setattr(cli, "DocumentProcessingService", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli, "seed_demo_documents", lambda *_args, **_kwargs: [])

    result = cli.main(
        [
            "seed-demo",
            "--db",
            str(tmp_path / "db.sqlite"),
            "--storage-root",
            str(tmp_path / "managed"),
            "--source-dir",
            str(tmp_path / "examples"),
        ]
    )

    assert result == 0
    assert captured["client"] == "env-token"


def test_parse_mineru_cli_uses_ensure_uploaded_for_retry(tmp_path, monkeypatch):
    import traceable_rag.cli as cli

    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    monkeypatch.setenv("MINERU_TOKEN", "env-token")
    calls = []

    class FakeService:
        def ensure_uploaded(self, knowledge_base_id, source_file, **kwargs):
            calls.append(("ensure", knowledge_base_id, Path(source_file), kwargs))
            return type("Uploaded", (), {"id": kwargs["document_id"]})()

        def process_document(self, knowledge_base_id, document_id, **kwargs):
            calls.append(("process", knowledge_base_id, document_id, kwargs))
            return Document(
                id=document_id,
                knowledge_base_id=knowledge_base_id,
                original_filename=source.name,
                display_name=source.stem,
                file_hash="hash",
                source_type="mineru_api",
                source_file_path=str(source),
                preview_file_path=str(tmp_path / "preview.pdf"),
                mineru_result_dir=str(tmp_path / "result"),
                page_count=1,
                processing_status="ready",
                processing_error=None,
                parser_provider="mineru",
            )

    monkeypatch.setattr(cli, "MinerUClient", lambda _key: object())
    monkeypatch.setattr(
        cli, "DocumentProcessingService", lambda *_args, **_kwargs: FakeService()
    )

    assert (
        cli.main(
            [
                "parse-mineru",
                "--file",
                str(source),
                "--out",
                str(tmp_path / "managed"),
                "--db",
                str(tmp_path / "db.sqlite"),
            ]
        )
        == 0
    )
    assert [call[0] for call in calls] == ["ensure", "process"]


def _seed_rebuild_fixture(store, tmp_path, document_id="upgrade-doc"):
    source = tmp_path / f"{document_id}.docx"
    preview = tmp_path / f"{document_id}.pdf"
    result_dir = tmp_path / f"{document_id}-result"
    source.write_bytes(b"source")
    preview.write_bytes(b"preview")
    result_dir.mkdir()
    (result_dir / "content_list_v2.json").write_text(
        json.dumps(
            [[
                {
                    "type": "paragraph",
                    "content": {
                        "paragraph_content": [
                            {"type": "text", "content": "升级后的正文"}
                        ]
                    },
                    "bbox": [1, 2, 30, 40],
                }
            ]],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    document = Document(
        id=document_id,
        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
        original_filename=source.name,
        display_name="待升级文档",
        file_hash="old-hash",
        source_type="upload",
        source_file_path=str(source),
        preview_file_path=str(preview),
        mineru_result_dir=str(result_dir),
        page_count=1,
        processing_status="ready",
        processing_error=None,
        parser_provider="mineru",
        parser_version="old",
        normalization_version=None,
    )
    store.upsert_document(document)
    old_block = Block(
        id=f"{document_id}:old",
        document_id=document_id,
        page_idx=0,
        block_idx=0,
        block_type="paragraph",
        text="旧版正文",
    )
    store.replace_blocks(DEFAULT_KNOWLEDGE_BASE_ID, document_id, [old_block])
    return store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, document_id), old_block


def test_rebuild_document_content_reuses_retained_result_without_external_mineru(tmp_path):
    store = RecordingStore(tmp_path / "runtime.db")
    original, _old_block = _seed_rebuild_fixture(store, tmp_path)

    class NoExternalMinerU:
        def parse_local_file(self, *_args, **_kwargs):
            raise AssertionError("retained-result rebuild must not call MinerU")

    service = DocumentProcessingService(
        store,
        tmp_path / "managed",
        mineru_client=NoExternalMinerU(),
        embedding_provider=FakeEmbeddingProvider([[0.25, 0.75]]),
    )

    rebuilt = service.rebuild_document_content(
        DEFAULT_KNOWLEDGE_BASE_ID, original.id
    )

    assert rebuilt.processing_status == "ready"
    assert rebuilt.normalization_version == NORMALIZATION_VERSION
    assert [block.text for block in store.list_blocks(DEFAULT_KNOWLEDGE_BASE_ID, original.id)] == [
        "升级后的正文"
    ]
    assert store.list_chunks(DEFAULT_KNOWLEDGE_BASE_ID, original.id)
    assert store.list_index_records(DEFAULT_KNOWLEDGE_BASE_ID, original.id)


def test_rebuild_document_content_upgrades_retained_visual_without_external_mineru(
    tmp_path,
):
    store = RecordingStore(tmp_path / "runtime.db")
    source = tmp_path / "visual-upgrade.docx"
    preview = tmp_path / "visual-upgrade.pdf"
    result_dir = tmp_path / "visual-upgrade-result"
    images = result_dir / "images"
    images.mkdir(parents=True)
    source.write_bytes(b"source")
    preview.write_bytes(b"preview")
    Image.new("RGB", (640, 320), "white").save(images / "route.png")
    (result_dir / "content_list_v2.json").write_text(
        json.dumps(
            [[
                {
                    "type": "image",
                    "content": {
                        "image_source": {"path": "images/route.png"},
                        "image_caption": [{"content": "项目总体技术路线图"}],
                        "image_footnote": [{"content": "不得进入图片代理文本"}],
                    },
                    "bbox": [1, 2, 300, 180],
                }
            ]],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    document = Document(
        id="visual-upgrade",
        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
        original_filename=source.name,
        display_name="待升级视觉文档",
        file_hash="old-visual-hash",
        source_type="upload",
        source_file_path=str(source),
        preview_file_path=str(preview),
        mineru_result_dir=str(result_dir),
        page_count=1,
        processing_status="ready",
        processing_error=None,
        parser_provider="mineru",
        parser_version="old",
        normalization_version="mineru-content.v3",
    )
    store.upsert_document(document)

    class NoExternalMinerU:
        def parse_local_file(self, *_args, **_kwargs):
            raise AssertionError("retained visual rebuild must not call MinerU")

    service = DocumentProcessingService(
        store,
        tmp_path / "managed",
        mineru_client=NoExternalMinerU(),
        embedding_provider=FakeEmbeddingProvider([[0.25, 0.75]]),
    )

    rebuilt = service.rebuild_document_content(
        DEFAULT_KNOWLEDGE_BASE_ID, document.id
    )

    assert rebuilt.normalization_version == "mineru-content.v4"
    assert Path(rebuilt.source_file_path or "").is_relative_to(service.managed_root)
    assert Path(rebuilt.preview_file_path or "").is_relative_to(service.managed_root)
    assert Path(rebuilt.mineru_result_dir or "").is_relative_to(service.managed_root)
    assert result_dir.is_dir()
    blocks = store.list_blocks(DEFAULT_KNOWLEDGE_BASE_ID, document.id)
    assets = store.list_assets(DEFAULT_KNOWLEDGE_BASE_ID, document.id)
    chunks = store.list_chunks(DEFAULT_KNOWLEDGE_BASE_ID, document.id)
    records = store.list_index_records(DEFAULT_KNOWLEDGE_BASE_ID, document.id)
    assert [(block.block_type, block.text) for block in blocks] == [
        ("image", "项目总体技术路线图")
    ]
    assert len(assets) == 1
    assert assets[0].file_path == "images/route.png"
    assert assets[0].content_type == "image/png"
    assert assets[0].metadata["width"] == 640
    assert assets[0].metadata["height"] == 320
    assert assets[0].metadata["preview_file_path"].startswith(
        ".traceable-rag/previews/"
    )
    assert chunks[0].block_ids == [blocks[0].id]
    assert chunks[0].modality_mix == ["image"]
    assert records[0].modality == "image"
    assert records[0].indexing_text == "项目总体技术路线图"
    assert "不得进入" not in records[0].indexing_text
    client = TestClient(
        create_app(
            db_path=store.db_path,
            managed_root=service.managed_root,
            start_processing_worker=False,
        )
    )
    response = client.get(
        f"/api/knowledge-bases/{DEFAULT_KNOWLEDGE_BASE_ID}/documents/"
        f"{document.id}/assets/{assets[0].id}"
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")


def test_failed_normalization_rebuild_preserves_previous_document_graph(tmp_path):
    store = RecordingStore(tmp_path / "runtime.db")
    original, old_block = _seed_rebuild_fixture(store, tmp_path)
    assert original is not None
    old_graph = (
        store.list_blocks(DEFAULT_KNOWLEDGE_BASE_ID, original.id),
        store.list_assets(DEFAULT_KNOWLEDGE_BASE_ID, original.id),
        store.list_chunks(DEFAULT_KNOWLEDGE_BASE_ID, original.id),
        store.list_index_records(DEFAULT_KNOWLEDGE_BASE_ID, original.id),
    )
    store.persist_error = RuntimeError("simulated normalization persist failure")
    service = DocumentProcessingService(
        store,
        tmp_path / "managed",
        embedding_provider=FakeEmbeddingProvider([[0.25, 0.75]]),
    )

    with pytest.raises(RuntimeError, match="normalization persist failure"):
        service.rebuild_document_content(
            DEFAULT_KNOWLEDGE_BASE_ID, original.id
        )

    assert store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, original.id) == original
    assert store.list_blocks(DEFAULT_KNOWLEDGE_BASE_ID, original.id) == [old_block]
    assert (
        store.list_blocks(DEFAULT_KNOWLEDGE_BASE_ID, original.id),
        store.list_assets(DEFAULT_KNOWLEDGE_BASE_ID, original.id),
        store.list_chunks(DEFAULT_KNOWLEDGE_BASE_ID, original.id),
        store.list_index_records(DEFAULT_KNOWLEDGE_BASE_ID, original.id),
    ) == old_graph
    assert list(service.managed_root.rglob("runs/*")) == []


def _removed_test_failed_rebuild_keeps_old_graph_and_immutable_trace_snapshot(tmp_path):
    store = RecordingStore(tmp_path / "runtime.db")
    original, old_block = _seed_rebuild_fixture(store, tmp_path)
    trace = QueryTrace(
        id="historical-trace",
        query="旧问题",
        answer="旧回答",
        source_type="manual_query",
        support_level="strong",
        knowledge_base_ids=[DEFAULT_KNOWLEDGE_BASE_ID],
        cited_evidence_ids=["old-evidence"],
        read_events=[],
        knowledge_base_snapshots=[
            {"id": DEFAULT_KNOWLEDGE_BASE_ID, "name": "默认知识库"}
        ],
        citation_snapshots=[
            {
                "evidence_id": "old-evidence",
                "document_id": original.id,
                "original_filename": original.original_filename,
                "human_page_number": 1,
                "block_ids": [old_block.id],
                "block_text": old_block.text,
            }
        ],
        created_at="2026-07-23T00:00:00.000000Z",
    )
    store.create_query_trace(trace)
    saved_trace = store.get_query_trace(trace.id)
    store.persist_error = RuntimeError("simulated atomic persistence failure")
    service = DocumentProcessingService(
        store,
        tmp_path / "managed",
        embedding_provider=FakeEmbeddingProvider([[0.25, 0.75]]),
    )

    with pytest.raises(RuntimeError, match="atomic persistence"):
        service.rebuild_document_content(
            DEFAULT_KNOWLEDGE_BASE_ID, original.id
        )

    assert store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, original.id) == original
    assert store.list_blocks(DEFAULT_KNOWLEDGE_BASE_ID, original.id) == [old_block]
    assert store.get_query_trace(trace.id) == saved_trace
