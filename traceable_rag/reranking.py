from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .embedding import UrllibHTTPClient
from .runtime_config import RuntimeConfig, RuntimeConfigError


@dataclass(frozen=True)
class RerankResult:
    index: int
    relevance_score: float


class OpenAICompatibleReranker:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model_name: str,
        *,
        http_client: UrllibHTTPClient | Any | None = None,
        timeout: int = 120,
        endpoint_path: str = "/v1/rerank",
    ):
        self.api_key = api_key
        if not endpoint_path.startswith("/") or "?" in endpoint_path or "#" in endpoint_path:
            raise ValueError("Reranker endpoint path is invalid")
        self.endpoint_path = endpoint_path
        self.base_url = _normalize_base_url(base_url, endpoint_path=endpoint_path)
        self.model_name = model_name
        self.http_client = http_client or UrllibHTTPClient()
        self.timeout = timeout

    def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int | None = None,
    ) -> list[RerankResult]:
        if not documents:
            return []
        requested = len(documents) if top_n is None else top_n
        if requested <= 0:
            raise ValueError("Reranker top_n must be positive")
        data = self.http_client.post_json(
            f"{self.base_url}{self.endpoint_path}",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            payload={
                "model": self.model_name,
                "query": query,
                "documents": documents,
                "top_n": min(requested, len(documents)),
                "return_documents": False,
            },
            timeout=self.timeout,
        )
        raw_results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(raw_results, list):
            raise ValueError("Reranker API returned invalid results")
        results: list[RerankResult] = []
        seen: set[int] = set()
        for item in raw_results:
            if not isinstance(item, dict):
                raise ValueError("Reranker API returned an invalid result")
            index = item.get("index")
            score = item.get("relevance_score")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < len(documents)
                or index in seen
                or isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(float(score))
            ):
                raise ValueError("Reranker API returned an invalid result")
            seen.add(index)
            results.append(RerankResult(index, float(score)))
        expected = min(requested, len(documents))
        if len(results) != expected:
            raise ValueError(
                f"Reranker API returned {len(results)} results, expected {expected}"
            )
        return results


def reranker_from_runtime_config(runtime: RuntimeConfig) -> OpenAICompatibleReranker:
    if not runtime.reranker_model:
        raise RuntimeConfigError("Reranker runtime configuration requires a model.")
    base_url = runtime.reranker_base_url
    api_key = runtime.reranker_api_key
    if base_url is None and api_key is None:
        base_url = runtime.embedding_base_url
        api_key = runtime.embedding_api_key
    if not base_url or not api_key:
        raise RuntimeConfigError(
            "Reranker runtime configuration requires the provider base URL and API key."
        )
    return OpenAICompatibleReranker(
        api_key=api_key,
        base_url=base_url,
        model_name=runtime.reranker_model,
        endpoint_path=(
            "/v2/rerank"
            if runtime.reranker_adapter in {"cohere", "cohere-compatible", "cohere_compatible"}
            else "/v1/rerank"
        ),
    )


def _normalize_base_url(base_url: str, *, endpoint_path: str = "/v1/rerank") -> str:
    try:
        parsed = urlsplit(base_url.strip())
        hostname = parsed.hostname
    except ValueError:
        raise ValueError("Reranker base URL is invalid.") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Reranker base URL must be an HTTP(S) origin without credentials, query, or fragment."
        )
    path = parsed.path.rstrip("/")
    endpoint_parts = endpoint_path.split("/")
    version_prefix = endpoint_parts[1] if len(endpoint_parts) > 2 else ""
    if version_prefix in {"v1", "v2"} and path.endswith(f"/{version_prefix}"):
        path = path[: -len(version_prefix) - 1]
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
