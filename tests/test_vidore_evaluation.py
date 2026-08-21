from __future__ import annotations

import io
import json
import tarfile
import threading
from pathlib import Path

import pytest
from pypdf import PdfWriter

from evaluation.vidore.embedding_cache import CachedRetryEmbeddingProvider
from evaluation.vidore.config import load_config
from evaluation.vidore.manifest import DatasetDocument, filter_english_queries, read_rows
from evaluation.vidore.metrics import evaluate_query, evaluate_run, normalize_qrels
from evaluation.vidore.mineru_artifacts import safe_extract_tar_gz, validate_mineru_document
from evaluation.vidore.official_evaluator import evaluate_run_official
from evaluation.vidore.pipeline import (
    CorpusPage,
    _dense_scores,
    _normalized_matrix,
    _top_positive_indices,
    _top_unique_chunk_indices,
    format_query_texts,
    normalize_corpus_rows,
    project_chunk_scores_to_pages,
)
from evaluation.vidore.pipeline import QueryResultDetail
from evaluation.vidore.reporting import write_results
from evaluation.vidore.reranker import (
    CachedRetryReranker,
    OpenAICompatibleReranker,
    RerankResult,
)
from evaluation.vidore.table_indexing import build_table_aware_index_records
from traceable_rag.embedding import EmbeddingProvider
from traceable_rag.models import Block, Chunk, IndexRecord


class StubProvider(EmbeddingProvider):
    provider_name = "stub"
    model_name = "stub-v1"

    def __init__(self, failures: int = 0):
        self.failures = failures
        self.calls = 0

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("transient")
        return [[float(len(text)), 1.0] for text in texts]


class ConcurrentStubProvider(EmbeddingProvider):
    provider_name = "concurrent-stub"
    model_name = "concurrent-stub-v1"

    def __init__(self, parties: int):
        self.barrier = threading.Barrier(parties, timeout=2)
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.barrier.wait()
            return [[float(len(text)), 1.0] for text in texts]
        finally:
            with self.lock:
                self.active -= 1


class StubRerankerHTTP:
    def __init__(self):
        self.calls: list[tuple[str, dict, dict, int]] = []

    def post_json(self, url, headers, payload, timeout):
        self.calls.append((url, headers, payload, timeout))
        return {
            "results": [
                {"index": index, "relevance_score": float(index) / 10}
                for index in reversed(range(len(payload["documents"])))
            ],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        }


class StubReranker:
    model_name = "stub-reranker"

    def __init__(self):
        self.calls = 0

    def rerank(self, query, documents, *, top_n=None):
        self.calls += 1
        return [
            RerankResult(index, float(len(documents) - index))
            for index in range(len(documents))
        ]


def _chunk(chunk_id: str, document_id: str, pages: list[int]) -> Chunk:
    return Chunk(chunk_id, document_id, 0, "test", [], None, [], "", "", pages, {}, ["text"], 10, "v1")


def _table_block(
    block_id: str,
    *,
    page: int,
    html: str,
    caption: list[str] | None = None,
) -> Block:
    return Block(
        id=block_id,
        document_id="doc",
        page_idx=page,
        block_idx=1,
        block_type="table",
        text="table fallback",
        html=html,
        bbox=[0, 0, 100, 100],
        page_size=[100, 100],
        metadata={"caption": caption or []},
    )


def _mineru_document(tmp_path: Path, *, pages: int = 1) -> tuple[DatasetDocument, Path]:
    pdf_path = tmp_path / "doc.pdf"
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=100, height=200)
    with pdf_path.open("wb") as handle:
        writer.write(handle)
    result_dir = tmp_path / "result"
    result_dir.mkdir()
    (result_dir / "processing_manifest.json").write_text(
        json.dumps({"doc_id": "doc", "status": "done"}), encoding="utf-8"
    )
    return DatasetDocument("doc", pdf_path, "0" * 64, pages), result_dir


def test_chunk_scores_project_to_all_pages_using_max_score_and_stable_order():
    scores = project_chunk_scores_to_pages(
        [(0.9, _chunk("a", "kb:doc", [0, 1])), (0.8, _chunk("b", "kb:doc", [1, 2]))],
        {("doc", 0): "p0", ("doc", 1): "p1", ("doc", 2): "p2"},
        {"kb:doc": "doc"},
        top_k_pages=3,
    )
    assert list(scores) == ["p0", "p1", "p2"]
    assert scores == {"p0": 0.9, "p1": 0.9, "p2": 0.8}


def test_dense_matrix_ranking_matches_cosine_order_and_filters_negative_scores():
    pytest.importorskip("numpy")
    matrix = _normalized_matrix([[1.0, 0.0], [1.0, 1.0], [-1.0, 0.0]])
    scores = _dense_scores(matrix, [1.0, 0.0])

    assert _top_positive_indices(scores, 3) == [0, 1]


def test_dense_ranking_deduplicates_multiple_table_records_for_same_chunk():
    pytest.importorskip("numpy")
    records = [
        IndexRecord(str(i), str(i), "doc", chunk_id, [], "table", "", [1.0], "p", "m", 1, "v")
        for i, chunk_id in enumerate(("chunk-a", "chunk-a", "chunk-b"))
    ]

    assert _top_unique_chunk_indices([0.9, 0.8, 0.7], records, 2) == [0, 2]


def test_qwen_query_prompt_matches_sentence_transformers_concatenation():
    prompt = (
        "Instruct: Given a web search query, retrieve relevant passages that answer the query\n"
        "Query:"
    )

    assert format_query_texts([{"query": "What was revenue?"}], prompt) == [
        "Instruct: Given a web search query, retrieve relevant passages that answer the query\n"
        "Query:What was revenue?"
    ]
    assert format_query_texts([{"query": "What was revenue?"}], "") == [
        "What was revenue?"
    ]


def test_optional_query_prompt_preserves_historical_fingerprints():
    config_root = Path(__file__).parents[1] / "evaluation" / "vidore" / "configs"

    assert load_config(config_root / "finance_en.json").fingerprint == "1688b6678d2fc287"
    assert (
        load_config(config_root / "finance_en_table_row_groups_v1.json").fingerprint
        == "fd2a14126a9c7ee1"
    )


def test_table_aware_index_replaces_stale_title_with_same_page_heading_and_caption(tmp_path):
    heading = Block(
        "heading", "doc", 3, 0, "title", "Correct Section", bbox=[0, 0, 1, 1], page_size=[100, 100]
    )
    table = _table_block(
        "table",
        page=3,
        html="<table><tr><th>Year</th><th>Rate</th></tr><tr><td>2024</td><td>5%</td></tr></table>",
        caption=["Deposit rates"],
    )
    chunk = Chunk(
        "chunk", "doc", 0, "baseline", ["table"], "Stale Global Title", [],
        "Year Rate 2024 5%", "Stale Global Title\nYear Rate 2024 5%", [3], {},
        ["table"], 10, "v1",
    )

    records = build_table_aware_index_records(
        [chunk], [heading, table], StubProvider(), index_version="table-test"
    )

    assert records
    assert all("Stale Global Title" not in record.indexing_text for record in records)
    assert "Correct Section" in records[0].indexing_text
    assert "Deposit rates" in records[0].indexing_text


def test_table_aware_index_adds_header_repeated_row_groups_for_large_table():
    rows = "".join(
        f"<tr><td>Series {i}</td><td>{'value ' * 90}</td></tr>"
        for i in range(6)
    )
    table = _table_block(
        "table",
        page=0,
        html=f"<table><tr><th>Series</th><th>Value</th></tr>{rows}</table>",
    )
    chunk = Chunk(
        "chunk", "doc", 0, "baseline", ["table"], "Old", [], "large table",
        "Old\nlarge table", [0], {}, ["table"], 700, "v1",
    )

    records = build_table_aware_index_records(
        [chunk], [table], StubProvider(), index_version="table-test"
    )
    row_records = [record for record in records if "Table columns:" in record.indexing_text]

    assert len(row_records) >= 3
    assert all("Table columns: Series | Value" in record.indexing_text for record in row_records)
    assert all(record.block_ids == chunk.block_ids for record in row_records)


def test_table_aware_index_keeps_non_table_indexing_text_unchanged():
    chunk = Chunk(
        "chunk", "doc", 0, "baseline", ["paragraph"], "Heading", [], "Body",
        "Heading\nBody", [0], {}, ["text"], 2, "v1",
    )
    paragraph = Block(
        "paragraph", "doc", 0, 0, "paragraph", "Body", bbox=[0, 0, 1, 1], page_size=[100, 100]
    )

    records = build_table_aware_index_records(
        [chunk], [paragraph], StubProvider(), index_version="table-test"
    )

    assert len(records) == 1
    assert records[0].indexing_text == chunk.indexing_text


def test_corpus_page_base_is_normalized_and_duplicates_are_rejected():
    pages = normalize_corpus_rows(
        [{"corpus_id": "p1", "doc_id": "d", "page_number_in_doc": 1, "markdown": "x"}],
        page_number_base=1,
    )
    assert pages == [CorpusPage("p1", "d", 0, "x")]
    with pytest.raises(ValueError, match="unique"):
        normalize_corpus_rows(
            [
                {"corpus_id": "p1", "doc_id": "d", "page_idx": 0},
                {"corpus_id": "p2", "doc_id": "d", "page_idx": 0},
            ],
            page_number_base=0,
        )


def test_zero_ids_are_valid_for_official_vidore_corpus_and_qrels():
    pages = normalize_corpus_rows(
        [{"corpus_id": 0, "doc_id": "d", "page_number_in_doc": 0}],
        page_number_base=0,
    )
    assert pages[0].corpus_id == "0"
    assert normalize_qrels([{"query_id": 0, "corpus_id": 0, "score": 2}]) == {
        "0": {"0": 2.0}
    }


def test_cached_provider_retries_deduplicates_and_reuses_sha256_cache(tmp_path):
    upstream = StubProvider(failures=1)
    provider = CachedRetryEmbeddingProvider(upstream, tmp_path, max_attempts=2, sleeper=lambda _: None)
    assert provider.embed_texts(["hello", "hello"]) == [[5.0, 1.0], [5.0, 1.0]]
    assert upstream.calls == 2
    assert provider.embed_texts(["hello"]) == [[5.0, 1.0]]
    assert upstream.calls == 2
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_cached_provider_persists_each_successful_request_batch(tmp_path):
    upstream = StubProvider()
    provider = CachedRetryEmbeddingProvider(
        upstream, tmp_path, request_batch_size=2
    )

    assert len(provider.embed_texts(["a", "bb", "ccc"])) == 3
    assert upstream.calls == 2
    assert len(list(tmp_path.glob("*.json"))) == 3


def test_cached_provider_runs_missing_batches_concurrently_and_keeps_order(tmp_path):
    upstream = ConcurrentStubProvider(parties=4)
    provider = CachedRetryEmbeddingProvider(
        upstream,
        tmp_path,
        request_batch_size=1,
        max_concurrency=4,
    )

    assert provider.embed_texts(["a", "bb", "ccc", "dddd"]) == [
        [1.0, 1.0],
        [2.0, 1.0],
        [3.0, 1.0],
        [4.0, 1.0],
    ]
    assert upstream.max_active == 4


def test_reranker_uses_documented_v1_contract_and_validates_results():
    http = StubRerankerHTTP()
    reranker = OpenAICompatibleReranker(
        "secret",
        "https://reranker.example/v1",
        "model",
        http_client=http,
    )

    results = reranker.rerank("query", ["first", "second"], top_n=2)

    assert results == [RerankResult(1, 0.1), RerankResult(0, 0.0)]
    url, headers, payload, timeout = http.calls[0]
    assert url == "https://reranker.example/v1/rerank"
    assert headers["Authorization"] == "Bearer secret"
    assert payload == {
        "model": "model",
        "query": "query",
        "documents": ["first", "second"],
        "top_n": 2,
        "return_documents": False,
    }
    assert timeout == 120


def test_cached_reranker_reuses_sha256_request_cache(tmp_path):
    upstream = StubReranker()
    reranker = CachedRetryReranker(upstream, tmp_path)
    requests = [("q", ["a", "b"]), ("q", ["a", "b"])]

    expected = [RerankResult(0, 2.0), RerankResult(1, 1.0)]
    assert reranker.rerank_many(requests) == [expected, expected]
    assert upstream.calls == 1
    assert reranker.rerank_many(requests[:1]) == [expected]
    assert upstream.calls == 1
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_safe_tar_rejects_traversal_and_links(tmp_path):
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        info = tarfile.TarInfo("../escape.txt")
        payload = b"bad"
        info.size = len(payload)
        bundle.addfile(info, io.BytesIO(payload))
    with pytest.raises(ValueError, match="Unsafe"):
        safe_extract_tar_gz(archive, tmp_path / "out")


def test_metrics_use_graded_ndcg_and_binary_recall_map():
    qrels = normalize_qrels(
        [
            {"query_id": "q", "corpus_id": "best", "score": 2},
            {"query_id": "q", "corpus_id": "needed", "score": 1},
        ]
    )
    values = evaluate_run(qrels, {"q": {"needed": 0.9, "best": 0.8}})
    assert values["recall@5"] == 1.0
    assert values["map"] == 1.0
    assert 0 < values["ndcg@10"] < 1.0


def test_official_pytrec_eval_matches_internal_top10_equivalents():
    pytest.importorskip("pytrec_eval")
    qrels = {
        "q": {"best": 2.0, "needed": 1.0},
        "q2": {"other": 1.0},
    }
    run = {
        "q": {"needed": 0.9, "best": 0.8, "wrong": 0.7},
        "q2": {"wrong": 0.5, "other": 0.4},
    }

    internal = evaluate_run(qrels, run)
    official = evaluate_run_official(
        qrels,
        run,
        metrics=("ndcg_cut_10", "recall_5", "recall_10", "map_cut_10"),
    )

    # pytrec_eval uses TREC's graded-gain convention, while the initial local
    # evaluator used exponential DCG gains. Recall and binary MAP are expected
    # to match; nDCG is intentionally recalculated rather than aliased.
    assert 0 < official["ndcg_cut_10"] <= 1
    assert official["ndcg_cut_10"] != pytest.approx(internal["ndcg@10"])
    assert official["recall_5"] == pytest.approx(internal["recall@5"])
    assert official["recall_10"] == pytest.approx(internal["recall@10"])
    assert official["map_cut_10"] == pytest.approx(internal["map"])


def test_english_query_filter_requires_expected_unique_count():
    rows = [
        {"query_id": "q1", "language": "english"},
        {"query_id": "q2", "language": "french"},
    ]
    assert filter_english_queries(rows, 1) == [rows[0]]
    with pytest.raises(ValueError, match="Expected 2"):
        filter_english_queries(rows, 2)


def test_batch_mineru_archive_extracts_nested_output_safely(tmp_path):
    archive = tmp_path / "batch.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        info = tarfile.TarInfo("mineru_output/doc/content_list_v2.json")
        payload = b"[[]]"
        info.size = len(payload)
        bundle.addfile(info, io.BytesIO(payload))
    destination = safe_extract_tar_gz(archive, tmp_path / "unpacked")
    assert (destination / "mineru_output/doc/content_list_v2.json").is_file()


def test_mineru_validation_accepts_v2_only_and_v1_outputs(tmp_path):
    document, result_dir = _mineru_document(tmp_path)
    (result_dir / "content_list_v2.json").write_text(
        json.dumps([[{"type": "paragraph", "bbox": [1, 2, 3, 4], "page_size": [100, 200], "content": {"paragraph_content": [{"type": "text", "content": "v2"}]}}]]),
        encoding="utf-8",
    )
    assert validate_mineru_document(document, result_dir).block_count == 1

    (result_dir / "content_list_v2.json").unlink()
    (result_dir / "content_list.json").write_text(
        json.dumps([{"type": "text", "text": "v1", "page_idx": 0, "bbox": [1, 2, 3, 4], "page_size": [100, 200]}]),
        encoding="utf-8",
    )
    assert validate_mineru_document(document, result_dir).block_count == 1


def test_mineru_validation_rejects_missing_pages_and_out_of_range_page(tmp_path):
    document, result_dir = _mineru_document(tmp_path, pages=2)
    (result_dir / "content_list_v2.json").write_text(json.dumps([[{"type": "paragraph", "content": {"paragraph_content": [{"content": "only page"}]}}]]), encoding="utf-8")
    with pytest.raises(ValueError, match="page count mismatch"):
        validate_mineru_document(document, result_dir)

    (result_dir / "content_list_v2.json").unlink()
    (result_dir / "content_list.json").write_text(json.dumps([{"type": "text", "text": "bad page", "page_idx": 2}]), encoding="utf-8")
    with pytest.raises(ValueError, match="page count mismatch|out of range"):
        validate_mineru_document(document, result_dir)


def test_mineru_validation_rejects_corrupt_json_missing_asset_and_failed_status(tmp_path):
    document, result_dir = _mineru_document(tmp_path)
    content_path = result_dir / "content_list.json"
    content_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        validate_mineru_document(document, result_dir)

    content_path.write_text(
        json.dumps([{"type": "image", "page_idx": 0, "bbox": [1, 2, 3, 4], "page_size": [100, 200], "img_path": "missing.png"}]),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="asset is missing"):
        validate_mineru_document(document, result_dir)

    (result_dir / "processing_manifest.json").write_text(
        json.dumps({"doc_id": "doc", "status": "failed"}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="did not complete"):
        validate_mineru_document(document, result_dir)


def test_read_rows_supports_parquet_column_projection(tmp_path):
    pyarrow = pytest.importorskip("pyarrow")
    import pyarrow.parquet as parquet

    path = tmp_path / "rows.parquet"
    parquet.write_table(
        pyarrow.table({"query_id": [1, 2], "query": ["a", "b"], "image": [b"large", b"payload"]}),
        path,
    )
    assert read_rows(path, columns=["query_id", "query"]) == [
        {"query_id": 1, "query": "a"},
        {"query_id": 2, "query": "b"},
    ]


def test_report_writes_aggregate_table_and_compact_worst_errors(tmp_path):
    qrels = {"q": {"gold": 2.0}}
    run = {"q": {"wrong": 0.9}}
    metrics = {"traceable_chunk": evaluate_run(qrels, run)}
    write_results(
        tmp_path,
        config={"fingerprint": "abc"},
        qrels=qrels,
        runs={"traceable_chunk": run},
        details={"traceable_chunk": [QueryResultDetail("q", "question", run["q"])]},
        metrics=metrics,
    )
    assert "traceable_chunk" in (tmp_path / "report.md").read_text(encoding="utf-8")
    errors = json.loads((tmp_path / "traceable_chunk.errors.json").read_text(encoding="utf-8"))
    assert errors[0]["query_id"] == "q"
    assert errors[0]["recall@10"] == 0.0
