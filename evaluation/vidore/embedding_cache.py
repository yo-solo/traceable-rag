from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from traceable_rag.embedding import EmbeddingProvider
from traceable_rag.indexing import validate_embedding_vectors


class CachedRetryEmbeddingProvider(EmbeddingProvider):
    def __init__(
        self,
        provider: EmbeddingProvider,
        cache_dir: str | Path,
        *,
        max_attempts: int = 3,
        backoff_seconds: float = 1.0,
        request_batch_size: int = 16,
        max_concurrency: int = 32,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if request_batch_size < 1:
            raise ValueError("request_batch_size must be positive")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self.provider = provider
        self.provider_name = provider.provider_name
        self.model_name = provider.model_name
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.request_batch_size = request_batch_size
        self.max_concurrency = max_concurrency
        self.sleeper = sleeper

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float] | None] = [None] * len(texts)
        missing: dict[str, tuple[str, list[int]]] = {}
        for index, text in enumerate(texts):
            key = self._key(text)
            cached = self._load(key)
            if cached is not None:
                results[index] = cached
            else:
                if key not in missing:
                    missing[key] = (text, [])
                missing[key][1].append(index)
        if missing:
            missing_items = list(missing.items())
            batches = [
                missing_items[start : start + self.request_batch_size]
                for start in range(0, len(missing_items), self.request_batch_size)
            ]
            first_error: Exception | None = None
            with ThreadPoolExecutor(
                max_workers=min(self.max_concurrency, len(batches)),
                thread_name_prefix="vidore-embedding",
            ) as executor:
                futures = {
                    executor.submit(
                        self._embed_with_retry,
                        [item[1][0] for item in batch],
                    ): batch
                    for batch in batches
                }
                for future in as_completed(futures):
                    batch = futures[future]
                    try:
                        vectors = future.result()
                        validate_embedding_vectors(
                            vectors, expected_count=len(batch)
                        )
                    except Exception as exc:
                        if first_error is None:
                            first_error = exc
                        continue
                    for (key, (_text, indices)), vector in zip(batch, vectors):
                        self._save(key, vector)
                        for index in indices:
                            results[index] = vector
            if first_error is not None:
                raise first_error
        vectors = [vector for vector in results if vector is not None]
        validate_embedding_vectors(vectors, expected_count=len(texts))
        return vectors

    def _embed_with_retry(self, texts: list[str]) -> list[list[float]]:
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self.provider.embed_texts(texts)
            except Exception:
                if attempt == self.max_attempts:
                    raise
                self.sleeper(self.backoff_seconds * (2 ** (attempt - 1)))
        raise AssertionError("unreachable")

    def _key(self, text: str) -> str:
        material = f"{self.provider_name}\0{self.model_name}\0{text}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def _load(self, key: str) -> list[float] | None:
        path = self.cache_dir / f"{key}.json"
        if not path.exists():
            return None
        try:
            vector = json.loads(path.read_text(encoding="utf-8"))
            validate_embedding_vectors([vector], expected_count=1)
            return vector
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def _save(self, key: str, vector: list[float]) -> None:
        path = self.cache_dir / f"{key}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(vector, separators=(",", ":")), encoding="utf-8")
        temporary.replace(path)
