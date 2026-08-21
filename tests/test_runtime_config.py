from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


def _runtime_config_module():
    try:
        return importlib.import_module("traceable_rag.runtime_config")
    except ModuleNotFoundError as exc:
        raise AssertionError("runtime configuration loader is not implemented") from exc


def test_process_environment_overrides_dotenv_and_defaults(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "\n".join(
            (
                "MINERU_TOKEN=dotenv-mineru-token",
                "OPENAI_EMBEDDING_BASE_URL=https://dotenv.example/v1",
                "OPENAI_EMBEDDING_API_KEY=dotenv-embedding-key",
                "OPENAI_EMBEDDING_MODEL=dotenv-model",
                "OPENAI_EMBEDDING_QUERY_PROMPT=Instruct: retrieve relevant passages\\nQuery:",
                "RERANKER_MODEL=dotenv-reranker",
                "TRACEABLE_RAG_DATABASE_PATH=dotenv/database.db",
            )
        ),
        encoding="utf-8",
    )

    config = _runtime_config_module().load_runtime_config(
        root=tmp_path,
        environ={
            "MINERU_TOKEN": "process-mineru-token",
            "OPENAI_EMBEDDING_API_KEY": "process-embedding-key",
            "TRACEABLE_RAG_DATABASE_PATH": "process/database.db",
        },
    )

    assert config.mineru_token == "process-mineru-token"
    assert config.embedding_base_url == "https://dotenv.example/v1"
    assert config.embedding_api_key == "process-embedding-key"
    assert config.embedding_model == "dotenv-model"
    assert config.embedding_query_prompt == "Instruct: retrieve relevant passages\\nQuery:"
    assert config.reranker_model == "dotenv-reranker"
    assert config.database_path.as_posix() == "process/database.db"
    assert config.managed_storage_root.as_posix() == "storage/managed"


def test_dotenv_example_documents_supported_roles_with_empty_defaults():
    example_path = Path(__file__).parents[1] / ".env.example"

    assert example_path.is_file()
    text = example_path.read_text(encoding="utf-8")
    for name in (
        "MINERU_TOKEN",
        "OPENAI_EMBEDDING_BASE_URL",
        "OPENAI_EMBEDDING_API_KEY",
        "OPENAI_EMBEDDING_MODEL",
        "OPENAI_EMBEDDING_QUERY_PROMPT",
        "TRACEABLE_RAG_RERANKER_BASE_URL",
        "TRACEABLE_RAG_RERANKER_API_KEY",
        "TRACEABLE_RAG_RERANKER_MODEL",
        "TRACEABLE_RAG_LLM_BASE_URL",
        "TRACEABLE_RAG_LLM_API_KEY",
        "TRACEABLE_RAG_LLM_MODEL",
        "TRACEABLE_RAG_DATABASE_PATH",
        "TRACEABLE_RAG_MANAGED_STORAGE_ROOT",
        "TRACEABLE_RAG_SETTINGS_PATH",
        "TRACEABLE_RAG_STATIC_DIR",
    ):
        assert f"{name}=" in text
    assert "OPENAI_API_KEY=" not in text
    assert "OPENAI_BASE_URL=" not in text
    assert "OPENAI_LLM_MODEL=" not in text
    assert "replace-with" not in text
    assignments = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in text.splitlines()
        if line and not line.startswith("#") and "=" in line
    }
    provider_names = [
        name
        for name in assignments
        if any(role in name for role in ("MINERU", "EMBEDDING", "RERANKER", "LLM"))
    ]
    assert provider_names
    assert all(assignments[name] == "" for name in provider_names)
    assert "Inline comments are not supported" in text
    assert "escape sequences are not processed" in text


def test_canonical_aliases_win_conflicts_and_process_legacy_beats_dotenv(tmp_path):
    (tmp_path / ".env").write_text(
        "\n".join(
            (
                "MINERU_TOKEN=dotenv-canonical-mineru",
                "MINERU_API_KEY=dotenv-legacy-mineru",
                "OPENAI_EMBEDDING_BASE_URL=https://dotenv-canonical.example/v1",
                "TRACEABLE_RAG_EMBEDDING_BASE_URL=https://dotenv-legacy.example/v1",
                "OPENAI_EMBEDDING_API_KEY=dotenv-canonical-key",
                "OPENAI_EMBEDDING_MODEL=dotenv-canonical-model",
            )
        ),
        encoding="utf-8",
    )

    config = _runtime_config_module().load_runtime_config(
        root=tmp_path,
        environ={
            "MINERU_TOKEN": "process-canonical-mineru",
            "MINERU_API_KEY": "process-legacy-mineru",
            "OPENAI_EMBEDDING_BASE_URL": "https://process-canonical.example/v1",
            "TRACEABLE_RAG_EMBEDDING_BASE_URL": "https://process-legacy.example/v1",
            "OPENAI_EMBEDDING_API_KEY": "process-canonical-key",
            "TRACEABLE_RAG_EMBEDDING_API_KEY": "process-legacy-key",
            "OPENAI_EMBEDDING_MODEL": "process-canonical-model",
            "TRACEABLE_RAG_EMBEDDING_MODEL": "process-legacy-model",
            "MINERU_AK": "ignored-ak",
            "MINERU_SK": "ignored-sk",
        },
    )

    assert config.mineru_token == "process-canonical-mineru"
    assert config.embedding_base_url == "https://process-canonical.example/v1"
    assert config.embedding_api_key == "process-canonical-key"
    assert config.embedding_model == "process-canonical-model"
    assert "ignored" not in repr(config)

    process_legacy = _runtime_config_module().load_runtime_config(
        root=tmp_path,
        environ={
            "TRACEABLE_RAG_EMBEDDING_BASE_URL": "https://process-legacy.example/v1",
            "TRACEABLE_RAG_EMBEDDING_API_KEY": "process-legacy-key",
            "TRACEABLE_RAG_EMBEDDING_MODEL": "process-legacy-model",
        },
    )

    assert process_legacy.embedding_base_url == "https://process-legacy.example/v1"
    assert process_legacy.embedding_api_key == "process-legacy-key"
    assert process_legacy.embedding_model == "process-legacy-model"


def test_dotenv_parser_supports_comments_export_and_simple_quotes():
    parsed = _runtime_config_module().parse_dotenv(
        "# comment\nexport FIRST='one'\nSECOND=\"two\"\nTHIRD = three\n"
    )

    assert parsed == {"FIRST": "one", "SECOND": "two", "THIRD": "three"}


def test_runtime_config_repr_hides_provider_secrets(tmp_path):
    mineru_secret = "real-mineru-secret-for-repr-test"
    embedding_secret = "real-embedding-secret-for-repr-test"

    config = _runtime_config_module().load_runtime_config(
        root=tmp_path,
        environ={
            "MINERU_TOKEN": mineru_secret,
            "OPENAI_EMBEDDING_API_KEY": embedding_secret,
        },
    )

    rendered = repr(config)
    assert mineru_secret not in rendered
    assert embedding_secret not in rendered


def test_runtime_config_parses_exact_trusted_hosts_and_rejects_wildcards(tmp_path):
    runtime_config = _runtime_config_module()
    config = runtime_config.load_runtime_config(
        root=tmp_path,
        environ={
            "TRACEABLE_RAG_TRUSTED_HOSTS": "127.0.0.1, localhost, ::1,localhost",
        },
    )

    assert config.trusted_hosts == ("127.0.0.1", "localhost", "::1")

    with pytest.raises(runtime_config.RuntimeConfigError, match="exact IP"):
        runtime_config.load_runtime_config(
            root=tmp_path,
            environ={"TRACEABLE_RAG_TRUSTED_HOSTS": "*.example.com"},
        )


def test_dotenv_reader_rejects_symlink_without_reading_target(tmp_path):
    secret = "symlink-target-secret"
    target = tmp_path / "secret-target"
    target.write_text(f"MINERU_TOKEN={secret}\n", encoding="utf-8")
    (tmp_path / ".env").symlink_to(target)

    with pytest.raises(Exception) as error:
        _runtime_config_module().load_runtime_config(root=tmp_path, environ={})

    assert type(error.value).__name__ == "RuntimeConfigError"
    assert secret not in str(error.value)
    assert str(target) not in str(error.value)


def test_dotenv_reader_rejects_fifo_without_blocking(tmp_path):
    fifo = tmp_path / ".env"
    os.mkfifo(fifo)

    with pytest.raises(Exception) as error:
        _runtime_config_module().load_runtime_config(root=tmp_path, environ={})

    assert type(error.value).__name__ == "RuntimeConfigError"
    assert "regular file" in str(error.value)


def test_dotenv_reader_rejects_files_larger_than_64_kib(tmp_path):
    (tmp_path / ".env").write_bytes(b"A=" + b"x" * (64 * 1024))

    with pytest.raises(Exception) as error:
        _runtime_config_module().load_runtime_config(root=tmp_path, environ={})

    assert type(error.value).__name__ == "RuntimeConfigError"
    assert "64 KiB" in str(error.value)


def test_dotenv_reader_sanitizes_operating_system_errors(tmp_path, monkeypatch):
    secret = "secret-path-fragment"
    (tmp_path / ".env").write_text("SAFE=value\n", encoding="utf-8")
    runtime_config = _runtime_config_module()

    def fail_open(*_args, **_kwargs):
        raise OSError(f"failed at /private/{secret}/.env")

    monkeypatch.setattr(runtime_config.os, "open", fail_open)

    with pytest.raises(runtime_config.RuntimeConfigError) as error:
        runtime_config.load_runtime_config(root=tmp_path, environ={})

    assert secret not in str(error.value)
    assert "/private/" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_dotenv_unicode_error_has_no_exception_chain_or_content(tmp_path):
    secret = "unicode-secret-content"
    (tmp_path / ".env").write_bytes(
        f"MINERU_TOKEN={secret}".encode("utf-8") + b"\xff\n"
    )
    runtime_config = _runtime_config_module()

    with pytest.raises(runtime_config.RuntimeConfigError) as error:
        runtime_config.load_runtime_config(root=tmp_path, environ={})

    assert "must be UTF-8" in str(error.value)
    assert secret not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
