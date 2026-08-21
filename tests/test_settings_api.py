from __future__ import annotations

import json
import inspect
import multiprocessing
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import ANY

import pytest
from fastapi.testclient import TestClient

from traceable_rag.models import Block, Chunk, Document, IndexRecord
import traceable_rag.settings as settings_module
import traceable_rag.web_app as web_app_module
from traceable_rag.store import DEFAULT_KNOWLEDGE_BASE_ID, EvidenceStore
from traceable_rag.web_app import create_app


TEST_SECRET = "sk-test-never-return-this-value"


def test_provider_status_has_no_unused_config_path_parameter():
    assert "config_path" not in inspect.signature(settings_module.provider_status).parameters


def test_safe_settings_enable_embedding_retrieval_by_default():
    settings = settings_module.SafeSettings()

    assert settings.default_retrieval_mode == "embedding"
    assert settings.default_use_embedding is True


def test_explicit_provider_status_does_not_load_unrelated_invalid_dotenv(
    tmp_path, monkeypatch
):
    (tmp_path / ".env").write_bytes(b"SECRET=\xff\n")
    monkeypatch.chdir(tmp_path)

    payload = settings_module.provider_payload(
        api_doc_path=tmp_path / "unused.txt",
        mineru_provider_configured=True,
        embedding_provider_configured=True,
    )

    assert payload == {
        "mineru": {"configured": True},
        "embedding": {"configured": True},
    }


class SlowSettingsStore(settings_module.SettingsStore):
    def _load_unlocked(self):
        loaded = super()._load_unlocked()
        time.sleep(0.15)
        return loaded


def _patch_settings_process(path, changes, start_event, result_queue):
    start_event.wait(timeout=5)
    try:
        loaded = SlowSettingsStore(path).patch(changes)
    except Exception as exc:
        result_queue.put(("error", type(exc).__name__))
    else:
        result_queue.put(("ok", loaded.settings.payload()))


def _load_settings_process(path, result_queue):
    try:
        loaded = settings_module.SettingsStore(path)._load_unlocked()
    except Exception as exc:
        result_queue.put(("error", type(exc).__name__))
    else:
        result_queue.put(("ok", loaded.status))


def _document(document_id: str, *, status: str, error: str | None = None) -> Document:
    return Document(
        id=document_id,
        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
        original_filename=f"{document_id}.docx",
        display_name=f"{document_id} display",
        file_hash=f"hash-{document_id}",
        source_type="upload",
        source_file_path=f"/private/source/{document_id}.docx",
        preview_file_path=f"/private/preview/{document_id}.pdf",
        mineru_result_dir=f"/private/mineru/{document_id}",
        page_count=1,
        processing_status=status,
        processing_error=error,
        parser_provider="mineru",
        parser_version="vlm",
    )


def _add_runtime_rows(store: EvidenceStore) -> None:
    ready = _document("ready-doc", status="ready")
    block = Block(
        id="ready-doc:block",
        document_id=ready.id,
        page_idx=0,
        block_idx=0,
        block_type="paragraph",
        text="编译器阶段说明",
    )
    chunk = Chunk(
        id="ready-doc:chunk",
        document_id=ready.id,
        chunk_idx=0,
        strategy="block_window_with_title_hints",
        block_ids=[block.id],
        title_context=None,
        inferred_heading_path=[],
        chunk_text=block.text or "",
        indexing_text=block.text or "",
        page_range=[0],
        bbox_by_page={},
        modality_mix=["text"],
        token_count=8,
        strategy_version="v1",
    )
    record = IndexRecord(
        id="ready-doc:record",
        evidence_id="evidence:ready-doc",
        document_id=ready.id,
        chunk_id=chunk.id,
        block_ids=[block.id],
        modality="text",
        indexing_text=chunk.indexing_text,
        vector=None,
        embedding_provider=None,
        embedding_model=None,
        embedding_dimension=None,
        index_version="v1",
    )
    store.replace_document_graph(
        DEFAULT_KNOWLEDGE_BASE_ID,
        ready,
        [block],
        [],
        [chunk],
        [record],
    )
    store.upsert_document(
        _document(
            "failed-doc",
            status="failed",
            error=f"conversion failed with {TEST_SECRET} at /private/source.docx",
        )
    )


def _make_client(tmp_path: Path, **overrides):
    db_path = tmp_path / "traceable.db"
    settings_path = tmp_path / "settings.json"
    app = create_app(
        db_path=db_path,
        managed_root=tmp_path / "managed",
        settings_path=settings_path,
        **overrides,
    )
    return TestClient(app), EvidenceStore(db_path), settings_path


def test_get_settings_returns_only_safe_configuration_and_runtime_summaries(tmp_path):
    client, store, _ = _make_client(tmp_path)
    _add_runtime_rows(store)

    response = client.get("/api/settings")

    assert response.status_code == 200
    payload = response.json()
    assert payload["settings"] == {
        "default_top_k": 5,
        "default_retrieval_mode": "embedding",
        "default_use_embedding": True,
        "public_base_url": "http://127.0.0.1:8000",
        "default_knowledge_base_id": None,
    }
    assert payload["runtime"]["service"] == {"healthy": True}
    assert payload["runtime"]["counts"] == {
        "knowledge_bases": 1,
        "documents": 2,
        "chunks": 1,
        "index_records": 1,
    }
    assert payload["runtime"]["recent_failures"] == [
        {
            "document_id": "failed-doc",
            "display_name": "failed-doc display",
            "summary": "文档处理失败，请检查服务日志后重试。",
            "updated_at": ANY,
        }
    ]
    serialized = response.text.casefold()
    for forbidden in (TEST_SECRET, "api_key", "authorization", "processing_error"):
        assert forbidden.casefold() not in serialized


def test_patch_settings_accepts_partial_whitelist_and_persists_across_app_restart(tmp_path):
    client, store, settings_path = _make_client(tmp_path)
    target_kb = client.post(
        "/api/knowledge-bases",
        json={"name": "默认示例库", "description": "设置页默认范围"},
    ).json()["knowledge_base"]

    response = client.patch(
        "/api/settings",
        json={
            "default_top_k": 12,
            "default_retrieval_mode": "embedding",
            "default_use_embedding": True,
            "public_base_url": "https://rag.example.test/",
            "default_knowledge_base_id": target_kb["id"],
        },
    )

    assert response.status_code == 200
    assert response.json()["settings"] == {
        "default_top_k": 12,
        "default_retrieval_mode": "embedding",
        "default_use_embedding": True,
        "public_base_url": "https://rag.example.test",
        "default_knowledge_base_id": target_kb["id"],
    }
    assert settings_path.stat().st_mode & 0o777 == 0o600
    assert json.loads(settings_path.read_text(encoding="utf-8")) == response.json()[
        "settings"
    ]

    restarted = TestClient(
        create_app(
            db_path=store.db_path,
            managed_root=tmp_path / "managed",
            settings_path=settings_path,
        )
    )
    assert restarted.get("/api/settings").json()["settings"] == response.json()[
        "settings"
    ]


def test_patch_settings_rejects_conflicting_retrieval_defaults(tmp_path):
    client, _, _ = _make_client(tmp_path)

    response = client.patch(
        "/api/settings",
        json={
            "default_retrieval_mode": "keyword",
            "default_use_embedding": True,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_settings"


@pytest.mark.parametrize(
    "changes",
    [
        {"default_retrieval_mode": "keyword"},
        {"default_use_embedding": False},
    ],
    ids=["mode-only", "boolean-only"],
)
def test_patch_either_retrieval_default_keeps_settings_consistent(tmp_path, changes):
    client, _, _ = _make_client(tmp_path)
    patched = client.patch("/api/settings", json=changes)

    assert patched.status_code == 200
    assert patched.json()["settings"]["default_retrieval_mode"] == "keyword"
    assert patched.json()["settings"]["default_use_embedding"] is False


def test_two_settings_store_instances_merge_concurrent_partial_updates(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_module.SettingsStore(settings_path).patch({"default_top_k": 5})
    stores = [SlowSettingsStore(settings_path), SlowSettingsStore(settings_path)]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda item: item[0].patch(item[1]),
                zip(
                    stores,
                    (
                        {"default_top_k": 17},
                        {"public_base_url": "https://two-instances.example.test"},
                    ),
                ),
            )
        )

    assert len(results) == 2
    stored = settings_module.SettingsStore(settings_path).load().settings
    assert stored.default_top_k == 17
    assert stored.public_base_url == "https://two-instances.example.test"


def test_settings_patch_merges_different_fields_across_processes(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_module.SettingsStore(settings_path).patch({"default_top_k": 5})
    context = multiprocessing.get_context("fork")
    start_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_patch_settings_process,
            args=(settings_path, changes, start_event, result_queue),
        )
        for changes in (
            {"default_top_k": 23},
            {"public_base_url": "https://multiprocess.example.test"},
        )
    ]
    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(timeout=5)
        assert process.exitcode == 0
    assert [result_queue.get(timeout=1)[0] for _process in processes] == ["ok", "ok"]

    stored = settings_module.SettingsStore(settings_path).load().settings
    assert stored.default_top_k == 23
    assert stored.public_base_url == "https://multiprocess.example.test"


def test_settings_lock_file_is_stable_regular_and_private(tmp_path):
    store = settings_module.SettingsStore(tmp_path / "settings.json")

    store.load()
    first_stat = store.lock_path.stat()
    store.patch({"default_top_k": 11})
    second_stat = store.lock_path.stat()

    assert first_stat.st_ino == second_stat.st_ino
    assert second_stat.st_mode & 0o777 == 0o600
    assert store.lock_path.is_file()


@pytest.mark.parametrize(
    "sensitive_field",
    ["api_key", "secret", "authorization", "mineru_token", "embedding_key"],
)
def test_patch_settings_rejects_sensitive_and_unknown_fields(tmp_path, sensitive_field):
    client, _, _ = _make_client(tmp_path)

    response = client.patch(
        "/api/settings",
        json={"default_top_k": 8, sensitive_field: TEST_SECRET},
    )

    assert response.status_code == 422
    assert TEST_SECRET not in response.text
    assert client.get("/api/settings").json()["settings"]["default_top_k"] == 5


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/settings", "PATCH"),
        ("/api/settings/diagnostics", "POST"),
    ],
)
@pytest.mark.parametrize(
    ("body", "expected_status"),
    [
        (json.dumps([TEST_SECRET]), 422),
        (json.dumps(TEST_SECRET), 422),
        ("[" * 10000 + json.dumps(TEST_SECRET) + "]" * 10000, 422),
        (f'{{"Authorization":"Bearer {TEST_SECRET}"', 422),
        (
            json.dumps(
                {
                    "target": "service",
                    "api_key": TEST_SECRET,
                    "padding": "x" * (70 * 1024),
                }
            ),
            413,
        ),
    ],
)
def test_settings_json_endpoints_reject_unsafe_bodies_without_echoing_input(
    tmp_path, path, method, body, expected_status
):
    client, _, _ = _make_client(tmp_path)

    response = client.request(
        method,
        path,
        content=body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == expected_status
    serialized = response.text.casefold()
    for forbidden in (TEST_SECRET, "api_key", "authorization", "bearer"):
        assert forbidden.casefold() not in serialized


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    [
        ({}, 422),
        ({"default_top_k": 0}, 422),
        ({"default_top_k": 51}, 422),
        ({"default_top_k": None}, 422),
        ({"default_retrieval_mode": None}, 422),
        ({"default_use_embedding": None}, 422),
        ({"public_base_url": None}, 422),
        ({"trace_retention_policy": None}, 422),
        ({"default_retrieval_mode": "hybrid"}, 422),
        ({"trace_retention_policy": "7_days"}, 422),
        ({"public_base_url": "file:///private/config"}, 422),
        ({"public_base_url": "https://user:secret@example.test"}, 422),
        ({"default_knowledge_base_id": "missing"}, 404),
    ],
)
def test_patch_settings_validates_safe_ranges_enums_urls_and_default_scope(
    tmp_path, payload, expected_status
):
    client, _, _ = _make_client(tmp_path)

    response = client.patch("/api/settings", json=payload)

    assert response.status_code == expected_status
    assert "secret" not in response.text.casefold()


@pytest.mark.parametrize(
    "public_base_url",
    [
        "https://example.test/\x00tail",
        "https://example.test/\x1ftail",
        "https://example.test/\x7ftail",
        "https://example.test:bad",
        "https://example.test:70000",
        "https://example.test:0",
        "https://exa mple.test",
        "https://bad..example.test",
        "http://[::1",
        "http://::1:8000",
    ],
)
def test_patch_settings_rejects_control_characters_and_invalid_network_origins(
    tmp_path, public_base_url
):
    client, _, _ = _make_client(tmp_path)

    response = client.patch(
        "/api/settings",
        json={"public_base_url": public_base_url},
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://127.0.0.1:8000/", "http://127.0.0.1:8000"),
        ("http://192.168.1.50", "http://192.168.1.50"),
        ("http://[::1]:8000/", "http://[::1]:8000"),
        ("https://rag.internal", "https://rag.internal"),
    ],
)
def test_public_base_url_allows_valid_loopback_private_and_ipv6_origins(
    value, expected
):
    assert settings_module.normalize_public_base_url(value) == expected


def test_provider_status_uses_safe_configuration_signals_without_reading_or_describing_sources(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    provider_config = tmp_path / "provider-config.txt"
    provider_config.write_text(f"API-Key: {TEST_SECRET}", encoding="utf-8")
    original_read_text = Path.read_text

    def forbid_provider_file_read(path: Path, *args, **kwargs):
        if path == provider_config:
            raise AssertionError("provider status must not read configuration contents")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", forbid_provider_file_read)
    client, _, _ = _make_client(tmp_path, api_doc_path=provider_config)

    response = client.get("/api/settings")

    assert response.status_code == 200
    assert response.json()["providers"] == {
        "mineru": {"configured": False},
        "embedding": {"configured": False},
    }
    assert TEST_SECRET not in response.text


def test_provider_status_ignores_blank_environment_values_and_is_provider_specific(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "ambiguous-provider-config.txt"
    config_path.write_text("provider configuration exists", encoding="utf-8")
    for name in (
        "MINERU_TOKEN",
        "MINERU_API_KEY",
        "TRACEABLE_RAG_EMBEDDING_API_KEY",
        "TRACEABLE_RAG_EMBEDDING_BASE_URL",
        "TRACEABLE_RAG_EMBEDDING_MODEL",
    ):
        monkeypatch.setenv(name, "   ")
    master_key_file = tmp_path / "provider-master-key"
    master_key_file.write_bytes(b"k" * 32)
    client, _, _ = _make_client(
        tmp_path,
        api_doc_path=config_path,
        provider_store_path=tmp_path / "providers.db",
        provider_master_key_file=master_key_file,
    )

    blank = client.get("/api/settings").json()["providers"]
    monkeypatch.setenv("MINERU_TOKEN", "mineru-configured")
    mineru_only = client.get("/api/settings").json()["providers"]
    monkeypatch.setenv("TRACEABLE_RAG_EMBEDDING_API_KEY", "embedding-key")
    monkeypatch.setenv("TRACEABLE_RAG_EMBEDDING_BASE_URL", "https://embed.test")
    monkeypatch.setenv("TRACEABLE_RAG_EMBEDDING_MODEL", "embed-model")
    both = client.get("/api/settings").json()["providers"]

    assert blank == {
        "mineru": {"configured": False},
        "embedding": {"configured": False},
    }
    assert mineru_only == {
        "mineru": {"configured": True},
        "embedding": {"configured": False},
    }
    assert both == {
        "mineru": {"configured": True},
        "embedding": {"configured": True},
    }


def test_provider_status_uses_dotenv_aliases_but_ignores_general_openai_llm_names(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    for name in (
        "MINERU_TOKEN",
        "MINERU_API_KEY",
        "OPENAI_EMBEDDING_BASE_URL",
        "OPENAI_EMBEDDING_API_KEY",
        "OPENAI_EMBEDDING_MODEL",
        "TRACEABLE_RAG_EMBEDDING_BASE_URL",
        "TRACEABLE_RAG_EMBEDDING_API_KEY",
        "TRACEABLE_RAG_EMBEDDING_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(
        "\n".join(
            (
                "MINERU_API_KEY=legacy-mineru-placeholder",
                "OPENAI_EMBEDDING_BASE_URL=https://embedding.example/v1",
                "OPENAI_EMBEDDING_API_KEY=embedding-placeholder",
                "OPENAI_EMBEDDING_MODEL=embedding-model",
                "OPENAI_API_KEY=ignored-llm-key",
                "OPENAI_BASE_URL=https://ignored-llm.example/v1",
                "OPENAI_LLM_MODEL=ignored-llm-model",
            )
        ),
        encoding="utf-8",
    )

    configured = settings_module.provider_payload(
        api_doc_path=tmp_path / "missing-provider-config.txt",
        mineru_provider_configured=None,
        embedding_provider_configured=None,
    )

    (tmp_path / ".env").write_text(
        "\n".join(
            (
                "OPENAI_API_KEY=ignored-llm-key",
                "OPENAI_BASE_URL=https://ignored-llm.example/v1",
                "OPENAI_LLM_MODEL=ignored-llm-model",
            )
        ),
        encoding="utf-8",
    )
    ignored_llm = settings_module.provider_payload(
        api_doc_path=tmp_path / "missing-provider-config.txt",
        mineru_provider_configured=None,
        embedding_provider_configured=None,
    )

    assert configured == {
        "mineru": {"configured": True},
        "embedding": {"configured": True},
    }
    assert ignored_llm == {
        "mineru": {"configured": False},
        "embedding": {"configured": False},
    }
    assert "llm" not in repr(configured).lower()
    assert "ignored-llm-key" not in repr(configured)


def test_explicit_provider_injection_status_takes_precedence_without_exposing_provider_objects(
    tmp_path
):
    provider = object()
    client, _, _ = _make_client(
        tmp_path,
        api_doc_path=tmp_path / "missing-provider-config.txt",
        embedding_provider=provider,
        mineru_provider_configured=True,
    )

    response = client.get("/api/settings")

    assert response.json()["providers"] == {
        "mineru": {"configured": True},
        "embedding": {"configured": True},
    }
    assert repr(provider) not in response.text


def test_diagnostics_run_only_on_demand_and_return_success_or_actionable_sanitized_failure(
    tmp_path
):
    calls: list[str] = []

    def mineru_check():
        calls.append("mineru")

    def embedding_check():
        calls.append("embedding")
        raise RuntimeError(f"upstream rejected {TEST_SECRET}")

    client, _, _ = _make_client(
        tmp_path,
        mineru_provider_configured=True,
        embedding_provider=object(),
        diagnostic_checks={
            "mineru": mineru_check,
            "embedding": embedding_check,
        },
    )

    assert client.get("/api/settings").status_code == 200
    assert calls == []

    success = client.post(
        "/api/settings/diagnostics", json={"target": "mineru"}
    )
    failure = client.post(
        "/api/settings/diagnostics", json={"target": "embedding"}
    )

    assert success.status_code == 200
    assert success.json()["diagnostic"] == {
        "target": "mineru",
        "ok": True,
        "code": "ok",
        "message": "MinerU 连接测试成功。",
        "action": None,
    }
    assert failure.status_code == 200
    assert failure.json()["diagnostic"]["target"] == "embedding"
    assert failure.json()["diagnostic"]["ok"] is False
    assert failure.json()["diagnostic"]["code"] == "diagnostic_failed"
    assert failure.json()["diagnostic"]["action"]
    assert TEST_SECRET not in failure.text
    assert calls == ["mineru", "embedding"]


def test_unconfigured_or_unavailable_diagnostic_is_actionable(tmp_path):
    client, _, _ = _make_client(
        tmp_path,
        api_doc_path=tmp_path / "missing-provider-config.txt",
        mineru_provider_configured=False,
        embedding_provider=object(),
    )

    unconfigured = client.post(
        "/api/settings/diagnostics", json={"target": "mineru"}
    ).json()["diagnostic"]
    unavailable = client.post(
        "/api/settings/diagnostics", json={"target": "embedding"}
    ).json()["diagnostic"]

    assert unconfigured["code"] == "provider_not_configured"
    assert unconfigured["ok"] is False
    assert unconfigured["action"]
    assert unavailable["code"] == "diagnostic_not_available"
    assert unavailable["ok"] is False
    assert unavailable["action"]


def test_service_diagnostic_returns_sanitized_failure_when_runtime_stats_raise(
    tmp_path, monkeypatch
):
    client, _, _ = _make_client(tmp_path)

    def fail_runtime(_store):
        raise sqlite3.DatabaseError(f"database failed with {TEST_SECRET}")

    monkeypatch.setattr(settings_module, "runtime_payload", fail_runtime)
    safe_client = TestClient(client.app, raise_server_exceptions=False)

    response = safe_client.post(
        "/api/settings/diagnostics", json={"target": "service"}
    )

    assert response.status_code == 200
    assert response.json()["diagnostic"] == {
        "target": "service",
        "ok": False,
        "code": "service_unavailable",
        "message": "服务或数据库状态检查失败。",
        "action": "检查数据库文件权限、完整性和服务日志后重试。",
    }
    assert TEST_SECRET not in response.text


def test_service_diagnostic_sanitizes_real_store_construction_failure(tmp_path):
    bad_db_path = tmp_path / f"{TEST_SECRET}-database-directory"
    bad_db_path.mkdir()
    app = create_app(
        db_path=bad_db_path,
        managed_root=tmp_path / "managed",
        settings_path=tmp_path / "settings.json",
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/settings/diagnostics", json={"target": "service"}
    )

    assert response.status_code == 200
    assert response.json()["diagnostic"] == {
        "target": "service",
        "ok": False,
        "code": "service_unavailable",
        "message": "服务或数据库状态检查失败。",
        "action": "检查数据库文件权限、完整性和服务日志后重试。",
    }
    assert TEST_SECRET not in response.text


@pytest.mark.parametrize("target", ["mineru", "embedding"])
def test_provider_diagnostics_do_not_construct_unrelated_failed_store(
    tmp_path, target
):
    bad_db_path = tmp_path / f"{TEST_SECRET}-database-directory"
    bad_db_path.mkdir()
    calls: list[str] = []

    def check():
        calls.append(target)

    app = create_app(
        db_path=bad_db_path,
        managed_root=tmp_path / "managed",
        settings_path=tmp_path / "settings.json",
        mineru_provider_configured=True,
        embedding_provider=object(),
        diagnostic_checks={target: check},
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/settings/diagnostics", json={"target": target}
    )

    assert response.status_code == 200
    assert response.json()["diagnostic"] == {
        "target": target,
        "ok": True,
        "code": "ok",
        "message": f"{'MinerU' if target == 'mineru' else 'Embedding'} 连接测试成功。",
        "action": None,
    }
    assert calls == [target]
    assert TEST_SECRET not in response.text


def test_get_settings_returns_safe_degraded_runtime_when_stats_raise(
    tmp_path, monkeypatch
):
    client, _, _ = _make_client(tmp_path)

    def fail_runtime(_store):
        raise sqlite3.DatabaseError(f"database failed with {TEST_SECRET}")

    monkeypatch.setattr(web_app_module, "runtime_payload", fail_runtime)
    safe_client = TestClient(client.app, raise_server_exceptions=False)

    response = safe_client.get("/api/settings")

    assert response.status_code == 200
    assert response.json()["runtime"] == {
        "service": {"healthy": False},
        "counts": {
            "knowledge_bases": 0,
            "documents": 0,
            "chunks": 0,
            "index_records": 0,
        },
        "recent_failures": [],
    }
    assert TEST_SECRET not in response.text


def test_skill_status_distinguishes_repository_template_from_user_installation(tmp_path):
    template = tmp_path / "skill-template" / "SKILL.md"
    installed = tmp_path / "installed-skill" / "SKILL.md"
    template.parent.mkdir(parents=True)
    template.write_text("# template", encoding="utf-8")
    client, _, _ = _make_client(
        tmp_path,
        skill_template_path=template,
        skill_install_path=installed,
    )

    first = client.get("/api/settings").json()["skill"]

    assert first == {"template_available": True, "installed": False}
    installed.parent.mkdir(parents=True)
    installed.write_text("# installed", encoding="utf-8")
    second = client.get("/api/settings").json()["skill"]
    assert second == {"template_available": True, "installed": True}


def test_settings_storage_is_atomic_merges_concurrent_partial_updates_and_recovers_from_bad_json(
    tmp_path
):
    client, _, settings_path = _make_client(tmp_path)
    settings_path.write_text(f'{{"default_top_k": 9, "secret": "{TEST_SECRET}"', encoding="utf-8")

    degraded = client.get("/api/settings")

    assert degraded.status_code == 200
    assert degraded.json()["settings"]["default_top_k"] == 5
    assert degraded.json()["persistence"]["status"] == "degraded"
    assert TEST_SECRET not in degraded.text

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                lambda item: client.patch("/api/settings", json=item),
                [
                    {"default_top_k": 17},
                    {"public_base_url": "https://concurrent.example.test/"},
                ],
            )
        )

    assert [response.status_code for response in responses] == [200, 200]
    stored = json.loads(settings_path.read_text(encoding="utf-8"))
    assert stored["default_top_k"] == 17
    assert stored["public_base_url"] == "https://concurrent.example.test"
    assert not list(settings_path.parent.glob(f".{settings_path.name}.*.tmp"))
    repaired = client.get("/api/settings")
    assert repaired.json()["persistence"]["status"] == "ok"


def test_settings_load_degrades_for_fifo_without_blocking(tmp_path):
    settings_path = tmp_path / "settings.json"
    os.mkfifo(settings_path)
    context = multiprocessing.get_context("fork")
    result_queue = context.Queue()
    process = context.Process(
        target=_load_settings_process,
        args=(settings_path, result_queue),
    )
    process.start()
    process.join(timeout=1)
    if process.is_alive():
        process.terminate()
        process.join(timeout=2)

    assert process.exitcode == 0
    assert result_queue.get(timeout=1) == ("ok", "degraded")


@pytest.mark.parametrize("kind", ["directory", "symlink", "device"])
def test_settings_load_degrades_for_non_regular_files(tmp_path, kind):
    if kind == "device":
        path = Path("/dev/null")
    else:
        path = tmp_path / "settings.json"
        if kind == "directory":
            path.mkdir()
        else:
            target = tmp_path / "target.json"
            target.write_text("{}", encoding="utf-8")
            path.symlink_to(target)

    loaded = settings_module.SettingsStore(path)._load_unlocked()

    assert loaded.status == "degraded"
    assert loaded.settings == settings_module.SafeSettings()


def test_settings_load_degrades_for_deep_json_without_crashing_api(tmp_path):
    client, _, settings_path = _make_client(tmp_path)
    settings_path.write_text("[" * 20_000 + "]" * 20_000, encoding="utf-8")
    safe_client = TestClient(client.app, raise_server_exceptions=False)

    response = safe_client.get("/api/settings")

    assert response.status_code == 200
    assert response.json()["persistence"]["status"] == "degraded"


def test_deleting_selected_default_knowledge_base_clears_setting(tmp_path):
    client, _, _ = _make_client(tmp_path)
    knowledge_base = client.post(
        "/api/knowledge-bases",
        json={"name": "待删除默认库"},
    ).json()["knowledge_base"]
    assert client.patch(
        "/api/settings",
        json={"default_knowledge_base_id": knowledge_base["id"]},
    ).status_code == 200

    deleted = client.delete(f"/api/knowledge-bases/{knowledge_base['id']}")

    assert deleted.status_code == 200
    assert (
        client.get("/api/settings").json()["settings"]["default_knowledge_base_id"]
        is None
    )


def test_patch_default_scope_cannot_race_with_knowledge_base_delete(
    tmp_path, monkeypatch
):
    patch_entered = threading.Event()
    release_patch = threading.Event()

    class PausingPatchSettingsStore(settings_module.SettingsStore):
        def patch(self, changes, **kwargs):
            if changes.get("default_knowledge_base_id") is not None:
                patch_entered.set()
                assert release_patch.wait(timeout=5)
            return super().patch(changes, **kwargs)

    monkeypatch.setattr(web_app_module, "SettingsStore", PausingPatchSettingsStore)
    client, _, _ = _make_client(tmp_path)
    knowledge_base = client.post(
        "/api/knowledge-bases",
        json={"name": "并发默认库"},
    ).json()["knowledge_base"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        patch_future = executor.submit(
            client.patch,
            "/api/settings",
            json={"default_knowledge_base_id": knowledge_base["id"]},
        )
        assert patch_entered.wait(timeout=5)
        deleted = client.delete(f"/api/knowledge-bases/{knowledge_base['id']}")
        release_patch.set()
        patched = patch_future.result(timeout=5)

    assert deleted.status_code == 200
    assert patched.status_code == 404
    assert (
        client.get("/api/settings").json()["settings"]["default_knowledge_base_id"]
        is None
    )


def test_settings_write_failure_prevents_default_knowledge_base_delete(
    tmp_path, monkeypatch
):
    client, store, _ = _make_client(tmp_path)
    knowledge_base = client.post(
        "/api/knowledge-bases",
        json={"name": "写入失败保护库"},
    ).json()["knowledge_base"]
    assert client.patch(
        "/api/settings",
        json={"default_knowledge_base_id": knowledge_base["id"]},
    ).status_code == 200

    monkeypatch.setattr(
        settings_module.SettingsStore,
        "_write_unlocked",
        lambda self, settings: (_ for _ in ()).throw(OSError("disk full")),
    )

    deleted = client.delete(f"/api/knowledge-bases/{knowledge_base['id']}")

    assert deleted.status_code == 500
    assert store.get_knowledge_base(knowledge_base["id"]) is not None


def test_settings_developer_reference_contains_only_safe_openapi_and_endpoint_examples(tmp_path):
    client, _, _ = _make_client(tmp_path)

    response = client.get("/api/settings")

    assert response.status_code == 200
    developer = response.json()["developer"]
    assert developer["openapi_url"] == "/openapi.json"
    assert {example["path"] for example in developer["examples"]} == {
        "/api/retrieval",
        "/api/sources/{source_id}/context?before=2&after=2",
        "/api/health",
    }
    retrieval_example = next(
        example
        for example in developer["examples"]
        if example["path"] == "/api/retrieval"
    )
    assert retrieval_example["body"]["query"] == (
        "本季度营业收入和净利润分别是多少？"
    )
    serialized = json.dumps(developer, ensure_ascii=False).casefold()
    for forbidden in ("api_key", "authorization", "bearer", TEST_SECRET.casefold()):
        assert forbidden not in serialized
