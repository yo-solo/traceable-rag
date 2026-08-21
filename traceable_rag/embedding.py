from __future__ import annotations

import json
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .runtime_config import RuntimeConfig, RuntimeConfigError, load_runtime_config


class EmbeddingProvider(ABC):
    provider_name: str
    model_name: str

    @abstractmethod
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def embed_queries(self, queries: list[str]) -> list[list[float]]:
        return self.embed_texts(queries)


class UrllibHTTPClient:
    class _RejectRedirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args: Any, **_kwargs: Any):
            return None

    @classmethod
    def _open(cls, request: urllib.request.Request, *, timeout: int):
        opener = urllib.request.build_opener(cls._RejectRedirects())
        return opener.open(request, timeout=timeout)

    def post_json(self, url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with self._open(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def get_json(
        self,
        url: str,
        headers: dict[str, str],
        timeout: int,
    ) -> dict[str, Any]:
        request = urllib.request.Request(url, headers=headers, method="GET")
        with self._open(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))


class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    provider_name = "openai-compatible"

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model_name: str,
        *,
        http_client: UrllibHTTPClient | Any | None = None,
        timeout: int = 60,
        batch_size: int = 16,
        query_prompt: str | None = None,
    ):
        self.api_key = api_key
        self.base_url = _normalize_embedding_base_url(base_url)
        self.model_name = model_name
        self.http_client = http_client or UrllibHTTPClient()
        self.timeout = timeout
        self.batch_size = batch_size
        self.query_prompt = query_prompt

    def embed_queries(self, queries: list[str]) -> list[list[float]]:
        if not self.query_prompt:
            return self.embed_texts(queries)
        return self.embed_texts([f"{self.query_prompt}{query}" for query in queries])

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            payload = {"model": self.model_name, "input": batch}
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            data = self.http_client.post_json(
                f"{self.base_url}/v1/embeddings",
                headers=headers,
                payload=payload,
                timeout=self.timeout,
            )
            items = data.get("data") if isinstance(data, dict) else None
            if not isinstance(items, list) or len(items) != len(batch):
                raise ValueError("Embedding API returned an invalid item index set.")
            ordered: list[list[float] | None] = [None] * len(batch)
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("Embedding API returned an invalid item index.")
                index = item.get("index")
                if (
                    isinstance(index, bool)
                    or not isinstance(index, int)
                    or not 0 <= index < len(batch)
                    or ordered[index] is not None
                    or "embedding" not in item
                ):
                    raise ValueError("Embedding API returned an invalid item index.")
                ordered[index] = item["embedding"]
            if any(vector is None for vector in ordered):
                raise ValueError("Embedding API returned an incomplete item index set.")
            vectors.extend(vector for vector in ordered if vector is not None)
        if len(vectors) != len(texts):
            raise ValueError(f"Embedding API returned {len(vectors)} vectors for {len(texts)} texts")
        return vectors


def provider_from_api_doc(
    api_doc_path: str | Path | None,
    *,
    runtime_root: str | Path | None = None,
) -> OpenAICompatibleEmbeddingProvider:
    runtime = load_runtime_config(root=runtime_root)
    base_url = runtime.embedding_base_url
    api_key = runtime.embedding_api_key
    model_name = runtime.embedding_model
    runtime_values = (base_url, api_key, model_name)
    if any(runtime_values) and not all(runtime_values):
        raise RuntimeConfigError(
            "Embedding runtime configuration requires base URL, API key, and model."
        )

    path = Path(api_doc_path) if api_doc_path is not None else None
    if path is not None and path.is_file() and not any(runtime_values):
        text = path.read_text(encoding="utf-8")
        section = _section(text, "# embedding")
        base_url = _extract_after(
            section,
            "BaseURL:",
            stop_prefixes=("API-Key:", "model_name:", "#"),
        )
        api_key = _extract_after(
            section,
            "API-Key:",
            stop_prefixes=("model_name:", "#"),
        )
        model_name = _extract_after(section, "model_name:")
    if not base_url or not api_key or not model_name:
        raise RuntimeConfigError(
            "Embedding configuration requires base URL, API key, and model."
        )
    return OpenAICompatibleEmbeddingProvider(
        api_key=api_key,
        base_url=base_url,
        model_name=model_name,
        query_prompt=_normalized_query_prompt(runtime.embedding_query_prompt),
    )


def provider_from_runtime_config(
    runtime: RuntimeConfig,
) -> OpenAICompatibleEmbeddingProvider:
    if not runtime.embedding_configured:
        raise RuntimeConfigError(
            "Embedding runtime configuration requires base URL, API key, and model."
        )
    return OpenAICompatibleEmbeddingProvider(
        api_key=runtime.embedding_api_key or "",
        base_url=runtime.embedding_base_url or "",
        model_name=runtime.embedding_model or "",
        query_prompt=_normalized_query_prompt(runtime.embedding_query_prompt),
    )


def embed_query_texts(
    provider: EmbeddingProvider | Any,
    queries: list[str],
) -> list[list[float]]:
    method = getattr(provider, "embed_queries", None)
    if callable(method):
        return method(queries)
    return provider.embed_texts(queries)


def _normalized_query_prompt(prompt: str | None) -> str | None:
    return prompt.replace("\\n", "\n") if prompt else None


def _normalize_embedding_base_url(base_url: str) -> str:
    try:
        parsed = urlsplit(base_url.strip())
        hostname = parsed.hostname
    except ValueError:
        raise ValueError("Embedding base URL is invalid.") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Embedding base URL must be an HTTP(S) origin without credentials, query, or fragment.")
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _extract_after(text: str, marker: str, stop_prefixes: tuple[str, ...] = ("#",)) -> str | None:
    idx = text.find(marker)
    if idx < 0:
        return None
    rest = text[idx + len(marker) :].strip().splitlines()
    values: list[str] = []
    for line in rest:
        line = line.strip()
        if not line:
            if values:
                break
            continue
        if any(line.startswith(prefix) for prefix in stop_prefixes):
            break
        values.append(line)
    return "\n".join(values).strip() or None


def _section(text: str, header: str) -> str:
    idx = text.find(header)
    if idx < 0:
        return text
    next_idx = text.find("\n#", idx + len(header))
    return text[idx:next_idx] if next_idx >= 0 else text[idx:]
