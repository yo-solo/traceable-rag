from dataclasses import replace

import pytest

from traceable_rag.models import Block, Chunk, Document, IndexRecord, KnowledgeBase
from traceable_rag.sdk import DocumentScopeError, TraceableRagClient
from traceable_rag.service_validation import (
    KnowledgeBaseScopeError,
    ServiceValidationError,
)
from traceable_rag.store import EvidenceStore


def make_store(tmp_path) -> EvidenceStore:
    store = EvidenceStore(tmp_path / "traceable.db")
    store.create_knowledge_base(KnowledgeBase(id="kb-a", name="编程资料库"))
    store.create_knowledge_base(KnowledgeBase(id="kb-b", name="就业资料库"))
    for kb_id, document_id, keyword in (
        ("kb-a", "doc-a", "编译器前端分析"),
        ("kb-b", "doc-b", "未来职业性别隔离"),
    ):
        document = Document(
            id=document_id,
            knowledge_base_id=kb_id,
            original_filename=f"{document_id}-business-report.pdf",
            display_name=f"{kb_id} 公开样例",
            file_hash=f"hash-{document_id}",
            source_type="upload",
            source_file_path=None,
            preview_file_path=None,
            mineru_result_dir=None,
            page_count=3,
            processing_status="ready",
            processing_error=None,
            parser_provider="mineru",
        )
        block = Block(
            id=f"{document_id}:body",
            document_id=document_id,
            page_idx=2,
            block_idx=0,
            block_type="paragraph",
            text=f"{keyword}时应启用备用资源。",
            bbox=[100, 200, 900, 420],
            page_size=[1000, 1400],
        )
        chunk = Chunk(
            id=f"{document_id}:chunk",
            document_id=document_id,
            chunk_idx=0,
            strategy="block_window",
            block_ids=[block.id],
            title_context="示例分析",
            inferred_heading_path=["示例分析"],
            chunk_text=block.text or "",
            indexing_text=block.text or "",
            page_range=[2],
            bbox_by_page={"2": [block.bbox]},
            modality_mix=["text"],
            token_count=20,
            strategy_version="v1",
        )
        record = IndexRecord(
            id=f"{document_id}:record",
            evidence_id=f"index:{document_id}",
            document_id=document_id,
            chunk_id=chunk.id,
            block_ids=chunk.block_ids,
            modality="text",
            indexing_text=chunk.indexing_text,
            vector=None,
            embedding_provider=None,
            embedding_model=None,
            embedding_dimension=None,
            index_version="v1",
        )
        store.replace_document_graph(
            kb_id, document, [block], [], [chunk], [record]
        )
    return store


def test_sdk_lists_knowledge_bases_and_documents_with_explicit_scope(tmp_path):
    client = TraceableRagClient(make_store(tmp_path))

    assert {item["id"] for item in client.list_knowledge_bases()} >= {"kb-a", "kb-b"}
    assert [item["id"] for item in client.list_documents("kb-a")] == ["doc-a"]
    with pytest.raises(KnowledgeBaseScopeError):
        client.list_documents("missing-kb")


def test_sdk_retrieve_matches_public_source_contract_and_document_scope(tmp_path):
    client = TraceableRagClient(make_store(tmp_path))

    response = client.retrieve(
        "未来职业性别隔离",
        knowledge_base_ids=["kb-a", "kb-b"],
        mode="keyword",
        document_ids=["doc-b"],
    )

    assert response["mode_requested"] == "keyword"
    assert response["pipeline_used"] == "keyword"
    assert response["degraded"] is False
    assert response["warnings"] == []
    assert {item["document_id"] for item in response["results"]} == {"doc-b"}
    assert set(response["results"][0]) == {
        "source_id",
        "source_url",
        "text",
        "document_id",
        "document_name",
        "human_page_number",
        "bbox",
        "score",
    }

    with pytest.raises(DocumentScopeError):
        client.retrieve(
            "未来职业性别隔离",
            knowledge_base_ids=["kb-a"],
            document_ids=["doc-b"],
        )


def test_sdk_auto_uses_explicit_vector_or_safely_degrades_to_keyword(tmp_path):
    store = make_store(tmp_path)
    record = store.list_index_records("kb-a", "doc-a")[0]
    store.replace_index_records(
        "kb-a",
        "doc-a",
        [
            replace(
                record,
                vector=[1.0, 0.0],
                embedding_provider="fake-provider",
                embedding_model="fake-model",
                embedding_dimension=2,
            )
        ],
    )
    client = TraceableRagClient(store)

    degraded = client.retrieve("编译器前端分析", knowledge_base_ids=["kb-a"])
    dense = client.retrieve(
        "编译器前端分析",
        knowledge_base_ids=["kb-a"],
        query_vector=[1.0, 0.0],
        embedding_provider="fake-provider",
        embedding_model="fake-model",
    )

    assert degraded["pipeline_used"] == "keyword"
    assert degraded["degraded"] is True
    assert degraded["warnings"][0]["code"] == "embedding_unavailable"
    assert dense["pipeline_used"] == "dense"
    assert dense["degraded"] is False
    assert dense["results"]


def test_sdk_source_context_and_validation_follow_http_bounds(tmp_path):
    store = make_store(tmp_path)
    body = store.list_blocks("kb-a", "doc-a", include_discarded=True)[0]
    heading = replace(
        body,
        id="doc-a:title",
        block_idx=0,
        block_type="title",
        text="示例分析",
        bbox=[5, 5, 200, 40],
    )
    document = store.get_document("kb-a", "doc-a")
    assert document is not None
    store.replace_document_graph(
        "kb-a",
        document,
        [heading, replace(body, block_idx=1)],
        [],
        store.list_chunks("kb-a", "doc-a"),
        store.list_index_records("kb-a", "doc-a"),
    )
    client = TraceableRagClient(store)
    source_id = client.retrieve(
        "编译器前端分析", knowledge_base_ids=["kb-a"], mode="keyword"
    )["results"][0]["source_id"]

    context = client.get_source_context(source_id, before=2, after=2)

    assert context["source"]["source_id"] == source_id
    assert context["source"]["document_id"] == "doc-a"
    assert [item["text"] for item in context["before"]] == ["示例分析"]
    with pytest.raises(ServiceValidationError):
        client.get_source_context(source_id, before=6)
    with pytest.raises(ServiceValidationError):
        client.retrieve("q", knowledge_base_ids=["kb-a"], mode="unsupported")  # type: ignore[arg-type]
