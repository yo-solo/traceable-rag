import json
import urllib.error

import pytest

from traceable_rag.embedding import (
    OpenAICompatibleEmbeddingProvider,
    provider_from_api_doc,
)


class FakeHTTP:
    def __init__(self):
        self.calls = []

    def post_json(self, url, headers, payload, timeout):
        self.calls.append((url, headers, payload, timeout))
        return {
            "data": [
                {"index": 0, "embedding": [0.1, 0.2]},
                {"index": 1, "embedding": [0.3, 0.4]},
            ]
        }


def test_openai_compatible_embedding_request_shape():
    http = FakeHTTP()
    provider = OpenAICompatibleEmbeddingProvider(
        api_key="sk-test",
        base_url="https://api.example.com",
        model_name="Qwen3-Embedding-8B",
        http_client=http,
    )

    vectors = provider.embed_texts(["hello", "world"])

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    url, headers, payload, timeout = http.calls[0]
    assert url == "https://api.example.com/v1/embeddings"
    assert headers["Authorization"] == "Bearer sk-test"
    assert payload == {"model": "Qwen3-Embedding-8B", "input": ["hello", "world"]}


def test_default_embedding_http_client_rejects_redirect_before_reusing_auth(
    monkeypatch,
):
    destination_requests = []

    class RedirectingOpener:
        def __init__(self, handler):
            self.handler = handler

        def open(self, request, timeout):
            redirected = self.handler.redirect_request(
                request,
                None,
                307,
                "Temporary Redirect",
                {"Location": "https://other.example/capture"},
                "https://other.example/capture",
            )
            if redirected is not None:
                destination_requests.append(dict(redirected.header_items()))
            raise urllib.error.HTTPError(
                request.full_url, 307, "redirect rejected", {}, None
            )

    monkeypatch.setattr(
        "traceable_rag.embedding.urllib.request.build_opener",
        lambda handler: RedirectingOpener(handler),
    )
    provider = OpenAICompatibleEmbeddingProvider(
        api_key="must-not-cross-origin",
        base_url="https://api.example.com",
        model_name="model",
    )

    with pytest.raises(urllib.error.HTTPError):
        provider.embed_texts(["document"])

    assert destination_requests == []


def test_embedding_query_prompt_only_applies_to_queries():
    http = FakeHTTP()
    provider = OpenAICompatibleEmbeddingProvider(
        api_key="sk-test",
        base_url="https://api.example.com",
        model_name="Qwen3-Embedding-8B",
        http_client=http,
        query_prompt="Instruct: retrieve relevant passages\nQuery:",
    )

    provider.embed_queries(["hello", "world"])

    assert http.calls[0][2]["input"] == [
        "Instruct: retrieve relevant passages\nQuery:hello",
        "Instruct: retrieve relevant passages\nQuery:world",
    ]


def test_missing_api_doc_falls_back_to_canonical_embedding_environment(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENAI_EMBEDDING_BASE_URL", "https://embed.example/v1")
    monkeypatch.setenv("OPENAI_EMBEDDING_API_KEY", "canonical-test-key")
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "canonical-test-model")

    provider = provider_from_api_doc(
        tmp_path / "missing-provider-config.txt",
        runtime_root=tmp_path,
    )

    assert provider.base_url == "https://embed.example"
    assert provider.api_key == "canonical-test-key"
    assert provider.model_name == "canonical-test-model"


def test_embedding_runtime_configuration_does_not_require_an_api_doc(
    tmp_path, monkeypatch
):
    for name in (
        "OPENAI_EMBEDDING_BASE_URL",
        "OPENAI_EMBEDDING_API_KEY",
        "OPENAI_EMBEDDING_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TRACEABLE_RAG_EMBEDDING_BASE_URL", "https://embed.example/v1")
    monkeypatch.setenv("TRACEABLE_RAG_EMBEDDING_API_KEY", "runtime-test-key")
    monkeypatch.setenv("TRACEABLE_RAG_EMBEDDING_MODEL", "runtime-test-model")

    provider = provider_from_api_doc(None, runtime_root=tmp_path)

    assert provider.base_url == "https://embed.example"
    assert provider.api_key == "runtime-test-key"
    assert provider.model_name == "runtime-test-model"


def test_embedding_base_url_with_v1_does_not_duplicate_api_prefix():
    http = FakeHTTP()
    provider = OpenAICompatibleEmbeddingProvider(
        api_key="placeholder-key",
        base_url="https://api.example.com/v1/",
        model_name="placeholder-model",
        http_client=http,
    )

    provider.embed_texts(["hello", "world"])

    assert http.calls[0][0] == "https://api.example.com/v1/embeddings"


def test_openai_compatible_embedding_sorts_out_of_order_items_by_index():
    class OutOfOrderHTTP:
        def post_json(self, *_args, **_kwargs):
            return {
                "data": [
                    {"index": 1, "embedding": [0.3, 0.4]},
                    {"index": 0, "embedding": [0.1, 0.2]},
                ]
            }

    provider = OpenAICompatibleEmbeddingProvider(
        api_key="placeholder-key",
        base_url="https://api.example.com",
        model_name="placeholder-model",
        http_client=OutOfOrderHTTP(),
    )

    assert provider.embed_texts(["first", "second"]) == [
        [0.1, 0.2],
        [0.3, 0.4],
    ]


@pytest.mark.parametrize(
    "items",
    [
        [
            {"embedding": [0.1, 0.2]},
            {"index": 1, "embedding": [0.3, 0.4]},
        ],
        [
            {"index": 0, "embedding": [0.1, 0.2]},
            {"index": 0, "embedding": [0.3, 0.4]},
        ],
        [
            {"index": 0, "embedding": [0.1, 0.2]},
            {"index": 2, "embedding": [0.3, 0.4]},
        ],
    ],
    ids=["missing", "duplicate", "out-of-range"],
)
def test_openai_compatible_embedding_rejects_invalid_item_indexes(items):
    class InvalidHTTP:
        def post_json(self, *_args, **_kwargs):
            return {"data": items}

    provider = OpenAICompatibleEmbeddingProvider(
        api_key="placeholder-key",
        base_url="https://api.example.com",
        model_name="placeholder-model",
        http_client=InvalidHTTP(),
    )

    with pytest.raises(ValueError, match="index"):
        provider.embed_texts(["first", "second"])


def test_partial_runtime_embedding_config_does_not_mix_with_api_doc(
    tmp_path, monkeypatch
):
    provider_doc = tmp_path / "provider.txt"
    provider_doc.write_text(
        "# embedding\n"
        "BaseURL:https://document.example/v1\n"
        "API-Key:\ndocument-secret\n"
        "model_name:document-model\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_EMBEDDING_API_KEY", "runtime-key-only")

    with pytest.raises(ValueError, match="runtime configuration requires"):
        provider_from_api_doc(provider_doc, runtime_root=tmp_path)


@pytest.mark.parametrize(
    ("base_url", "expected_url"),
    [
        (
            "https://api.example.com/proxy/v1",
            "https://api.example.com/proxy/v1/embeddings",
        ),
        (
            "https://api.example.com/v10",
            "https://api.example.com/v10/v1/embeddings",
        ),
    ],
)
def test_embedding_base_url_preserves_proxy_paths_and_only_strips_v1(
    base_url, expected_url
):
    http = FakeHTTP()
    provider = OpenAICompatibleEmbeddingProvider(
        api_key="placeholder-key",
        base_url=base_url,
        model_name="placeholder-model",
        http_client=http,
    )

    provider.embed_texts(["hello", "world"])

    assert http.calls[0][0] == expected_url


@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://api.example.com/v1",
        "https://user:password@api.example.com/v1",
        "https://api.example.com/v1?secret=value",
        "https://api.example.com/v1#fragment",
        "https:///v1",
    ],
)
def test_embedding_base_url_rejects_unsafe_or_non_http_urls(base_url):
    with pytest.raises(ValueError, match="Embedding base URL"):
        OpenAICompatibleEmbeddingProvider(
            api_key="placeholder-key",
            base_url=base_url,
            model_name="placeholder-model",
        )
