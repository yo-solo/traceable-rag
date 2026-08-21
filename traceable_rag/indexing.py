from __future__ import annotations

import math
import uuid
from dataclasses import replace
from numbers import Real

from .embedding import EmbeddingProvider
from .models import Block, Chunk, IndexRecord
from .table_indexing import table_contexts, table_index_variants


def build_index_records(
    chunks: list[Chunk],
    embedding_provider: EmbeddingProvider | None = None,
    *,
    blocks: list[Block] | None = None,
    index_version: str = "m2.v2",
) -> list[IndexRecord]:
    blocks_by_id = {block.id: block for block in blocks or []}
    contexts_by_table_id = table_contexts(blocks or [])
    records: list[IndexRecord] = []
    for chunk in chunks:
        chunk_blocks = [
            blocks_by_id[block_id]
            for block_id in chunk.block_ids
            if block_id in blocks_by_id
        ]
        variants = table_index_variants(
            chunk,
            chunk_blocks,
            table_context_by_id=contexts_by_table_id,
        )
        if variants:
            records.extend(
                _build_record(chunk, indexing_text, variant, index_version)
                for variant, indexing_text in variants
            )
        else:
            records.append(_build_record(chunk, chunk.indexing_text, "base", index_version))
    if embedding_provider is not None:
        return embed_index_records(records, embedding_provider)
    return records


def _build_record(
    chunk: Chunk,
    indexing_text: str,
    variant: str,
    index_version: str,
) -> IndexRecord:
    modality = (
        "mixed"
        if len(chunk.modality_mix) > 1
        else (chunk.modality_mix[0] if chunk.modality_mix else "text")
    )
    suffix = "" if variant == "base" else f":{variant}"
    return IndexRecord(
        id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{chunk.id}:index:{index_version}{suffix}")),
        evidence_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{chunk.id}:evidence{suffix}")),
        document_id=chunk.document_id,
        chunk_id=chunk.id,
        block_ids=chunk.block_ids,
        modality=modality,
        indexing_text=indexing_text,
        vector=None,
        embedding_provider=None,
        embedding_model=None,
        embedding_dimension=None,
        index_version=index_version,
    )


def embed_index_records(
    records: list[IndexRecord], embedding_provider: EmbeddingProvider
) -> list[IndexRecord]:
    vectors = embedding_provider.embed_texts(
        [record.indexing_text for record in records]
    )
    dimension = validate_embedding_vectors(vectors, expected_count=len(records))
    return [
        replace(
            record,
            vector=vector,
            embedding_provider=embedding_provider.provider_name,
            embedding_model=embedding_provider.model_name,
            embedding_dimension=dimension,
        )
        for record, vector in zip(records, vectors)
    ]


def validate_embedding_vectors(
    vectors: object,
    *,
    expected_count: int,
    expected_dimension: int | None = None,
) -> int:
    if not isinstance(vectors, list) or len(vectors) != expected_count:
        raise ValueError("Embedding batch count is invalid.")
    dimension = expected_dimension
    for vector in vectors:
        if not isinstance(vector, list) or not vector:
            raise ValueError("Embedding vector is invalid.")
        if dimension is None:
            dimension = len(vector)
        if len(vector) != dimension:
            raise ValueError("Embedding vector dimensions are inconsistent.")
        if any(
            isinstance(value, bool)
            or not isinstance(value, Real)
            or not math.isfinite(float(value))
            for value in vector
        ):
            raise ValueError("Embedding vector contains invalid values.")
        if not any(float(value) != 0.0 for value in vector):
            raise ValueError("Embedding vector has zero norm.")
    if expected_count > 0 and (dimension is None or dimension <= 0):
        raise ValueError("Embedding vector dimension is invalid.")
    return dimension or 0


def complete_embedding_dimension(
    records: list[IndexRecord],
    *,
    provider_name: str | None = None,
    model_name: str | None = None,
) -> int | None:
    if not records or not provider_name or not model_name:
        return None
    if any(
        not record.embedding_provider
        or not record.embedding_model
        or record.embedding_dimension is None
        or record.embedding_dimension <= 0
        or record.vector is None
        or record.embedding_provider != provider_name
        or record.embedding_model != model_name
        for record in records
    ):
        return None
    dimensions = {record.embedding_dimension for record in records}
    providers = {record.embedding_provider for record in records}
    models = {record.embedding_model for record in records}
    if len(dimensions) != 1 or len(providers) != 1 or len(models) != 1:
        return None
    dimension = next(iter(dimensions))
    try:
        validate_embedding_vectors(
            [record.vector for record in records],
            expected_count=len(records),
            expected_dimension=dimension,
        )
    except ValueError:
        return None
    return dimension
