from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from dataclasses import replace
from urllib.parse import urlencode

from .models import Block, Document, EvidenceCandidate
from .indexing import complete_embedding_dimension, validate_embedding_vectors
from .public_payload import (
    is_server_local_path,
    public_block_payload,
    public_evidence_payload,
)
from .retrieval import Retriever
from .store import EvidenceStore


_MAX_EVIDENCE_LOCATOR_LENGTH = 4096
_BLOCK_TOKEN_LENGTH = 43
_MAX_INDEX_EVIDENCE_SEGMENT_LENGTH = (
    _MAX_EVIDENCE_LOCATOR_LENGTH - len("ev1..") - _BLOCK_TOKEN_LENGTH
)
MAX_EVIDENCE_WEB_URL_LENGTH = 4096


class NoIndexedDocumentsError(LookupError):
    """Raised when a valid knowledge-base scope has no searchable index rows."""


class EvidenceNotFoundError(LookupError):
    """Raised when an evidence id does not identify a stored index record."""


class EvidenceWebUrlTooLongError(ValueError):
    """Raised when no supported evidence deep link fits the URL contract."""


@dataclass(frozen=True)
class EvidenceSearchResult:
    results: list[dict]
    retrieval_mode_used: str
    pipeline_used: str
    degraded: bool
    warnings: list[str]
    indexing_text_by_evidence_id: dict[str, str]
    record_id_by_evidence_id: dict[str, str]


class EvidenceService:
    """Authoritative builder for search and evidence-detail responses."""

    def __init__(self, store: EvidenceStore, *, reranker: object | None = None):
        self.store = store
        self.reranker = reranker

    def search(
        self,
        query: str,
        knowledge_base_ids: list[str],
        *,
        query_vector: list[float] | None = None,
        embedding_provider: str | None = None,
        embedding_model: str | None = None,
        top_k: int = 5,
    ) -> list[dict]:
        return self.search_with_mode(
            query,
            knowledge_base_ids,
            retrieval_mode_requested=(
                "embedding" if query_vector is not None else "keyword"
            ),
            query_vector=query_vector,
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
            top_k=top_k,
        ).results

    def search_with_mode(
        self,
        query: str,
        knowledge_base_ids: list[str],
        *,
        retrieval_mode_requested: str,
        query_vector: list[float] | None = None,
        embedding_provider: str | None = None,
        embedding_model: str | None = None,
        retrieval_scope: tuple[list, list, list, list] | None = None,
        top_k: int = 5,
    ) -> EvidenceSearchResult:
        documents, chunks, records, blocks = (
            retrieval_scope
            if retrieval_scope is not None
            else self.store.load_retrieval_scope(knowledge_base_ids)
        )
        if not records:
            raise NoIndexedDocumentsError("The selected knowledge bases are not indexed.")
        retrieval_mode_used = "keyword"
        pipeline_used = "keyword"
        degraded = False
        warnings: list[str] = []
        effective_query_vector = None
        if retrieval_mode_requested == "embedding" and query_vector is not None:
            dimension = complete_embedding_dimension(
                records,
                provider_name=embedding_provider,
                model_name=embedding_model,
            )
            try:
                query_dimension = validate_embedding_vectors(
                    [query_vector],
                    expected_count=1,
                )
            except ValueError:
                query_dimension = None
            if dimension is not None and query_dimension == dimension:
                retrieval_mode_used = "embedding"
                pipeline_used = "dense"
                effective_query_vector = query_vector
            else:
                degraded = True
                warnings.append(
                    "所选知识库的 Embedding 索引未完整就绪或与查询模型不兼容，已回退关键词检索。"
                )
        documents_by_id = {document.id: document for document in documents}
        chunks_by_id = {chunk.id: chunk for chunk in chunks}
        records_by_evidence_id = {record.evidence_id: record for record in records}
        blocks_by_id = {block.id: block for block in blocks}
        retriever = Retriever(chunks, records, blocks)
        active_reranker = (
            self.reranker if retrieval_mode_used == "embedding" else None
        )
        if active_reranker is not None:
            pipeline_used = "dense_reranker"
        try:
            candidates = retriever.search(
                query,
                query_vector=effective_query_vector,
                top_k=top_k,
                reranker=active_reranker,
            )
        except Exception:
            if active_reranker is None:
                raise
            pipeline_used = "dense"
            degraded = True
            warnings.append("Reranker 当前不可用，已回退 Dense 检索。")
            candidates = retriever.search(
                query,
                query_vector=effective_query_vector,
                top_k=top_k,
            )
        result_entries = [
            (
                self._payload(
                    candidate,
                    documents_by_id[candidate.document_id],
                    blocks_by_id,
                    locator_id=_make_evidence_locator(
                        records_by_evidence_id[candidate.id],
                        chunks_by_id[candidate.chunk_id],
                        blocks_by_id[candidate.block_ids[0]],
                    ),
                    retrieval={
                        "score": candidate.score,
                        "support_level": candidate.support_level,
                        "mode": candidate.metadata["retrieval_mode"],
                    },
                ),
                records_by_evidence_id[candidate.id].indexing_text,
            )
            for candidate in candidates
            if candidate.block_ids
        ]
        return EvidenceSearchResult(
            results=[payload for payload, _indexing_text in result_entries],
            retrieval_mode_used=retrieval_mode_used,
            pipeline_used=pipeline_used,
            degraded=degraded,
            warnings=warnings,
            indexing_text_by_evidence_id={
                payload["index_evidence_id"]: indexing_text
                for payload, indexing_text in result_entries
            },
            record_id_by_evidence_id={
                evidence_id: record.id
                for evidence_id, record in records_by_evidence_id.items()
            },
        )

    def get(self, evidence_id: str) -> dict:
        index_evidence_id, supporting_block_token = _parse_evidence_locator(evidence_id)
        loaded = self.store.load_evidence_scope(index_evidence_id)
        if loaded is None:
            raise EvidenceNotFoundError(evidence_id)
        document, chunk, record, blocks = loaded
        blocks_by_id = {block.id: block for block in blocks}
        matching_block_ids = [
            block.id
            for block_id in record.block_ids
            if (block := blocks_by_id.get(block_id)) is not None
            and _evidence_version_token(record, chunk, block)
            == supporting_block_token
        ]
        if len(matching_block_ids) != 1:
            raise EvidenceNotFoundError(evidence_id)
        supporting_block = blocks_by_id.get(matching_block_ids[0])
        if (
            supporting_block is None
            or supporting_block.id not in record.block_ids
            or supporting_block.document_id != document.id
            or supporting_block.is_discarded
        ):
            raise EvidenceNotFoundError(evidence_id)
        candidate = Retriever([chunk], [record]).get_evidence(index_evidence_id)
        if candidate is None:
            raise EvidenceNotFoundError(evidence_id)
        candidate = replace(
            candidate,
            block_ids=[supporting_block.id],
            page_range=[supporting_block.page_idx],
            bbox_by_page=(
                {str(supporting_block.page_idx): [supporting_block.bbox]}
                if supporting_block.bbox
                else {}
            ),
        )
        return self._payload(
            candidate,
            document,
            blocks_by_id,
            locator_id=evidence_id,
            retrieval=None,
        )

    def _payload(
        self,
        evidence: EvidenceCandidate,
        document: Document,
        blocks_by_id: dict[str, Block],
        *,
        locator_id: str,
        retrieval: dict | None,
    ) -> dict:
        supporting_blocks = [
            blocks_by_id[block_id]
            for block_id in evidence.block_ids
            if block_id in blocks_by_id
        ]
        page_indices = sorted({block.page_idx for block in supporting_blocks})
        first_page_idx = page_indices[0] if page_indices else None
        page_sizes = _bbox_page_size_by_page(supporting_blocks, evidence.bbox_by_page)
        web_url = _evidence_web_url(
            document,
            evidence,
            locator_id=locator_id,
            human_page_number=(first_page_idx + 1 if first_page_idx is not None else 1),
        )
        return public_evidence_payload({
            "id": locator_id,
            "evidence_id": locator_id,
            "index_evidence_id": evidence.id,
            "knowledge_base_id": document.knowledge_base_id,
            "document_id": document.id,
            "display_name": document.display_name,
            "original_filename": document.original_filename,
            "chunk_id": evidence.chunk_id,
            "chunk_text": evidence.display_text,
            "supporting_block_ids": evidence.block_ids,
            "supporting_blocks": [
                public_block_payload(block) for block in supporting_blocks
            ],
            "block_token": (
                _block_token(evidence.block_ids[0])
                if evidence.block_ids
                else None
            ),
            "page_idx": first_page_idx,
            "human_page_number": (
                first_page_idx + 1 if first_page_idx is not None else None
            ),
            "page_range": evidence.page_range,
            "bbox_by_page": evidence.bbox_by_page,
            "bbox_page_size_by_page": page_sizes,
            "modality": evidence.modality,
            "retrieval": retrieval,
            "title_context": evidence.metadata.get("title_context"),
            "web_url": web_url,
        })


def _bbox_page_size_by_page(
    blocks: list[Block], bbox_by_page: dict[str, list[list[float]]]
) -> dict[str, list[float]]:
    page_sizes: dict[str, list[float]] = {}
    for page_key in bbox_by_page:
        try:
            page_idx = int(page_key)
        except ValueError:
            continue
        page_size = next(
            (
                block.page_size
                for block in blocks
                if block.page_idx == page_idx and block.page_size
            ),
            None,
        )
        if page_size:
            page_sizes[page_key] = page_size
    return page_sizes


def _evidence_web_url(
    document: Document,
    evidence: EvidenceCandidate,
    *,
    locator_id: str,
    human_page_number: int,
) -> str:
    block_id = evidence.block_ids[0] if evidence.block_ids else ""
    base_url = (
        f"/knowledge-bases/{document.knowledge_base_id}/documents/{document.id}"
    )
    raw_query = urlencode(
        {
            "page": human_page_number,
            "block": block_id,
            "evidence": locator_id,
        }
    )
    raw_url = f"{base_url}?{raw_query}"
    if len(raw_url) <= MAX_EVIDENCE_WEB_URL_LENGTH:
        return raw_url
    bounded_query = urlencode(
        {
            "page": human_page_number,
            "block_token": _block_token(block_id),
            "evidence": locator_id,
        }
    )
    bounded_url = f"{base_url}?{bounded_query}"
    if len(bounded_url) > MAX_EVIDENCE_WEB_URL_LENGTH:
        raise EvidenceWebUrlTooLongError(
            "Evidence web URL exceeds the supported length"
        )
    return bounded_url


def _make_evidence_locator(record, chunk, block: Block) -> str:
    index_segment = _b64url_encode(record.evidence_id)
    if not record.evidence_id:
        raise ValueError("Index evidence id is required for a stable locator")
    if len(index_segment) > _MAX_INDEX_EVIDENCE_SEGMENT_LENGTH:
        raise EvidenceWebUrlTooLongError(
            "Evidence locator exceeds the supported length"
        )
    locator = f"ev1.{index_segment}.{_evidence_version_token(record, chunk, block)}"
    if len(locator) > _MAX_EVIDENCE_LOCATOR_LENGTH:
        raise EvidenceWebUrlTooLongError(
            "Evidence locator exceeds the supported length"
        )
    return locator


def _parse_evidence_locator(value: str) -> tuple[str, str]:
    if len(value) > _MAX_EVIDENCE_LOCATOR_LENGTH:
        raise EvidenceNotFoundError(value)
    parts = value.split(".")
    if len(parts) != 3 or parts[0] != "ev1":
        raise EvidenceNotFoundError(value)
    index_segment, block_token = parts[1], parts[2]
    if (
        not index_segment
        or len(index_segment) > _MAX_INDEX_EVIDENCE_SEGMENT_LENGTH
        or len(block_token) != _BLOCK_TOKEN_LENGTH
        or re.fullmatch(r"[A-Za-z0-9_-]+", block_token) is None
    ):
        raise EvidenceNotFoundError(value)
    try:
        index_evidence_id = _b64url_decode(index_segment)
    except (ValueError, UnicodeDecodeError):
        raise EvidenceNotFoundError(value) from None
    if not index_evidence_id:
        raise EvidenceNotFoundError(value)
    if _b64url_encode(index_evidence_id) != index_segment:
        raise EvidenceNotFoundError(value)
    return index_evidence_id, block_token


def _block_token(block_id: str) -> str:
    digest = hashlib.sha256(block_id.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _evidence_version_token(record, chunk, block: Block) -> str:
    canonical = {
        "record": {
            "id": record.id,
            "evidence_id": record.evidence_id,
            "document_id": record.document_id,
            "chunk_id": record.chunk_id,
            "block_ids": record.block_ids,
            "modality": record.modality,
            "indexing_text": record.indexing_text,
            "embedding_provider": record.embedding_provider,
            "embedding_model": record.embedding_model,
            "embedding_dimension": record.embedding_dimension,
            "index_version": record.index_version,
        },
        "chunk": {
            "id": chunk.id,
            "document_id": chunk.document_id,
            "chunk_idx": chunk.chunk_idx,
            "strategy": chunk.strategy,
            "block_ids": chunk.block_ids,
            "title_context": chunk.title_context,
            "inferred_heading_path": chunk.inferred_heading_path,
            "chunk_text": chunk.chunk_text,
            "indexing_text": chunk.indexing_text,
            "page_range": chunk.page_range,
            "bbox_by_page": chunk.bbox_by_page,
            "modality_mix": chunk.modality_mix,
            "token_count": chunk.token_count,
            "strategy_version": chunk.strategy_version,
        },
        "selected_block": {
            "id": block.id,
            "document_id": block.document_id,
            "page_idx": block.page_idx,
            "block_idx": block.block_idx,
            "block_type": block.block_type,
            "text": block.text,
            "html": block.html,
            "asset_id": block.asset_id,
            "bbox": block.bbox,
            "page_size": block.page_size,
            "is_discarded": block.is_discarded,
            "metadata": _locator_metadata(block.metadata),
        },
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(hashlib.sha256(encoded).digest()).decode(
        "ascii"
    ).rstrip("=")


def _locator_metadata(value):
    if isinstance(value, dict):
        return {key: _locator_metadata(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_locator_metadata(item) for item in value]
    if is_server_local_path(value):
        return None
    return value


def _b64url_encode(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        ).decode("utf-8")
    except Exception as exc:
        raise ValueError("Invalid evidence locator") from exc
