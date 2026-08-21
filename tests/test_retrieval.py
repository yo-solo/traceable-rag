import pytest
from types import SimpleNamespace

from traceable_rag.evidence_service import EvidenceService
from traceable_rag.models import Block, Chunk, Document, IndexRecord
from traceable_rag.retrieval import Retriever


def make_chunk(idx, text, vector=None):
    chunk = Chunk(
        id=f"c{idx}",
        document_id="doc1",
        chunk_idx=idx,
        strategy="block_window_with_title_hints",
        block_ids=[f"b{idx}"],
        title_context="编译器分析",
        inferred_heading_path=[],
        chunk_text=text,
        indexing_text=f"编译器分析\n{text}",
        page_range=[idx],
        bbox_by_page={str(idx): [[0, 0, 1, 1]]},
        modality_mix=["text"],
        token_count=len(text),
        strategy_version="v1",
    )
    record = IndexRecord(
        id=f"ir{idx}",
        evidence_id=f"evidence-{idx}",
        document_id="doc1",
        chunk_id=chunk.id,
        block_ids=chunk.block_ids,
        modality="text",
        indexing_text=chunk.indexing_text,
        vector=vector,
        embedding_provider="fake" if vector else None,
        embedding_model="fake" if vector else None,
        embedding_dimension=len(vector) if vector else None,
        index_version="test",
    )
    return chunk, record


def test_keyword_retrieval_returns_locatable_evidence():
    chunk1, record1 = make_chunk(1, "编译器前端包含词法分析和语法分析")
    chunk2, record2 = make_chunk(2, "其他无关内容")

    candidates = Retriever([chunk1, chunk2], [record1, record2]).search("编译器 词法分析")

    assert candidates
    assert candidates[0].chunk_id == "c1"
    assert candidates[0].block_ids == ["b1"]
    assert candidates[0].page_range == [1]
    assert candidates[0].bbox_by_page == {"1": [[0, 0, 1, 1]]}
    assert candidates[0].support_level in {"strong", "weak"}
    assert candidates[0].id == record1.evidence_id
    assert Retriever([chunk1, chunk2], [record1, record2]).get_evidence(record1.evidence_id).id == record1.evidence_id


def test_vector_retrieval_uses_embeddings_when_available():
    chunk1, record1 = make_chunk(1, "中端优化", vector=[1.0, 0.0])
    chunk2, record2 = make_chunk(2, "无关内容", vector=[0.0, 1.0])

    candidates = Retriever([chunk1, chunk2], [record1, record2]).search("query", query_vector=[0.9, 0.1])

    assert candidates[0].chunk_id == "c1"
    assert candidates[0].score > candidates[1].score
    assert candidates[0].metadata["retrieval_mode"] == "embedding"


def test_retrieval_deduplicates_chunk_variants_before_reranking():
    chunk1, record1 = make_chunk(1, "first", vector=[1.0, 0.0])
    chunk2, record2 = make_chunk(2, "second", vector=[0.7, 0.3])
    record1_variant = IndexRecord(
        **{
            **record1.__dict__,
            "id": "ir1-variant",
            "evidence_id": "evidence-1-variant",
            "indexing_text": "first table row variant",
            "vector": [0.9, 0.1],
        }
    )

    class ReverseReranker:
        def __init__(self):
            self.documents = None

        def rerank(self, _query, documents, *, top_n=None):
            self.documents = documents
            return [
                SimpleNamespace(index=1, relevance_score=0.9),
                SimpleNamespace(index=0, relevance_score=0.2),
            ][:top_n]

    reranker = ReverseReranker()
    candidates = Retriever(
        [chunk1, chunk2], [record1, record1_variant, record2]
    ).search(
        "query",
        query_vector=[1.0, 0.0],
        top_k=2,
        reranker=reranker,
    )

    assert len(reranker.documents) == 2
    assert [candidate.chunk_id for candidate in candidates] == ["c2", "c1"]


def test_query_vector_does_not_mark_keyword_scored_record_as_embedding_hit():
    chunk, record = make_chunk(1, "关键词正文", vector=None)

    candidates = Retriever([chunk], [record]).search(
        "关键词", query_vector=[0.9, 0.1]
    )

    assert candidates[0].metadata["retrieval_mode"] == "keyword"


def test_get_evidence_does_not_label_vector_record_as_embedding_retrieval():
    chunk, record = make_chunk(1, "详情读取正文", vector=[1.0, 0.0])

    candidate = Retriever([chunk], [record]).get_evidence(record.evidence_id)

    assert candidate is not None
    assert candidate.metadata["retrieval_mode"] == "keyword"


def test_vector_retrieval_is_stable_for_extreme_finite_values():
    chunk, record = make_chunk(1, "极大有限向量", vector=[1e308, 1e308])

    candidates = Retriever([chunk], [record]).search(
        "query", query_vector=[1e308, 1e308]
    )

    assert len(candidates) == 1
    assert candidates[0].score == pytest.approx(1.0)
    assert candidates[0].metadata["retrieval_mode"] == "embedding"


def test_embedding_only_hit_can_use_html_only_table_as_support():
    table = Block(
        id="table",
        document_id="doc",
        page_idx=0,
        block_idx=0,
        block_type="table_body",
        text=None,
        html="<table><tr><td>表格内容</td></tr></table>",
    )
    chunk = Chunk(
        id="chunk",
        document_id="doc",
        chunk_idx=0,
        strategy="block",
        block_ids=[table.id],
        title_context=None,
        inferred_heading_path=[],
        chunk_text=table.html or "",
        indexing_text=table.html or "",
        page_range=[0],
        bbox_by_page={},
        modality_mix=["table"],
        token_count=8,
        strategy_version="v1",
    )
    record = IndexRecord(
        id="record",
        evidence_id="evidence",
        document_id="doc",
        chunk_id=chunk.id,
        block_ids=[table.id],
        modality="table",
        indexing_text=chunk.indexing_text,
        vector=[1.0, 0.0],
        embedding_provider="fake",
        embedding_model="fake",
        embedding_dimension=2,
        index_version="v1",
    )

    results = Retriever([chunk], [record], [table]).search(
        "字面不命中", query_vector=[1.0, 0.0]
    )

    assert results[0].block_ids == [table.id]


def test_evidence_service_reports_reranker_fallback_and_keeps_indexing_text():
    class FailingReranker:
        def rerank(self, *_args, **_kwargs):
            raise TimeoutError("reranker timed out")

    class PassingReranker:
        def rerank(self, _query, documents, *, top_n=None):
            return [
                SimpleNamespace(index=index, relevance_score=1.0 - index / 10)
                for index in range(min(top_n or len(documents), len(documents)))
            ]

    block = Block(
        id="b1",
        document_id="doc1",
        page_idx=0,
        block_idx=0,
        block_type="paragraph",
        text="source text with a supported claim",
        bbox=[0, 0, 1, 1],
        page_size=[100, 100],
    )
    chunk, record = make_chunk(1, block.text or "", vector=[1.0, 0.0])
    record = IndexRecord(
        **{**record.__dict__, "indexing_text": "ranking representation only"}
    )
    document = Document(
        id="doc1",
        knowledge_base_id="kb1",
        original_filename="source.pdf",
        display_name="Source",
        file_hash="hash",
        source_type="upload",
        source_file_path=None,
        preview_file_path=None,
        mineru_result_dir=None,
        page_count=1,
        processing_status="ready",
        processing_error=None,
        parser_provider="mineru",
    )

    result = EvidenceService(object(), reranker=FailingReranker()).search_with_mode(
        "supported claim",
        ["kb1"],
        retrieval_mode_requested="embedding",
        query_vector=[1.0, 0.0],
        embedding_provider="fake",
        embedding_model="fake",
        retrieval_scope=([document], [chunk], [record], [block]),
    )

    assert result.retrieval_mode_used == "embedding"
    assert result.pipeline_used == "dense"
    assert result.degraded is True
    assert result.results
    assert result.indexing_text_by_evidence_id[
        result.results[0]["index_evidence_id"]
    ] == "ranking representation only"

    reranked = EvidenceService(object(), reranker=PassingReranker()).search_with_mode(
        "supported claim",
        ["kb1"],
        retrieval_mode_requested="embedding",
        query_vector=[1.0, 0.0],
        embedding_provider="fake",
        embedding_model="fake",
        retrieval_scope=([document], [chunk], [record], [block]),
    )

    assert reranked.pipeline_used == "dense_reranker"
    assert reranked.degraded is False
