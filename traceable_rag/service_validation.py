from __future__ import annotations


MAX_QUERY_LENGTH = 4000
MAX_SCOPE_ITEMS = 50
MAX_KNOWLEDGE_BASE_ID_LENGTH = 255


class KnowledgeBaseScopeError(LookupError):
    def __init__(self, knowledge_base_id: str):
        self.knowledge_base_id = knowledge_base_id
        super().__init__(knowledge_base_id)


class ServiceValidationError(ValueError):
    """Raised when a direct service call violates the public input contract."""


def normalize_query_inputs(
    query: str,
    knowledge_base_ids: list[str],
    top_k: int,
) -> tuple[str, list[str], int]:
    if not isinstance(query, str):
        raise ServiceValidationError("query must be a string.")
    normalized_query = query.strip()
    if not normalized_query or len(normalized_query) > MAX_QUERY_LENGTH:
        raise ServiceValidationError("query is empty or too long.")
    if not isinstance(knowledge_base_ids, list) or not 1 <= len(
        knowledge_base_ids
    ) <= MAX_SCOPE_ITEMS:
        raise ServiceValidationError(
            "knowledge_base_ids must contain from 1 to 50 ids."
        )
    normalized_scope: list[str] = []
    for knowledge_base_id in knowledge_base_ids:
        if not isinstance(knowledge_base_id, str):
            raise ServiceValidationError("knowledge_base_ids must contain strings.")
        value = knowledge_base_id.strip()
        if not value or len(value) > MAX_KNOWLEDGE_BASE_ID_LENGTH:
            raise ServiceValidationError(
                "knowledge_base_ids contains an invalid id."
            )
        normalized_scope.append(value)
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 50:
        raise ServiceValidationError("top_k must be an integer from 1 to 50.")
    return normalized_query, list(dict.fromkeys(normalized_scope)), top_k
