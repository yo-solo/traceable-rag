from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .request_security import normalize_trusted_hosts


DEFAULT_DATABASE_PATH = Path("storage/traceable_v3.db")
DEFAULT_MANAGED_STORAGE_ROOT = Path("storage/managed")
DEFAULT_SETTINGS_PATH = Path("storage/settings.json")
DEFAULT_STATIC_DIR = Path("web/dist")
DEFAULT_PROVIDER_STORE_PATH = Path("/data/providers.db")
DEFAULT_MASTER_KEY_FILE = Path("/run/secrets/traceable-rag-master-key")
DOTENV_MAX_BYTES = 64 * 1024

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class RuntimeConfigError(ValueError):
    """Raised when local runtime configuration cannot be safely loaded."""


@dataclass(frozen=True)
class RuntimeConfig:
    mineru_token: str | None = field(repr=False)
    embedding_base_url: str | None
    embedding_api_key: str | None = field(repr=False)
    embedding_model: str | None
    embedding_query_prompt: str | None
    reranker_model: str | None
    database_path: Path
    managed_storage_root: Path
    settings_path: Path
    static_dir: Path
    mineru_base_url: str | None = None
    mineru_model: str | None = None
    mineru_adapter: str | None = None
    embedding_adapter: str | None = None
    reranker_base_url: str | None = None
    reranker_api_key: str | None = field(default=None, repr=False)
    reranker_adapter: str | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = field(default=None, repr=False)
    llm_model: str | None = None
    llm_adapter: str | None = None
    provider_store_path: Path = DEFAULT_PROVIDER_STORE_PATH
    master_key_file: Path = DEFAULT_MASTER_KEY_FILE
    admin_token_file: Path | None = None
    provider_management_allow_insecure_dev: bool = False
    trusted_hosts: tuple[str, ...] | None = None

    @property
    def mineru_configured(self) -> bool:
        return self.mineru_token is not None

    @property
    def embedding_configured(self) -> bool:
        return all(
            (self.embedding_base_url, self.embedding_api_key, self.embedding_model)
        )

    @property
    def reranker_configured(self) -> bool:
        independent = all(
            (self.reranker_base_url, self.reranker_api_key, self.reranker_model)
        )
        legacy = bool(
            self.reranker_model
            and self.reranker_base_url is None
            and self.reranker_api_key is None
            and self.embedding_base_url
            and self.embedding_api_key
        )
        return bool(independent or legacy)

    @property
    def llm_configured(self) -> bool:
        return all((self.llm_base_url, self.llm_api_key, self.llm_model))


def load_runtime_config(
    *,
    root: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> RuntimeConfig:
    runtime_root = Path.cwd() if root is None else Path(root)
    process_environment = os.environ if environ is None else environ
    dotenv_environment = _read_dotenv(runtime_root / ".env")

    return RuntimeConfig(
        mineru_token=_resolve(
            process_environment,
            dotenv_environment,
            "MINERU_TOKEN",
            "MINERU_API_KEY",
        ),
        mineru_base_url=_resolve(
            process_environment,
            dotenv_environment,
            "TRACEABLE_RAG_MINERU_BASE_URL",
            "MINERU_BASE_URL",
        ),
        mineru_model=_resolve(
            process_environment,
            dotenv_environment,
            "TRACEABLE_RAG_MINERU_MODEL",
            "MINERU_MODEL",
        ),
        mineru_adapter=_resolve(
            process_environment,
            dotenv_environment,
            "TRACEABLE_RAG_MINERU_ADAPTER",
        ),
        embedding_base_url=_resolve(
            process_environment,
            dotenv_environment,
            "OPENAI_EMBEDDING_BASE_URL",
            "TRACEABLE_RAG_EMBEDDING_BASE_URL",
        ),
        embedding_api_key=_resolve(
            process_environment,
            dotenv_environment,
            "OPENAI_EMBEDDING_API_KEY",
            "TRACEABLE_RAG_EMBEDDING_API_KEY",
        ),
        embedding_model=_resolve(
            process_environment,
            dotenv_environment,
            "OPENAI_EMBEDDING_MODEL",
            "TRACEABLE_RAG_EMBEDDING_MODEL",
        ),
        embedding_adapter=_resolve(
            process_environment,
            dotenv_environment,
            "TRACEABLE_RAG_EMBEDDING_ADAPTER",
            "OPENAI_EMBEDDING_ADAPTER",
        ),
        embedding_query_prompt=_resolve(
            process_environment,
            dotenv_environment,
            "OPENAI_EMBEDDING_QUERY_PROMPT",
            "TRACEABLE_RAG_EMBEDDING_QUERY_PROMPT",
        ),
        reranker_base_url=_resolve(
            process_environment,
            dotenv_environment,
            "TRACEABLE_RAG_RERANKER_BASE_URL",
            "RERANKER_BASE_URL",
        ),
        reranker_api_key=_resolve(
            process_environment,
            dotenv_environment,
            "TRACEABLE_RAG_RERANKER_API_KEY",
            "RERANKER_API_KEY",
        ),
        reranker_model=_resolve(
            process_environment,
            dotenv_environment,
            "TRACEABLE_RAG_RERANKER_MODEL",
            "RERANKER_MODEL",
        ),
        reranker_adapter=_resolve(
            process_environment,
            dotenv_environment,
            "TRACEABLE_RAG_RERANKER_ADAPTER",
            "RERANKER_ADAPTER",
        ),
        llm_base_url=_resolve(
            process_environment,
            dotenv_environment,
            "TRACEABLE_RAG_LLM_BASE_URL",
            "OPENAI_LLM_BASE_URL",
        ),
        llm_api_key=_resolve(
            process_environment,
            dotenv_environment,
            "TRACEABLE_RAG_LLM_API_KEY",
            "OPENAI_LLM_API_KEY",
        ),
        llm_model=_resolve(
            process_environment,
            dotenv_environment,
            "TRACEABLE_RAG_LLM_MODEL",
            "OPENAI_LLM_MODEL",
        ),
        llm_adapter=_resolve(
            process_environment,
            dotenv_environment,
            "TRACEABLE_RAG_LLM_ADAPTER",
            "OPENAI_LLM_ADAPTER",
        ),
        database_path=Path(
            _resolve(
                process_environment,
                dotenv_environment,
                "TRACEABLE_RAG_DATABASE_PATH",
            )
            or DEFAULT_DATABASE_PATH
        ),
        managed_storage_root=Path(
            _resolve(
                process_environment,
                dotenv_environment,
                "TRACEABLE_RAG_MANAGED_STORAGE_ROOT",
            )
            or DEFAULT_MANAGED_STORAGE_ROOT
        ),
        settings_path=Path(
            _resolve(
                process_environment,
                dotenv_environment,
                "TRACEABLE_RAG_SETTINGS_PATH",
            )
            or DEFAULT_SETTINGS_PATH
        ),
        static_dir=Path(
            _resolve(
                process_environment,
                dotenv_environment,
                "TRACEABLE_RAG_STATIC_DIR",
            )
            or DEFAULT_STATIC_DIR
        ),
        provider_store_path=Path(
            _resolve(
                process_environment,
                dotenv_environment,
                "TRACEABLE_RAG_PROVIDER_STORE_PATH",
            )
            or DEFAULT_PROVIDER_STORE_PATH
        ),
        master_key_file=Path(
            _resolve(
                process_environment,
                dotenv_environment,
                "TRACEABLE_RAG_MASTER_KEY_FILE",
            )
            or DEFAULT_MASTER_KEY_FILE
        ),
        admin_token_file=(
            Path(admin_token_file)
            if (
                admin_token_file := _resolve(
                    process_environment,
                    dotenv_environment,
                    "TRACEABLE_RAG_ADMIN_TOKEN_FILE",
                )
            )
            else None
        ),
        provider_management_allow_insecure_dev=_resolve_bool(
            process_environment,
            dotenv_environment,
            "TRACEABLE_RAG_PROVIDER_ALLOW_INSECURE_DEV",
        ),
        trusted_hosts=_resolve_trusted_hosts(
            process_environment,
            dotenv_environment,
        ),
    )


def _read_dotenv(path: Path) -> dict[str, str]:
    inspection_failed = False
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return {}
    except OSError:
        inspection_failed = True
    if inspection_failed:
        raise RuntimeConfigError(
            ".env could not be inspected safely; check its type and permissions."
        )

    if not stat.S_ISREG(before.st_mode):
        raise RuntimeConfigError(".env must be a regular file, not a link or device.")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    read_failed = False
    try:
        descriptor = os.open(path, flags)
        try:
            after = os.fstat(descriptor)
            if (
                not stat.S_ISREG(after.st_mode)
                or before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
            ):
                raise RuntimeConfigError(
                    ".env changed while being opened and was not read."
                )
            if after.st_size > DOTENV_MAX_BYTES:
                raise RuntimeConfigError(".env exceeds the 64 KiB safety limit.")
            data = bytearray()
            while len(data) <= DOTENV_MAX_BYTES:
                chunk = os.read(
                    descriptor,
                    min(8192, DOTENV_MAX_BYTES + 1 - len(data)),
                )
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > DOTENV_MAX_BYTES:
                raise RuntimeConfigError(".env exceeds the 64 KiB safety limit.")
        finally:
            os.close(descriptor)
    except RuntimeConfigError:
        raise
    except OSError:
        read_failed = True
    if read_failed:
        raise RuntimeConfigError(
            ".env could not be read safely; check its type and permissions."
        )

    decode_failed = False
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        decode_failed = True
    if decode_failed:
        raise RuntimeConfigError(
            ".env must be UTF-8; re-save it using .env.example as the template."
        )
    return parse_dotenv(text)


def parse_dotenv(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            raise RuntimeConfigError(
                f"Invalid .env syntax on line {line_number}; expected NAME=VALUE."
            )
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if not _ENV_NAME.fullmatch(name):
            raise RuntimeConfigError(
                f"Invalid .env variable name on line {line_number}."
            )
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[name] = value
    return values


def _resolve(
    process_environment: Mapping[str, str],
    dotenv_environment: Mapping[str, str],
    canonical_name: str,
    *legacy_names: str,
) -> str | None:
    names = (canonical_name, *legacy_names)
    for source in (process_environment, dotenv_environment):
        for name in names:
            value = source.get(name)
            if value is not None and value.strip():
                return value.strip()
    return None


def _resolve_bool(
    process_environment: Mapping[str, str],
    dotenv_environment: Mapping[str, str],
    name: str,
) -> bool:
    value = _resolve(process_environment, dotenv_environment, name)
    if value is None:
        return False
    normalized = value.casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeConfigError(f"{name} must be a boolean value.")


def _resolve_trusted_hosts(
    process_environment: Mapping[str, str],
    dotenv_environment: Mapping[str, str],
) -> tuple[str, ...] | None:
    value = _resolve(
        process_environment,
        dotenv_environment,
        "TRACEABLE_RAG_TRUSTED_HOSTS",
    )
    if value is None:
        return None
    try:
        return normalize_trusted_hosts(value)
    except ValueError:
        raise RuntimeConfigError(
            "TRACEABLE_RAG_TRUSTED_HOSTS must contain exact IP addresses or DNS names."
        ) from None
