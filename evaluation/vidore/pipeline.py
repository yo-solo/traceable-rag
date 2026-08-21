from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from traceable_rag.chunking import BlockWindowChunker
from traceable_rag.embedding import EmbeddingProvider
from traceable_rag.indexing import build_index_records
from traceable_rag.mineru_import import parse_mineru_directory
from traceable_rag.models import Chunk, IndexRecord, KnowledgeBase
from traceable_rag.store import EvidenceStore

from .config import BenchmarkConfig
from .manifest import DatasetManifest
from .reranker import CachedRetryReranker
from .table_indexing import build_table_aware_index_records


Run = dict[str, dict[str, float]]


@dataclass(frozen=True)
class CorpusPage:
    corpus_id: str
    doc_id: str
    page_idx: int
    markdown: str


@dataclass(frozen=True)
class QueryResultDetail:
    query_id: str
    query: str
    scores: dict[str, float]
    chunks: tuple[dict[str, Any], ...] = ()


def format_query_texts(
    query_rows: Iterable[dict[str, Any]], query_prompt: str
) -> list[str]:
    return [
        f"{query_prompt}{str(row.get('query') or '')}"
        for row in query_rows
    ]


def normalize_corpus_rows(rows: Iterable[dict[str, Any]], *, page_number_base: int) -> list[CorpusPage]:
    pages: list[CorpusPage] = []
    seen_corpus: set[str] = set()
    seen_location: set[tuple[str, int]] = set()
    for row in rows:
        raw_corpus_id = row.get("corpus_id")
        if raw_corpus_id is None:
            raw_corpus_id = row.get("id")
        corpus_id = "" if raw_corpus_id is None else str(raw_corpus_id)
        raw_doc_id = row.get("doc_id")
        doc_id = "" if raw_doc_id is None else str(raw_doc_id)
        page_number = row.get("page_number_in_doc", row.get("page_idx"))
        if not corpus_id or not doc_id or isinstance(page_number, bool) or not isinstance(page_number, int):
            raise ValueError("Corpus row is missing corpus_id, doc_id, or page number")
        page_idx = page_number - page_number_base
        location = (doc_id, page_idx)
        if page_idx < 0 or corpus_id in seen_corpus or location in seen_location:
            raise ValueError("Corpus ids and document page locations must be unique")
        seen_corpus.add(corpus_id)
        seen_location.add(location)
        pages.append(CorpusPage(corpus_id, doc_id, page_idx, str(row.get("markdown") or "")))
    return pages


def import_and_index(
    manifest: DatasetManifest,
    mineru_root: str | Path,
    store: EvidenceStore,
    provider: EmbeddingProvider,
    config: BenchmarkConfig,
) -> dict[str, int]:
    mineru_root = Path(mineru_root)
    nested_root = mineru_root / "mineru_output"
    if nested_root.is_dir() and not (mineru_root / manifest.documents[0].doc_id).is_dir():
        mineru_root = nested_root
    if store.get_knowledge_base(config.knowledge_base_id) is None:
        store.create_knowledge_base(KnowledgeBase(config.knowledge_base_id, config.knowledge_base_name))
    document_count = chunk_count = record_count = 0
    chunker = BlockWindowChunker(config.target_tokens, config.max_tokens)
    for item in manifest.documents:
        result_dir = mineru_root / item.doc_id
        parsed = parse_mineru_directory(
            result_dir,
            knowledge_base_id=config.knowledge_base_id,
            document_id=f"{config.knowledge_base_id}:{item.doc_id}",
            original_filename=item.pdf_path.name,
            display_name=item.doc_id,
            source_file_path=item.pdf_path,
            preview_file_path=item.pdf_path,
            source_type="vidore_v3",
        )
        if parsed.document.page_count != item.page_count:
            raise ValueError(f"MinerU page count mismatch for {item.doc_id}")
        chunks = chunker.chunk(parsed.document.id, parsed.blocks)
        if config.table_index_mode == "table_row_groups_v1":
            records = build_table_aware_index_records(
                chunks,
                parsed.blocks,
                provider,
                index_version=f"vidore.{config.fingerprint}",
            )
        else:
            records = build_index_records(
                chunks,
                provider,
                index_version=f"vidore.{config.fingerprint}",
            )
        store.replace_document_graph(
            config.knowledge_base_id,
            replace(parsed.document, processing_status="ready"),
            parsed.blocks,
            parsed.assets,
            chunks,
            records,
        )
        document_count += 1
        chunk_count += len(chunks)
        record_count += len(records)
    return {"documents": document_count, "chunks": chunk_count, "records": record_count}


def project_chunk_scores_to_pages(
    ranked_chunks: Iterable[tuple[float, Chunk]],
    corpus_by_location: dict[tuple[str, int], str],
    doc_id_by_document_id: dict[str, str],
    *,
    top_k_pages: int,
) -> dict[str, float]:
    scores: dict[str, float] = {}
    first_seen: dict[str, int] = {}
    order = 0
    for score, chunk in ranked_chunks:
        doc_id = doc_id_by_document_id.get(chunk.document_id)
        if doc_id is None:
            continue
        for page_idx in chunk.page_range:
            corpus_id = corpus_by_location.get((doc_id, page_idx))
            if corpus_id is None:
                continue
            if corpus_id not in first_seen:
                first_seen[corpus_id] = order
                order += 1
            scores[corpus_id] = max(scores.get(corpus_id, -math.inf), score)
    ranked = sorted(scores.items(), key=lambda item: (-item[1], first_seen[item[0]], item[0]))
    return dict(ranked[:top_k_pages])


class TraceableChunkPipeline:
    def __init__(
        self,
        store: EvidenceStore,
        provider: EmbeddingProvider,
        config: BenchmarkConfig,
        corpus_pages: Iterable[CorpusPage],
        reranker: CachedRetryReranker | None = None,
    ):
        self.store = store
        self.provider = provider
        self.config = config
        self.reranker = reranker
        self.corpus_by_location = {(page.doc_id, page.page_idx): page.corpus_id for page in corpus_pages}
        documents, chunks, records, blocks = store.load_retrieval_scope([config.knowledge_base_id])
        self.chunks = chunks
        self.chunks_by_id = {chunk.id: chunk for chunk in chunks}
        self.records = [record for record in records if record.vector]
        self.record_matrix = _normalized_matrix(
            [record.vector or [] for record in self.records]
        )
        prefix = f"{config.knowledge_base_id}:"
        self.doc_id_by_document_id = {
            document.id: document.id[len(prefix):] if document.id.startswith(prefix) else document.display_name
            for document in documents
        }

    def search(self, query_rows: Iterable[dict[str, Any]]) -> tuple[Run, list[QueryResultDetail]]:
        query_rows = list(query_rows)
        query_vectors = self.provider.embed_texts(
            format_query_texts(query_rows, self.config.query_prompt)
        )
        candidate_count = (
            self.config.reranker_candidate_chunks or self.config.top_k_chunks
        )
        candidates_by_query: list[list[tuple[int, float, Chunk]]] = []
        for vector in query_vectors:
            scores = _dense_scores(self.record_matrix, vector)
            candidates_by_query.append(
                [
                    (
                        index,
                        float(scores[index]),
                        self.chunks_by_id[self.records[index].chunk_id],
                    )
                    for index in _top_unique_chunk_indices(
                        scores,
                        self.records,
                        candidate_count,
                    )
                ]
            )
        reranked = None
        if self.reranker is not None:
            reranked = self.reranker.rerank_many(
                [
                    (
                        str(row.get("query") or ""),
                        [
                            self.records[index].indexing_text
                            for index, _score, _chunk in candidates
                        ],
                    )
                    for row, candidates in zip(query_rows, candidates_by_query)
                ]
            )
        run: Run = {}
        details: list[QueryResultDetail] = []
        for position, (row, candidates) in enumerate(
            zip(query_rows, candidates_by_query)
        ):
            query_id = str(row["query_id"])
            query = str(row.get("query") or "")
            if reranked is None:
                ranked = [
                    (dense_score, dense_score, index, chunk)
                    for index, dense_score, chunk in candidates[
                        : self.config.top_k_chunks
                    ]
                ]
            else:
                ordered = sorted(
                    enumerate(reranked[position]),
                    key=lambda item: (-item[1].relevance_score, item[0]),
                )
                ranked = [
                    (
                        result.relevance_score,
                        candidates[result.index][1],
                        candidates[result.index][0],
                        candidates[result.index][2],
                    )
                    for _order, result in ordered[: self.config.top_k_chunks]
                ]
            ranked_chunks = [(score, chunk) for score, _dense, _index, chunk in ranked]
            page_scores = project_chunk_scores_to_pages(
                ranked_chunks,
                self.corpus_by_location,
                self.doc_id_by_document_id,
                top_k_pages=self.config.top_k_pages,
            )
            run[query_id] = page_scores
            details.append(
                QueryResultDetail(
                    query_id,
                    query,
                    page_scores,
                    tuple(
                        {
                            "chunk_id": chunk.id,
                            "document_id": chunk.document_id,
                            "page_range": chunk.page_range,
                            "token_count": chunk.token_count,
                            "score": score,
                            "dense_score": dense_score,
                            "index_record_id": self.records[index].id,
                        }
                        for score, dense_score, index, chunk in ranked
                    ),
                )
            )
        return run, details


class PageDensePipeline:
    def __init__(
        self,
        pages: Iterable[CorpusPage],
        provider: EmbeddingProvider,
        *,
        query_prompt: str = "",
    ):
        self.pages = list(pages)
        self.provider = provider
        self.query_prompt = query_prompt
        self.vectors = provider.embed_texts([page.markdown for page in self.pages])
        self.matrix = _normalized_matrix(self.vectors)
        self.records = [
            IndexRecord(
                id=page.corpus_id,
                evidence_id=page.corpus_id,
                document_id=page.doc_id,
                chunk_id=page.corpus_id,
                block_ids=[],
                modality="text",
                indexing_text=page.markdown,
                vector=vector,
                embedding_provider=provider.provider_name,
                embedding_model=provider.model_name,
                embedding_dimension=len(vector),
                index_version="vidore-page-dense.v1",
            )
            for page, vector in zip(self.pages, self.vectors)
        ]

    def search(self, query_rows: Iterable[dict[str, Any]], *, top_k: int) -> tuple[Run, list[QueryResultDetail]]:
        query_rows = list(query_rows)
        query_vectors = self.provider.embed_texts(
            format_query_texts(query_rows, self.query_prompt)
        )
        run: Run = {}
        details: list[QueryResultDetail] = []
        for row, query_vector in zip(query_rows, query_vectors):
            query_id = str(row["query_id"])
            query = str(row.get("query") or "")
            scores_array = _dense_scores(self.matrix, query_vector)
            ranked = [
                (self.records[index].id, float(scores_array[index]))
                for index in _top_positive_indices(scores_array, top_k)
            ]
            scores = dict(ranked)
            run[query_id] = scores
            details.append(QueryResultDetail(query_id, query, scores))
        return run, details


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _normalized_matrix(vectors: list[list[float]]):
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "Dense ViDoRe evaluation requires the eval optional dependencies"
        ) from exc
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim != 2 or not matrix.shape[0] or not matrix.shape[1]:
        raise ValueError("Embedding matrix must be non-empty and two-dimensional")
    if not np.isfinite(matrix).all():
        raise ValueError("Embedding matrix contains non-finite values")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise ValueError("Embedding matrix contains zero-length vectors")
    return matrix / norms


def _dense_scores(matrix, query_vector: list[float]):
    import numpy as np

    query = np.asarray(query_vector, dtype=np.float32)
    if query.ndim != 1 or query.shape[0] != matrix.shape[1]:
        raise ValueError("Query embedding dimension does not match the index")
    norm = np.linalg.norm(query)
    if not np.isfinite(norm) or norm == 0:
        raise ValueError("Query embedding is invalid")
    return matrix @ (query / norm)


def _top_positive_indices(scores, top_k: int) -> list[int]:
    import numpy as np

    scores = np.asarray(scores)
    order = np.argsort(-scores, kind="stable")
    return [int(index) for index in order if scores[index] > 0][:top_k]


def _top_unique_chunk_indices(
    scores, records: list[IndexRecord], top_k: int
) -> list[int]:
    import numpy as np

    scores = np.asarray(scores)
    selected: list[int] = []
    seen_chunks: set[str] = set()
    for raw_index in np.argsort(-scores, kind="stable"):
        index = int(raw_index)
        if scores[index] <= 0:
            break
        chunk_id = records[index].chunk_id
        if chunk_id in seen_chunks:
            continue
        seen_chunks.add(chunk_id)
        selected.append(index)
        if len(selected) >= top_k:
            break
    return selected
