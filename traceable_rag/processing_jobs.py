from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import replace
from typing import Callable

from .document_processing import (
    DocumentProcessingService,
    MinerUConfigurationError,
    NormalizationSourceUnavailableError,
    PageLimitExceededError,
)
from .mineru_client import (
    MinerUAPIError,
    MinerUUploadError,
    sanitize_diagnostic_text,
)
from .models import ProcessingJob
from .store import EvidenceStore


logger = logging.getLogger(__name__)

NORMALIZATION_UPGRADE_PREFIX = "normalize_upgrade:"


def normalization_upgrade_operation(normalization_version: str) -> str:
    return f"{NORMALIZATION_UPGRADE_PREFIX}{normalization_version}"


def is_normalization_upgrade_operation(operation: str) -> bool:
    return operation == "normalize_upgrade" or operation.startswith(
        NORMALIZATION_UPGRADE_PREFIX
    )


class ProcessingJobAlreadyActiveError(RuntimeError):
    pass


_MINERU_ERRORS: dict[str, tuple[str, str, bool]] = {
    "-500": ("mineru_invalid_request", "MinerU 请求参数无效。", False),
    "-10001": ("mineru_service_unavailable", "MinerU 服务暂时异常，请稍后重试。", True),
    "-10002": ("mineru_invalid_request", "MinerU 请求参数格式错误。", False),
    "-60001": ("mineru_upload_url_failed", "MinerU 生成上传地址失败，请稍后重试。", True),
    "-60002": ("mineru_unsupported_file", "MinerU 无法识别该文件格式。", False),
    "-60003": ("mineru_file_read_failed", "MinerU 无法读取文件，请检查文件是否损坏。", False),
    "-60004": ("mineru_empty_file", "MinerU 检测到空文件。", False),
    "-60005": ("mineru_file_too_large", "文件超过 MinerU 当前 200MB 限制。", False),
    "-60006": ("page_limit_exceeded", "文件页数超过 MinerU 限制，请拆分后重试。", False),
    "-60007": ("mineru_service_unavailable", "MinerU 模型服务暂时不可用，请稍后重试。", True),
    "-60008": ("mineru_file_read_timeout", "MinerU 读取文件超时，请稍后重试。", True),
    "-60009": ("mineru_queue_full", "MinerU 任务队列已满，请稍后重试。", True),
    "-60010": ("mineru_extract_failed", "MinerU 解析失败，请稍后重试。", True),
    "-60011": ("mineru_uploaded_file_missing", "MinerU 未找到已上传的有效文件。", True),
    "-60012": ("mineru_task_not_found", "MinerU 任务不存在或已失效。", True),
    "-60013": ("mineru_task_forbidden", "当前凭据无权访问该 MinerU 任务。", False),
    "-60014": ("mineru_task_running", "MinerU 任务仍在运行，当前不能删除。", True),
    "-60015": ("mineru_conversion_failed", "MinerU 文件转换失败，可尝试转为 PDF 后上传。", False),
    "-60016": ("mineru_conversion_failed", "MinerU 文件转换失败，请更换导出格式后重试。", False),
    "-60017": ("mineru_retry_limit_reached", "MinerU 已达到该任务重试上限。", False),
    "-60018": ("mineru_daily_limit_reached", "MinerU 今日解析任务额度已用尽。", True),
    "-60019": ("mineru_html_quota_exhausted", "MinerU HTML 解析额度已用尽。", True),
    "-60020": ("mineru_file_split_failed", "MinerU 文件拆分失败，请稍后重试。", True),
    "-60021": ("mineru_page_count_failed", "MinerU 无法读取文件页数，请稍后重试。", True),
    "-60022": ("mineru_web_read_failed", "MinerU 网页读取失败或被限频，请稍后重试。", True),
    "401": ("mineru_auth_failed", "MinerU 凭据无效或已过期。", False),
    "403": ("mineru_auth_failed", "MinerU 拒绝当前凭据访问。", False),
    "429": ("mineru_rate_limited", "MinerU 请求过于频繁，请稍后重试。", True),
}


def enqueue_document_processing(
    store: EvidenceStore,
    knowledge_base_id: str,
    document_id: str,
    *,
    operation: str = "process",
) -> ProcessingJob:
    try:
        job = store.enqueue_processing_job(
            str(uuid.uuid4()), knowledge_base_id, document_id, operation=operation
        )
    except sqlite3.IntegrityError as exc:
        raise ProcessingJobAlreadyActiveError(document_id) from exc
    document = store.get_document(knowledge_base_id, document_id)
    if document is not None and not is_normalization_upgrade_operation(operation):
        store.upsert_document(
            replace(document, processing_status="queued", processing_error=None)
        )
    return job


class ProcessingWorker:
    def __init__(
        self,
        store: EvidenceStore | None = None,
        service: DocumentProcessingService | None = None,
        *,
        store_factory: Callable[[], EvidenceStore] | None = None,
        service_factory: Callable[[], DocumentProcessingService] | None = None,
        max_pages: int = 200,
        poll_interval: float = 0.5,
    ):
        if store is None and store_factory is None:
            raise ValueError("A processing-job store or store factory is required")
        if service is None and service_factory is None:
            raise ValueError("A processing service or service factory is required")
        self._store = store
        self._service = service
        self._store_factory = store_factory
        self._service_factory = service_factory
        self.max_pages = max_pages
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._fatal_error_type: str | None = None

    @property
    def store(self) -> EvidenceStore:
        if self._store is None:
            assert self._store_factory is not None
            self._store = self._store_factory()
        return self._store

    @property
    def service(self) -> DocumentProcessingService:
        if self._service is None:
            assert self._service_factory is not None
            self._service = self._service_factory()
        return self._service

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        recovered = self.store.recover_processing_jobs()
        if recovered:
            logger.info("processing_jobs_recovered count=%s", recovered)
        self._stop.clear()
        self._fatal_error_type = None
        self._thread = threading.Thread(
            target=self._run, name="traceable-rag-processing-worker", daemon=True
        )
        self._thread.start()

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def fatal_error_type(self) -> str | None:
        return self._fatal_error_type

    def notify(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                job = self.store.claim_next_processing_job()
                if job is None:
                    self._wake.wait(self.poll_interval)
                    self._wake.clear()
                    continue
                self._process(job)
        except Exception as exc:
            self._fatal_error_type = type(exc).__name__
            logger.error(
                "processing_worker_stopped status=fatal exception_type=%s",
                self._fatal_error_type,
            )

    def _process(self, job: ProcessingJob) -> None:
        started = time.monotonic()
        normalization_upgrade = is_normalization_upgrade_operation(job.operation)
        current_stage = "normalizing" if normalization_upgrade else "converting"

        def progress(stage: str) -> None:
            nonlocal current_stage
            current_stage = stage
            self.store.update_processing_job(job.id, stage=stage)
            document = self.store.get_document(job.knowledge_base_id, job.document_id)
            if document is not None and not normalization_upgrade:
                status = "indexing" if stage == "indexing" else "parsing"
                self.store.upsert_document(replace(document, processing_status=status))
            logger.info(
                "processing_job_stage job_id=%s knowledge_base_id=%s document_id=%s "
                "stage=%s status=running duration_ms=%s",
                job.id,
                job.knowledge_base_id,
                job.document_id,
                stage,
                int((time.monotonic() - started) * 1000),
            )

        try:
            if normalization_upgrade:
                self.service.rebuild_document_content(
                    job.knowledge_base_id,
                    job.document_id,
                    progress=progress,
                )
            else:
                self.service.process_document(
                    job.knowledge_base_id,
                    job.document_id,
                    max_pages=self.max_pages,
                    progress=progress,
                )
        except Exception as exc:
            code, message, retryable = classify_processing_error(exc, current_stage)
            diagnostic = processing_error_diagnostic(exc)
            self.store.update_processing_job(
                job.id,
                status="failed",
                stage=current_stage,
                error_code=code,
                error_message=message,
                retryable=retryable,
                finished=True,
            )
            logger.error(
                "processing_job_failed job_id=%s knowledge_base_id=%s document_id=%s "
                "stage=%s status=failed error_code=%s duration_ms=%s exception_type=%s "
                "diagnostic_id=%s diagnostic=%s",
                job.id,
                job.knowledge_base_id,
                job.document_id,
                current_stage,
                code,
                int((time.monotonic() - started) * 1000),
                type(exc).__name__,
                job.id,
                json.dumps(diagnostic, ensure_ascii=False, separators=(",", ":")),
            )
            return
        self.store.update_processing_job(
            job.id, status="succeeded", stage="complete", finished=True
        )
        logger.info(
            "processing_job_succeeded job_id=%s knowledge_base_id=%s document_id=%s "
            "stage=complete status=succeeded duration_ms=%s",
            job.id,
            job.knowledge_base_id,
            job.document_id,
            int((time.monotonic() - started) * 1000),
        )


def classify_processing_error(
    exc: Exception, stage: str
) -> tuple[str, str, bool]:
    if isinstance(exc, MinerUConfigurationError):
        return (
            "document_processing_not_configured",
            "文档解析服务尚未配置。请在运行状态中确认服务配置后再重新读取。",
            False,
        )
    if isinstance(exc, NormalizationSourceUnavailableError):
        return (
            "normalization_source_unavailable",
            "这份旧文档缺少可复用的内容结果，请手动重新处理。",
            False,
        )
    if isinstance(exc, PageLimitExceededError):
        return (
            "page_limit_exceeded",
            f"文件共 {exc.page_count} 页，超过 MinerU 当前 {exc.page_limit} 页处理上限。请拆分文件后重新上传。",
            False,
        )
    if isinstance(exc, MinerUAPIError):
        mapped = _MINERU_ERRORS.get(exc.code)
        if mapped is not None:
            return mapped
        return "mineru_rejected", "MinerU 拒绝了文档处理请求。", False
    if isinstance(exc, TimeoutError):
        return "mineru_timeout", "等待 MinerU 处理结果超时，请稍后重试。", True
    if stage == "converting":
        return "conversion_failed", "文档转换为 PDF 失败。", False
    if stage == "page_check":
        return "pdf_inspection_failed", "无法读取转换后 PDF 的页数。", False
    if stage == "mineru_submit":
        return "mineru_upload_failed", "文件提交到 MinerU 失败，请稍后重试。", True
    if stage in {"mineru_poll", "mineru_download"}:
        return "mineru_result_download_failed", "获取 MinerU 处理结果失败，请稍后重试。", True
    if stage == "normalizing":
        return "normalization_failed", "MinerU 结果归一化失败。", False
    if stage == "indexing":
        return "indexing_failed", "文档索引生成失败。", True
    if stage == "persist":
        return "storage_failed", "文档处理结果保存失败。", True
    return "unexpected_processing_error", "文档处理遇到未预期错误。", True


def processing_error_diagnostic(exc: Exception) -> dict[str, object]:
    if isinstance(exc, MinerUConfigurationError):
        return {"reason": "missing_configuration"}
    if isinstance(exc, MinerUAPIError):
        return {
            "provider": "mineru",
            "provider_code": exc.code,
            "provider_response": sanitize_diagnostic_text(exc.provider_message),
        }
    if isinstance(exc, MinerUUploadError):
        diagnostic: dict[str, object] = {
            "provider": "mineru_upload",
            "reason": exc.reason,
        }
        if exc.http_status is not None:
            diagnostic["http_status"] = exc.http_status
        if exc.curl_exit_code is not None:
            diagnostic["curl_exit_code"] = exc.curl_exit_code
        if exc.response_detail:
            diagnostic["provider_response"] = sanitize_diagnostic_text(
                exc.response_detail
            )
        return diagnostic
    if isinstance(exc, PageLimitExceededError):
        return {
            "reason": "page_limit_exceeded",
            "page_count": exc.page_count,
            "page_limit": exc.page_limit,
        }
    if isinstance(exc, TimeoutError):
        return {"reason": "timeout"}
    return {"reason": "unexpected", "exception_type": type(exc).__name__}
