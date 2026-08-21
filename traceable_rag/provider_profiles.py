from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import sqlite3
import stat
import threading
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit, urlunsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .embedding import OpenAICompatibleEmbeddingProvider, UrllibHTTPClient
from .mineru_client import MINERU_BASE_URL, MinerUClient
from .reranking import OpenAICompatibleReranker
from .runtime_config import RuntimeConfig


PROVIDER_ROLES = ("mineru", "embedding", "reranker", "llm")
DEFAULT_PROVIDER_STORE_PATH = Path("/data/providers.db")
DEFAULT_MASTER_KEY_FILE = Path("/run/secrets/traceable-rag-master-key")
PROVIDER_ADMIN_TOKEN_HEADER = "X-Traceable-RAG-Admin-Token"
MASTER_KEY_BYTES = 32
MAX_SECRET_BYTES = 64 * 1024
MAX_OPTIONS_BYTES = 16 * 1024

_PRESERVE_SECRET = object()
_ROLE_DEFAULTS: dict[str, dict[str, Any]] = {
    "mineru": {
        "adapter": "mineru_official",
        "display_name": "MinerU",
        "base_url": MINERU_BASE_URL,
        "model": "vlm",
    },
    "embedding": {
        "adapter": "openai_compatible",
        "display_name": "Embedding",
        "base_url": None,
        "model": None,
    },
    "reranker": {
        "adapter": "siliconflow_qwen",
        "display_name": "Reranker",
        "base_url": "https://api.siliconflow.cn",
        "model": "Qwen/Qwen3-Reranker-8B",
    },
    "llm": {
        "adapter": "openai_compatible",
        "display_name": "LLM",
        "base_url": None,
        "model": None,
    },
}
_OPTION_FIELDS = {
    "mineru": frozenset({"timeout", "language"}),
    "embedding": frozenset({"timeout", "batch_size", "query_prompt"}),
    "reranker": frozenset({"timeout"}),
    "llm": frozenset({"timeout"}),
}
_ADAPTER_ALIASES = {
    "mineru": {
        "mineru": "mineru_official",
        "mineru-official": "mineru_official",
        "mineru_official": "mineru_official",
    },
    "embedding": {
        "openai-compatible": "openai_compatible",
        "openai_compatible": "openai_compatible",
        "siliconflow": "siliconflow",
    },
    "reranker": {
        "siliconflow": "siliconflow_qwen",
        "siliconflow-qwen": "siliconflow_qwen",
        "siliconflow_qwen": "siliconflow_qwen",
        "qwen-compatible": "siliconflow_qwen",
        "qwen_compatible": "siliconflow_qwen",
        "cohere": "cohere",
        "cohere-compatible": "cohere",
        "cohere_compatible": "cohere",
        "jina": "jina",
        "jina-compatible": "jina",
        "jina_compatible": "jina",
    },
    "llm": {
        "openai-compatible": "openai_compatible",
        "openai_compatible": "openai_compatible",
        "siliconflow": "siliconflow",
    },
}
_ADAPTER_DEFAULTS: dict[tuple[str, str], tuple[str | None, str | None]] = {
    ("mineru", "mineru_official"): (MINERU_BASE_URL, "vlm"),
    ("embedding", "siliconflow"): ("https://api.siliconflow.cn", None),
    ("reranker", "siliconflow_qwen"): (
        "https://api.siliconflow.cn",
        "Qwen/Qwen3-Reranker-8B",
    ),
    ("reranker", "cohere"): ("https://api.cohere.com", "rerank-v3.5"),
    ("reranker", "jina"): (
        "https://api.jina.ai",
        "jina-reranker-v2-base-multilingual",
    ),
    ("llm", "siliconflow"): ("https://api.siliconflow.cn", None),
}


class ProviderProfileError(ValueError):
    """Base class for safe Provider configuration failures."""


class ProviderStoreUnavailableError(ProviderProfileError):
    """The encrypted Provider store is not safely available."""


class ProviderStoreKeyError(ProviderStoreUnavailableError):
    """The configured key cannot decrypt the Provider store."""


class ProviderProfileNotConfiguredError(ProviderProfileError):
    """A complete Provider profile is required for this operation."""


class ProviderConnectionError(ProviderProfileError):
    """A Provider connection test failed without exposing provider details."""


class ProviderTestNotSupportedError(ProviderProfileError):
    """The Provider requires a real end-to-end workflow for safe testing."""


class _RejectProviderRedirects(urllib.request.HTTPRedirectHandler):
    """Keep Provider credentials on the explicitly configured origin."""

    def redirect_request(self, request, fp, code, msg, headers, new_url):
        return None


class ProviderHTTPClient(UrllibHTTPClient):
    """JSON client that rejects every redirect before credentials can move."""

    @staticmethod
    def _open(request: urllib.request.Request, *, timeout: int):
        opener = urllib.request.build_opener(_RejectProviderRedirects())
        return opener.open(request, timeout=timeout)

    def post_json(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: int,
    ) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method="POST",
        )
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


@dataclass(frozen=True)
class ProviderProfile:
    role: str
    adapter: str
    display_name: str
    base_url: str | None
    model: str | None
    options: dict[str, Any]
    secret: str | None = field(default=None, repr=False, compare=False)
    source: str = "stored"
    created_at: str | None = None
    updated_at: str | None = None
    last_test_status: str | None = None
    last_tested_at: str | None = None

    @property
    def secret_present(self) -> bool:
        return bool(self.secret)

    @property
    def configured(self) -> bool:
        if self.role == "mineru":
            return self.secret_present
        return bool(self.base_url and self.model and self.secret)


@dataclass(frozen=True)
class StoredProviderMetadata:
    role: str
    adapter: str
    display_name: str
    base_url: str | None
    model: str | None
    options: dict[str, Any]
    secret_present: bool
    created_at: str
    updated_at: str
    last_test_status: str | None
    last_tested_at: str | None


def read_master_key(path: str | Path) -> bytes:
    data = _read_regular_file_bytes(
        path,
        maximum_bytes=MASTER_KEY_BYTES,
        description="Provider master key",
    )
    if len(data) != MASTER_KEY_BYTES:
        raise ProviderStoreUnavailableError(
            "Provider master key must contain exactly 32 raw bytes."
        )
    return data


def read_admin_token(path: str | Path) -> str:
    data = _read_regular_file_bytes(
        path,
        maximum_bytes=4096,
        description="Provider management token",
    )
    try:
        token = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise ProviderStoreUnavailableError(
            "Provider management token file must be UTF-8."
        ) from None
    if not token or len(token) > 4096:
        raise ProviderStoreUnavailableError(
            "Provider management token file is empty or invalid."
        )
    return token


def _read_regular_file_bytes(
    path: str | Path,
    *,
    maximum_bytes: int,
    description: str,
) -> bytes:
    target = Path(path)
    try:
        before = os.lstat(target)
    except (FileNotFoundError, OSError):
        raise ProviderStoreUnavailableError(
            f"{description} file is unavailable."
        ) from None
    if not stat.S_ISREG(before.st_mode):
        raise ProviderStoreUnavailableError(
            f"{description} must be a regular file, not a link or device."
        )
    flags = os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(target, flags)
        try:
            after = os.fstat(descriptor)
            if (
                not stat.S_ISREG(after.st_mode)
                or before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
            ):
                raise ProviderStoreUnavailableError(
                    f"{description} changed while being opened."
                )
            payload = bytearray()
            while len(payload) <= maximum_bytes:
                chunk = os.read(
                    descriptor,
                    min(4096, maximum_bytes + 1 - len(payload)),
                )
                if not chunk:
                    break
                payload.extend(chunk)
        finally:
            os.close(descriptor)
    except ProviderStoreUnavailableError:
        raise
    except OSError:
        raise ProviderStoreUnavailableError(
            f"{description} could not be read safely."
        ) from None
    if len(payload) > maximum_bytes:
        raise ProviderStoreUnavailableError(f"{description} file is too large.")
    return bytes(payload)


class ProviderProfileStore:
    """SQLite metadata with per-secret AES-256-GCM authenticated encryption."""

    def __init__(self, path: str | Path, master_key: bytes):
        if len(master_key) != MASTER_KEY_BYTES:
            raise ProviderStoreUnavailableError(
                "Provider master key must contain exactly 32 raw bytes."
            )
        self.path = Path(path)
        self._cipher = AESGCM(master_key)
        self._lock = threading.RLock()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(path={self.path!r}, master_key=<redacted>)"

    def initialize(self) -> None:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise ProviderStoreUnavailableError(
                    "Provider store directory could not be prepared safely."
                ) from None
            if self.path.is_symlink():
                raise ProviderStoreUnavailableError(
                    "Provider store path must not be a symbolic link."
                )
            with self._connection() as connection:
                connection.execute(
                    """
                    create table if not exists provider_profiles (
                        role text primary key,
                        adapter text not null,
                        display_name text not null,
                        base_url text,
                        model text,
                        options_json text not null,
                        secret_nonce blob,
                        secret_ciphertext blob,
                        created_at text not null,
                        updated_at text not null,
                        last_test_status text,
                        last_tested_at text
                    )
                    """
                )
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                raise ProviderStoreUnavailableError(
                    "Provider store permissions could not be secured."
                ) from None

    def verify(self) -> None:
        with self._connection() as connection:
            rows = connection.execute(
                """
                select role, adapter, base_url, model, options_json,
                       secret_nonce, secret_ciphertext
                from provider_profiles
                where secret_nonce is not null or secret_ciphertext is not null
                """
            ).fetchall()
        for row in rows:
            options = _decode_options(row["options_json"], row["role"])
            self._decrypt_secret(
                role=row["role"],
                adapter=row["adapter"],
                base_url=row["base_url"],
                model=row["model"],
                options=options,
                nonce=row["secret_nonce"],
                ciphertext=row["secret_ciphertext"],
            )

    def get(self, role: str) -> ProviderProfile | None:
        role = _validate_role(role)
        with self._connection() as connection:
            row = connection.execute(
                "select * from provider_profiles where role = ?", (role,)
            ).fetchone()
        if row is None:
            return None
        options = _decode_options(row["options_json"], role)
        secret = self._decrypt_secret(
            role=role,
            adapter=row["adapter"],
            base_url=row["base_url"],
            model=row["model"],
            options=options,
            nonce=row["secret_nonce"],
            ciphertext=row["secret_ciphertext"],
        )
        return ProviderProfile(
            role=role,
            adapter=row["adapter"],
            display_name=row["display_name"],
            base_url=row["base_url"],
            model=row["model"],
            options=options,
            secret=secret,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_test_status=row["last_test_status"],
            last_tested_at=row["last_tested_at"],
        )

    def metadata(self, role: str) -> StoredProviderMetadata | None:
        role = _validate_role(role)
        with self._connection() as connection:
            row = connection.execute(
                "select * from provider_profiles where role = ?", (role,)
            ).fetchone()
        if row is None:
            return None
        return StoredProviderMetadata(
            role=role,
            adapter=row["adapter"],
            display_name=row["display_name"],
            base_url=row["base_url"],
            model=row["model"],
            options=_decode_options(row["options_json"], role),
            secret_present=row["secret_ciphertext"] is not None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_test_status=row["last_test_status"],
            last_tested_at=row["last_tested_at"],
        )

    def put(
        self,
        profile: ProviderProfile,
        *,
        secret_update: object | str | None = _PRESERVE_SECRET,
    ) -> ProviderProfile:
        normalized = normalize_profile(profile)
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("begin immediate")
            previous = connection.execute(
                "select * from provider_profiles where role = ?", (normalized.role,)
            ).fetchone()
            if secret_update is _PRESERVE_SECRET:
                secret = self._secret_from_row(previous)
            elif secret_update is None or not str(secret_update).strip():
                secret = None
            else:
                secret = str(secret_update).strip()
                if len(secret.encode("utf-8")) > MAX_SECRET_BYTES:
                    raise ProviderProfileError("Provider credential is too large.")
            if secret is None:
                nonce = None
                ciphertext = None
            else:
                nonce = os.urandom(12)
                ciphertext = self._cipher.encrypt(
                    nonce,
                    secret.encode("utf-8"),
                    self._associated_data(
                        role=normalized.role,
                        adapter=normalized.adapter,
                        base_url=normalized.base_url,
                        model=normalized.model,
                        options=normalized.options,
                    ),
                )
            created_at = previous["created_at"] if previous is not None else now
            connection.execute(
                """
                insert into provider_profiles (
                    role, adapter, display_name, base_url, model, options_json,
                    secret_nonce, secret_ciphertext, created_at, updated_at,
                    last_test_status, last_tested_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, null, null)
                on conflict(role) do update set
                    adapter = excluded.adapter,
                    display_name = excluded.display_name,
                    base_url = excluded.base_url,
                    model = excluded.model,
                    options_json = excluded.options_json,
                    secret_nonce = excluded.secret_nonce,
                    secret_ciphertext = excluded.secret_ciphertext,
                    updated_at = excluded.updated_at,
                    last_test_status = null,
                    last_tested_at = null
                """,
                (
                    normalized.role,
                    normalized.adapter,
                    normalized.display_name,
                    normalized.base_url,
                    normalized.model,
                    _encode_options(normalized.options),
                    nonce,
                    ciphertext,
                    created_at,
                    now,
                ),
            )
            connection.commit()
        saved = self.get(normalized.role)
        if saved is None:  # pragma: no cover - guarded by the transaction
            raise ProviderStoreUnavailableError("Provider profile was not persisted.")
        return saved

    def delete(self, role: str) -> bool:
        role = _validate_role(role)
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "delete from provider_profiles where role = ?", (role,)
            )
            return cursor.rowcount > 0

    def record_test(self, role: str, status: str, tested_at: str) -> None:
        role = _validate_role(role)
        if status not in {"success", "failed"}:
            raise ProviderProfileError("Provider test status is invalid.")
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                update provider_profiles
                set last_test_status = ?, last_tested_at = ?
                where role = ?
                """,
                (status, tested_at, role),
            )

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self.path, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.execute("pragma busy_timeout = 10000")
            return connection
        except sqlite3.Error:
            raise ProviderStoreUnavailableError(
                "Provider store could not be opened safely."
            ) from None

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        except sqlite3.Error:
            raise ProviderStoreUnavailableError(
                "Provider store operation failed safely."
            ) from None
        finally:
            connection.close()

    @staticmethod
    def _associated_data(
        *,
        role: str,
        adapter: str,
        base_url: str | None,
        model: str | None,
        options: Mapping[str, Any],
    ) -> bytes:
        metadata = json.dumps(
            {
                "adapter": adapter,
                "base_url": base_url,
                "model": model,
                "options": options,
                "role": role,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return b"traceable-rag-provider-profile:v2\0" + metadata

    def _secret_from_row(self, row: sqlite3.Row | None) -> str | None:
        if row is None:
            return None
        options = _decode_options(row["options_json"], row["role"])
        return self._decrypt_secret(
            role=row["role"],
            adapter=row["adapter"],
            base_url=row["base_url"],
            model=row["model"],
            options=options,
            nonce=row["secret_nonce"],
            ciphertext=row["secret_ciphertext"],
        )

    def _decrypt_secret(
        self,
        *,
        role: str,
        adapter: str,
        base_url: str | None,
        model: str | None,
        options: Mapping[str, Any],
        nonce: bytes | None,
        ciphertext: bytes | None,
    ) -> str | None:
        if nonce is None and ciphertext is None:
            return None
        if not isinstance(nonce, bytes) or not isinstance(ciphertext, bytes):
            raise ProviderStoreKeyError(
                "Provider credential ciphertext is invalid or cannot be decrypted."
            )
        try:
            plaintext = self._cipher.decrypt(
                nonce,
                ciphertext,
                self._associated_data(
                    role=role,
                    adapter=adapter,
                    base_url=base_url,
                    model=model,
                    options=options,
                ),
            )
            return plaintext.decode("utf-8")
        except (InvalidTag, UnicodeDecodeError, TypeError, ValueError):
            raise ProviderStoreKeyError(
                "Provider credential ciphertext is invalid or cannot be decrypted."
            ) from None


class ProviderRegistry:
    """Resolve saved profiles first, then environment profiles, on every request."""

    def __init__(
        self,
        *,
        runtime: RuntimeConfig | None,
        runtime_root: str | Path | None = None,
        store_path: str | Path = DEFAULT_PROVIDER_STORE_PATH,
        master_key_file: str | Path = DEFAULT_MASTER_KEY_FILE,
        http_client: UrllibHTTPClient | Any | None = None,
    ):
        self.runtime = runtime
        self.runtime_root = runtime_root
        self.store_path = Path(store_path)
        self.master_key_file = Path(master_key_file)
        self.http_client = http_client or ProviderHTTPClient()
        self._store: ProviderProfileStore | None = None
        self._lock = threading.RLock()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(store_path={self.store_path!r}, "
            "master_key=<redacted>)"
        )

    def storage_available(self) -> bool:
        try:
            self._available_store().verify()
        except ProviderStoreUnavailableError:
            return False
        return True

    def resolve(self, role: str) -> ProviderProfile | None:
        role = _validate_role(role)
        saved = self._available_store().get(role)
        if saved is not None:
            return saved
        return environment_profile(self._current_runtime(), role)

    def metadata_payload(self, role: str) -> dict[str, Any]:
        role = _validate_role(role)
        storage_available = True
        try:
            store = self._available_store()
            store.verify()
            metadata = store.metadata(role)
        except ProviderStoreUnavailableError:
            storage_available = False
            metadata = None
        if not storage_available:
            defaults = default_profile(role)
            return {
                "role": role,
                "adapter": defaults.adapter,
                "display_name": defaults.display_name,
                "base_url": defaults.base_url,
                "model": defaults.model,
                "options": {},
                "source": "default",
                "configured": False,
                "secret_present": False,
                "status": "unavailable",
                "last_test_status": None,
                "last_tested_at": None,
            }
        if metadata is not None:
            configured = _metadata_configured(metadata)
            return {
                "role": role,
                "adapter": metadata.adapter,
                "display_name": metadata.display_name,
                "base_url": metadata.base_url,
                "model": metadata.model,
                "options": metadata.options,
                "source": "stored",
                "configured": configured,
                "secret_present": metadata.secret_present,
                "status": "configured" if configured else "incomplete",
                "last_test_status": metadata.last_test_status,
                "last_tested_at": metadata.last_tested_at,
            }
        profile = environment_profile(self._current_runtime(), role)
        if profile is not None:
            return _profile_payload(profile)
        defaults = default_profile(role)
        return {
            "role": role,
            "adapter": defaults.adapter,
            "display_name": defaults.display_name,
            "base_url": defaults.base_url,
            "model": defaults.model,
            "options": {},
            "source": "default",
            "configured": False,
            "secret_present": False,
            "status": "not_configured",
            "last_test_status": None,
            "last_tested_at": None,
        }

    def all_metadata_payload(self) -> dict[str, Any]:
        available = self.storage_available()
        return {
            "storage_available": available,
            "profiles": {
                role: self.metadata_payload(role) for role in PROVIDER_ROLES
            },
        }

    def put(
        self,
        role: str,
        changes: Mapping[str, Any],
        *,
        secret_update: object | str | None = _PRESERVE_SECRET,
    ) -> ProviderProfile:
        role = _validate_role(role)
        store = self._available_store()
        current = store.get(role)
        base = current or default_profile(role)
        values = {
            "adapter": base.adapter,
            "display_name": base.display_name,
            "base_url": base.base_url,
            "model": base.model,
            "options": dict(base.options),
        }
        for name in values:
            if name in changes:
                values[name] = changes[name]
        if values["display_name"] is None or not str(values["display_name"]).strip():
            values["display_name"] = _ROLE_DEFAULTS[role]["display_name"]
        profile = ProviderProfile(role=role, secret=base.secret, **values)
        return store.put(profile, secret_update=secret_update)

    def delete(self, role: str) -> bool:
        return self._available_store().delete(role)

    def preview(
        self,
        role: str,
        changes: Mapping[str, Any],
        *,
        secret_update: object | str | None = _PRESERVE_SECRET,
    ) -> ProviderProfile:
        role = _validate_role(role)
        current = self.resolve(role)
        base = current or default_profile(role)
        values = {
            "adapter": base.adapter,
            "display_name": base.display_name,
            "base_url": base.base_url,
            "model": base.model,
            "options": dict(base.options),
        }
        for name in values:
            if name in changes:
                values[name] = changes[name]
        if values["display_name"] is None or not str(values["display_name"]).strip():
            values["display_name"] = _ROLE_DEFAULTS[role]["display_name"]
        if secret_update is _PRESERVE_SECRET:
            secret = base.secret
        elif secret_update is None or not str(secret_update).strip():
            secret = None
        else:
            secret = str(secret_update).strip()
        return normalize_profile(
            ProviderProfile(
                role=role,
                secret=secret,
                source="temporary",
                **values,
            )
        )

    def record_test(self, role: str, status: str, tested_at: str) -> None:
        self._available_store().record_test(role, status, tested_at)

    def has_saved_profile(self, role: str) -> bool:
        store = self._available_store()
        store.verify()
        return store.metadata(role) is not None

    def embedding_provider(self) -> OpenAICompatibleEmbeddingProvider:
        profile = self._require_configured("embedding")
        provider = OpenAICompatibleEmbeddingProvider(
            api_key=profile.secret or "",
            base_url=profile.base_url or "",
            model_name=profile.model or "",
            http_client=self.http_client,
            timeout=_integer_option(profile.options, "timeout", 60, 1, 600),
            batch_size=_integer_option(profile.options, "batch_size", 16, 1, 256),
            query_prompt=_string_option(profile.options, "query_prompt"),
        )
        if profile.source == "stored":
            endpoint_fingerprint = hashlib.sha256(
                (profile.base_url or "").encode("utf-8")
            ).hexdigest()[:16]
            provider.provider_name = (
                f"provider-profile:{profile.adapter}:{endpoint_fingerprint}"
            )
        elif profile.adapter == "openai_compatible":
            # Preserve the v0.4.1 index fingerprint for unchanged environment
            # deployments. Persisted profiles intentionally receive an endpoint-
            # bound fingerprint so switching services cannot reuse stale vectors.
            provider.provider_name = "openai-compatible"
        else:
            provider.provider_name = profile.adapter
        return provider

    def reranker(self) -> OpenAICompatibleReranker:
        return reranker_from_profile(
            self._require_configured("reranker"), http_client=self.http_client
        )

    def mineru_client(self) -> tuple[MinerUClient, ProviderProfile]:
        profile = self._require_configured("mineru")
        return (
            MinerUClient(
                profile.secret or "",
                base_url=profile.base_url or MINERU_BASE_URL,
                timeout=_integer_option(profile.options, "timeout", 60, 1, 600),
            ),
            profile,
        )

    def _require_configured(self, role: str) -> ProviderProfile:
        profile = self.resolve(role)
        if profile is None or not profile.configured:
            raise ProviderProfileNotConfiguredError(
                f"{_ROLE_DEFAULTS[role]['display_name']} Provider is not configured."
            )
        return profile

    def _available_store(self) -> ProviderProfileStore:
        with self._lock:
            if self._store is None:
                key = read_master_key(self.master_key_file)
                store = ProviderProfileStore(self.store_path, key)
                store.initialize()
                store.verify()
                self._store = store
            return self._store

    def _current_runtime(self) -> RuntimeConfig | None:
        if self.runtime is not None:
            return self.runtime
        try:
            from .runtime_config import load_runtime_config

            return load_runtime_config(root=self.runtime_root)
        except Exception:
            return None


class DynamicEmbeddingProvider:
    """Worker-safe facade that snapshots the active profile for each embed call."""

    def __init__(self, registry: ProviderRegistry):
        self.registry = registry
        self._local = threading.local()

    @property
    def provider_name(self) -> str:
        value = getattr(self._local, "provider_name", None)
        if value is not None:
            return value
        return self.registry._require_configured("embedding").adapter

    @property
    def model_name(self) -> str:
        value = getattr(self._local, "model_name", None)
        if value is not None:
            return value
        return self.registry._require_configured("embedding").model or ""

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        provider = self.registry.embedding_provider()
        self._local.provider_name = provider.provider_name
        self._local.model_name = provider.model_name
        return provider.embed_texts(texts)

    def embed_queries(self, queries: list[str]) -> list[list[float]]:
        provider = self.registry.embedding_provider()
        self._local.provider_name = provider.provider_name
        self._local.model_name = provider.model_name
        return provider.embed_queries(queries)


class DynamicMinerUParser:
    def __init__(self, registry: ProviderRegistry):
        self.registry = registry

    def configured(self) -> bool:
        profile = self.registry.resolve("mineru")
        return bool(profile and profile.configured)

    def parse_local_file(self, *args: Any, **kwargs: Any):
        client, profile = self.registry.mineru_client()
        if profile.model:
            kwargs["model_version"] = profile.model
        if profile.options.get("language"):
            kwargs["language"] = profile.options["language"]
        return client.parse_local_file(*args, **kwargs)


def default_profile(role: str, *, adapter: str | None = None) -> ProviderProfile:
    role = _validate_role(role)
    canonical_adapter = _normalize_adapter(
        role, adapter or _ROLE_DEFAULTS[role]["adapter"]
    )
    base_url, model = _ADAPTER_DEFAULTS.get(
        (role, canonical_adapter),
        (_ROLE_DEFAULTS[role]["base_url"], _ROLE_DEFAULTS[role]["model"]),
    )
    return ProviderProfile(
        role=role,
        adapter=canonical_adapter,
        display_name=_ROLE_DEFAULTS[role]["display_name"],
        base_url=base_url,
        model=model,
        options={},
        source="default",
    )


def environment_profile(
    runtime: RuntimeConfig | None, role: str
) -> ProviderProfile | None:
    if runtime is None:
        return None
    role = _validate_role(role)
    if role == "mineru":
        if not any(
            (runtime.mineru_token, runtime.mineru_base_url, runtime.mineru_model)
        ):
            return None
        return normalize_profile(
            ProviderProfile(
                role=role,
                adapter=runtime.mineru_adapter or "mineru_official",
                display_name="MinerU",
                base_url=runtime.mineru_base_url or MINERU_BASE_URL,
                model=runtime.mineru_model or "vlm",
                options={},
                secret=runtime.mineru_token,
                source="environment",
            )
        )
    if role == "embedding":
        if not any(
            (
                runtime.embedding_base_url,
                runtime.embedding_api_key,
                runtime.embedding_model,
                runtime.embedding_adapter,
                runtime.embedding_query_prompt,
            )
        ):
            return None
        options = (
            {"query_prompt": runtime.embedding_query_prompt}
            if runtime.embedding_query_prompt
            else {}
        )
        return normalize_profile(
            ProviderProfile(
                role=role,
                adapter=runtime.embedding_adapter or "openai_compatible",
                display_name="Embedding",
                base_url=runtime.embedding_base_url,
                model=runtime.embedding_model,
                options=options,
                secret=runtime.embedding_api_key,
                source="environment",
            )
        )
    if role == "reranker":
        if not any(
            (
                runtime.reranker_base_url,
                runtime.reranker_api_key,
                runtime.reranker_model,
                runtime.reranker_adapter,
            )
        ):
            return None
        legacy_environment = (
            runtime.reranker_model is not None
            and runtime.reranker_base_url is None
            and runtime.reranker_api_key is None
            and runtime.embedding_base_url is not None
            and runtime.embedding_api_key is not None
        )
        return normalize_profile(
            ProviderProfile(
                role=role,
                adapter=runtime.reranker_adapter or "siliconflow_qwen",
                display_name="Reranker",
                base_url=(
                    runtime.embedding_base_url
                    if legacy_environment
                    else runtime.reranker_base_url
                ),
                model=runtime.reranker_model,
                options={},
                secret=(
                    runtime.embedding_api_key
                    if legacy_environment
                    else runtime.reranker_api_key
                ),
                source=(
                    "environment_legacy" if legacy_environment else "environment"
                ),
            )
        )
    if not any(
        (
            runtime.llm_base_url,
            runtime.llm_api_key,
            runtime.llm_model,
            runtime.llm_adapter,
        )
    ):
        return None
    return normalize_profile(
        ProviderProfile(
            role=role,
            adapter=runtime.llm_adapter or "openai_compatible",
            display_name="LLM",
            base_url=runtime.llm_base_url,
            model=runtime.llm_model,
            options={},
            secret=runtime.llm_api_key,
            source="environment",
        )
    )


def normalize_profile(profile: ProviderProfile) -> ProviderProfile:
    role = _validate_role(profile.role)
    adapter = _normalize_adapter(role, profile.adapter)
    display_name = str(profile.display_name).strip()
    if not display_name or len(display_name) > 120:
        raise ProviderProfileError("Provider display name is invalid.")
    base_url = _normalize_optional_url(profile.base_url)
    model = _normalize_optional_text(profile.model, maximum=500)
    options = _normalize_options(profile.options, role)
    secret = profile.secret.strip() if isinstance(profile.secret, str) else None
    if secret and len(secret.encode("utf-8")) > MAX_SECRET_BYTES:
        raise ProviderProfileError("Provider credential is too large.")
    return replace(
        profile,
        role=role,
        adapter=adapter,
        display_name=display_name,
        base_url=base_url,
        model=model,
        options=options,
        secret=secret or None,
    )


def reranker_from_profile(
    profile: ProviderProfile,
    *,
    http_client: UrllibHTTPClient | Any | None = None,
) -> OpenAICompatibleReranker:
    profile = normalize_profile(profile)
    if not profile.configured:
        raise ProviderProfileNotConfiguredError(
            "Reranker Provider is not configured."
        )
    endpoint_path = "/v2/rerank" if profile.adapter == "cohere" else "/v1/rerank"
    return OpenAICompatibleReranker(
        api_key=profile.secret or "",
        base_url=profile.base_url or "",
        model_name=profile.model or "",
        http_client=(http_client or ProviderHTTPClient()),
        timeout=_integer_option(profile.options, "timeout", 120, 1, 600),
        endpoint_path=endpoint_path,
    )


def test_provider_connection(
    profile: ProviderProfile,
    *,
    http_client: UrllibHTTPClient | Any | None = None,
) -> None:
    profile = normalize_profile(profile)
    if not profile.configured:
        raise ProviderProfileNotConfiguredError(
            f"{profile.display_name} Provider is incomplete."
        )
    client = http_client or ProviderHTTPClient()
    if profile.role == "mineru":
        raise ProviderTestNotSupportedError(
            "MinerU requires a real document end-to-end test."
        )
    try:
        if profile.role == "embedding":
            provider = OpenAICompatibleEmbeddingProvider(
                api_key=profile.secret or "",
                base_url=profile.base_url or "",
                model_name=profile.model or "",
                http_client=client,
                timeout=_integer_option(profile.options, "timeout", 30, 1, 120),
                batch_size=1,
            )
            vectors = provider.embed_texts(["Traceable RAG connectivity test"])
            if len(vectors) != 1 or not isinstance(vectors[0], list):
                raise ValueError("invalid embedding response")
            return
        if profile.role == "reranker":
            results = reranker_from_profile(
                profile, http_client=client
            ).rerank(
                "Traceable RAG connectivity test",
                ["Traceable RAG connectivity test"],
                top_n=1,
            )
            if len(results) != 1:
                raise ValueError("invalid reranker response")
            return
        headers = {
            "Authorization": f"Bearer {profile.secret}",
            "Content-Type": "application/json",
        }
        timeout = _integer_option(profile.options, "timeout", 30, 1, 120)
        if profile.role == "llm":
            payload = client.post_json(
                f"{_api_root(profile.base_url or '')}/v1/chat/completions",
                headers=headers,
                payload={
                    "model": profile.model,
                    "messages": [
                        {"role": "user", "content": "Reply with OK."}
                    ],
                    "max_tokens": 1,
                    "stream": False,
                },
                timeout=timeout,
            )
            if not isinstance(payload, dict) or not isinstance(
                payload.get("choices"), list
            ):
                raise ValueError("invalid LLM response")
            return
    except ProviderProfileError:
        raise
    except Exception:
        raise ProviderConnectionError(
            f"{profile.display_name} connection test failed."
        ) from None


def provider_management_authorized(
    *,
    client_host: str | None,
    supplied_token: str | None,
    token_file: str | Path | None,
    allow_insecure_dev: bool = False,
) -> tuple[bool, str | None]:
    if token_file is not None:
        try:
            expected = read_admin_token(token_file)
        except ProviderStoreUnavailableError:
            return False, "unavailable"
        supplied = supplied_token or ""
        return hmac.compare_digest(supplied, expected), None
    if client_host == "testclient":
        return True, None
    if not allow_insecure_dev:
        return False, None
    try:
        return bool(client_host and ipaddress.ip_address(client_host).is_loopback), None
    except ValueError:
        return False, None


def secret_preserve_sentinel() -> object:
    return _PRESERVE_SECRET


def _profile_payload(profile: ProviderProfile) -> dict[str, Any]:
    return {
        "role": profile.role,
        "adapter": profile.adapter,
        "display_name": profile.display_name,
        "base_url": profile.base_url,
        "model": profile.model,
        "options": profile.options,
        "source": profile.source,
        "configured": profile.configured,
        "secret_present": profile.secret_present,
        "status": "configured" if profile.configured else "incomplete",
        "last_test_status": profile.last_test_status,
        "last_tested_at": profile.last_tested_at,
    }


def _metadata_configured(metadata: StoredProviderMetadata) -> bool:
    if metadata.role == "mineru":
        return metadata.secret_present
    return bool(metadata.base_url and metadata.model and metadata.secret_present)


def _validate_role(role: str) -> str:
    normalized = str(role).strip().lower()
    if normalized not in PROVIDER_ROLES:
        raise ProviderProfileError("Provider role is unsupported.")
    return normalized


def _normalize_adapter(role: str, adapter: str) -> str:
    value = str(adapter).strip().lower()
    canonical = _ADAPTER_ALIASES[role].get(value)
    if canonical is None:
        raise ProviderProfileError("Provider adapter is unsupported for this role.")
    return canonical


def _normalize_optional_text(value: Any, *, maximum: int) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if len(normalized) > maximum or any(ord(character) < 32 for character in normalized):
        raise ProviderProfileError("Provider text field is invalid.")
    return normalized


def _normalize_optional_url(value: Any) -> str | None:
    normalized = _normalize_optional_text(value, maximum=2048)
    if normalized is None:
        return None
    try:
        parsed = urlsplit(normalized)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ProviderProfileError("Provider base URL is invalid.") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ProviderProfileError(
            "Provider base URL must be a credential-free HTTP(S) URL."
        )
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
    )


def _normalize_options(value: Any, role: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProviderProfileError("Provider options must be a JSON object.")
    unknown = set(value) - _OPTION_FIELDS[role]
    if unknown:
        raise ProviderProfileError("Provider options contain unsupported fields.")
    if any(isinstance(item, (dict, list)) for item in value.values()):
        raise ProviderProfileError("Provider options must use scalar values.")
    normalized = dict(value)
    if "timeout" in normalized:
        _integer_option(normalized, "timeout", 30, 1, 600)
    if "batch_size" in normalized:
        _integer_option(normalized, "batch_size", 16, 1, 256)
    if "query_prompt" in normalized:
        prompt = normalized["query_prompt"]
        if not isinstance(prompt, str) or len(prompt) > 4000:
            raise ProviderProfileError(
                "Provider option query_prompt must be a bounded string."
            )
    if "language" in normalized:
        language = normalized["language"]
        if (
            not isinstance(language, str)
            or not language.strip()
            or len(language.strip()) > 20
        ):
            raise ProviderProfileError(
                "Provider option language must be a bounded string."
            )
        normalized["language"] = language.strip()
    try:
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise ProviderProfileError("Provider options must contain valid JSON.") from None
    if len(encoded) > MAX_OPTIONS_BYTES:
        raise ProviderProfileError("Provider options are too large.")
    decoded = json.loads(encoded.decode("utf-8"))
    return decoded


def _encode_options(options: dict[str, Any]) -> str:
    return json.dumps(
        options,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _decode_options(value: str, role: str) -> dict[str, Any]:
    try:
        role = _validate_role(role)
        decoded = json.loads(value)
        return _normalize_options(decoded, role)
    except (
        KeyError,
        ProviderProfileError,
        TypeError,
        json.JSONDecodeError,
        RecursionError,
    ):
        raise ProviderStoreUnavailableError(
            "Provider metadata is invalid."
        ) from None


def _integer_option(
    options: Mapping[str, Any],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = options.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProviderProfileError(f"Provider option {name} must be an integer.")
    if not minimum <= value <= maximum:
        raise ProviderProfileError(f"Provider option {name} is outside the safe range.")
    return value


def _string_option(options: Mapping[str, Any], name: str) -> str | None:
    value = options.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 4000:
        raise ProviderProfileError(f"Provider option {name} must be a string.")
    return value.replace("\\n", "\n")


def _api_root(base_url: str) -> str:
    normalized = _normalize_optional_url(base_url)
    if normalized is None:
        raise ProviderProfileError("Provider base URL is required.")
    return normalized[: -len("/v1")] if normalized.endswith("/v1") else normalized


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_now() -> str:
    return _utc_now()
