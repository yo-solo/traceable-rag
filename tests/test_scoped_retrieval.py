import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import parse_qs, quote_plus, urlparse

import pytest
from fastapi.testclient import TestClient

import traceable_rag.evidence_service as evidence_service_module
from traceable_rag.evidence_service import EvidenceNotFoundError, _parse_evidence_locator
from traceable_rag.models import Block, Chunk, Document, IndexRecord, KnowledgeBase
from traceable_rag.retrieval import Retriever
from traceable_rag.store import EvidenceStore
from traceable_rag.web_app import create_app


def add_knowledge_base(store: EvidenceStore, kb_id: str, name: str) -> None:
    store.create_knowledge_base(KnowledgeBase(id=kb_id, name=name))


def add_indexed_document(
    store: EvidenceStore,
    tmp_path,
    *,
    kb_id: str,
    document_id: str,
    display_name: str,
    keyword: str,
    vector=None,
):
    source = tmp_path / f"{document_id}.docx"
    preview = tmp_path / "managed" / f"{document_id}.pdf"
    source.write_bytes(b"source")
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_bytes(b"%PDF-preview")
    document = Document(
        id=document_id,
        knowledge_base_id=kb_id,
        original_filename=f"{document_id}-original.docx",
        display_name=display_name,
        file_hash=f"hash-{document_id}",
        source_type="upload",
        source_file_path=str(source),
        preview_file_path=str(preview),
        mineru_result_dir=None,
        page_count=3,
        processing_status="ready",
        processing_error=None,
        parser_provider="mineru",
    )
    title = Block(
        id=f"{document_id}:title",
        document_id=document_id,
        page_idx=0,
        block_idx=0,
        block_type="title",
        text="示例分析",
        bbox=[5, 5, 200, 40],
        page_size=[1000, 1400],
    )
    body = Block(
        id=f"{document_id}:body",
        document_id=document_id,
        page_idx=2,
        block_idx=1,
        block_type="paragraph",
        text=f"正文包含{keyword}并给出可核验结论",
        bbox=[100, 200, 900, 420],
        page_size=[1000, 1400],
    )
    chunk = Chunk(
        id=f"{document_id}:chunk",
        document_id=document_id,
        chunk_idx=0,
        strategy="block_window_with_title_hints",
        block_ids=[title.id, body.id],
        title_context=title.text,
        inferred_heading_path=[title.text or ""],
        chunk_text=f"{title.text}\n{body.text}",
        indexing_text=f"{title.text}\n{body.text}",
        page_range=[0, 2],
        bbox_by_page={
            "0": [title.bbox],
            "2": [body.bbox],
        },
        modality_mix=["text"],
        token_count=30,
        strategy_version="v1",
    )
    record = IndexRecord(
        id=f"{document_id}:record",
        evidence_id=f"evidence:{document_id}",
        document_id=document_id,
        chunk_id=chunk.id,
        block_ids=chunk.block_ids,
        modality="text",
        indexing_text=chunk.indexing_text,
        vector=vector,
        embedding_provider="fake" if vector else None,
        embedding_model="fake" if vector else None,
        embedding_dimension=len(vector) if vector else None,
        index_version="v1",
    )
    store.replace_document_graph(kb_id, document, [title, body], [], [chunk], [record])
    return document, title, body, chunk, record


def make_client(tmp_path, *, embedding_provider=None):
    db_path = tmp_path / "traceable.db"
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("test web app", encoding="utf-8")
    store = EvidenceStore(db_path)
    add_knowledge_base(store, "kb-a", "第一资料库")
    add_knowledge_base(store, "kb-b", "第二资料库")
    first = add_indexed_document(
        store,
        tmp_path,
        kb_id="kb-a",
        document_id="doc-a",
        display_name="第一份公开样例",
        keyword="共享关键词",
    )
    second = add_indexed_document(
        store,
        tmp_path,
        kb_id="kb-b",
        document_id="doc-b",
        display_name="第二份公开样例",
        keyword="共享关键词",
    )
    client = TestClient(
        create_app(
            db_path=db_path,
            managed_root=tmp_path / "managed",
            static_dir=static_dir,
            embedding_provider=embedding_provider,
        )
    )
    return client, store, first, second


def search(client: TestClient, knowledge_base_ids, query="共享关键词"):
    return client.post(
        "/api/evidence/search",
        json={
            "query": query,
            "knowledge_base_ids": knowledge_base_ids,
            "top_k": 10,
        },
    )


def retrieve(
    client: TestClient,
    knowledge_base_ids,
    *,
    query="共享关键词",
    mode="keyword",
    document_ids=None,
):
    payload = {
        "query": query,
        "knowledge_base_ids": knowledge_base_ids,
        "mode": mode,
        "top_k": 10,
    }
    if document_ids is not None:
        payload["document_ids"] = document_ids
    return client.post("/api/retrieval", json=payload)


def test_retrieval_api_returns_only_agent_facing_source_fields(tmp_path):
    client, _, _, _ = make_client(tmp_path)

    response = retrieve(client, ["kb-a"])

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode_requested"] == "keyword"
    assert payload["pipeline_used"] == "keyword"
    assert payload["degraded"] is False
    assert payload["warnings"] == []
    assert len(payload["results"]) == 1
    source = payload["results"][0]
    assert re.fullmatch(r"src_[0-9a-f]{32}", source["source_id"])
    assert source["source_url"] == f"/sources/{source['source_id']}"
    assert source["document_id"] == "doc-a"
    assert source["document_name"] == "第一份公开样例"
    assert source["human_page_number"] == 3
    assert source["bbox"] == [100.0, 200.0, 900.0, 420.0]
    assert "共享关键词" in source["text"]
    assert set(source) == {
        "source_id",
        "source_url",
        "text",
        "document_id",
        "document_name",
        "human_page_number",
        "bbox",
        "score",
    }


def test_retrieval_api_scopes_documents_and_rejects_cross_scope_ids(tmp_path):
    client, _, _, _ = make_client(tmp_path)

    scoped = retrieve(client, ["kb-a", "kb-b"], document_ids=["doc-b"])
    outside = retrieve(client, ["kb-a"], document_ids=["doc-b"])

    assert scoped.status_code == 200
    assert {item["document_id"] for item in scoped.json()["results"]} == {"doc-b"}
    assert outside.status_code == 404
    assert outside.json()["detail"]["code"] == "document_not_found"


def test_retrieval_auto_reports_safe_keyword_fallback(tmp_path):
    client, _, _, _ = make_client(
        tmp_path,
        embedding_provider=SimpleNamespace(provider_name=None, model_name=None),
    )

    response = retrieve(client, ["kb-a"], mode="auto")

    assert response.status_code == 200
    payload = response.json()
    assert payload["pipeline_used"] == "keyword"
    assert payload["degraded"] is True
    assert payload["warnings"][0]["code"] == "embedding_unavailable"


def test_source_context_returns_ordered_neighbours_without_internal_ids(tmp_path):
    client, _, _, _ = make_client(tmp_path)
    source_id = retrieve(client, ["kb-a"]).json()["results"][0]["source_id"]

    response = client.get(f"/api/sources/{source_id}/context")

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"]["source_id"] == source_id
    assert payload["source"]["document_id"] == "doc-a"
    assert payload["source"]["human_page_number"] == 3
    assert payload["source"]["title_path"] == ["示例分析"]
    assert [item["text"] for item in payload["before"]] == ["示例分析"]
    assert payload["after"] == []
    assert payload["truncated"] is False
    serialized = response.text
    for internal_name in ("record_id", "chunk_id", "block_id", "evidence_id"):
        assert internal_name not in serialized


def test_source_context_distinguishes_unknown_and_gone_sources(tmp_path):
    client, store, first, _ = make_client(tmp_path)
    source_id = retrieve(client, ["kb-a"]).json()["results"][0]["source_id"]
    document, title, body, chunk, record = first

    store.replace_document_graph(
        "kb-a", document, [title, body], [], [chunk], [record]
    )

    gone = client.get(f"/api/sources/{source_id}/context")
    unknown = client.get("/api/sources/src_00000000000000000000000000000000/context")
    invalid = client.get("/api/sources/not-a-source/context")
    too_wide = client.get(
        "/api/sources/src_00000000000000000000000000000000/context?before=6"
    )

    assert gone.status_code == 410
    assert gone.json()["detail"]["code"] == "source_gone"
    assert unknown.status_code == 404
    assert invalid.status_code == 404
    assert too_wide.status_code == 422


def _removed_test_evidence_search_isolates_single_kb_and_merges_multiple_kbs(tmp_path):
    client, _, _, _ = make_client(tmp_path)

    first_only = search(client, ["kb-a"])
    both = search(client, ["kb-a", "kb-b"])

    assert first_only.status_code == 200
    assert first_only.json()["retrieval_mode_requested"] == "embedding"
    assert first_only.json()["retrieval_mode_used"] == "keyword"
    assert first_only.json()["warnings"]
    assert {item["knowledge_base_id"] for item in first_only.json()["results"]} == {
        "kb-a"
    }
    assert both.status_code == 200
    assert {item["knowledge_base_id"] for item in both.json()["results"]} == {
        "kb-a",
        "kb-b",
    }


def _removed_test_evidence_search_explicit_keyword_overrides_embedding_default(tmp_path):
    client, _, _, _ = make_client(tmp_path)

    response = client.post(
        "/api/evidence/search",
        json={
            "query": "共享关键词",
            "knowledge_base_ids": ["kb-a"],
            "top_k": 10,
            "use_embedding": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["retrieval_mode_requested"] == "keyword"
    assert response.json()["retrieval_mode_used"] == "keyword"
    assert response.json()["warnings"] == []


def _removed_test_evidence_search_reports_actual_embedding_mode_for_complete_index(tmp_path):
    class Provider:
        provider_name = "fake"
        model_name = "fake"

        def embed_texts(self, texts):
            return [[0.8, 0.2] for _text in texts]

    db_path = tmp_path / "traceable.db"
    store = EvidenceStore(db_path)
    add_knowledge_base(store, "kb-a", "第一资料库")
    add_indexed_document(
        store,
        tmp_path,
        kb_id="kb-a",
        document_id="doc-a",
        display_name="第一份公开样例",
        keyword="共享关键词",
        vector=[0.8, 0.2],
    )
    client = TestClient(
        create_app(
            db_path=db_path,
            managed_root=tmp_path / "managed",
            settings_path=tmp_path / "settings.json",
            embedding_provider=Provider(),
        )
    )

    response = search(client, ["kb-a"])

    assert response.status_code == 200
    assert response.json()["retrieval_mode_requested"] == "embedding"
    assert response.json()["retrieval_mode_used"] == "embedding"
    assert response.json()["warnings"] == []
    assert response.json()["results"][0]["retrieval"]["mode"] == "embedding"


def _removed_test_evidence_search_uses_injected_reranker(tmp_path):
    class Provider:
        provider_name = "fake"
        model_name = "fake"

        def embed_texts(self, texts):
            return [[0.8, 0.2] for _text in texts]

    class Reranker:
        def __init__(self):
            self.calls = []

        def rerank(self, query, documents, *, top_n=None):
            self.calls.append((query, documents, top_n))
            return [
                SimpleNamespace(index=index, relevance_score=1.0 - index / 10)
                for index in range(len(documents))
            ]

    db_path = tmp_path / "traceable.db"
    store = EvidenceStore(db_path)
    add_knowledge_base(store, "kb-a", "第一资料库")
    add_indexed_document(
        store,
        tmp_path,
        kb_id="kb-a",
        document_id="doc-a",
        display_name="第一份公开样例",
        keyword="共享关键词",
        vector=[0.8, 0.2],
    )
    reranker = Reranker()
    client = TestClient(
        create_app(
            db_path=db_path,
            managed_root=tmp_path / "managed",
            settings_path=tmp_path / "settings.json",
            embedding_provider=Provider(),
            reranker=reranker,
        )
    )

    response = search(client, ["kb-a"])

    assert response.status_code == 200
    assert response.json()["retrieval_mode_used"] == "embedding"
    assert len(reranker.calls) == 1


def test_evidence_service_missing_embedding_identity_falls_back_to_keyword(tmp_path):
    db_path = tmp_path / "traceable.db"
    store = EvidenceStore(db_path)
    add_knowledge_base(store, "kb-a", "第一资料库")
    add_indexed_document(
        store,
        tmp_path,
        kb_id="kb-a",
        document_id="doc-a",
        display_name="第一份公开样例",
        keyword="共享关键词",
        vector=[0.8, 0.2],
    )

    result = evidence_service_module.EvidenceService(store).search_with_mode(
        "字面不命中",
        ["kb-a"],
        retrieval_mode_requested="embedding",
        query_vector=[0.8, 0.2],
    )

    assert result.retrieval_mode_used == "keyword"
    assert result.warnings
    assert result.results == []


def test_evidence_service_wrong_embedding_model_falls_back_to_keyword(tmp_path):
    db_path = tmp_path / "traceable.db"
    store = EvidenceStore(db_path)
    add_knowledge_base(store, "kb-a", "第一资料库")
    add_indexed_document(
        store,
        tmp_path,
        kb_id="kb-a",
        document_id="doc-a",
        display_name="第一份公开样例",
        keyword="共享关键词",
        vector=[0.8, 0.2],
    )

    result = evidence_service_module.EvidenceService(store).search_with_mode(
        "字面不命中",
        ["kb-a"],
        retrieval_mode_requested="embedding",
        query_vector=[0.8, 0.2],
        embedding_provider="fake",
        embedding_model="same-dimension-other-model",
    )

    assert result.retrieval_mode_used == "keyword"
    assert result.warnings
    assert result.results == []


def test_evidence_service_reranker_failure_falls_back_to_dense(tmp_path):
    db_path = tmp_path / "traceable.db"
    store = EvidenceStore(db_path)
    add_knowledge_base(store, "kb-a", "第一资料库")
    add_indexed_document(
        store,
        tmp_path,
        kb_id="kb-a",
        document_id="doc-a",
        display_name="第一份公开样例",
        keyword="共享关键词",
        vector=[0.8, 0.2],
    )

    class FailingReranker:
        def rerank(self, *_args, **_kwargs):
            raise TimeoutError("provider timeout")

    result = evidence_service_module.EvidenceService(
        store, reranker=FailingReranker()
    ).search_with_mode(
        "字面不命中",
        ["kb-a"],
        retrieval_mode_requested="embedding",
        query_vector=[0.8, 0.2],
        embedding_provider="fake",
        embedding_model="fake",
    )

    assert result.retrieval_mode_used == "embedding"
    assert result.pipeline_used == "dense"
    assert result.degraded is True
    assert result.results
    assert any("Reranker" in warning for warning in result.warnings)
    assert result.indexing_text_by_evidence_id[
        result.results[0]["index_evidence_id"]
    ] == "示例分析\n正文包含共享关键词并给出可核验结论"


def _removed_test_legacy_evidence_search_uses_embedding_with_explicit_identity(tmp_path):
    db_path = tmp_path / "traceable.db"
    store = EvidenceStore(db_path)
    add_knowledge_base(store, "kb-a", "第一资料库")
    add_indexed_document(
        store,
        tmp_path,
        kb_id="kb-a",
        document_id="doc-a",
        display_name="第一份公开样例",
        keyword="共享关键词",
        vector=[0.8, 0.2],
    )

    results = evidence_service_module.EvidenceService(store).search(
        "字面不命中",
        ["kb-a"],
        query_vector=[0.8, 0.2],
        embedding_provider="fake",
        embedding_model="fake",
    )

    assert len(results) == 1
    assert results[0]["retrieval"]["mode"] == "embedding"


def _removed_test_evidence_search_provider_failure_returns_safe_keyword_fallback(tmp_path):
    secret = "sk-evidence-upstream-secret"

    class Provider:
        provider_name = "fake"
        model_name = "fake"

        def embed_texts(self, texts):
            raise RuntimeError(f"upstream error: {secret}")

    client, store, _, _ = make_client(tmp_path)
    record = store.list_index_records("kb-a", "doc-a")[0]
    store.replace_index_records(
        "kb-a",
        "doc-a",
        [
            replace(
                record,
                vector=[0.8, 0.2],
                embedding_provider="fake",
                embedding_model="fake",
                embedding_dimension=2,
            )
        ],
    )
    client = TestClient(
        create_app(
            db_path=store.db_path,
            managed_root=tmp_path / "managed",
            settings_path=tmp_path / "settings.json",
            embedding_provider=Provider(),
        )
    )

    response = search(client, ["kb-a"])

    assert response.status_code == 200
    assert response.json()["retrieval_mode_used"] == "keyword"
    assert any("查询向量" in warning for warning in response.json()["warnings"])
    assert secret not in response.text


def _removed_test_evidence_search_incomplete_index_skips_query_embedding_provider(tmp_path):
    class Provider:
        provider_name = "fake"
        model_name = "fake"

        def __init__(self):
            self.calls = []

        def embed_texts(self, texts):
            self.calls.append(list(texts))
            return [[0.8, 0.2]]

    provider = Provider()
    client, store, _, _ = make_client(tmp_path)
    client = TestClient(
        create_app(
            db_path=store.db_path,
            managed_root=tmp_path / "managed",
            settings_path=tmp_path / "settings.json",
            embedding_provider=provider,
        )
    )

    response = search(client, ["kb-a"])

    assert response.status_code == 200
    assert response.json()["retrieval_mode_requested"] == "embedding"
    assert response.json()["retrieval_mode_used"] == "keyword"
    assert provider.calls == []
    assert any("索引" in warning for warning in response.json()["warnings"])


def _removed_test_evidence_search_rejects_empty_or_unknown_scope_and_unindexed_kb(tmp_path):
    client, store, _, _ = make_client(tmp_path)

    assert search(client, []).status_code == 422
    unknown = search(client, ["missing-kb"])
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["code"] == "knowledge_base_not_found"

    add_knowledge_base(store, "kb-empty", "未索引资料库")
    unindexed = search(client, ["kb-empty"])
    assert unindexed.status_code == 409
    assert unindexed.json()["detail"]["code"] == "no_indexed_documents"


def _removed_test_evidence_metadata_comes_from_actual_supporting_block_and_get_uses_same_shape(tmp_path):
    client, _, first, _ = make_client(tmp_path)
    document, _, body, chunk, record = first

    response = search(client, ["kb-a"])

    assert response.status_code == 200
    evidence = response.json()["results"][0]
    assert evidence["id"].startswith("ev1.")
    assert evidence["evidence_id"] == evidence["id"]
    assert evidence["index_evidence_id"] == record.evidence_id
    assert evidence["knowledge_base_id"] == "kb-a"
    assert evidence["document_id"] == document.id
    assert evidence["display_name"] == document.display_name
    assert evidence["original_filename"] == document.original_filename
    assert evidence["chunk_text"] == chunk.chunk_text
    assert evidence["supporting_block_ids"] == [body.id]
    assert [block["id"] for block in evidence["supporting_blocks"]] == [body.id]
    assert evidence["human_page_number"] == 3
    assert evidence["bbox_by_page"] == {"2": [body.bbox]}
    assert evidence["bbox_page_size_by_page"] == {"2": [1000, 1400]}
    assert evidence["web_url"].startswith(
        f"/knowledge-bases/kb-a/documents/{document.id}?page=3"
    )

    detail = client.get(f"/api/evidence/{evidence['id']}")
    assert detail.status_code == 200
    assert detail.json().keys() == evidence.keys()
    assert detail.json()["supporting_block_ids"] == evidence["supporting_block_ids"]
    assert client.get("/api/evidence/missing-evidence").status_code == 404


def test_embedding_hit_falls_back_to_first_valid_body_block():
    title = Block(
        id="title",
        document_id="doc",
        page_idx=0,
        block_idx=0,
        block_type="title",
        text="标题",
    )
    first_body = Block(
        id="body-1",
        document_id="doc",
        page_idx=0,
        block_idx=1,
        block_type="paragraph",
        text="第一段有效正文",
        bbox=[1, 2, 3, 4],
    )
    second_body = Block(
        id="body-2",
        document_id="doc",
        page_idx=1,
        block_idx=0,
        block_type="paragraph",
        text="第二段正文",
        bbox=[5, 6, 7, 8],
    )
    chunk = Chunk(
        id="chunk",
        document_id="doc",
        chunk_idx=0,
        strategy="block",
        block_ids=[title.id, first_body.id, second_body.id],
        title_context="标题",
        inferred_heading_path=[],
        chunk_text="标题\n第一段有效正文\n第二段正文",
        indexing_text="标题\n第一段有效正文\n第二段正文",
        page_range=[0, 1],
        bbox_by_page={},
        modality_mix=["text"],
        token_count=20,
        strategy_version="v1",
    )
    record = IndexRecord(
        id="record",
        evidence_id="evidence",
        document_id="doc",
        chunk_id=chunk.id,
        block_ids=chunk.block_ids,
        modality="text",
        indexing_text=chunk.indexing_text,
        vector=[1.0, 0.0],
        embedding_provider="fake",
        embedding_model="fake",
        embedding_dimension=2,
        index_version="v1",
    )

    result = Retriever(
        [chunk], [record], [title, first_body, second_body]
    ).search("字面不命中", query_vector=[1.0, 0.0])

    assert result[0].block_ids == [first_body.id]
    assert result[0].page_range == [0]
    assert result[0].bbox_by_page == {"0": [first_body.bbox]}


def test_scoped_retrieval_reads_one_consistent_snapshot_during_concurrent_delete(
    tmp_path, monkeypatch
):
    _, store, first, _ = make_client(tmp_path)
    document, _, _, chunk, record = first
    with store.connect() as connection:
        connection.execute("pragma journal_mode = wal")
    writer = EvidenceStore(store.db_path)
    selected_documents = threading.Event()
    release_reader = threading.Event()
    original_connect = store.connect

    class CursorProxy:
        def __init__(self, cursor, should_pause):
            self.cursor = cursor
            self.should_pause = should_pause

        def fetchall(self):
            rows = self.cursor.fetchall()
            if self.should_pause:
                selected_documents.set()
                assert release_reader.wait(timeout=5)
            return rows

    class ConnectionProxy:
        def __init__(self, connection):
            self.connection = connection

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, *args):
            return self.connection.__exit__(*args)

        def execute(self, sql, parameters=()):
            cursor = self.connection.execute(sql, parameters)
            normalized = " ".join(sql.lower().split())
            return CursorProxy(
                cursor,
                normalized.startswith("select documents.* from documents")
                and "knowledge_base_id in" in normalized,
            )

    monkeypatch.setattr(store, "connect", lambda: ConnectionProxy(original_connect()))

    with ThreadPoolExecutor(max_workers=2) as executor:
        reading = executor.submit(store.load_retrieval_scope, ["kb-a"])
        assert selected_documents.wait(timeout=5)
        assert writer.delete_document("kb-a", document.id) is True
        release_reader.set()
        documents, chunks, records, blocks = reading.result(timeout=5)

    assert [item.id for item in documents] == [document.id]
    assert [item.id for item in chunks] == [chunk.id]
    assert [item.id for item in records] == [record.id]
    assert {block.document_id for block in blocks} == {document.id}


def _removed_test_search_locator_get_resolves_the_exact_same_supporting_block(tmp_path):
    client, store, first, _ = make_client(tmp_path)
    document, _, _, _, _ = first
    first_body = Block(
        id="doc-a:body-first",
        document_id=document.id,
        page_idx=0,
        block_idx=0,
        block_type="paragraph",
        text="第一段普通正文",
        bbox=[10, 20, 300, 100],
        page_size=[1000, 1400],
    )
    matched_body = Block(
        id="doc-a:body-matched",
        document_id=document.id,
        page_idx=1,
        block_idx=0,
        block_type="paragraph",
        text="第二段包含精确定位词",
        bbox=[100, 220, 800, 360],
        page_size=[1000, 1400],
    )
    chunk = Chunk(
        id="doc-a:multi-block-chunk",
        document_id=document.id,
        chunk_idx=0,
        strategy="block",
        block_ids=[first_body.id, matched_body.id],
        title_context=None,
        inferred_heading_path=[],
        chunk_text=f"{first_body.text}\n{matched_body.text}",
        indexing_text=f"{first_body.text}\n{matched_body.text}",
        page_range=[0, 1],
        bbox_by_page={"0": [first_body.bbox], "1": [matched_body.bbox]},
        modality_mix=["text"],
        token_count=20,
        strategy_version="v1",
    )
    record = IndexRecord(
        id="doc-a:multi-block-record",
        evidence_id="evidence:doc-a:multi",
        document_id=document.id,
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
        "kb-a", document, [first_body, matched_body], [], [chunk], [record]
    )

    search_response = search(client, ["kb-a"], query="精确定位词")

    assert search_response.status_code == 200
    evidence = search_response.json()["results"][0]
    assert evidence["id"].startswith("ev1.")
    assert evidence["id"] != record.evidence_id
    assert evidence["index_evidence_id"] == record.evidence_id
    assert evidence["supporting_block_ids"] == [matched_body.id]
    assert evidence["retrieval"]["score"] > 0

    detail = client.get(f"/api/evidence/{evidence['id']}")

    assert detail.status_code == 200
    detail_payload = detail.json()
    for field in (
        "id",
        "index_evidence_id",
        "supporting_block_ids",
        "page_idx",
        "human_page_number",
        "bbox_by_page",
        "bbox_page_size_by_page",
        "web_url",
    ):
        assert detail_payload[field] == evidence[field]
    assert detail_payload["retrieval"] is None


def _removed_test_search_locator_get_preserves_html_only_table_block(tmp_path):
    client, store, first, _ = make_client(tmp_path)
    document, _, _, _, _ = first
    table = Block(
        id="doc-a:table",
        document_id=document.id,
        page_idx=1,
        block_idx=0,
        block_type="table_body",
        text=None,
        html="<table><tr><td>表格精确定位词</td></tr></table>",
        bbox=[80, 160, 920, 760],
        page_size=[1000, 1400],
    )
    chunk = Chunk(
        id="doc-a:table-chunk",
        document_id=document.id,
        chunk_idx=0,
        strategy="block",
        block_ids=[table.id],
        title_context=None,
        inferred_heading_path=[],
        chunk_text=table.html or "",
        indexing_text=table.html or "",
        page_range=[table.page_idx],
        bbox_by_page={str(table.page_idx): [table.bbox]},
        modality_mix=["table"],
        token_count=10,
        strategy_version="v1",
    )
    record = IndexRecord(
        id="doc-a:table-record",
        evidence_id="evidence:doc-a:table",
        document_id=document.id,
        chunk_id=chunk.id,
        block_ids=[table.id],
        modality="table",
        indexing_text=chunk.indexing_text,
        vector=None,
        embedding_provider=None,
        embedding_model=None,
        embedding_dimension=None,
        index_version="v1",
    )
    store.replace_document_graph("kb-a", document, [table], [], [chunk], [record])

    search_response = search(client, ["kb-a"], query="表格精确定位词")
    evidence = search_response.json()["results"][0]
    detail = client.get(f"/api/evidence/{evidence['id']}")

    assert detail.status_code == 200
    assert detail.json()["supporting_block_ids"] == [table.id]
    assert detail.json()["supporting_blocks"][0]["html"] == table.html


def _removed_test_evidence_locator_rejects_noncanonical_and_oversized_encodings(tmp_path):
    client, _, _, _ = make_client(tmp_path)
    evidence = search(client, ["kb-a"]).json()["results"][0]
    prefix, index_segment, block_segment = evidence["id"].split(".")

    padded = f"{prefix}.{index_segment}=.{block_segment}"
    assert client.get(f"/api/evidence/{padded}").status_code == 404

    replacement = "A" if block_segment[-1] != "A" else "B"
    malicious = f"{prefix}.{index_segment}.{block_segment[:-1]}{replacement}"
    assert client.get(f"/api/evidence/{malicious}").status_code == 404

    oversized = f"ev1.{'YQ' * 2050}.Yg"
    with pytest.raises(EvidenceNotFoundError):
        _parse_evidence_locator(oversized)


def replace_with_single_searchable_block(
    store: EvidenceStore,
    knowledge_base_id: str,
    document: Document,
    *,
    block_id: str,
    evidence_id: str,
    keyword: str,
) -> Block:
    block = Block(
        id=block_id,
        document_id=document.id,
        page_idx=2,
        block_idx=0,
        block_type="paragraph",
        text=f"证据块包含{keyword}",
        bbox=[100, 200, 900, 420],
        page_size=[1000, 1400],
    )
    chunk = Chunk(
        id="doc-a:long-id-chunk",
        document_id=document.id,
        chunk_idx=0,
        strategy="block",
        block_ids=[block.id],
        title_context=None,
        inferred_heading_path=[],
        chunk_text=block.text or "",
        indexing_text=block.text or "",
        page_range=[block.page_idx],
        bbox_by_page={str(block.page_idx): [block.bbox]},
        modality_mix=["text"],
        token_count=10,
        strategy_version="v1",
    )
    record = IndexRecord(
        id="doc-a:long-id-record",
        evidence_id=evidence_id,
        document_id=document.id,
        chunk_id=chunk.id,
        block_ids=[block.id],
        modality="text",
        indexing_text=chunk.indexing_text,
        vector=None,
        embedding_provider=None,
        embedding_model=None,
        embedding_dimension=None,
        index_version="v1",
    )
    store.replace_document_graph(
        knowledge_base_id, document, [block], [], [chunk], [record]
    )
    return block


def _removed_test_web_url_keeps_raw_block_when_complete_url_fits_limit(tmp_path):
    client, store, first, _ = make_client(tmp_path)
    document, _, _, _, _ = first
    block = replace_with_single_searchable_block(
        store,
        "kb-a",
        document,
        block_id="mineru:" + "b" * 1200,
        evidence_id="evidence:raw-block-fits",
        keyword="原始定位词",
    )

    evidence = search(client, ["kb-a"], query="原始定位词").json()["results"][0]
    parsed_url = urlparse(evidence["web_url"])
    query = parse_qs(parsed_url.query)

    assert query["block"] == [block.id]
    assert "block_token" not in query
    assert len(evidence["web_url"]) <= evidence_service_module.MAX_EVIDENCE_WEB_URL_LENGTH


def _removed_test_locator_uses_fixed_block_token_for_very_long_mineru_block_id(tmp_path):
    client, store, first, _ = make_client(tmp_path)
    document, _, _, _, _ = first
    block = replace_with_single_searchable_block(
        store,
        "kb-a",
        document,
        block_id="mineru:" + "超长块标识" * 2000,
        evidence_id="e" * 1000,
        keyword="稳定定位词",
    )

    evidence = search(client, ["kb-a"], query="稳定定位词").json()["results"][0]
    detail = client.get(f"/api/evidence/{evidence['id']}")

    assert len(evidence["id"].split(".")[2]) == 43
    assert len(evidence["index_evidence_id"]) == 1000
    assert len(evidence["web_url"]) <= evidence_service_module.MAX_EVIDENCE_WEB_URL_LENGTH
    assert block.id not in evidence["web_url"]
    assert quote_plus(block.id) not in evidence["web_url"]
    assert f"evidence={evidence['id']}" in evidence["web_url"]
    assert detail.status_code == 200
    assert detail.json()["supporting_block_ids"] == [block.id]
    parsed_url = urlparse(evidence["web_url"])
    assert parsed_url.path == f"/knowledge-bases/kb-a/documents/{document.id}"
    assert parse_qs(parsed_url.query)["evidence"] == [evidence["id"]]
    block_token = parse_qs(parsed_url.query)["block_token"][0]
    assert evidence["block_token"] == block_token
    assert detail.json()["block_token"] == block_token
    route = client.get(evidence["web_url"])
    assert route.status_code == 200


def _removed_test_search_rejects_evidence_when_token_url_exceeds_limit(tmp_path):
    db_path = tmp_path / "traceable.db"
    store = EvidenceStore(db_path)
    knowledge_base_id = "kb-too-long"
    add_knowledge_base(store, knowledge_base_id, "超长标识资料库")
    indexed = add_indexed_document(
        store,
        tmp_path,
        kb_id=knowledge_base_id,
        document_id="doc-too-long",
        display_name="超长链接文档",
        keyword="超限定位词",
    )
    document, _, _, _, _ = indexed
    replace_with_single_searchable_block(
        store,
        knowledge_base_id,
        document,
        block_id="mineru:" + "超长块标识" * 2000,
        evidence_id="e" * 3000,
        keyword="超限定位词",
    )

    with pytest.raises(evidence_service_module.EvidenceWebUrlTooLongError):
        evidence_service_module.EvidenceService(store).search(
            "超限定位词", [knowledge_base_id]
        )

    client = TestClient(
        create_app(db_path=db_path, managed_root=tmp_path / "managed"),
        raise_server_exceptions=False,
    )
    response = search(client, [knowledge_base_id], query="超限定位词")

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "evidence_link_too_long",
            "message": "Evidence link exceeds the supported URL length.",
        }
    }
    assert "results" not in response.json()


def _removed_test_search_rejects_evidence_when_locator_exceeds_limit(tmp_path):
    db_path = tmp_path / "traceable.db"
    store = EvidenceStore(db_path)
    knowledge_base_id = "kb-locator-too-long"
    add_knowledge_base(store, knowledge_base_id, "Locator 超限资料库")
    indexed = add_indexed_document(
        store,
        tmp_path,
        kb_id=knowledge_base_id,
        document_id="doc-locator-too-long",
        display_name="Locator 超限文档",
        keyword="Locator 超限定位词",
    )
    document, _, _, _, _ = indexed
    replace_with_single_searchable_block(
        store,
        knowledge_base_id,
        document,
        block_id="mineru:locator-too-long",
        evidence_id="e" * 3100,
        keyword="Locator 超限定位词",
    )

    with pytest.raises(evidence_service_module.EvidenceWebUrlTooLongError):
        evidence_service_module.EvidenceService(store).search(
            "Locator 超限定位词", [knowledge_base_id]
        )

    client = TestClient(
        create_app(db_path=db_path, managed_root=tmp_path / "managed"),
        raise_server_exceptions=False,
    )
    response = search(client, [knowledge_base_id], query="Locator 超限定位词")

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "evidence_link_too_long",
            "message": "Evidence link exceeds the supported URL length.",
        }
    }
    assert "results" not in response.json()


def _removed_test_locator_token_collision_is_rejected_as_ambiguous(tmp_path, monkeypatch):
    client, store, first, _ = make_client(tmp_path)
    document, _, _, _, _ = first
    first_block = Block(
        id="doc-a:collision-a",
        document_id=document.id,
        page_idx=0,
        block_idx=0,
        block_type="paragraph",
        text="冲突定位词只在第一块",
    )
    second_block = Block(
        id="doc-a:collision-b",
        document_id=document.id,
        page_idx=1,
        block_idx=0,
        block_type="paragraph",
        text="第二块普通正文",
    )
    chunk = Chunk(
        id="doc-a:collision-chunk",
        document_id=document.id,
        chunk_idx=0,
        strategy="block",
        block_ids=[first_block.id, second_block.id],
        title_context=None,
        inferred_heading_path=[],
        chunk_text=f"{first_block.text}\n{second_block.text}",
        indexing_text=f"{first_block.text}\n{second_block.text}",
        page_range=[0, 1],
        bbox_by_page={},
        modality_mix=["text"],
        token_count=10,
        strategy_version="v1",
    )
    record = IndexRecord(
        id="doc-a:collision-record",
        evidence_id="evidence:doc-a:collision",
        document_id=document.id,
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
        "kb-a", document, [first_block, second_block], [], [chunk], [record]
    )
    monkeypatch.setattr(
        evidence_service_module,
        "_evidence_version_token",
        lambda *_args: "A" * 43,
        raising=False,
    )

    evidence = search(client, ["kb-a"], query="冲突定位词").json()["results"][0]

    assert client.get(f"/api/evidence/{evidence['id']}").status_code == 404


def _removed_test_locator_survives_document_rename_but_invalidates_after_content_reindex(
    tmp_path,
):
    client, store, first, _ = make_client(tmp_path)
    document, title, body, chunk, record = first
    evidence = search(client, ["kb-a"], query="共享关键词").json()["results"][0]

    assert store.update_document_display_name("kb-a", document.id, "重命名业务文档")
    renamed_detail = client.get(f"/api/evidence/{evidence['id']}")
    assert renamed_detail.status_code == 200
    assert renamed_detail.json()["display_name"] == "重命名业务文档"

    renamed_document = store.get_document("kb-a", document.id)
    changed_body = replace(body, text=f"{body.text} 内容版本二")
    changed_chunk = replace(
        chunk,
        chunk_text=f"{title.text}\n{changed_body.text}",
        indexing_text=f"{title.text}\n{changed_body.text}",
    )
    changed_record = replace(
        record,
        indexing_text=changed_chunk.indexing_text,
        index_version="v2",
    )
    store.replace_document_graph(
        "kb-a",
        renamed_document,
        [title, changed_body],
        [],
        [changed_chunk],
        [changed_record],
    )

    assert client.get(f"/api/evidence/{evidence['id']}").status_code == 404
    refreshed = search(client, ["kb-a"], query="共享关键词").json()["results"][0]
    assert refreshed["evidence_id"] != evidence["evidence_id"]


def _removed_test_locator_ignores_vector_values_but_binds_embedding_identity(tmp_path):
    db_path = tmp_path / "traceable.db"
    store = EvidenceStore(db_path)
    add_knowledge_base(store, "kb-a", "第一资料库")
    document, title, body, chunk, record = add_indexed_document(
        store,
        tmp_path,
        kb_id="kb-a",
        document_id="doc-vector",
        display_name="向量公开样例",
        keyword="向量关键词",
        vector=[0.1, 0.2],
    )
    client = TestClient(create_app(db_path=db_path, managed_root=tmp_path / "managed"))
    evidence = search(client, ["kb-a"], query="向量关键词").json()["results"][0]

    store.replace_document_graph(
        "kb-a",
        document,
        [title, body],
        [],
        [chunk],
        [replace(record, vector=[0.2, 0.1])],
    )

    assert client.get(f"/api/evidence/{evidence['id']}").status_code == 200

    store.replace_document_graph(
        "kb-a",
        document,
        [title, body],
        [],
        [chunk],
        [replace(record, vector=[0.2, 0.1], embedding_model="fake-v2")],
    )

    assert client.get(f"/api/evidence/{evidence['id']}").status_code == 404
