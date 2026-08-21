from __future__ import annotations

import fcntl
import ipaddress
import json
import os
import re
import stat
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterator, Mapping, TypeVar
from urllib.parse import unquote, urlsplit, urlunsplit

from .store import EvidenceStore
from .runtime_config import RuntimeConfig, load_runtime_config


SETTINGS_FILE_MAX_BYTES = 64 * 1024
RETRIEVAL_MODES = frozenset({"keyword", "embedding"})
SETTINGS_FIELDS = frozenset(
    {
        "default_top_k",
        "default_retrieval_mode",
        "default_use_embedding",
        "public_base_url",
        "default_knowledge_base_id",
    }
)
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_T = TypeVar("_T")


class InvalidSettingsFile(ValueError):
    """Raised internally when the persisted settings file cannot be trusted."""


@dataclass(frozen=True)
class SafeSettings:
    default_top_k: int = 5
    default_retrieval_mode: str = "embedding"
    default_use_embedding: bool = True
    public_base_url: str = "http://127.0.0.1:8000"
    default_knowledge_base_id: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> SafeSettings:
        if set(value) != SETTINGS_FIELDS:
            raise InvalidSettingsFile("Settings file fields do not match the safe schema")
        settings = cls(**dict(value))
        settings.validate()
        return settings

    def validate(self) -> None:
        if (
            not isinstance(self.default_top_k, int)
            or isinstance(self.default_top_k, bool)
            or not 1 <= self.default_top_k <= 50
        ):
            raise InvalidSettingsFile("default_top_k is outside the safe range")
        if (
            not isinstance(self.default_retrieval_mode, str)
            or self.default_retrieval_mode not in RETRIEVAL_MODES
        ):
            raise InvalidSettingsFile("default_retrieval_mode is unsupported")
        if not isinstance(self.default_use_embedding, bool):
            raise InvalidSettingsFile("default_use_embedding must be boolean")
        if self.default_use_embedding != (
            self.default_retrieval_mode == "embedding"
        ):
            raise InvalidSettingsFile("retrieval defaults must be consistent")
        if normalize_public_base_url(self.public_base_url) != self.public_base_url:
            raise InvalidSettingsFile("public_base_url is not normalized")
        if self.default_knowledge_base_id is not None and (
            not isinstance(self.default_knowledge_base_id, str)
            or not self.default_knowledge_base_id.strip()
            or len(self.default_knowledge_base_id) > 255
        ):
            raise InvalidSettingsFile("default_knowledge_base_id is invalid")

    def payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SettingsLoadResult:
    settings: SafeSettings
    status: str

    def persistence_payload(self) -> dict[str, str | None]:
        return {
            "status": self.status,
            "message": (
                None
                if self.status == "ok"
                else "设置文件不可用，当前已安全回退到默认值；保存一次即可修复。"
            ),
        }


class SettingsStore:
    """Persist the non-sensitive settings document with atomic replacement."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self._lock = threading.RLock()

    def load(self) -> SettingsLoadResult:
        try:
            with self._locked(exclusive=False):
                return self._load_unlocked()
        except (OSError, InvalidSettingsFile):
            return SettingsLoadResult(SafeSettings(), "degraded")

    def patch(
        self,
        changes: Mapping[str, Any],
        *,
        validate: Callable[[SafeSettings], None] | None = None,
        transaction: Callable[[SafeSettings], ContextManager[Any]] | None = None,
    ) -> SettingsLoadResult:
        with self._locked(exclusive=True):
            current = self._load_unlocked().settings
            updated = replace(current, **dict(changes))
            updated.validate()
            if validate is not None:
                validate(updated)
            if transaction is None:
                self._write_unlocked(updated)
            else:
                try:
                    with transaction(updated):
                        self._write_unlocked(updated)
                except Exception:
                    try:
                        self._write_unlocked(current)
                    except OSError:
                        pass
                    raise
            return SettingsLoadResult(updated, "ok")

    def run_with_default_knowledge_base_cleared(
        self,
        knowledge_base_id: str,
        operation: Callable[[], _T],
    ) -> _T:
        with self._locked(exclusive=True):
            current = self._load_unlocked().settings
            should_clear = current.default_knowledge_base_id == knowledge_base_id
            if should_clear:
                self._write_unlocked(
                    replace(current, default_knowledge_base_id=None)
                )
            try:
                result = operation()
            except Exception:
                if should_clear:
                    try:
                        self._write_unlocked(current)
                    except OSError:
                        pass
                raise
            if should_clear and result is False:
                self._write_unlocked(current)
            return result

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        with self._lock:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            flags = (
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            descriptor = os.open(self.lock_path, flags, 0o600)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise InvalidSettingsFile("Settings lock is not a regular file")
                os.fchmod(descriptor, 0o600)
                fcntl.flock(
                    descriptor,
                    fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
                )
                yield
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    def _load_unlocked(self) -> SettingsLoadResult:
        try:
            path_metadata = self.path.lstat()
        except FileNotFoundError:
            return SettingsLoadResult(SafeSettings(), "ok")
        try:
            if (
                not stat.S_ISREG(path_metadata.st_mode)
                or path_metadata.st_size > SETTINGS_FILE_MAX_BYTES
            ):
                raise InvalidSettingsFile("Settings file is not a bounded regular file")
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            descriptor = os.open(self.path, flags)
            try:
                opened_metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened_metadata.st_mode)
                    or opened_metadata.st_size > SETTINGS_FILE_MAX_BYTES
                    or opened_metadata.st_dev != path_metadata.st_dev
                    or opened_metadata.st_ino != path_metadata.st_ino
                ):
                    raise InvalidSettingsFile(
                        "Settings file changed or is not a bounded regular file"
                    )
                raw_bytes = _read_bounded(descriptor, SETTINGS_FILE_MAX_BYTES)
            finally:
                os.close(descriptor)
            raw = raw_bytes.decode("utf-8")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise InvalidSettingsFile("Settings file must contain an object")
            return SettingsLoadResult(SafeSettings.from_mapping(value), "ok")
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            RecursionError,
            TypeError,
            InvalidSettingsFile,
        ):
            return SettingsLoadResult(SafeSettings(), "degraded")

    def _write_unlocked(self, settings: SafeSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.parent / f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        payload = json.dumps(
            settings.payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as target:
                descriptor = None
                target.write(payload)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            _fsync_directory(self.path.parent)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)


def normalize_public_base_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Public base URL must be a string")
    normalized = value.strip().rstrip("/")
    if _contains_control_character(normalized) or _contains_control_character(
        unquote(normalized)
    ):
        raise ValueError("Public base URL contains control characters")
    try:
        parsed = urlsplit(normalized)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Public base URL has an invalid host or port") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Public base URL must be an HTTP(S) origin without credentials")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Public base URL port is outside the valid range")
    _validate_hostname(hostname)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def runtime_payload(store: EvidenceStore) -> dict[str, Any]:
    with store.connect() as connection:
        counts = connection.execute(
            """
            select
                (select count(*) from knowledge_bases) as knowledge_bases,
                (select count(*) from documents) as documents,
                (select count(*) from chunks) as chunks,
                (select count(*) from index_records) as index_records
            """
        ).fetchone()
        failures = connection.execute(
            """
            select id, display_name, updated_at
            from documents
            where processing_status = 'failed'
            order by updated_at desc, id
            limit 5
            """
        ).fetchall()
    return {
        "service": {"healthy": True},
        "counts": {
            "knowledge_bases": counts["knowledge_bases"],
            "documents": counts["documents"],
            "chunks": counts["chunks"],
            "index_records": counts["index_records"],
        },
        "recent_failures": [
            {
                "document_id": row["id"],
                "display_name": row["display_name"],
                "summary": "文档处理失败，请检查服务日志后重试。",
                "updated_at": row["updated_at"],
            }
            for row in failures
        ],
    }


def unavailable_runtime_payload() -> dict[str, Any]:
    return {
        "service": {"healthy": False},
        "counts": {
            "knowledge_bases": 0,
            "documents": 0,
            "chunks": 0,
            "index_records": 0,
        },
        "recent_failures": [],
    }


def provider_status(
    *,
    explicit: bool | None,
    configured: bool,
) -> dict[str, bool]:
    if explicit is True:
        return {"configured": True}
    if explicit is False:
        return {"configured": False}
    return {"configured": configured}


def provider_payload(
    *,
    api_doc_path: Path | None,
    mineru_provider_configured: bool | None,
    embedding_provider_configured: bool | None,
    runtime_root: str | Path | None = None,
    runtime_config: RuntimeConfig | None = None,
) -> dict[str, dict[str, bool]]:
    del api_doc_path
    runtime = runtime_config
    if runtime is None and (
        mineru_provider_configured is None
        or embedding_provider_configured is None
    ):
        runtime = load_runtime_config(root=runtime_root)
    return {
        "mineru": provider_status(
            explicit=mineru_provider_configured,
            configured=bool(runtime and runtime.mineru_configured),
        ),
        "embedding": provider_status(
            explicit=embedding_provider_configured,
            configured=bool(runtime and runtime.embedding_configured),
        ),
    }


def skill_payload(template_path: Path, install_path: Path) -> dict[str, bool]:
    return {
        "template_available": template_path.is_file(),
        "installed": install_path.is_file(),
    }


def developer_payload() -> dict[str, Any]:
    scope = ["knowledge-base-id"]
    return {
        "openapi_url": "/openapi.json",
        "examples": [
            {
                "label": "Agent Retrieval",
                "method": "POST",
                "path": "/api/retrieval",
                "body": {
                    "query": "本季度营业收入和净利润分别是多少？",
                    "knowledge_base_ids": scope,
                    "mode": "auto",
                    "top_k": 8,
                },
            },
            {
                "label": "Source Context",
                "method": "GET",
                "path": "/api/sources/{source_id}/context?before=2&after=2",
                "body": None,
            },
            {
                "label": "Health",
                "method": "GET",
                "path": "/api/health",
                "body": None,
            },
        ],
    }


class Diagnostics:
    def __init__(
        self,
        store_factory: Callable[[], EvidenceStore],
        providers: Mapping[str, Mapping[str, Any]],
        checks: Mapping[str, Callable[[], Any]] | None = None,
    ):
        self.store_factory = store_factory
        self.providers = providers
        self.checks = dict(checks or {})

    def run(self, target: str) -> dict[str, Any]:
        if target == "service":
            try:
                runtime_payload(self.store_factory())
            except Exception:
                return _diagnostic_result(
                    target,
                    ok=False,
                    code="service_unavailable",
                    message="服务或数据库状态检查失败。",
                    action="检查数据库文件权限、完整性和服务日志后重试。",
                )
            return _diagnostic_result(
                target,
                ok=True,
                code="ok",
                message="服务与数据库连接正常。",
            )

        provider = self.providers[target]
        display_name = "MinerU" if target == "mineru" else "Embedding"
        if provider["configured"] is not True:
            return _diagnostic_result(
                target,
                ok=False,
                code="provider_not_configured",
                message=f"{display_name} 尚未配置。",
                action=f"在服务端配置 {display_name} Provider 后重试。",
            )
        check = self.checks.get(target)
        if check is None:
            return _diagnostic_result(
                target,
                ok=False,
                code="diagnostic_not_available",
                message=f"{display_name} 已配置，但当前进程没有诊断适配器。",
                action=f"启动服务时注入 {display_name} 诊断检查后重试。",
            )
        try:
            check()
        except Exception:
            return _diagnostic_result(
                target,
                ok=False,
                code="diagnostic_failed",
                message=f"{display_name} 连接测试失败。",
                action=f"检查服务端 {display_name} 配置和网络连通性后重试。",
            )
        return _diagnostic_result(
            target,
            ok=True,
            code="ok",
            message=f"{display_name} 连接测试成功。",
        )


def _diagnostic_result(
    target: str,
    *,
    ok: bool,
    code: str,
    message: str,
    action: str | None = None,
) -> dict[str, Any]:
    return {
        "target": target,
        "ok": ok,
        "code": code,
        "message": message,
        "action": action,
    }


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_bounded(descriptor: int, maximum_bytes: int) -> bytes:
    payload = bytearray()
    while len(payload) <= maximum_bytes:
        chunk = os.read(descriptor, min(8192, maximum_bytes + 1 - len(payload)))
        if not chunk:
            break
        payload.extend(chunk)
    if len(payload) > maximum_bytes:
        raise InvalidSettingsFile("Settings file exceeds maximum size")
    return bytes(payload)


def _contains_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _validate_hostname(hostname: str) -> None:
    if ":" in hostname:
        try:
            parsed = ipaddress.ip_address(hostname)
        except ValueError as exc:
            raise ValueError("Public base URL has an invalid IPv6 hostname") from exc
        if parsed.version != 6:
            raise ValueError("Public base URL has an invalid IPv6 hostname")
        return
    try:
        ipaddress.ip_address(hostname)
        return
    except ValueError:
        if all(character.isdigit() or character == "." for character in hostname):
            raise ValueError("Public base URL has an invalid IP address")
    candidate = hostname.rstrip(".")
    try:
        ascii_hostname = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("Public base URL has an invalid hostname") from exc
    if not ascii_hostname or len(ascii_hostname) > 253:
        raise ValueError("Public base URL has an invalid hostname")
    labels = ascii_hostname.split(".")
    if any(not _HOST_LABEL.fullmatch(label) for label in labels):
        raise ValueError("Public base URL has an invalid hostname")


def _nonempty_environment_value(name: str) -> bool:
    value = os.environ.get(name)
    return isinstance(value, str) and bool(value.strip())
