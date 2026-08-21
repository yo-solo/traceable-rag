from __future__ import annotations

import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from . import __version__
from .store import SCHEMA_VERSION, EvidenceStore


DEFAULT_MINIMUM_FREE_BYTES = 256 * 1024 * 1024


class WorkerStatus(Protocol):
    @property
    def is_alive(self) -> bool: ...

    @property
    def fatal_error_type(self) -> str | None: ...


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    ok: bool
    message: str

    def payload(self) -> dict[str, str | bool]:
        return {"name": self.name, "ok": self.ok, "message": self.message}


def liveness_payload() -> dict[str, str | bool]:
    return {"ok": True, "status": "live", "version": __version__}


def readiness_payload(
    *,
    store_factory: Callable[[], EvidenceStore],
    managed_root: str | Path,
    worker: WorkerStatus | None,
    worker_required: bool,
    minimum_free_bytes: int = DEFAULT_MINIMUM_FREE_BYTES,
    provider_infrastructure_check: Callable[[], bool] | None = None,
    normalization_version: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    checks = [
        _database_check(store_factory),
        _storage_write_check(Path(managed_root)),
        _disk_space_check(Path(managed_root), minimum_free_bytes),
        _worker_check(worker, required=worker_required),
        _provider_infrastructure_check(provider_infrastructure_check),
        _content_upgrade_check(store_factory, normalization_version),
    ]
    ready = all(check.ok for check in checks)
    return ready, {
        "ok": ready,
        "status": "ready" if ready else "not_ready",
        "version": __version__,
        "checks": [check.payload() for check in checks],
    }


def _database_check(
    store_factory: Callable[[], EvidenceStore],
) -> ReadinessCheck:
    try:
        store = store_factory()
        with store.connect() as connection:
            result = connection.execute(
                "select value from schema_metadata where key = 'schema_version'"
            ).fetchone()
        if result is None or result[0] != SCHEMA_VERSION:
            return ReadinessCheck(
                "database", False, "数据库版本与当前服务不兼容。"
            )
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        return ReadinessCheck("database", False, "数据库不可用或版本不兼容。")
    return ReadinessCheck("database", True, "数据库可用且版本兼容。")


def _storage_write_check(path: Path) -> ReadinessCheck:
    probe: Path | None = None
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".readiness-{uuid.uuid4().hex}.tmp"
        descriptor = os.open(
            probe,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            os.write(descriptor, b"ready\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        return ReadinessCheck("storage", False, "数据目录不可写。")
    finally:
        if probe is not None:
            probe.unlink(missing_ok=True)
    return ReadinessCheck("storage", True, "数据目录可写。")


def _disk_space_check(path: Path, minimum_free_bytes: int) -> ReadinessCheck:
    try:
        free = shutil.disk_usage(path).free
    except OSError:
        return ReadinessCheck("disk_space", False, "无法读取数据目录剩余空间。")
    if free < minimum_free_bytes:
        return ReadinessCheck(
            "disk_space",
            False,
            f"数据目录剩余空间不足 {minimum_free_bytes} 字节。",
        )
    return ReadinessCheck("disk_space", True, "数据目录剩余空间充足。")


def _worker_check(
    worker: WorkerStatus | None, *, required: bool
) -> ReadinessCheck:
    if not required:
        return ReadinessCheck("worker", True, "当前进程未要求启动处理 Worker。")
    if worker is None or not worker.is_alive:
        return ReadinessCheck("worker", False, "文档处理 Worker 未运行。")
    if worker.fatal_error_type is not None:
        return ReadinessCheck("worker", False, "文档处理 Worker 已异常停止。")
    return ReadinessCheck("worker", True, "文档处理 Worker 正常运行。")


def _provider_infrastructure_check(
    check: Callable[[], bool] | None,
) -> ReadinessCheck:
    if check is None:
        return ReadinessCheck(
            "provider_infrastructure",
            True,
            "当前运行方式未要求持久化 Provider 管理基础设施。",
        )
    try:
        available = bool(check())
    except Exception:
        available = False
    if not available:
        return ReadinessCheck(
            "provider_infrastructure",
            False,
            "Provider 主密钥、管理授权或加密存储不可用。",
        )
    return ReadinessCheck(
        "provider_infrastructure",
        True,
        "Provider 主密钥、管理授权和加密存储可用。",
    )


def _content_upgrade_check(
    store_factory: Callable[[], EvidenceStore],
    normalization_version: str | None,
) -> ReadinessCheck:
    try:
        store = store_factory()
        with store.connect() as connection:
            if normalization_version is None:
                pending = int(
                    connection.execute(
                        """
                        select count(*)
                        from processing_jobs
                        where operation like 'normalize_upgrade:%'
                          and status in ('queued', 'running')
                        """
                    ).fetchone()[0]
                )
                failed = int(
                    connection.execute(
                        """
                        select count(*)
                        from processing_jobs
                        where operation like 'normalize_upgrade:%'
                          and status = 'failed'
                        """
                    ).fetchone()[0]
                )
            else:
                target_operation = f"normalize_upgrade:{normalization_version}"
                pending = int(
                    connection.execute(
                        """
                        select count(*)
                        from documents
                        where coalesce(normalization_version, '') != ?
                          and processing_status in ('parsed', 'ready')
                        """,
                        (normalization_version,),
                    ).fetchone()[0]
                )
                failed = int(
                    connection.execute(
                        """
                        with ranked as (
                            select processing_jobs.status,
                                   row_number() over (
                                       partition by processing_jobs.document_id
                                       order by processing_jobs.created_at desc,
                                                processing_jobs.rowid desc
                                   ) as position
                            from processing_jobs
                            join documents
                              on documents.id = processing_jobs.document_id
                            where processing_jobs.operation = ?
                              and coalesce(documents.normalization_version, '') != ?
                        )
                        select count(*) from ranked
                        where position = 1 and status = 'failed'
                        """,
                        (target_operation, normalization_version),
                    ).fetchone()[0]
                )
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        return ReadinessCheck(
            "content_upgrade", False, "无法确认内容升级状态。"
        )
    if failed:
        return ReadinessCheck(
            "content_upgrade", False, f"有 {failed} 个内容升级任务失败。"
        )
    if pending:
        return ReadinessCheck(
            "content_upgrade", False, f"仍有 {pending} 个文档的内容升级未完成。"
        )
    return ReadinessCheck("content_upgrade", True, "没有待完成的内容升级任务。")
