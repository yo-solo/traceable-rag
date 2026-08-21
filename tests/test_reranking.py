import pytest

from traceable_rag.reranking import OpenAICompatibleReranker


class FakeHTTP:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post_json(self, url, headers, payload, timeout):
        self.calls.append((url, headers, payload, timeout))
        return self.payload


def test_openai_compatible_reranker_request_and_order():
    http = FakeHTTP(
        {
            "results": [
                {"index": 1, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.2},
            ]
        }
    )
    reranker = OpenAICompatibleReranker(
        api_key="test-key",
        base_url="https://api.example.com/v1",
        model_name="Qwen3-Reranker-8B",
        http_client=http,
    )

    results = reranker.rerank("query", ["first", "second"])

    assert [result.index for result in results] == [1, 0]
    assert http.calls[0][0] == "https://api.example.com/v1/rerank"
    assert http.calls[0][2] == {
        "model": "Qwen3-Reranker-8B",
        "query": "query",
        "documents": ["first", "second"],
        "top_n": 2,
        "return_documents": False,
    }


@pytest.mark.parametrize(
    "results",
    [
        [{"index": 0, "relevance_score": float("nan")}],
        [{"index": 2, "relevance_score": 0.5}],
        [{"index": 0, "relevance_score": 0.5}, {"index": 0, "relevance_score": 0.4}],
    ],
)
def test_openai_compatible_reranker_rejects_invalid_results(results):
    reranker = OpenAICompatibleReranker(
        api_key="test-key",
        base_url="https://api.example.com",
        model_name="reranker",
        http_client=FakeHTTP({"results": results}),
    )

    with pytest.raises(ValueError):
        reranker.rerank("query", ["first", "second"])
