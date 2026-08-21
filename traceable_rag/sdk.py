from __future__ import annotations

from typing import Any, Literal

from .evidence_service import EvidenceService
from .service_validation import (
    KnowledgeBaseScopeError,
    ServiceValidationError,
    normalize_query_inputs,
)
from .source_service import SourceService


class DocumentScopeError(LookupError):
    """Raised when a document is absent from an existing knowledge-base scope."""

    def __init__(self, document_id: str):
        self.document_id = document_id
        super().__init__(document_id)


class TraceableRagClient:
    """In-process client for the retrieval and Source-context public contract."""

    def __init__(self, store, *, reranker: object | None = None):
        self.store = store
        self.evidence_service = EvidenceService(store, reranker=reranker)
        self.source_service = SourceService(store)

    def list_knowledge_bases(self) -> list[dict[str, object]]:
        stats = self.store.list_knowledge_base_document_stats()
        return [
            {
                "id": knowledge_base.id,
                "name": knowledge_base.name,
                "description": knowledge_base.description,
                "created_at": knowledge_base.created_at,
                "updated_at": knowledge_base.updated_at,
                **stats.get(
                    knowledge_base.id,
                    {
                        "document_count": 0,
                        "ready_document_count": 0,
                        "processing_document_count": 0,
                        "failed_document_count": 0,
                    },
                ),
            }
            for knowledge_base in self.store.list_knowledge_bases()
        ]

    def list_documents(self, knowledge_base_id: str) -> list[dict[str, object]]:
        if self.store.get_knowledge_base(knowledge_base_id) is None:
            raise KnowledgeBaseScopeError(knowledge_base_id)
        stats = self.store.list_document_graph_stats(knowledge_base_id)
        return [
            {
                "id": document.id,
                "knowledge_base_id": document.knowledge_base_id,
                "original_filename": document.original_filename,
                "display_name": document.display_name,
                "source_type": document.source_type,
                "page_count": document.page_count,
                "processing_status": document.processing_status,
                "parser_provider": document.parser_provider,
                "parser_version": document.parser_version,
                "created_at": document.created_at,
                "updated_at": document.updated_at,
                "stats": stats.get(document.id, {}),
            }
            for document in self.store.list_documents(knowledge_base_id)
        ]

    def retrieve(
        self,
        query: str,
        *,
        knowledge_base_ids: list[str],
        mode: Literal["auto", "keyword"] = "auto",
        document_ids: list[str] | None = None,
        top_k: int = 8,
        query_vector: list[float] | None = None,
        embedding_provider: str | None = None,
        embedding_model: str | None = None,
    ) -> dict[str, Any]:
        query, scope, top_k = normalize_query_inputs(query, knowledge_base_ids, top_k)
        if mode not in {"auto", "keyword"}:
            raise ServiceValidationError("mode must be 'auto' or 'keyword'.")
        for knowledge_base_id in scope:
            if self.store.get_knowledge_base(knowledge_base_id) is None:
                raise KnowledgeBaseScopeError(knowledge_base_id)
        document_scope = _normalize_document_ids(document_ids)
        if document_scope is not None:
            existing_pairs = self.store.existing_document_pairs(
                (knowledge_base_id, document_id)
                for knowledge_base_id in scope
                for document_id in document_scope
            )
            existing_document_ids = {
                document_id for _knowledge_base_id, document_id in existing_pairs
            }
            if existing_document_ids != set(document_scope):
                missing = next(
                    document_id
                    for document_id in document_scope
                    if document_id not in existing_document_ids
                )
                raise DocumentScopeError(missing)

        retrieval_scope = self.store.load_retrieval_scope(
            scope, document_ids=document_scope
        )
        embedding_warning: str | None = None
        retrieval_mode_requested = "keyword"
        if mode == "auto":
            if (
                query_vector is not None
                and isinstance(embedding_provider, str)
                and embedding_provider
                and isinstance(embedding_model, str)
                and embedding_model
            ):
                retrieval_mode_requested = "embedding"
            else:
                embedding_warning = (
                    "Embedding Provider 未配置或当前不可用，已回退关键词检索。"
                )

        search_result = self.evidence_service.search_with_mode(
            query,
            scope,
            retrieval_mode_requested=retrieval_mode_requested,
            query_vector=query_vector if retrieval_mode_requested == "embedding" else None,
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
            retrieval_scope=retrieval_scope,
            top_k=top_k,
        )
        warnings = list(
            dict.fromkeys(
                ([embedding_warning] if embedding_warning else [])
                + search_result.warnings
            )
        )
        return {
            "query": query,
            "knowledge_base_ids": scope,
            "document_ids": document_scope,
            "mode_requested": mode,
            "pipeline_used": search_result.pipeline_used,
            "degraded": search_result.degraded or bool(embedding_warning),
            "warnings": [_retrieval_warning(message) for message in warnings],
            "results": self.source_service.retrieval_sources(search_result),
        }

    def get_source_context(
        self, source_id: str, *, before: int = 2, after: int = 2
    ) -> dict[str, Any]:
        _validate_context_bound(before, name="before")
        _validate_context_bound(after, name="after")
        return self.source_service.context(source_id, before=before, after=after)

def _normalize_document_ids(document_ids: list[str] | None) -> list[str] | None:
    if document_ids is None:
        return None
    if not isinstance(document_ids, list) or not 1 <= len(document_ids) <= 50:
        raise ServiceValidationError("document_ids must contain from 1 to 50 ids.")
    normalized = []
    for document_id in document_ids:
        if not isinstance(document_id, str):
            raise ServiceValidationError("document_ids must contain strings.")
        value = document_id.strip()
        if not value or len(value) > 255:
            raise ServiceValidationError("document_ids contains an invalid id.")
        normalized.append(value)
    return list(dict.fromkeys(normalized))


def _validate_context_bound(value: int, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 5:
        raise ServiceValidationError(f"{name} must be an integer from 0 to 5.")


def _retrieval_warning(message: str) -> dict[str, str]:
    if "Reranker" in message:
        code = "reranker_unavailable"
    elif "索引未完整" in message or "查询模型不兼容" in message:
        code = "embedding_index_incompatible"
    elif "查询向量生成失败" in message:
        code = "embedding_request_failed"
    elif "Embedding Provider" in message:
        code = "embedding_unavailable"
    else:
        code = "retrieval_degraded"
    return {"code": code, "message": message}
