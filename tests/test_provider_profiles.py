from __future__ import annotations

import os
import sqlite3
import urllib.error
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from traceable_rag.models import Block, Chunk, Document, IndexRecord
from traceable_rag.provider_profiles import (
    DynamicEmbeddingProvider,
    ProviderConnectionError,
    ProviderProfile,
    ProviderProfileError,
    ProviderProfileStore,
    ProviderRegistry,
    ProviderStoreKeyError,
    ProviderStoreUnavailableError,
    ProviderTestNotSupportedError,
    environment_profile,
    provider_management_authorized,
    read_master_key,
    reranker_from_profile,
    test_provider_connection as run_provider_connection_test,
)
from traceable_rag.runtime_config import load_runtime_config
from traceable_rag.store import DEFAULT_KNOWLEDGE_BASE_ID, EvidenceStore
from traceable_rag.web_app import create_app


SECRET = "sk-provider-secret-never-return"


class FakeProviderHTTP:
    def __init__(self):
        self.calls: list[tuple[str, str, dict, dict]] = []
        self.failure: Exception | None = None

    def post_json(self, url, headers, payload, timeout):
        self.calls.append(("POST", url, headers, payload))
        if self.failure is not None:
            raise self.failure
        if url.endswith("/embeddings"):
            return {
                "data": [
                    {"index": index, "embedding": [1.0, 0.0]}
                    for index, _text in enumerate(payload["input"])
                ]
            }
        if url.endswith("/rerank"):
            return {
                "results": [
                    {"index": index, "relevance_score": 1.0 - index / 10}
                    for index in range(payload["top_n"])
                ]
            }
        if url.endswith("/chat/completions"):
            return {"choices": [{"message": {"role": "assistant", "content": "OK"}}]}
        raise AssertionError(url)

    def get_json(self, url, headers, timeout):
        self.calls.append(("GET", url, headers, {}))
        raise AssertionError("MinerU tests must not issue a synthetic batch request")


def _key_file(tmp_path: Path, value: bytes | None = None) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "master-key"
    path.write_bytes(value or os.urandom(32))
    return path


def _registry(tmp_path: Path, *, runtime=None, http_client=None) -> ProviderRegistry:
    return ProviderRegistry(
        runtime=runtime,
        store_path=tmp_path / "providers.db",
        master_key_file=_key_file(tmp_path),
        http_client=http_client,
    )


def _embedding_profile(
    *,
    model: str = "embedding-model",
    base_url: str = "https://embedding.example/v1",
    secret: str = SECRET,
    source: str = "stored",
) -> ProviderProfile:
    return ProviderProfile(
        role="embedding",
        adapter="openai_compatible",
        display_name="Embedding",
        base_url=base_url,
        model=model,
        options={},
        secret=secret,
        source=source,
    )


def _make_client(
    tmp_path: Path,
    *,
    http_client=None,
    admin_token_file=None,
    allow_insecure_dev: bool | None = None,
    client_address: tuple[str, int] = ("testclient", 50000),
):
    key_file = _key_file(tmp_path)
    app = create_app(
        db_path=tmp_path / "traceable.db",
        managed_root=tmp_path / "managed",
        settings_path=tmp_path / "settings.json",
        provider_store_path=tmp_path / "providers.db",
        provider_master_key_file=key_file,
        provider_admin_token_file=admin_token_file,
        provider_http_client=http_client,
        provider_test_http_client=http_client,
        provider_management_allow_insecure_dev=allow_insecure_dev,
        trusted_hosts=("testserver",),
        start_processing_worker=False,
    )
    return TestClient(app, client=client_address), app


def test_master_key_reader_requires_regular_non_symlink_exact_raw_32_bytes(tmp_path):
    key = os.urandom(32)
    key_path = _key_file(tmp_path, key)

    assert read_master_key(key_path) == key

    key_path.write_bytes(os.urandom(31))
    with pytest.raises(ProviderStoreUnavailableError, match="exactly 32"):
        read_master_key(key_path)

    target = tmp_path / "real-key"
    target.write_bytes(os.urandom(32))
    key_path.unlink()
    key_path.symlink_to(target)
    with pytest.raises(ProviderStoreUnavailableError, match="regular file"):
        read_master_key(key_path)


def test_store_encrypts_secret_and_wrong_key_fails_without_secret_detail(tmp_path):
    database = tmp_path / "providers.db"
    key = os.urandom(32)
    store = ProviderProfileStore(database, key)
    store.initialize()
    saved = store.put(_embedding_profile(secret=SECRET), secret_update=SECRET)

    assert saved.secret == SECRET
    assert SECRET not in repr(saved)
    assert SECRET.encode() not in database.read_bytes()
    with sqlite3.connect(database) as connection:
        nonce, ciphertext = connection.execute(
            "select secret_nonce, secret_ciphertext from provider_profiles"
        ).fetchone()
    assert len(nonce) == 12
    assert SECRET.encode() not in ciphertext
    assert "redacted" in repr(store)

    wrong = ProviderProfileStore(database, os.urandom(32))
    wrong.initialize()
    with pytest.raises(ProviderStoreKeyError) as error:
        wrong.get("embedding")
    assert SECRET not in str(error.value)
    assert database.as_posix() not in str(error.value)


def test_store_reencrypts_preserved_secret_under_updated_bound_metadata(tmp_path):
    database = tmp_path / "providers.db"
    store = ProviderProfileStore(database, os.urandom(32))
    store.initialize()
    saved = store.put(_embedding_profile(), secret_update=SECRET)
    with sqlite3.connect(database) as connection:
        old_nonce, old_ciphertext = connection.execute(
            "select secret_nonce, secret_ciphertext from provider_profiles"
        ).fetchone()

    updated = store.put(
        replace(
            saved,
            adapter="siliconflow",
            base_url="https://updated.example/v1",
            model="updated-model",
            options={"timeout": 45, "query_prompt": "Query: "},
        )
    )
    with sqlite3.connect(database) as connection:
        new_nonce, new_ciphertext = connection.execute(
            "select secret_nonce, secret_ciphertext from provider_profiles"
        ).fetchone()

    assert updated.secret == SECRET
    assert updated.adapter == "siliconflow"
    assert updated.base_url == "https://updated.example/v1"
    assert updated.model == "updated-model"
    assert updated.options == {"query_prompt": "Query: ", "timeout": 45}
    assert new_nonce != old_nonce
    assert new_ciphertext != old_ciphertext


@pytest.mark.parametrize(
    ("column", "tampered_value", "lookup_role"),
    [
        ("role", "llm", "llm"),
        ("adapter", "siliconflow", "embedding"),
        ("base_url", "https://tampered.example/v1", "embedding"),
        ("model", "tampered-model", "embedding"),
        ("options_json", '{"timeout":45}', "embedding"),
    ],
)
def test_store_fails_closed_when_bound_metadata_is_tampered(
    tmp_path, column, tampered_value, lookup_role
):
    database = tmp_path / "providers.db"
    store = ProviderProfileStore(database, os.urandom(32))
    store.initialize()
    store.put(_embedding_profile(), secret_update=SECRET)
    with sqlite3.connect(database) as connection:
        connection.execute(
            f"update provider_profiles set {column} = ? where role = 'embedding'",
            (tampered_value,),
        )

    with pytest.raises(ProviderStoreKeyError):
        store.get(lookup_role)


def test_store_update_preserves_omitted_secret_and_explicit_null_deletes_it(tmp_path):
    registry = _registry(tmp_path)
    registry.put(
        "embedding",
        {
            "adapter": "openai_compatible",
            "base_url": "https://one.example/v1",
            "model": "model-a",
        },
        secret_update=SECRET,
    )

    registry.put("embedding", {"model": "model-b"})
    preserved = registry.resolve("embedding")
    assert preserved is not None
    assert preserved.secret == SECRET
    assert preserved.model == "model-b"

    registry.put("embedding", {}, secret_update=None)
    deleted = registry.resolve("embedding")
    assert deleted is not None
    assert deleted.secret is None
    assert registry.metadata_payload("embedding")["configured"] is False


def test_store_unavailable_never_falls_back_to_environment_profile(tmp_path):
    runtime = load_runtime_config(
        root=tmp_path,
        environ={
            "OPENAI_EMBEDDING_BASE_URL": "https://environment.example/v1",
            "OPENAI_EMBEDDING_API_KEY": "environment-key",
            "OPENAI_EMBEDDING_MODEL": "environment-model",
        },
    )
    registry = ProviderRegistry(
        runtime=runtime,
        store_path=tmp_path / "providers.db",
        master_key_file=tmp_path / "missing-master-key",
    )

    with pytest.raises(ProviderStoreUnavailableError):
        registry.resolve("embedding")
    payload = registry.metadata_payload("embedding")
    assert payload == {
        "role": "embedding",
        "adapter": "openai_compatible",
        "display_name": "Embedding",
        "base_url": None,
        "model": None,
        "options": {},
        "source": "default",
        "configured": False,
        "secret_present": False,
        "status": "unavailable",
        "last_test_status": None,
        "last_tested_at": None,
    }
    assert registry.all_metadata_payload()["storage_available"] is False


def test_wrong_store_key_never_falls_back_to_environment_profile(tmp_path):
    database = tmp_path / "providers.db"
    correct_key = os.urandom(32)
    store = ProviderProfileStore(database, correct_key)
    store.initialize()
    store.put(_embedding_profile(), secret_update=SECRET)
    wrong_key_file = _key_file(tmp_path, os.urandom(32))
    runtime = load_runtime_config(
        root=tmp_path,
        environ={
            "OPENAI_EMBEDDING_BASE_URL": "https://environment.example/v1",
            "OPENAI_EMBEDDING_API_KEY": "environment-key",
            "OPENAI_EMBEDDING_MODEL": "environment-model",
        },
    )
    registry = ProviderRegistry(
        runtime=runtime,
        store_path=database,
        master_key_file=wrong_key_file,
    )

    with pytest.raises(ProviderStoreKeyError):
        registry.resolve("embedding")
    payload = registry.metadata_payload("embedding")
    assert payload["status"] == "unavailable"
    assert payload["source"] == "default"
    assert payload["configured"] is False
    assert payload["model"] is None


def test_unopenable_store_never_falls_back_to_environment_profile(tmp_path):
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("blocked", encoding="utf-8")
    runtime = load_runtime_config(
        root=tmp_path,
        environ={
            "OPENAI_EMBEDDING_BASE_URL": "https://environment.example/v1",
            "OPENAI_EMBEDDING_API_KEY": "environment-key",
            "OPENAI_EMBEDDING_MODEL": "environment-model",
        },
    )
    registry = ProviderRegistry(
        runtime=runtime,
        store_path=blocked_parent / "providers.db",
        master_key_file=_key_file(tmp_path),
    )

    with pytest.raises(ProviderStoreUnavailableError):
        registry.resolve("embedding")
    payload = registry.metadata_payload("embedding")
    assert payload["status"] == "unavailable"
    assert payload["source"] == "default"
    assert payload["configured"] is False


def test_environment_reranker_legacy_fallback_is_explicit_and_new_config_is_independent(
    tmp_path,
):
    legacy_runtime = load_runtime_config(
        root=tmp_path,
        environ={
            "OPENAI_EMBEDDING_BASE_URL": "https://legacy.example/v1",
            "OPENAI_EMBEDDING_API_KEY": "legacy-key",
            "OPENAI_EMBEDDING_MODEL": "embedding-model",
            "RERANKER_MODEL": "reranker-model",
        },
    )
    legacy = environment_profile(legacy_runtime, "reranker")
    assert legacy is not None
    assert legacy.source == "environment_legacy"
    assert legacy.base_url == "https://legacy.example/v1"
    assert legacy.secret == "legacy-key"
    assert legacy.configured is True

    independent_runtime = load_runtime_config(
        root=tmp_path,
        environ={
            "OPENAI_EMBEDDING_BASE_URL": "https://embedding.example/v1",
            "OPENAI_EMBEDDING_API_KEY": "embedding-key",
            "OPENAI_EMBEDDING_MODEL": "embedding-model",
            "TRACEABLE_RAG_RERANKER_BASE_URL": "https://rerank.example/v2",
            "TRACEABLE_RAG_RERANKER_API_KEY": "rerank-key",
            "TRACEABLE_RAG_RERANKER_MODEL": "rerank-model",
            "TRACEABLE_RAG_RERANKER_ADAPTER": "cohere",
        },
    )
    independent = environment_profile(independent_runtime, "reranker")
    assert independent is not None
    assert independent.source == "environment"
    assert independent.base_url == "https://rerank.example/v2"
    assert independent.secret == "rerank-key"
    assert independent.adapter == "cohere"


@pytest.mark.parametrize(
    ("adapter", "base_url", "expected_url"),
    [
        ("siliconflow_qwen", "https://sf.example/v1", "https://sf.example/v1/rerank"),
        ("cohere", "https://cohere.example/v2", "https://cohere.example/v2/rerank"),
        ("jina", "https://jina.example/v1", "https://jina.example/v1/rerank"),
    ],
)
def test_reranker_adapter_request_shapes_do_not_duplicate_version_prefix(
    adapter, base_url, expected_url
):
    http = FakeProviderHTTP()
    profile = ProviderProfile(
        role="reranker",
        adapter=adapter,
        display_name="Reranker",
        base_url=base_url,
        model="reranker-model",
        options={"timeout": 45},
        secret=SECRET,
    )

    results = reranker_from_profile(profile, http_client=http).rerank(
        "query", ["first", "second"], top_n=1
    )

    assert len(results) == 1
    method, url, headers, payload = http.calls[0]
    assert method == "POST"
    assert url == expected_url
    assert headers["Authorization"] == f"Bearer {SECRET}"
    assert payload == {
        "model": "reranker-model",
        "query": "query",
        "documents": ["first", "second"],
        "top_n": 1,
        "return_documents": False,
    }


def test_embedding_llm_and_mineru_connection_test_protocols_are_safe():
    http = FakeProviderHTTP()
    embedding = _embedding_profile()
    llm = ProviderProfile(
        role="llm",
        adapter="openai_compatible",
        display_name="LLM",
        base_url="https://llm.example/v1",
        model="llm-model",
        options={"timeout": 12},
        secret=SECRET,
    )
    mineru = ProviderProfile(
        role="mineru",
        adapter="mineru_official",
        display_name="MinerU",
        base_url="https://mineru.net",
        model="vlm",
        options={},
        secret=SECRET,
    )

    run_provider_connection_test(embedding, http_client=http)
    run_provider_connection_test(llm, http_client=http)
    with pytest.raises(ProviderTestNotSupportedError):
        run_provider_connection_test(mineru, http_client=http)

    assert http.calls[0][1] == "https://embedding.example/v1/embeddings"
    assert http.calls[0][3] == {
        "model": "embedding-model",
        "input": ["Traceable RAG connectivity test"],
    }
    assert http.calls[1][1] == "https://llm.example/v1/chat/completions"
    assert http.calls[1][3]["max_tokens"] == 1
    assert [call[0] for call in http.calls] == ["POST", "POST"]


def test_provider_runtime_and_connection_tests_reject_cross_origin_redirects(
    tmp_path, monkeypatch
):
    redirect_base_url = "https://configured.example"
    destination_url = "https://different-origin.example/credential-capture"
    redirect_requests: list[dict[str, str]] = []
    destination_requests: list[dict[str, str]] = []

    class SimulatedRedirectOpener:
        def __init__(self, redirect_handler):
            self.redirect_handler = redirect_handler

        def open(self, request, timeout):
            redirect_requests.append(dict(request.header_items()))
            redirected = self.redirect_handler.redirect_request(
                request,
                None,
                307,
                "Temporary Redirect",
                {"Location": destination_url},
                destination_url,
            )
            if redirected is not None:
                destination_requests.append(dict(redirected.header_items()))
            raise urllib.error.HTTPError(
                request.full_url,
                307,
                "Provider redirect rejected",
                {},
                None,
            )

    def simulated_build_opener(*handlers):
        assert len(handlers) == 1
        return SimulatedRedirectOpener(handlers[0])

    monkeypatch.setattr(
        "traceable_rag.provider_profiles.urllib.request.build_opener",
        simulated_build_opener,
    )
    registry = _registry(tmp_path)
    registry.put(
        "embedding",
        {
            "adapter": "openai_compatible",
            "base_url": redirect_base_url,
            "model": "embedding-model",
        },
        secret_update=SECRET,
    )
    registry.put(
        "reranker",
        {
            "adapter": "siliconflow_qwen",
            "base_url": redirect_base_url,
            "model": "reranker-model",
        },
        secret_update=SECRET,
    )

    with pytest.raises(urllib.error.HTTPError):
        registry.embedding_provider().embed_texts(["runtime embedding"])
    with pytest.raises(urllib.error.HTTPError):
        registry.reranker().rerank("runtime reranker", ["document"], top_n=1)

    profiles = [
        _embedding_profile(base_url=redirect_base_url),
        ProviderProfile(
            role="reranker",
            adapter="siliconflow_qwen",
            display_name="Reranker",
            base_url=redirect_base_url,
            model="reranker-model",
            options={},
            secret=SECRET,
        ),
        ProviderProfile(
            role="llm",
            adapter="openai_compatible",
            display_name="LLM",
            base_url=redirect_base_url,
            model="llm-model",
            options={},
            secret=SECRET,
        ),
    ]
    for profile in profiles:
        with pytest.raises(ProviderConnectionError):
            run_provider_connection_test(profile)

    assert len(redirect_requests) == 5
    assert all(
        headers.get("Authorization") == f"Bearer {SECRET}"
        for headers in redirect_requests
    )
    assert destination_requests == []


def test_options_are_role_whitelisted_to_prevent_accidental_secret_echo():
    with pytest.raises(ProviderProfileError, match="unsupported"):
        run_provider_connection_test(
            replace(_embedding_profile(), options={"key": SECRET}),
            http_client=FakeProviderHTTP(),
        )


def test_management_boundary_only_auto_allows_testclient_without_token_file():
    assert provider_management_authorized(
        client_host="testclient",
        supplied_token=None,
        token_file=None,
    ) == (True, None)
    assert provider_management_authorized(
        client_host="127.0.0.1",
        supplied_token=None,
        token_file=None,
    ) == (False, None)
    assert provider_management_authorized(
        client_host="127.0.0.1",
        supplied_token=None,
        token_file=None,
        allow_insecure_dev=True,
    ) == (True, None)
    assert provider_management_authorized(
        client_host="::1",
        supplied_token=None,
        token_file=None,
        allow_insecure_dev=True,
    ) == (True, None)
    assert provider_management_authorized(
        client_host="192.0.2.10",
        supplied_token=None,
        token_file=None,
        allow_insecure_dev=True,
    ) == (False, None)


def test_provider_api_requires_explicit_insecure_dev_for_real_loopback(tmp_path):
    body = {
        "adapter": "openai_compatible",
        "base_url": "https://embedding.example/v1",
        "model": "model",
        "api_key": SECRET,
    }
    test_client, locked_app = _make_client(tmp_path / "locked")
    assert test_client.put(
        "/api/provider-profiles/embedding", json=body
    ).status_code == 200

    real_loopback = TestClient(locked_app, client=("127.0.0.1", 50001))
    assert real_loopback.put(
        "/api/provider-profiles/embedding", json={"model": "blocked"}
    ).status_code == 403

    allowed_loopback, allowed_app = _make_client(
        tmp_path / "allowed",
        allow_insecure_dev=True,
        client_address=("127.0.0.1", 50002),
    )
    assert allowed_loopback.put(
        "/api/provider-profiles/embedding", json=body
    ).status_code == 200
    remote = TestClient(allowed_app, client=("192.0.2.10", 50003))
    assert remote.put(
        "/api/provider-profiles/embedding", json={"model": "blocked"}
    ).status_code == 403


def test_provider_api_never_returns_secret_and_supports_preserve_delete_and_source(
    tmp_path,
):
    http = FakeProviderHTTP()
    client, app = _make_client(tmp_path, http_client=http)

    saved = client.put(
        "/api/provider-profiles/embedding",
        json={
            "adapter": "openai_compatible",
            "display_name": "Primary embedding",
            "base_url": "https://embedding.example/v1",
            "model": "model-a",
            "options": {"query_prompt": "Query: "},
            "api_key": SECRET,
        },
    )

    assert saved.status_code == 200
    assert saved.json()["source"] == "stored"
    assert saved.json()["secret_present"] is True
    assert saved.json()["configured"] is True
    assert SECRET not in saved.text
    assert SECRET[:8] not in saved.text
    assert "api_key" not in saved.text

    preserved = client.put(
        "/api/provider-profiles/embedding", json={"model": "model-b"}
    )
    assert preserved.status_code == 200
    assert app.state.provider_registry.resolve("embedding").secret == SECRET

    listing = client.get("/api/provider-profiles")
    assert listing.status_code == 200
    assert listing.json()["storage_available"] is True
    assert listing.json()["profiles"]["embedding"]["model"] == "model-b"
    assert SECRET not in listing.text

    removed_secret = client.put(
        "/api/provider-profiles/embedding", json={"api_key": None}
    )
    assert removed_secret.json()["secret_present"] is False
    assert removed_secret.json()["configured"] is False
    assert client.delete("/api/provider-profiles/embedding").status_code == 204
    assert client.get("/api/provider-profiles").json()["profiles"]["embedding"][
        "source"
    ] == "default"


def test_provider_api_admin_token_and_generic_failure_do_not_leak_credentials(tmp_path):
    admin_token = "admin-token-value"
    token_file = tmp_path / "admin-token"
    token_file.write_text(admin_token + "\n", encoding="utf-8")
    http = FakeProviderHTTP()
    client, _app = _make_client(
        tmp_path, http_client=http, admin_token_file=token_file
    )
    body = {
        "adapter": "openai_compatible",
        "base_url": "https://embedding.example",
        "model": "model",
        "api_key": SECRET,
    }

    assert client.put("/api/provider-profiles/embedding", json=body).status_code == 403
    assert client.put(
        "/api/provider-profiles/embedding",
        json=body,
        headers={"X-Traceable-RAG-Admin-Token": "wrong"},
    ).status_code == 403
    authorized = client.put(
        "/api/provider-profiles/embedding",
        json=body,
        headers={"X-Traceable-RAG-Admin-Token": admin_token},
    )
    assert authorized.status_code == 200
    assert admin_token not in authorized.text
    assert SECRET not in authorized.text

    http.failure = RuntimeError(f"upstream included {SECRET}")
    failed = client.post(
        "/api/provider-profiles/embedding/test",
        headers={"X-Traceable-RAG-Admin-Token": admin_token},
    )
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert SECRET not in failed.text


def test_provider_test_uses_saved_or_temporary_profile_without_saving_temporary_secret(
    tmp_path,
):
    http = FakeProviderHTTP()
    client, app = _make_client(tmp_path, http_client=http)
    client.put(
        "/api/provider-profiles/embedding",
        json={
            "adapter": "openai_compatible",
            "base_url": "https://embedding.example",
            "model": "saved-model",
            "api_key": SECRET,
        },
    )

    saved_test = client.post("/api/provider-profiles/embedding/test")
    assert saved_test.status_code == 200
    assert saved_test.json()["status"] == "success"
    assert client.get("/api/provider-profiles").json()["profiles"]["embedding"][
        "last_test_status"
    ] == "success"

    temporary = client.post(
        "/api/provider-profiles/embedding/test",
        json={"model": "temporary-model", "api_key": "temporary-secret"},
    )
    assert temporary.status_code == 200
    assert temporary.json()["status"] == "success"
    assert "temporary-secret" not in temporary.text
    saved = app.state.provider_registry.resolve("embedding")
    assert saved is not None
    assert saved.model == "saved-model"
    assert saved.secret == SECRET
    assert http.calls[-1][3]["model"] == "temporary-model"


def test_mineru_test_is_non_calling_and_llm_test_warns_about_possible_cost(tmp_path):
    http = FakeProviderHTTP()
    client, _app = _make_client(tmp_path, http_client=http)
    client.put(
        "/api/provider-profiles/mineru",
        json={"adapter": "mineru_official", "api_key": SECRET},
    )
    mineru = client.post("/api/provider-profiles/mineru/test")
    assert mineru.status_code == 200
    assert mineru.json()["status"] == "not_supported"
    assert http.calls == []

    client.put(
        "/api/provider-profiles/llm",
        json={
            "adapter": "openai_compatible",
            "base_url": "https://llm.example/v1",
            "model": "llm-model",
            "api_key": SECRET,
        },
    )
    llm = client.post("/api/provider-profiles/llm/test")
    assert llm.status_code == 200
    assert llm.json()["status"] == "success"
    assert "charges" in llm.json()["message"]


def test_environment_profile_reappears_after_stored_override_is_deleted(tmp_path):
    runtime = load_runtime_config(
        root=tmp_path,
        environ={
            "OPENAI_EMBEDDING_BASE_URL": "https://environment.example/v1",
            "OPENAI_EMBEDDING_API_KEY": "environment-key",
            "OPENAI_EMBEDDING_MODEL": "environment-model",
        },
    )
    registry = _registry(tmp_path, runtime=runtime)
    assert registry.metadata_payload("embedding")["source"] == "environment"

    registry.put(
        "embedding",
        {
            "adapter": "openai_compatible",
            "base_url": "https://stored.example/v1",
            "model": "stored-model",
        },
        secret_update=SECRET,
    )
    assert registry.metadata_payload("embedding")["source"] == "stored"
    registry.delete("embedding")
    restored = registry.metadata_payload("embedding")
    assert restored["source"] == "environment"
    assert restored["model"] == "environment-model"


def test_provider_api_and_retrieval_do_not_claim_or_use_environment_when_store_unavailable(
    tmp_path, monkeypatch
):
    runtime = load_runtime_config(
        root=tmp_path,
        environ={
            "OPENAI_EMBEDDING_BASE_URL": "https://environment.example/v1",
            "OPENAI_EMBEDDING_API_KEY": "environment-key",
            "OPENAI_EMBEDDING_MODEL": "environment-model",
        },
    )
    app = create_app(
        db_path=tmp_path / "traceable.db",
        managed_root=tmp_path / "managed",
        settings_path=tmp_path / "settings.json",
        runtime_config=runtime,
        provider_store_path=tmp_path / "providers.db",
        provider_master_key_file=tmp_path / "missing-master-key",
        start_processing_worker=False,
    )
    client = TestClient(app)

    listing = client.get("/api/provider-profiles")
    assert listing.status_code == 200
    assert listing.json()["storage_available"] is False
    embedding = listing.json()["profiles"]["embedding"]
    assert embedding["status"] == "unavailable"
    assert embedding["source"] == "default"
    assert embedding["configured"] is False
    assert embedding["model"] is None
    settings = client.get("/api/settings")
    assert settings.status_code == 200
    assert settings.json()["providers"]["embedding"]["configured"] is False
    unavailable_test = client.post("/api/provider-profiles/embedding/test")
    assert unavailable_test.status_code == 503
    assert unavailable_test.json()["detail"]["code"] == "provider_store_unavailable"

    legacy_fallback_called = False

    def legacy_fallback(*args, **kwargs):
        nonlocal legacy_fallback_called
        legacy_fallback_called = True
        raise AssertionError("legacy Provider fallback must not run")

    monkeypatch.setattr(
        "traceable_rag.web_app.provider_from_api_doc", legacy_fallback
    )
    retrieval = client.post(
        "/api/retrieval",
        json={
            "query": "provider",
            "knowledge_base_ids": [DEFAULT_KNOWLEDGE_BASE_ID],
            "mode": "auto",
            "top_k": 1,
        },
    )
    assert retrieval.status_code == 409
    assert legacy_fallback_called is False


def test_hot_update_changes_next_embedding_call_and_endpoint_bound_fingerprint(tmp_path):
    http = FakeProviderHTTP()
    registry = _registry(tmp_path, http_client=http)
    registry.put(
        "embedding",
        {
            "adapter": "openai_compatible",
            "base_url": "https://one.example/v1",
            "model": "model-a",
        },
        secret_update=SECRET,
    )
    dynamic = DynamicEmbeddingProvider(registry)
    dynamic.embed_texts(["first"])
    fingerprint_a = dynamic.provider_name
    assert dynamic.model_name == "model-a"
    assert http.calls[-1][1] == "https://one.example/v1/embeddings"
    assert http.calls[-1][3]["model"] == "model-a"

    registry.put(
        "embedding",
        {"base_url": "https://two.example/v1", "model": "model-b"},
    )
    dynamic.embed_texts(["second"])
    fingerprint_b = dynamic.provider_name
    assert dynamic.model_name == "model-b"
    assert http.calls[-1][1] == "https://two.example/v1/embeddings"
    assert http.calls[-1][3]["model"] == "model-b"
    assert fingerprint_a != fingerprint_b


def test_old_environment_openai_embedding_keeps_v041_index_fingerprint(tmp_path):
    runtime = load_runtime_config(
        root=tmp_path,
        environ={
            "OPENAI_EMBEDDING_BASE_URL": "https://legacy.example/v1",
            "OPENAI_EMBEDDING_API_KEY": "legacy-key",
            "OPENAI_EMBEDDING_MODEL": "legacy-model",
        },
    )
    registry = _registry(tmp_path, runtime=runtime, http_client=FakeProviderHTTP())
    provider = registry.embedding_provider()

    assert provider.provider_name == "openai-compatible"
    assert provider.model_name == "legacy-model"


def test_saved_embedding_hot_update_is_used_by_next_retrieval_request(tmp_path):
    http = FakeProviderHTTP()
    client, app = _make_client(tmp_path, http_client=http)
    client.put(
        "/api/provider-profiles/embedding",
        json={
            "adapter": "openai_compatible",
            "base_url": "https://one.example/v1",
            "model": "model-a",
            "api_key": SECRET,
        },
    )
    registry = app.state.provider_registry
    first_provider = registry.embedding_provider()
    store = EvidenceStore(tmp_path / "traceable.db")
    document = Document(
        id="doc-provider-hot-update",
        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
        original_filename="provider.pdf",
        display_name="Provider hot update",
        file_hash="hash-provider-hot-update",
        source_type="upload",
        source_file_path=None,
        preview_file_path=None,
        mineru_result_dir=None,
        page_count=1,
        processing_status="ready",
        processing_error=None,
        parser_provider="mineru",
        parser_version="vlm",
    )
    block = Block(
        id="doc-provider-hot-update:block",
        document_id=document.id,
        page_idx=0,
        block_idx=0,
        block_type="paragraph",
        text="provider hot update evidence",
        bbox=[1, 1, 10, 10],
        page_size=[100, 100],
    )
    chunk = Chunk(
        id="doc-provider-hot-update:chunk",
        document_id=document.id,
        chunk_idx=0,
        strategy="block",
        block_ids=[block.id],
        title_context=None,
        inferred_heading_path=[],
        chunk_text=block.text or "",
        indexing_text=block.text or "",
        page_range=[0],
        bbox_by_page={"0": [block.bbox]},
        modality_mix=["text"],
        token_count=5,
        strategy_version="v1",
    )

    def persist(provider_name: str, model_name: str) -> None:
        record = IndexRecord(
            id="doc-provider-hot-update:record",
            evidence_id="evidence:provider-hot-update",
            document_id=document.id,
            chunk_id=chunk.id,
            block_ids=[block.id],
            modality="text",
            indexing_text=chunk.indexing_text,
            vector=[1.0, 0.0],
            embedding_provider=provider_name,
            embedding_model=model_name,
            embedding_dimension=2,
            index_version="v1",
        )
        store.replace_document_graph(
            DEFAULT_KNOWLEDGE_BASE_ID, document, [block], [], [chunk], [record]
        )

    persist(first_provider.provider_name, first_provider.model_name)
    first = client.post(
        "/api/retrieval",
        json={
            "query": "provider",
            "knowledge_base_ids": [DEFAULT_KNOWLEDGE_BASE_ID],
            "mode": "auto",
            "top_k": 1,
        },
    )
    assert first.status_code == 200
    assert first.json()["pipeline_used"] == "dense"
    assert http.calls[-1][3]["model"] == "model-a"

    client.put(
        "/api/provider-profiles/embedding",
        json={"base_url": "https://two.example/v1", "model": "model-b"},
    )
    second_provider = registry.embedding_provider()
    persist(second_provider.provider_name, second_provider.model_name)
    second = client.post(
        "/api/retrieval",
        json={
            "query": "provider",
            "knowledge_base_ids": [DEFAULT_KNOWLEDGE_BASE_ID],
            "mode": "auto",
            "top_k": 1,
        },
    )
    assert second.status_code == 200
    assert second.json()["pipeline_used"] == "dense"
    assert http.calls[-1][1] == "https://two.example/v1/embeddings"
    assert http.calls[-1][3]["model"] == "model-b"
