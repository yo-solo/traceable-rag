from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BenchmarkConfig:
    dataset_id: str
    dataset_revision: str
    knowledge_base_id: str
    knowledge_base_name: str
    query_language: str
    expected_documents: int
    expected_pages: int
    expected_queries: int
    corpus_page_number_base: int
    target_tokens: int
    max_tokens: int
    top_k_chunks: int
    top_k_pages: int
    metrics: tuple[str, ...]
    artifact_root: str
    table_index_mode: str = "baseline"
    query_prompt: str = ""
    reranker_candidate_chunks: int = 0
    evaluator: str = "internal"

    @property
    def fingerprint(self) -> str:
        values = asdict(self)
        # Keep historical run fingerprints stable for configurations created
        # before optional treatment fields were supported.
        if values["table_index_mode"] == "baseline":
            values.pop("table_index_mode")
        if not values["query_prompt"]:
            values.pop("query_prompt")
        if not values["reranker_candidate_chunks"]:
            values.pop("reranker_candidate_chunks")
        if values["evaluator"] == "internal":
            values.pop("evaluator")
        payload = json.dumps(values, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def load_config(path: str | Path | None = None) -> BenchmarkConfig:
    config_path = Path(path) if path else Path(__file__).parent / "configs" / "finance_en.json"
    payload: dict[str, Any] = json.loads(config_path.read_text(encoding="utf-8"))
    payload["metrics"] = tuple(payload["metrics"])
    config = BenchmarkConfig(**payload)
    if config.target_tokens <= 0 or config.max_tokens < config.target_tokens:
        raise ValueError("Chunk token limits are invalid")
    if config.top_k_chunks <= 0 or config.top_k_pages <= 0:
        raise ValueError("Retrieval top-k values must be positive")
    if config.corpus_page_number_base not in {0, 1}:
        raise ValueError("corpus_page_number_base must be 0 or 1")
    if config.table_index_mode not in {"baseline", "table_row_groups_v1"}:
        raise ValueError("Unsupported table_index_mode")
    if config.reranker_candidate_chunks < 0:
        raise ValueError("reranker_candidate_chunks cannot be negative")
    if (
        config.reranker_candidate_chunks
        and config.reranker_candidate_chunks < config.top_k_chunks
    ):
        raise ValueError("Reranker candidates must cover final Chunk top-k")
    if config.evaluator not in {"internal", "official_pytrec_eval"}:
        raise ValueError("Unsupported evaluator")
    return config
