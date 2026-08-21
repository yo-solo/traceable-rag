from __future__ import annotations

import hashlib
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from traceable_rag.embedding import UrllibHTTPClient


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
    ):
        self.api_key = api_key
        self.base_url = _normalize_base_url(base_url)
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
        payload = {
            "model": self.model_name,
            "query": query,
            "documents": documents,
            "top_n": min(requested, len(documents)),
            "return_documents": False,
        }
        data = self.http_client.post_json(
            f"{self.base_url}/v1/rerank",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            payload=payload,
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


class CachedRetryReranker:
    def __init__(
        self,
        reranker: OpenAICompatibleReranker,
        cache_dir: str | Path,
        *,
        max_attempts: int = 3,
        backoff_seconds: float = 1.0,
        max_concurrency: int = 64,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if max_attempts < 1 or max_concurrency < 1:
            raise ValueError("Reranker attempts and concurrency must be positive")
        self.reranker = reranker
        self.model_name = reranker.model_name
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.max_concurrency = max_concurrency
        self.sleeper = sleeper

    def rerank_many(
        self,
        requests: list[tuple[str, list[str]]],
    ) -> list[list[RerankResult]]:
        output: list[list[RerankResult] | None] = [None] * len(requests)
        missing: dict[str, tuple[str, list[str], list[int]]] = {}
        for position, (query, documents) in enumerate(requests):
            key = self._key(query, documents)
            cached = self._load(key, len(documents))
            if cached is not None:
                output[position] = cached
            else:
                if key not in missing:
                    missing[key] = (query, documents, [])
                missing[key][2].append(position)
        first_error: Exception | None = None
        if missing:
            with ThreadPoolExecutor(
                max_workers=min(self.max_concurrency, len(missing)),
                thread_name_prefix="vidore-reranker",
            ) as executor:
                futures = {
                    executor.submit(self._rerank_with_retry, query, documents): (
                        key,
                        documents,
                        positions,
                    )
                    for key, (query, documents, positions) in missing.items()
                }
                for future in as_completed(futures):
                    key, documents, positions = futures[future]
                    try:
                        results = future.result()
                    except Exception as exc:
                        if first_error is None:
                            first_error = exc
                        continue
                    self._save(key, results)
                    for position in positions:
                        output[position] = results
        if first_error is not None:
            raise first_error
        if any(results is None for results in output):
            raise ValueError("Reranker did not produce every requested result")
        return [results for results in output if results is not None]

    def _rerank_with_retry(
        self, query: str, documents: list[str]
    ) -> list[RerankResult]:
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self.reranker.rerank(
                    query,
                    documents,
                    top_n=len(documents),
                )
            except Exception:
                if attempt == self.max_attempts:
                    raise
                self.sleeper(self.backoff_seconds * (2 ** (attempt - 1)))
        raise AssertionError("unreachable")

    def _key(self, query: str, documents: list[str]) -> str:
        material = json.dumps(
            {
                "model": self.model_name,
                "query": query,
                "documents": documents,
                "top_n": len(documents),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def _load(self, key: str, document_count: int) -> list[RerankResult] | None:
        path = self.cache_dir / f"{key}.json"
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            results = [RerankResult(**item) for item in payload]
            if (
                len(results) != document_count
                or {item.index for item in results} != set(range(document_count))
                or any(not math.isfinite(item.relevance_score) for item in results)
            ):
                return None
            return results
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _save(self, key: str, results: list[RerankResult]) -> None:
        path = self.cache_dir / f"{key}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps([asdict(item) for item in results], separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(path)


def _normalize_base_url(base_url: str) -> str:
    try:
        parsed = urlsplit(base_url.strip())
    except ValueError:
        raise ValueError("Reranker base URL is invalid") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Reranker base URL must be a credential-free HTTP(S) URL")
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
