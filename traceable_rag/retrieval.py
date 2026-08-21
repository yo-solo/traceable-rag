from __future__ import annotations

import math
import re
from typing import Any

from .models import Block, Chunk, EvidenceCandidate, IndexRecord


class Retriever:
    def __init__(
        self,
        chunks: list[Chunk],
        index_records: list[IndexRecord],
        blocks: list[Block] | None = None,
    ):
        self.chunks_by_id = {chunk.id: chunk for chunk in chunks}
        self.records = index_records
        self.blocks_by_id = {block.id: block for block in blocks or []}

    def search(
        self,
        query: str,
        *,
        query_vector: list[float] | None = None,
        top_k: int = 5,
        reranker: Any | None = None,
        candidate_k: int = 200,
    ) -> list[EvidenceCandidate]:
        scored: list[tuple[float, IndexRecord, bool]] = []
        for record in self.records:
            embedding_hit = query_vector is not None and record.vector is not None
            if embedding_hit:
                score = _cosine(query_vector, record.vector)
            else:
                score = _keyword_score(query, record.indexing_text)
            if score > 0:
                scored.append((score, record, embedding_hit))

        scored.sort(key=lambda item: item[0], reverse=True)
        unique_chunks: list[tuple[float, IndexRecord, bool]] = []
        seen_chunk_ids: set[str] = set()
        for item in scored:
            chunk_id = item[1].chunk_id
            if chunk_id in seen_chunk_ids:
                continue
            seen_chunk_ids.add(chunk_id)
            unique_chunks.append(item)

        ranked = unique_chunks
        if reranker is not None and unique_chunks:
            pool = unique_chunks[: max(top_k, candidate_k)]
            reranked = reranker.rerank(
                query,
                [record.indexing_text for _score, record, _hit in pool],
                top_n=len(pool),
            )
            ranked = [
                (
                    result.relevance_score,
                    pool[result.index][1],
                    pool[result.index][2],
                )
                for result in reranked
            ]
        candidates: list[EvidenceCandidate] = []
        for score, record, embedding_hit in ranked:
            candidate = self._to_evidence(
                record,
                score,
                query=query,
                embedding_hit=embedding_hit,
            )
            if candidate is not None:
                candidates.append(candidate)
            if len(candidates) >= top_k:
                break
        return candidates

    def get_evidence(self, evidence_id: str) -> EvidenceCandidate | None:
        for record in self.records:
            if record.evidence_id != evidence_id:
                continue
            candidate = self._to_evidence(
                record,
                0.0,
            )
            if candidate is not None:
                return candidate
        return None

    def _to_evidence(
        self,
        record: IndexRecord,
        score: float,
        *,
        query: str | None = None,
        embedding_hit: bool = False,
    ) -> EvidenceCandidate | None:
        chunk = self.chunks_by_id[record.chunk_id]
        scoped_blocks = [
            self.blocks_by_id[block_id]
            for block_id in record.block_ids
            if block_id in self.blocks_by_id
        ]
        if scoped_blocks:
            supporting_blocks = _literal_supporting_blocks(query or "", scoped_blocks)
            if not supporting_blocks and embedding_hit:
                fallback = _first_valid_body_block(scoped_blocks)
                supporting_blocks = [fallback] if fallback is not None else []
            if not supporting_blocks:
                return None
            supporting_block_ids = [block.id for block in supporting_blocks]
            page_range = sorted({block.page_idx for block in supporting_blocks})
            bbox_by_page = _bbox_by_page(supporting_blocks)
        else:
            # Older in-memory callers may not supply blocks. Store-backed evidence
            # always does, but preserving record locality keeps the Retriever useful.
            supporting_block_ids = record.block_ids
            page_range = chunk.page_range
            bbox_by_page = chunk.bbox_by_page
        support_level = "strong" if score >= 0.35 else "weak"
        return EvidenceCandidate(
            id=record.evidence_id,
            document_id=record.document_id,
            chunk_id=record.chunk_id,
            block_ids=supporting_block_ids,
            display_text=chunk.chunk_text,
            modality=record.modality,
            page_range=page_range,
            bbox_by_page=bbox_by_page,
            score=score,
            support_level=support_level,
            metadata={
                "title_context": chunk.title_context,
                "index_version": record.index_version,
                "embedding_provider": record.embedding_provider,
                "embedding_model": record.embedding_model,
                "retrieval_mode": "embedding" if embedding_hit else "keyword",
            },
        )


def _literal_supporting_blocks(query: str, blocks: list[Block]) -> list[Block]:
    terms = [term.casefold() for term in _terms(query)]
    if not terms:
        return []
    matches: list[tuple[int, Block]] = []
    for block in blocks:
        content = _block_content(block).casefold()
        hit_count = sum(1 for term in terms if term in content) if content else 0
        if hit_count:
            matches.append((hit_count, block))
    if not matches:
        return []
    best_score = max(score for score, _block in matches)
    return [next(block for score, block in matches if score == best_score)]


def _first_valid_body_block(blocks: list[Block]) -> Block | None:
    non_body_types = {
        "title",
        "heading",
        "section_header",
        "page_header",
        "page_footer",
        "footnote",
    }
    for block in blocks:
        if (
            not block.is_discarded
            and block.block_type.casefold() not in non_body_types
            and bool(_block_content(block).strip())
        ):
            return block
    return next(
        (
            block
            for block in blocks
            if not block.is_discarded and bool(_block_content(block).strip())
        ),
        None,
    )


def _block_content(block: Block) -> str:
    return "\n".join(part for part in (block.text, block.html) if part)


def _bbox_by_page(blocks: list[Block]) -> dict[str, list[list[float]]]:
    result: dict[str, list[list[float]]] = {}
    for block in blocks:
        if block.bbox:
            result.setdefault(str(block.page_idx), []).append(block.bbox)
    return result


def _keyword_score(query: str, text: str) -> float:
    query_terms = _terms(query)
    if not query_terms:
        return 0.0
    haystack = text.lower()
    hits = 0
    for term in query_terms:
        if term.lower() in haystack:
            hits += 1
    return hits / max(len(query_terms), 1)


def _terms(text: str) -> list[str]:
    ascii_terms = re.findall(r"[A-Za-z0-9_]{2,}", text)
    cjk_runs = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    cjk_terms: list[str] = []
    for run in cjk_runs:
        if len(run) <= 4:
            cjk_terms.append(run)
        else:
            cjk_terms.extend(run[i : i + 4] for i in range(0, len(run) - 3, 2))
    return ascii_terms + cjk_terms


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    if any(not math.isfinite(value) for value in (*a, *b)):
        return 0.0
    scale_a = max(abs(value) for value in a)
    scale_b = max(abs(value) for value in b)
    if scale_a == 0 or scale_b == 0:
        return 0.0
    scaled_a = [value / scale_a for value in a]
    scaled_b = [value / scale_b for value in b]
    norm_a = math.hypot(*scaled_a)
    norm_b = math.hypot(*scaled_b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    score = math.fsum(
        (x / norm_a) * (y / norm_b)
        for x, y in zip(scaled_a, scaled_b)
    )
    return max(-1.0, min(1.0, score))
