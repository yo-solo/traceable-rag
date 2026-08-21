from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


JsonDict = dict[str, Any]


@dataclass(frozen=True)
class KnowledgeBase:
    id: str
    name: str
    description: str = ""
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class Document:
    id: str
    knowledge_base_id: str
    original_filename: str
    display_name: str
    file_hash: str
    source_type: str
    source_file_path: str | None
    preview_file_path: str | None
    mineru_result_dir: str | None
    page_count: int
    processing_status: str
    processing_error: str | None
    parser_provider: str
    parser_version: str | None = None
    normalization_version: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class ProcessingJob:
    id: str
    knowledge_base_id: str
    document_id: str
    operation: str
    status: str
    stage: str
    attempt: int
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool | None = None
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class Block:
    id: str
    document_id: str
    page_idx: int
    block_idx: int
    block_type: str
    text: str | None = None
    html: str | None = None
    asset_id: str | None = None
    bbox: list[float] | None = None
    page_size: list[float] | None = None
    source_ref: str | None = None
    is_discarded: bool = False
    metadata: JsonDict = field(default_factory=dict)


@dataclass(frozen=True)
class Asset:
    id: str
    document_id: str
    block_id: str
    asset_type: str
    file_path: str | None
    content_type: str | None
    file_hash: str | None
    page_idx: int
    bbox: list[float] | None
    related_text: str | None = None
    metadata: JsonDict = field(default_factory=dict)


@dataclass(frozen=True)
class Chunk:
    id: str
    document_id: str
    chunk_idx: int
    strategy: str
    block_ids: list[str]
    title_context: str | None
    inferred_heading_path: list[str]
    chunk_text: str
    indexing_text: str
    page_range: list[int]
    bbox_by_page: dict[str, list[list[float]]]
    modality_mix: list[str]
    token_count: int
    strategy_version: str


@dataclass(frozen=True)
class IndexRecord:
    id: str
    evidence_id: str
    document_id: str
    chunk_id: str
    block_ids: list[str]
    modality: str
    indexing_text: str
    vector: list[float] | None
    embedding_provider: str | None
    embedding_model: str | None
    embedding_dimension: int | None
    index_version: str


@dataclass(frozen=True)
class SourceAnchor:
    """Opaque, current-version locator for one retrievable source block."""

    id: str
    document_id: str
    record_id: str
    anchor_block_id: str
    active: bool
    invalidated_at: str | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class EvidenceCandidate:
    id: str
    document_id: str
    chunk_id: str
    block_ids: list[str]
    display_text: str
    modality: str
    page_range: list[int]
    bbox_by_page: dict[str, list[list[float]]]
    score: float
    support_level: str
    metadata: JsonDict = field(default_factory=dict)
