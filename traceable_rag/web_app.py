from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .api_models import (
    KnowledgeBaseId,
    KnowledgeBaseCreateRequest,
    KnowledgeBasePatchRequest,
    KnowledgeBaseDeleteRequest,
    DocumentPatchRequest,
    ManagedProcessRequest,
    RetrievalRequest,
    RetrievalWarningModel,
    VisualAssetReferenceModel,
    VisualAssetDetailModel,
    DocumentVisualAssetDetailModel,
    RetrievalSourceModel,
    RetrievalApiResponseModel,
    SourceContextItemModel,
    SourceContextSourceModel,
    SourceContextResponseModel,
    ApiErrorDetailModel,
    ApiErrorResponseModel,
    HealthResponseModel,
    KnowledgeBasePayloadResponse,
    KnowledgeBaseResponseModel,
    KnowledgeBaseListResponseModel,
    KnowledgeBaseDeleteResponseModel,
    DocumentDeleteResponseModel,
    ProcessingJobErrorResponse,
    ProcessingJobPayloadResponse,
    DocumentBlockResponse,
    DocumentPageMetadataResponse,
    DocumentBlocksResponseModel,
    DocumentGraphStatsResponse,
    DocumentPayloadResponse,
    DocumentResponseModel,
    ProcessingJobResponseModel,
    ProcessingJobListResponseModel,
    UploadDocumentResponseModel,
    DocumentListResponseModel,
    ManagedProcessResponseModel,
    SettingsPatchRequest,
    DiagnosticRequest,
    SafeSettingsResponse,
    SettingsPersistenceResponse,
    RuntimeServiceResponse,
    RuntimeCountsResponse,
    RuntimeFailureResponse,
    RuntimeStatusResponse,
    ProviderStatusResponse,
    ProvidersResponse,
    ProviderRole,
    ProviderProfileWriteRequest,
    ProviderProfileResponse,
    ProviderProfilesMapResponse,
    ProviderProfilesResponse,
    ProviderTestResultResponse,
    SkillStatusResponse,
    ApiReferenceExampleResponse,
    DeveloperReferenceResponse,
    SettingsResponseModel,
    SettingsPatchResponseModel,
    DiagnosticResultResponse,
    DiagnosticResponseModel,
)
from .document_processing import (
    DocumentAlreadyExistsError,
    DocumentBusyError,
    DocumentProcessingService,
    KnowledgeBaseDeleteConfirmationError,
    KnowledgeBaseNotFoundError,
    KnowledgeBaseRecoveryBlockedError,
)
from .embedding import (
    embed_query_texts,
    provider_from_api_doc,
)
from .evidence_service import (
    EvidenceService,
    EvidenceWebUrlTooLongError,
    NoIndexedDocumentsError,
)
from .models import Document, KnowledgeBase, ProcessingJob
from .mineru_import import NORMALIZATION_VERSION, load_cross_page_block_merges
from .processing_jobs import (
    ProcessingJobAlreadyActiveError,
    ProcessingWorker,
    enqueue_document_processing,
    is_normalization_upgrade_operation,
    normalization_upgrade_operation,
)
from .indexing import complete_embedding_dimension, validate_embedding_vectors
from .image_assets import resolve_managed_image_asset, visual_asset_payload
from .public_payload import public_document_blocks, public_page_metadata
from .request_limits import ApiWriteRequestBodyLimitMiddleware
from .readiness import liveness_payload, readiness_payload
from .request_security import BrowserRequestBoundaryMiddleware
from .provider_profiles import (
    DEFAULT_MASTER_KEY_FILE,
    DEFAULT_PROVIDER_STORE_PATH,
    PROVIDER_ADMIN_TOKEN_HEADER,
    DynamicEmbeddingProvider,
    DynamicMinerUParser,
    ProviderProfileError,
    ProviderProfileNotConfiguredError,
    ProviderRegistry,
    ProviderStoreUnavailableError,
    ProviderTestNotSupportedError,
    environment_profile,
    provider_management_authorized,
    read_admin_token,
    secret_preserve_sentinel,
    test_provider_connection,
    utc_now,
)
from .runtime_config import (
    DEFAULT_DATABASE_PATH,
    DEFAULT_MANAGED_STORAGE_ROOT,
    DEFAULT_SETTINGS_PATH,
    DEFAULT_STATIC_DIR,
    RuntimeConfig,
    load_runtime_config,
)
from .settings import (
    Diagnostics,
    SettingsStore,
    developer_payload,
    runtime_payload,
    skill_payload,
    unavailable_runtime_payload,
)
from .source_service import SourceGoneError, SourceNotFoundError, SourceService
from .store import DocumentNotFoundError, EvidenceStore


SETTINGS_REQUEST_MAX_BYTES = 64 * 1024


OPENAPI_TAGS = [
    {"name": "系统", "description": "检查服务可用性和当前数据规模。"},
    {"name": "知识库", "description": "管理知识库及其名称、说明和文档统计。"},
    {"name": "文档", "description": "上传和管理文档，并读取预览、结构化内容和图片资产。"},
    {"name": "处理任务", "description": "提交文档处理任务并查询队列和执行进度。"},
    {"name": "检索", "description": "在指定知识库和文档范围内检索可核验原文。"},
    {"name": "原文定位", "description": "根据检索结果读取命中片段及其有限上下文。"},
    {"name": "设置", "description": "读取和修改非敏感设置，并检查运行依赖。"},
    {"name": "Provider", "description": "安全管理外部 Provider 配置和连接测试。"},
]


def _api_error_responses(*status_codes: int) -> dict[int, dict[str, Any]]:
    descriptions = {
        400: "请求格式无效。",
        403: "当前请求无权执行此管理操作。",
        404: "请求的资源不存在。",
        409: "当前资源状态不允许执行该操作。",
        410: "资源曾经存在，但当前版本已不可用。",
        413: "请求体或上传文件超过限制。",
        422: "请求字段不符合接口约束。",
        500: "服务未能安全完成操作。",
        503: "依赖服务未配置或暂时不可用。",
    }
    return {
        status_code: {
            "model": ApiErrorResponseModel,
            "description": descriptions[status_code],
        }
        for status_code in status_codes
    }


def create_app(
    *,
    db_path: str | Path = "storage/traceable_v3.db",
    api_doc_path: str | Path | None = None,
    static_dir: str | Path = "web/dist",
    managed_root: str | Path = "storage/managed",
    document_processing_service: DocumentProcessingService | None = None,
    embedding_provider: Any | None = None,
    reranker: Any | None = None,
    max_upload_bytes: int = 100 * 1024 * 1024,
    settings_path: str | Path = "storage/settings.json",
    mineru_provider_configured: bool | None = None,
    diagnostic_checks: Mapping[str, Callable[[], Any]] | None = None,
    skill_template_path: str | Path = "integrations/codex-skill/traceable-rag/SKILL.md",
    skill_install_path: str | Path = "~/.codex/skills/traceable-rag/SKILL.md",
    runtime_root: str | Path | None = None,
    runtime_config: RuntimeConfig | None = None,
    start_processing_worker: bool = True,
    mineru_max_pages: int | None = None,
    provider_store_path: str | Path | None = None,
    provider_master_key_file: str | Path | None = None,
    provider_admin_token_file: str | Path | None = None,
    provider_management_allow_insecure_dev: bool | None = None,
    trusted_hosts: tuple[str, ...] | None = None,
    provider_http_client: Any | None = None,
    provider_test_http_client: Any | None = None,
) -> FastAPI:
    """Create the current knowledge-base API.

    Managed document processing is used for every upload and retry.
    """

    runtime = runtime_config
    if runtime is None and runtime_root is not None:
        runtime = load_runtime_config(root=runtime_root)
    if runtime is not None:
        if Path(db_path) == DEFAULT_DATABASE_PATH:
            db_path = runtime.database_path
        if Path(static_dir) == DEFAULT_STATIC_DIR:
            static_dir = runtime.static_dir
        if Path(managed_root) == DEFAULT_MANAGED_STORAGE_ROOT:
            managed_root = runtime.managed_storage_root
        if Path(settings_path) == DEFAULT_SETTINGS_PATH:
            settings_path = runtime.settings_path
    if provider_store_path is None:
        provider_store_path = (
            runtime.provider_store_path
            if runtime is not None
            else DEFAULT_PROVIDER_STORE_PATH
        )
    if provider_master_key_file is None:
        provider_master_key_file = (
            runtime.master_key_file if runtime is not None else DEFAULT_MASTER_KEY_FILE
        )
    if provider_admin_token_file is None and runtime is not None:
        provider_admin_token_file = runtime.admin_token_file
    if provider_management_allow_insecure_dev is None:
        provider_management_allow_insecure_dev = bool(
            runtime and runtime.provider_management_allow_insecure_dev
        )
    if trusted_hosts is None:
        trusted_hosts = runtime.trusted_hosts if runtime is not None else None
    if trusted_hosts is None:
        trusted_hosts = (
            ("127.0.0.1", "localhost", "::1")
            if provider_admin_token_file is not None
            else ()
        )

    app = FastAPI(
        title="Traceable RAG Retrieval API",
        version="0.5.0",
        description=(
            "面向 AI Agent 的可核验文档检索 API。所有 Retrieval 都必须显式限定"
            "知识库范围，并返回可跳转到原文定位页的 Source。服务端不生成答案，也"
            "不保存 Agent Trace。Verifiable document retrieval for AI agents with "
            "scoped Retrieval and clickable Source context."
        ),
        openapi_tags=OPENAPI_TAGS,
        servers=[{"url": "/", "description": "当前同源部署"}],
    )
    app.add_middleware(ApiWriteRequestBodyLimitMiddleware)
    app.add_middleware(
        BrowserRequestBoundaryMiddleware,
        trusted_hosts=trusted_hosts,
    )
    db_path = Path(db_path)
    api_doc_path = Path(api_doc_path) if api_doc_path is not None else None
    static_dir = Path(static_dir)
    managed_root = Path(managed_root)
    settings_store = SettingsStore(settings_path)
    provider_registry = ProviderRegistry(
        runtime=runtime,
        runtime_root=runtime_root,
        store_path=provider_store_path,
        master_key_file=provider_master_key_file,
        http_client=provider_http_client,
    )
    app.state.provider_registry = provider_registry
    skill_template_path = Path(skill_template_path).expanduser()
    skill_install_path = Path(skill_install_path).expanduser()

    def store() -> EvidenceStore:
        return EvidenceStore(db_path)

    cached_processing_service: DocumentProcessingService | None = None
    def current_reranker() -> Any | None:
        if reranker is not None:
            return reranker
        try:
            return provider_registry.reranker()
        except Exception:
            return None

    def processing_service() -> DocumentProcessingService:
        nonlocal cached_processing_service
        if cached_processing_service is not None:
            return cached_processing_service
        if document_processing_service is not None:
            cached_processing_service = document_processing_service
            return cached_processing_service
        automatic_embedding_provider = (
            embedding_provider
            if embedding_provider is not None
            else DynamicEmbeddingProvider(provider_registry)
        )
        cached_processing_service = DocumentProcessingService(
            store(),
            managed_root,
            mineru_client=DynamicMinerUParser(provider_registry),
            embedding_provider=automatic_embedding_provider,
            embedding_api_doc_path=api_doc_path,
            runtime_root=(runtime_root if runtime_root is not None else Path.cwd()),
        )
        return cached_processing_service

    resolved_mineru_max_pages = mineru_max_pages
    if resolved_mineru_max_pages is None:
        try:
            resolved_mineru_max_pages = int(os.environ.get("MINERU_MAX_PAGES", "200"))
        except ValueError:
            resolved_mineru_max_pages = 200
    resolved_mineru_max_pages = max(1, resolved_mineru_max_pages)
    processing_worker = ProcessingWorker(
        store_factory=store,
        service_factory=processing_service,
        max_pages=resolved_mineru_max_pages,
    )
    app.state.processing_worker = processing_worker
    if start_processing_worker:
        def start_worker_and_enqueue_content_upgrades() -> None:
            processing_worker.start()
            current = store()
            queued = False
            for document in current.list_documents_needing_normalization(
                NORMALIZATION_VERSION
            ):
                try:
                    enqueue_document_processing(
                        current,
                        document.knowledge_base_id,
                        document.id,
                        operation=normalization_upgrade_operation(
                            NORMALIZATION_VERSION
                        ),
                    )
                    queued = True
                except ProcessingJobAlreadyActiveError:
                    continue
            if queued:
                processing_worker.notify()

        app.router.add_event_handler("startup", start_worker_and_enqueue_content_upgrades)
        app.router.add_event_handler("shutdown", processing_worker.stop)

    def source_service() -> SourceService:
        return SourceService(store())

    @app.get(
        "/livez",
        tags=["系统"],
        summary="进程存活探针",
        description="仅确认当前 ASGI 进程仍可响应，不访问数据库、存储或外部 Provider。",
    )
    def livez() -> dict:
        return liveness_payload()

    @app.get(
        "/readyz",
        tags=["系统"],
        summary="服务就绪探针",
        description=(
            "检查数据库、数据目录、剩余空间、处理 Worker、Provider 安全基础设施"
            "和当前版本待完成的内容升级。"
        ),
        response_model=None,
    )
    def readyz() -> Any:
        provider_infrastructure_check = None
        if provider_admin_token_file is not None:
            def provider_infrastructure_check() -> bool:
                read_admin_token(provider_admin_token_file)
                return provider_registry.storage_available()

        ready, payload = readiness_payload(
            store_factory=store,
            managed_root=managed_root,
            worker=processing_worker,
            worker_required=start_processing_worker,
            provider_infrastructure_check=provider_infrastructure_check,
            normalization_version=NORMALIZATION_VERSION,
        )
        if not ready:
            return JSONResponse(payload, status_code=503)
        return payload

    def query_embedding(
        query: str,
        *,
        retrieval_mode_requested: str,
        retrieval_scope: tuple[list, list, list, list],
    ) -> tuple[list[float] | None, str | None, str | None, list[str]]:
        if retrieval_mode_requested != "embedding":
            return None, None, None, []
        try:
            provider = embedding_provider
            if provider is None:
                try:
                    # Resolve one concrete provider snapshot so the index fingerprint
                    # and outbound request cannot observe different hot-reload states.
                    provider = provider_registry.embedding_provider()
                except ProviderStoreUnavailableError:
                    raise
                except Exception:
                    if provider_registry.has_saved_profile("embedding"):
                        raise
                    provider = provider_from_api_doc(
                        api_doc_path,
                        runtime_root=runtime_root,
                    )
        except Exception:
            return (
                None,
                None,
                None,
                ["Embedding Provider 未配置或当前不可用，已回退关键词检索。"],
            )
        provider_name = getattr(provider, "provider_name", None)
        model_name = getattr(provider, "model_name", None)
        if (
            not isinstance(provider_name, str)
            or not provider_name
            or not isinstance(model_name, str)
            or not model_name
        ):
            return (
                None,
                None,
                None,
                ["Embedding Provider 未配置或当前不可用，已回退关键词检索。"],
            )
        records = retrieval_scope[2]
        if complete_embedding_dimension(
            records,
            provider_name=provider_name,
            model_name=model_name,
        ) is None:
            return (
                None,
                provider_name,
                model_name,
                [
                    "所选知识库的 Embedding 索引未完整就绪或与查询模型不兼容，已回退关键词检索。"
                ],
            )
        try:
            vectors = embed_query_texts(provider, [query])
            validate_embedding_vectors(vectors, expected_count=1)
            return vectors[0], provider_name, model_name, []
        except Exception:
            return (
                None,
                None,
                None,
                ["查询向量生成失败，已回退关键词检索。"],
            )

    @app.get(
        "/api/health",
        tags=["系统"],
        summary="查看服务健康状态",
        description=(
            "用于负载均衡探活和状态页。返回服务是否正常，以及当前知识库、文档总数；"
            "不会访问 MinerU、Embedding 或 Reranker 等外部服务。"
        ),
        response_model=HealthResponseModel,
    )
    def health() -> dict:
        current = store()
        knowledge_bases = current.list_knowledge_bases()
        documents = [
            document
            for knowledge_base in knowledge_bases
            for document in current.list_documents(knowledge_base.id)
        ]
        return {
            "ok": True,
            "knowledge_bases": len(knowledge_bases),
            "documents": len(documents),
        }

    def current_provider_payload() -> dict:
        def configured(role: str) -> bool:
            persisted = provider_registry.metadata_payload(role)
            return bool(
                persisted["status"] != "unavailable" and persisted["configured"]
            )

        return {
            "mineru": {
                "configured": (
                    mineru_provider_configured
                    if mineru_provider_configured is not None
                    else configured("mineru")
                )
            },
            "embedding": {
                "configured": (
                    True
                    if embedding_provider is not None
                    else configured("embedding")
                )
            },
        }

    def require_provider_management(request: Request) -> None:
        authorized, unavailable = provider_management_authorized(
            client_host=(request.client.host if request.client is not None else None),
            supplied_token=request.headers.get(PROVIDER_ADMIN_TOKEN_HEADER),
            token_file=provider_admin_token_file,
            allow_insecure_dev=bool(provider_management_allow_insecure_dev),
        )
        if unavailable is not None:
            _raise_api_error(
                503,
                "provider_management_auth_unavailable",
                "Provider management authorization is unavailable.",
            )
        if not authorized:
            _raise_api_error(
                403,
                "provider_management_forbidden",
                "Provider management is restricted to authorized local requests.",
            )

    @app.get(
        "/api/provider-profiles",
        tags=["Provider"],
        summary="获取安全化 Provider 配置",
        description=(
            "返回四个 Provider 角色的非敏感元数据、来源和测试状态。密钥及其片段"
            "永远不会出现在响应中。"
        ),
        response_model=ProviderProfilesResponse,
    )
    def get_provider_profiles() -> dict:
        return provider_registry.all_metadata_payload()

    @app.put(
        "/api/provider-profiles/{role}",
        tags=["Provider"],
        summary="保存 Provider 配置",
        description=(
            "加密保存指定角色配置。省略 `api_key` 会保留旧密钥，显式传 `null` 或"
            "空字符串会删除密钥；响应只返回密钥是否存在。"
        ),
        response_model=ProviderProfileResponse,
        responses=_api_error_responses(403, 422, 503),
    )
    async def put_provider_profile(role: ProviderRole, request: Request) -> dict:
        require_provider_management(request)
        payload = await _read_bounded_json_object(
            request,
            invalid_code="invalid_provider_profile",
            invalid_message="Provider profile must be a valid JSON object.",
        )
        try:
            write = ProviderProfileWriteRequest.model_validate(payload)
        except (ValidationError, TypeError, ValueError):
            _raise_api_error(
                422,
                "invalid_provider_profile",
                "Provider profile contains unsupported or invalid fields.",
            )
        if not write.model_fields_set:
            _raise_api_error(
                422,
                "invalid_provider_profile",
                "At least one Provider profile field is required.",
            )
        changes = write.model_dump(
            mode="python", exclude_unset=True, exclude={"api_key"}
        )
        if changes.get("adapter", object()) is None:
            changes.pop("adapter", None)
        secret_update: object | str | None = secret_preserve_sentinel()
        if "api_key" in write.model_fields_set:
            secret_update = (
                write.api_key.get_secret_value() if write.api_key is not None else None
            )
        try:
            provider_registry.put(role, changes, secret_update=secret_update)
            return provider_registry.metadata_payload(role)
        except ProviderStoreUnavailableError:
            _raise_api_error(
                503,
                "provider_store_unavailable",
                "Encrypted Provider storage is unavailable.",
            )
        except ProviderProfileError:
            _raise_api_error(
                422,
                "invalid_provider_profile",
                "Provider profile contains unsupported or invalid fields.",
            )
        except Exception:
            _raise_api_error(
                503,
                "provider_store_unavailable",
                "Encrypted Provider storage is unavailable.",
            )

    @app.delete(
        "/api/provider-profiles/{role}",
        tags=["Provider"],
        summary="删除持久化 Provider 配置",
        description=(
            "只删除加密存储中的配置。若同一角色仍由环境变量配置，删除后环境配置会"
            "重新生效，并在列表中标记其来源。"
        ),
        status_code=204,
        responses=_api_error_responses(403, 503),
    )
    def delete_provider_profile(role: ProviderRole, request: Request) -> Response:
        require_provider_management(request)
        try:
            provider_registry.delete(role)
        except ProviderStoreUnavailableError:
            _raise_api_error(
                503,
                "provider_store_unavailable",
                "Encrypted Provider storage is unavailable.",
            )
        except Exception:
            _raise_api_error(
                503,
                "provider_store_unavailable",
                "Encrypted Provider storage is unavailable.",
            )
        return Response(status_code=204)

    @app.post(
        "/api/provider-profiles/{role}/test",
        tags=["Provider"],
        summary="测试 Provider 连接",
        description=(
            "使用已保存配置或请求中的临时配置执行最小连接测试。Embedding、Reranker"
            "和 LLM 测试会访问外部服务并可能产生费用；MinerU 需用真实文档做端到端验证。"
        ),
        response_model=ProviderTestResultResponse,
        responses=_api_error_responses(403, 422, 503),
    )
    async def test_provider_profile(role: ProviderRole, request: Request) -> dict:
        require_provider_management(request)
        payload = await _read_bounded_json_object(
            request,
            invalid_code="invalid_provider_profile",
            invalid_message="Provider profile must be a valid JSON object.",
            allow_empty=True,
        )
        try:
            write = ProviderProfileWriteRequest.model_validate(payload)
        except (ValidationError, TypeError, ValueError):
            _raise_api_error(
                422,
                "invalid_provider_profile",
                "Provider profile contains unsupported or invalid fields.",
            )
        temporary = bool(write.model_fields_set)
        if temporary:
            changes = write.model_dump(
                mode="python", exclude_unset=True, exclude={"api_key"}
            )
            if changes.get("adapter", object()) is None:
                changes.pop("adapter", None)
            secret_update: object | str | None = secret_preserve_sentinel()
            if "api_key" in write.model_fields_set:
                secret_update = (
                    write.api_key.get_secret_value()
                    if write.api_key is not None
                    else None
                )
            try:
                profile = provider_registry.preview(
                    role, changes, secret_update=secret_update
                )
            except ProviderStoreUnavailableError:
                _raise_api_error(
                    503,
                    "provider_store_unavailable",
                    "Encrypted Provider storage is unavailable.",
                )
            except ProviderProfileError:
                _raise_api_error(
                    422,
                    "invalid_provider_profile",
                    "Provider profile contains unsupported or invalid fields.",
                )
        else:
            try:
                profile = provider_registry.resolve(role)
            except ProviderStoreUnavailableError:
                _raise_api_error(
                    503,
                    "provider_store_unavailable",
                    "Encrypted Provider storage is unavailable.",
                )
            if profile is None:
                _raise_api_error(
                    422,
                    "provider_not_configured",
                    "Provider profile is not configured.",
                )
        tested_at = utc_now()
        try:
            test_provider_connection(
                profile,
                http_client=(provider_test_http_client or provider_http_client),
            )
        except ProviderTestNotSupportedError:
            return {
                "role": role,
                "ok": False,
                "status": "not_supported",
                "message": "MinerU requires a real document end-to-end test.",
                "tested_at": tested_at,
            }
        except ProviderProfileNotConfiguredError:
            _raise_api_error(
                422,
                "provider_not_configured",
                "Provider profile is incomplete.",
            )
        except Exception:
            saved_profile = False
            if not temporary:
                try:
                    saved_profile = provider_registry.has_saved_profile(role)
                except ProviderStoreUnavailableError:
                    saved_profile = False
            if saved_profile:
                try:
                    provider_registry.record_test(role, "failed", tested_at)
                except Exception:
                    pass
            return {
                "role": role,
                "ok": False,
                "status": "failed",
                "message": (
                    "LLM connection test failed and may have incurred Provider charges."
                    if role == "llm"
                    else "Provider connection test failed."
                ),
                "tested_at": tested_at,
            }
        saved_profile = False
        if not temporary:
            try:
                saved_profile = provider_registry.has_saved_profile(role)
            except ProviderStoreUnavailableError:
                saved_profile = False
        if saved_profile:
            try:
                provider_registry.record_test(role, "success", tested_at)
            except Exception:
                pass
        return {
            "role": role,
            "ok": True,
            "status": "success",
            "message": (
                "LLM connection test succeeded and may incur Provider charges."
                if role == "llm"
                else "Provider connection test succeeded."
            ),
            "tested_at": tested_at,
        }

    @app.get(
        "/api/settings",
        tags=["设置"],
        summary="获取设置和运行状态",
        description=(
            "用于设置页和接入检查。返回非敏感默认配置、运行统计、外部服务配置状态、"
            "Codex Skill 状态和开发者入口；不会返回密钥、Authorization 或服务器内部路径。"
        ),
        response_model=SettingsResponseModel,
    )
    def get_settings() -> dict:
        loaded = settings_store.load()
        try:
            runtime = runtime_payload(store())
        except Exception:
            runtime = unavailable_runtime_payload()
        return {
            "settings": loaded.settings.payload(),
            "persistence": loaded.persistence_payload(),
            "runtime": runtime,
            "providers": current_provider_payload(),
            "skill": skill_payload(skill_template_path, skill_install_path),
            "developer": developer_payload(),
        }

    @app.patch(
        "/api/settings",
        tags=["设置"],
        summary="更新非敏感设置",
        description=(
            "修改允许公开编辑的默认设置，并以原子方式保存。仅接受白名单字段；设置默认"
            "知识库时，该知识库必须存在。"
        ),
        response_model=SettingsPatchResponseModel,
        responses=_api_error_responses(404, 422, 500),
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": SettingsPatchRequest.model_json_schema(),
                    }
                },
            }
        },
    )
    async def patch_settings(request_body: Request) -> dict:
        payload = await _read_bounded_json_object(
            request_body,
            invalid_code="invalid_settings",
            invalid_message="Settings payload must be a valid JSON object.",
        )
        try:
            request = SettingsPatchRequest.model_validate(payload)
        except (ValidationError, TypeError, ValueError):
            _raise_api_error(
                422,
                "invalid_settings",
                "Settings payload contains unsupported or invalid fields.",
            )
        changes = request.model_dump(mode="python", exclude_unset=True)
        mode_present = "default_retrieval_mode" in changes
        boolean_present = "default_use_embedding" in changes
        if mode_present and boolean_present:
            if changes["default_use_embedding"] != (
                changes["default_retrieval_mode"] == "embedding"
            ):
                _raise_api_error(
                    422,
                    "invalid_settings",
                    "Retrieval defaults must be consistent.",
                )
        elif mode_present:
            changes["default_use_embedding"] = (
                changes["default_retrieval_mode"] == "embedding"
            )
        elif boolean_present:
            changes["default_retrieval_mode"] = (
                "embedding" if changes["default_use_embedding"] else "keyword"
            )

        def validate_default_scope(updated) -> None:
            if updated.default_knowledge_base_id is not None:
                _require_knowledge_base(
                    store(), updated.default_knowledge_base_id
                )

        try:
            loaded = settings_store.patch(
                changes,
                validate=validate_default_scope,
            )
        except (OSError, ValueError, sqlite3.Error):
            _raise_api_error(
                500,
                "settings_persistence_failed",
                "Settings could not be persisted safely.",
            )
        return {
            "settings": loaded.settings.payload(),
            "persistence": loaded.persistence_payload(),
        }

    @app.post(
        "/api/settings/diagnostics",
        tags=["设置"],
        summary="运行依赖诊断",
        description=(
            "检查本地服务、MinerU 或 Embedding 服务是否可用。根据 `target` 执行对应检查；"
            "访问外部服务时可能产生少量调用费用，响应只返回安全化结果。"
        ),
        response_model=DiagnosticResponseModel,
        responses=_api_error_responses(422),
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": DiagnosticRequest.model_json_schema(),
                    }
                },
            }
        },
    )
    async def run_diagnostic(request_body: Request) -> dict:
        payload = await _read_bounded_json_object(
            request_body,
            invalid_code="invalid_diagnostic",
            invalid_message="Diagnostic payload must be a valid JSON object.",
        )
        try:
            request = DiagnosticRequest.model_validate(payload)
        except (ValidationError, TypeError, ValueError):
            _raise_api_error(
                422,
                "invalid_diagnostic",
                "Diagnostic target is invalid.",
            )
        diagnostic = Diagnostics(
            store,
            current_provider_payload(),
            diagnostic_checks,
        ).run(request.target)
        return {"diagnostic": diagnostic}

    @app.get(
        "/api/knowledge-bases",
        tags=["知识库"],
        summary="获取知识库列表",
        description=(
            "用于展示知识库选择器或管理列表。每项包含基本信息，以及文档就绪、处理中"
            "和失败数量。"
        ),
        response_model=KnowledgeBaseListResponseModel,
    )
    def list_knowledge_bases() -> dict:
        current = store()
        document_stats = current.list_knowledge_base_document_stats()
        return {
            "knowledge_bases": [
                _knowledge_base_payload(
                    knowledge_base,
                    document_stats.get(knowledge_base.id),
                )
                for knowledge_base in current.list_knowledge_bases()
            ]
        }

    @app.post(
        "/api/knowledge-bases",
        tags=["知识库"],
        summary="创建知识库",
        description=(
            "创建新的知识库，供后续上传和检索文档。名称去除首尾空白后必须唯一；"
            "成功返回新知识库详情。"
        ),
        status_code=201,
        response_model=KnowledgeBaseResponseModel,
        responses=_api_error_responses(409),
    )
    def create_knowledge_base(request: KnowledgeBaseCreateRequest) -> dict:
        current = store()
        knowledge_base = KnowledgeBase(
            id=str(uuid.uuid4()),
            name=request.name,
            description=request.description,
        )
        try:
            current.create_knowledge_base(knowledge_base)
        except sqlite3.IntegrityError:
            _raise_api_error(
                409,
                "knowledge_base_name_conflict",
                "A knowledge base with this name already exists.",
            )
        saved = current.get_knowledge_base(knowledge_base.id)
        return {"knowledge_base": _knowledge_base_payload(saved or knowledge_base)}

    @app.get(
        "/api/knowledge-bases/{knowledge_base_id}",
        tags=["知识库"],
        summary="获取知识库详情",
        description=(
            "用于打开知识库管理页。返回名称、说明和最新文档状态统计；知识库不存在时"
            "返回 404。"
        ),
        response_model=KnowledgeBaseResponseModel,
        responses=_api_error_responses(404),
    )
    def get_knowledge_base(knowledge_base_id: str) -> dict:
        current = store()
        knowledge_base = _require_knowledge_base(current, knowledge_base_id)
        return {
            "knowledge_base": _knowledge_base_payload(
                knowledge_base,
                current.get_knowledge_base_document_stats(knowledge_base_id),
            )
        }

    @app.patch(
        "/api/knowledge-bases/{knowledge_base_id}",
        tags=["知识库"],
        summary="更新知识库信息",
        description=(
            "修改知识库名称和/或说明，至少传入一项。名称必须唯一；有文档正在处理或"
            "存储待恢复时会拒绝修改。"
        ),
        response_model=KnowledgeBaseResponseModel,
        responses=_api_error_responses(404, 409, 503),
    )
    def patch_knowledge_base(
        knowledge_base_id: str, request: KnowledgeBasePatchRequest
    ) -> dict:
        current = store()
        _require_knowledge_base(current, knowledge_base_id)
        try:
            saved = processing_service().update_knowledge_base(
                knowledge_base_id,
                name=request.name,
                description=request.description,
            )
        except sqlite3.IntegrityError:
            _raise_api_error(
                409,
                "knowledge_base_name_conflict",
                "A knowledge base with this name already exists.",
            )
        except KnowledgeBaseNotFoundError:
            _raise_api_error(404, "knowledge_base_not_found", "Knowledge base not found.")
        except DocumentBusyError:
            _raise_api_error(409, "document_busy", "The knowledge base is currently busy.")
        except KnowledgeBaseRecoveryBlockedError:
            _raise_api_error(
                503,
                "knowledge_base_recovery_blocked",
                "The knowledge base is blocked pending storage recovery.",
            )
        return {
            "knowledge_base": _knowledge_base_payload(
                saved,
                current.get_knowledge_base_document_stats(knowledge_base_id),
            )
        }

    @app.delete(
        "/api/knowledge-bases/{knowledge_base_id}",
        tags=["知识库"],
        summary="删除知识库",
        description=(
            "删除指定知识库及其中的文档和派生数据。知识库非空时，需在请求体 "
            "`confirm_name` 传入当前名称；操作不可撤销。"
        ),
        response_model=KnowledgeBaseDeleteResponseModel,
        responses=_api_error_responses(404, 409, 500, 503),
    )
    def delete_knowledge_base(
        knowledge_base_id: str,
        request: KnowledgeBaseDeleteRequest | None = None,
    ) -> dict:
        _require_knowledge_base(store(), knowledge_base_id)
        try:
            deleted = settings_store.run_with_default_knowledge_base_cleared(
                knowledge_base_id,
                lambda: processing_service().delete_knowledge_base(
                    knowledge_base_id,
                    confirm_name=(
                        request.confirm_name if request is not None else None
                    ),
                ),
            )
        except KnowledgeBaseDeleteConfirmationError:
            _raise_api_error(
                409,
                "knowledge_base_delete_confirmation_required",
                "Deleting a non-empty knowledge base requires its exact name.",
            )
        except DocumentBusyError:
            _raise_api_error(409, "document_busy", "A document is currently busy.")
        except KnowledgeBaseRecoveryBlockedError:
            _raise_api_error(
                503,
                "knowledge_base_recovery_blocked",
                "The knowledge base is blocked pending storage recovery.",
            )
        except Exception:
            _raise_api_error(
                500,
                "knowledge_base_delete_failed",
                "The knowledge base could not be deleted safely.",
            )
        if not deleted:
            _raise_api_error(404, "knowledge_base_not_found", "Knowledge base not found.")
        return {"deleted": True, "knowledge_base_id": knowledge_base_id}

    @app.get(
        "/api/knowledge-bases/{knowledge_base_id}/documents",
        tags=["文档"],
        summary="获取知识库文档列表",
        description=(
            "用于展示知识库内的文档及处理进度。返回每个文档的处理状态、索引是否可用、"
            "内容统计和最近处理任务。"
        ),
        response_model=DocumentListResponseModel,
        responses=_api_error_responses(404),
    )
    def list_documents(knowledge_base_id: str) -> dict:
        current = store()
        _require_knowledge_base(current, knowledge_base_id)
        graph_stats = current.list_document_graph_stats(knowledge_base_id)
        return {
            "documents": [
                _document_payload(
                    current,
                    document,
                    stats=graph_stats.get(document.id),
                )
                for document in current.list_documents(knowledge_base_id)
            ]
        }

    @app.post(
        "/api/knowledge-bases/{knowledge_base_id}/documents/upload",
        tags=["文档"],
        summary="上传并处理文档",
        description=(
            "上传一个 PDF、Office 文档或 JPG、JPEG、PNG 图片，并创建异步解析和索引任务。"
            "图片会生成单页 PDF 预览，并以原图直接提交 MinerU。使用 "
            "`multipart/form-data` 的 `file` 字段，`display_name` 可选；成功后可通过返回的"
            "任务 ID 查询进度。"
        ),
        status_code=201,
        response_model=UploadDocumentResponseModel,
        responses=_api_error_responses(400, 404, 409, 413, 503),
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "multipart/form-data": {
                        "schema": {
                            "type": "object",
                            "required": ["file"],
                            "properties": {
                                "file": {
                                    "type": "string",
                                    "format": "binary",
                                    "description": (
                                        "待处理的 PDF、Office 文档，或 JPG、JPEG、PNG 图片。"
                                    ),
                                },
                                "display_name": {
                                    "type": "string",
                                    "maxLength": 255,
                                    "description": "可选显示名称；默认使用文件名。",
                                    "example": "某季度财务报告.pdf",
                                },
                            },
                        }
                    },
                    "application/octet-stream": {
                        "schema": {"type": "string", "format": "binary"}
                    },
                },
            }
        },
    )
    async def upload_document(knowledge_base_id: str, request: Request) -> dict:
        current = store()
        _require_knowledge_base(current, knowledge_base_id)
        service = processing_service()
        if not service.mineru_configured():
            _raise_api_error(
                503,
                "document_processing_not_configured",
                "文档解析服务尚未配置。请在运行状态中确认服务配置后再上传。",
            )
        upload = await _read_upload(request, max_upload_bytes=max_upload_bytes)
        original_filename = _safe_upload_filename(str(upload["filename"]))
        display_name = str(upload.get("display_name") or Path(original_filename).stem).strip()
        if not display_name or len(display_name) > 255:
            _raise_api_error(422, "invalid_document_name", "Document name is invalid.")
        try:
            document = service.register_upload_bytes(
                knowledge_base_id,
                original_filename,
                upload["content"],
                display_name=display_name,
            )
        except sqlite3.IntegrityError as exc:
            if "display_name_key" in str(exc):
                _raise_api_error(
                    409,
                    "document_name_conflict",
                    "A document with this name already exists in the knowledge base.",
                )
            _raise_api_error(
                409,
                "document_conflict",
                "The uploaded document conflicts with an existing document.",
            )
        except DocumentAlreadyExistsError:
            _raise_api_error(
                409,
                "document_conflict",
                "The uploaded document conflicts with an existing document.",
            )
        except DocumentBusyError:
            _raise_api_error(409, "document_busy", "The document is currently busy.")
        except KnowledgeBaseRecoveryBlockedError:
            _raise_api_error(
                503,
                "knowledge_base_recovery_blocked",
                "The knowledge base is blocked pending storage recovery.",
            )
        try:
            job = enqueue_document_processing(current, knowledge_base_id, document.id)
        except ProcessingJobAlreadyActiveError:
            _raise_api_error(409, "processing_job_active", "The document already has an active processing job.")
        processing_worker.notify()
        queued_document = _require_document(current, knowledge_base_id, document.id)
        return {
            "document": _document_payload(current, queued_document),
            "job": _processing_job_payload(current, job),
        }

    @app.get(
        "/api/processing-jobs/{job_id}",
        tags=["处理任务"],
        summary="获取处理任务状态",
        description=(
            "按任务 ID 获取异步处理任务的最新状态。上传、重新解析或重建索引后，可轮询"
            "本接口直到任务成功或失败。"
        ),
        response_model=ProcessingJobResponseModel,
        responses=_api_error_responses(404),
    )
    def get_processing_job(job_id: str) -> dict:
        current = store()
        job = current.get_processing_job(job_id)
        if job is None:
            _raise_api_error(404, "processing_job_not_found", "Processing job not found.")
        return {"job": _processing_job_payload(current, job)}

    @app.get(
        "/api/knowledge-bases/{knowledge_base_id}/processing-jobs",
        tags=["处理任务"],
        summary="获取知识库处理任务",
        description=(
            "返回指定知识库的文档处理任务，供任务列表、进度轮询和失败排查。每项包含当前"
            "阶段、状态、队列位置、尝试次数和安全错误摘要。"
        ),
        response_model=ProcessingJobListResponseModel,
        responses=_api_error_responses(404),
    )
    def list_processing_jobs(knowledge_base_id: str) -> dict:
        current = store()
        _require_knowledge_base(current, knowledge_base_id)
        return {
            "jobs": [
                _processing_job_payload(current, job)
                for job in current.list_processing_jobs(knowledge_base_id)
            ]
        }

    @app.get(
        "/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}",
        tags=["文档"],
        summary="获取文档详情",
        description=(
            "用于详情页展示或轮询单个文档状态。返回文件信息、处理与索引状态、预览地址"
            "和最近处理任务。"
        ),
        response_model=DocumentResponseModel,
        responses=_api_error_responses(404),
    )
    def get_document(knowledge_base_id: str, document_id: str) -> dict:
        current = store()
        document = _require_document(current, knowledge_base_id, document_id)
        return {"document": _document_payload(current, document)}

    @app.patch(
        "/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}",
        tags=["文档"],
        summary="重命名文档",
        description=(
            "修改指定文档的显示名称。名称在同一知识库内必须唯一；文档处理中或存储待"
            "恢复时会拒绝。"
        ),
        response_model=DocumentResponseModel,
        responses=_api_error_responses(404, 409, 503),
    )
    def patch_document(
        knowledge_base_id: str,
        document_id: str,
        request: DocumentPatchRequest,
    ) -> dict:
        current = store()
        _require_document(current, knowledge_base_id, document_id)
        try:
            saved = processing_service().rename_document(
                knowledge_base_id, document_id, request.display_name
            )
        except sqlite3.IntegrityError:
            _raise_api_error(
                409,
                "document_name_conflict",
                "A document with this name already exists in the knowledge base.",
            )
        except DocumentNotFoundError:
            _raise_api_error(404, "document_not_found", "Document not found.")
        except DocumentBusyError:
            _raise_api_error(409, "document_busy", "The document is currently busy.")
        except KnowledgeBaseRecoveryBlockedError:
            _raise_api_error(
                503,
                "knowledge_base_recovery_blocked",
                "The knowledge base is blocked pending storage recovery.",
            )
        return {"document": _document_payload(current, saved)}

    @app.delete(
        "/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}",
        tags=["文档"],
        summary="删除文档",
        description=(
            "删除指定文档及其解析结果、预览文件和索引数据。文档正在处理或存储待恢复时"
            "会拒绝；操作不可撤销。"
        ),
        response_model=DocumentDeleteResponseModel,
        responses=_api_error_responses(404, 409, 503),
    )
    def delete_document(knowledge_base_id: str, document_id: str) -> dict:
        current = store()
        _require_document(current, knowledge_base_id, document_id)
        try:
            deleted = processing_service().delete_document(
                knowledge_base_id, document_id
            )
        except DocumentBusyError:
            _raise_api_error(409, "document_busy", "The document is currently busy.")
        except KnowledgeBaseRecoveryBlockedError:
            _raise_api_error(
                503,
                "knowledge_base_recovery_blocked",
                "The knowledge base is blocked pending storage recovery.",
            )
        if not deleted:
            _raise_api_error(404, "document_not_found", "Document not found.")
        return {"deleted": True, "document_id": document_id}

    @app.get(
        "/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}/file",
        tags=["文档"],
        summary="获取文档 PDF 预览",
        description=(
            "返回用于原文核验的 PDF 预览文件，可直接显示或下载。预览尚未生成时返回 "
            "409，预览文件不存在时返回 404。"
        ),
        response_class=FileResponse,
        responses={
            200: {
                "description": "PDF 文件。",
                "content": {
                    "application/pdf": {
                        "schema": {"type": "string", "format": "binary"}
                    }
                },
            },
            **_api_error_responses(404, 409),
        },
    )
    def document_file(knowledge_base_id: str, document_id: str):
        current = store()
        document = _require_document(current, knowledge_base_id, document_id)
        if not document.preview_file_path:
            _raise_api_error(409, "preview_not_ready", "Document preview is not ready.")
        preview = _managed_preview_path(
            Path(processing_service().managed_root), document.preview_file_path
        )
        if preview is None:
            _raise_api_error(404, "preview_not_found", "Document preview was not found.")
        return FileResponse(
            preview,
            media_type="application/pdf",
            filename=f"{document.display_name}.pdf",
        )

    @app.get(
        "/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}"
        "/assets/{asset_id}",
        tags=["文档"],
        summary="获取文档图片",
        description=(
            "返回文档解析出的图片或图表。默认返回原图；`variant=preview` 返回较小的"
            "预览图。可使用响应 `ETag` 和请求 `If-None-Match` 做条件缓存；资源必须属于"
            "指定知识库和文档。"
        ),
        response_class=FileResponse,
        responses={
            200: {
                "description": "图片二进制。",
                "content": {
                    "image/jpeg": {
                        "schema": {"type": "string", "format": "binary"}
                    },
                    "image/png": {
                        "schema": {"type": "string", "format": "binary"}
                    },
                    "image/webp": {
                        "schema": {"type": "string", "format": "binary"}
                    },
                },
                "headers": {
                    "ETag": {
                        "description": "基于当前原图或预览图实际 SHA-256 的缓存校验值。",
                        "schema": {"type": "string"},
                    }
                },
            },
            304: {
                "description": "If-None-Match 命中，图片内容未变化。",
                "headers": {
                    "ETag": {
                        "description": "当前图片资产的 SHA-256 缓存校验值。",
                        "schema": {"type": "string"},
                    },
                    "Cache-Control": {
                        "description": "私有缓存的重新验证策略。",
                        "schema": {"type": "string"},
                    },
                },
            },
            **_api_error_responses(404),
        },
    )
    def document_asset(
        knowledge_base_id: str,
        document_id: str,
        asset_id: str,
        request: Request,
        variant: Literal["original", "preview"] = Query(default="original"),
    ):
        current = store()
        document = _require_document(current, knowledge_base_id, document_id)
        asset = current.get_asset(knowledge_base_id, document_id, asset_id)
        if asset is None or asset.asset_type not in {"image", "chart"}:
            _raise_api_error(404, "asset_not_found", "Image asset was not found.")
        if variant == "preview":
            key = asset.metadata.get("preview_file_path")
            expected_content_type = asset.metadata.get("preview_content_type")
            expected_file_hash = asset.metadata.get("preview_file_hash")
        else:
            key = asset.file_path
            expected_content_type = asset.content_type
            expected_file_hash = asset.file_hash
        resolved = resolve_managed_image_asset(
            Path(processing_service().managed_root),
            document.mineru_result_dir,
            key if isinstance(key, str) else None,
            expected_content_type=(
                expected_content_type
                if isinstance(expected_content_type, str)
                else None
            ),
            expected_file_hash=(
                expected_file_hash if isinstance(expected_file_hash, str) else None
            ),
        )
        if resolved is None:
            _raise_api_error(404, "asset_unavailable", "Image asset is unavailable.")
        etag = f'"sha256-{resolved.file_hash}"'
        response_headers = {
            "Cache-Control": "private, max-age=0, must-revalidate",
            "ETag": etag,
            "X-Content-Type-Options": "nosniff",
        }
        if _if_none_match_matches(request.headers.get("if-none-match"), etag):
            return Response(status_code=304, headers=response_headers)
        return FileResponse(
            resolved.path,
            media_type=resolved.info.content_type,
            headers=response_headers,
        )

    @app.get(
        "/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}/blocks",
        tags=["文档"],
        summary="获取文档结构化内容（Blocks）",
        description=(
            "返回解析后的文本块、页码与坐标、逐页信息和图片资产引用。适合构建原文"
            "查看器或排查解析结果，不用于相关性检索。"
        ),
        response_model=DocumentBlocksResponseModel,
        responses=_api_error_responses(404),
    )
    def document_blocks(knowledge_base_id: str, document_id: str) -> dict:
        current = store()
        document = _require_document(current, knowledge_base_id, document_id)
        blocks = current.list_blocks(knowledge_base_id, document_id)
        geometry_overrides, merge_into = load_cross_page_block_merges(
            document.mineru_result_dir,
            blocks,
        )
        assets = current.list_assets(knowledge_base_id, document_id)
        asset_payloads = {
            asset.id: payload
            for asset in assets
            if (
                payload := visual_asset_payload(
                    knowledge_base_id,
                    asset,
                    detailed=True,
                    include_block_id=True,
                )
            )
            is not None
        }
        return {
            "blocks": public_document_blocks(
                blocks,
                geometry_overrides=geometry_overrides,
                merge_into=merge_into,
            ),
            "pages": public_page_metadata(blocks),
            "assets": asset_payloads,
        }

    @app.post(
        "/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}/parse",
        tags=["处理任务"],
        summary="重新解析文档",
        description=(
            "将文档重新加入异步解析队列，适用于解析失败或需要重跑解析的情况。接口返回 "
            "202 和任务信息；`model_version` 与 `language` 目前仅为兼容字段。"
        ),
        status_code=202,
        response_model=ManagedProcessResponseModel,
        responses=_api_error_responses(404, 409, 503),
    )
    def parse_document(
        knowledge_base_id: str,
        document_id: str,
        request: ManagedProcessRequest | None = None,
    ) -> dict:
        return _enqueue_managed_processing(
            store(),
            processing_worker,
            processing_service(),
            knowledge_base_id,
            document_id,
            operation="parse",
        )

    @app.post(
        "/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}/index",
        tags=["处理任务"],
        summary="重新生成文档索引",
        description=(
            "将文档重新加入异步处理队列，以恢复或重建可检索索引。接口返回 202 和任务"
            "信息；请求体中的兼容字段不会覆盖服务器配置。"
        ),
        status_code=202,
        response_model=ManagedProcessResponseModel,
        responses=_api_error_responses(404, 409, 503),
    )
    def index_document(
        knowledge_base_id: str,
        document_id: str,
        request: ManagedProcessRequest | None = None,
    ) -> dict:
        return _enqueue_managed_processing(
            store(),
            processing_worker,
            processing_service(),
            knowledge_base_id,
            document_id,
            operation="index",
        )

    @app.post(
        "/api/retrieval",
        tags=["检索"],
        summary="检索可核验原文（Sources）",
        description=(
            "在指定知识库（可选限定文档）中检索与 `query` 相关的原文片段，返回可继续"
            "读取上下文的 `source_id` 和定位信息。`mode=keyword` 强制关键词检索；"
            "`mode=auto` 使用最佳可用管线，降级时通过 `degraded` 和 `warnings` 说明。"
            "可能调用外部模型并产生费用。"
        ),
        response_model=RetrievalApiResponseModel,
        response_model_exclude_unset=True,
        responses=_api_error_responses(404, 409),
    )
    def retrieve_sources(request: RetrievalRequest) -> dict:
        current = store()
        for knowledge_base_id in request.knowledge_base_ids:
            _require_knowledge_base(current, knowledge_base_id)
        if request.document_ids is not None:
            requested_document_ids = set(request.document_ids)
            existing_pairs = current.existing_document_pairs(
                (knowledge_base_id, document_id)
                for knowledge_base_id in request.knowledge_base_ids
                for document_id in request.document_ids
            )
            existing_document_ids = {
                document_id for _knowledge_base_id, document_id in existing_pairs
            }
            if existing_document_ids != requested_document_ids:
                _raise_api_error(
                    404,
                    "document_not_found",
                    "One or more documents were not found in the selected knowledge bases.",
                )

        retrieval_scope = current.load_retrieval_scope(
            request.knowledge_base_ids,
            document_ids=request.document_ids,
        )
        retrieval_mode_requested = (
            "keyword" if request.mode == "keyword" else "embedding"
        )
        vector, provider_name, model_name, embedding_warnings = query_embedding(
            request.query,
            retrieval_mode_requested=retrieval_mode_requested,
            retrieval_scope=retrieval_scope,
        )
        try:
            search_result = EvidenceService(
                current, reranker=current_reranker()
            ).search_with_mode(
                request.query,
                request.knowledge_base_ids,
                retrieval_mode_requested=retrieval_mode_requested,
                query_vector=vector,
                embedding_provider=provider_name,
                embedding_model=model_name,
                retrieval_scope=retrieval_scope,
                top_k=request.top_k,
            )
            sources = SourceService(current).retrieval_sources(search_result)
        except NoIndexedDocumentsError:
            _raise_api_error(
                409,
                "no_indexed_documents",
                "The selected scope has no indexed documents.",
            )
        except EvidenceWebUrlTooLongError:
            _raise_api_error(
                409,
                "source_generation_failed",
                "A source locator could not be generated.",
            )
        warning_messages = list(
            dict.fromkeys(embedding_warnings + search_result.warnings)
        )
        return {
            "query": request.query,
            "knowledge_base_ids": request.knowledge_base_ids,
            "document_ids": request.document_ids,
            "mode_requested": request.mode,
            "pipeline_used": search_result.pipeline_used,
            "degraded": search_result.degraded or bool(embedding_warnings),
            "warnings": [_retrieval_warning(message) for message in warning_messages],
            "results": sources,
        }

    @app.get(
        "/api/sources/{source_id}/context",
        tags=["原文定位"],
        summary="获取 Source 原文上下文",
        description=(
            "根据检索结果中的 `source_id` 获取命中片段及前后文、页码、坐标、标题路径和"
            "文档预览地址。`before`/`after` 可设为 0–5；文档更新或删除导致 Source "
            "失效时返回 410。"
        ),
        response_model=SourceContextResponseModel,
        response_model_exclude_unset=True,
        responses=_api_error_responses(404, 410),
    )
    def source_context(
        source_id: str,
        before: int = Query(default=2, ge=0, le=5),
        after: int = Query(default=2, ge=0, le=5),
    ) -> dict:
        try:
            return source_service().context(source_id, before=before, after=after)
        except SourceNotFoundError:
            _raise_api_error(404, "source_not_found", "Source not found.")
        except SourceGoneError:
            _raise_api_error(
                410,
                "source_gone",
                "Source is no longer available because the document changed or was removed.",
            )

    @app.api_route(
        "/api/documents",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    @app.api_route(
        "/api/documents/{full_path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    @app.api_route(
        "/api/retrieval/search",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    @app.api_route(
        "/api/rag/ask",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    @app.api_route(
        "/api/agent/investigate",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    @app.api_route(
        "/api/query",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    @app.api_route(
        "/api/evidence/{full_path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    @app.api_route(
        "/api/traces",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    @app.api_route(
        "/api/traces/{full_path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    def removed_legacy_api_route(full_path: str = ""):
        raise HTTPException(status_code=404, detail="Not Found")

    @app.api_route(
        "/kb/documents",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    @app.api_route(
        "/kb/documents/{full_path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    def removed_legacy_document_route(full_path: str = ""):
        raise HTTPException(status_code=404, detail="Not Found")

    assets_dir = static_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="web-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str, request: Request):
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")

        static_root = static_dir.resolve()
        requested_file = (static_root / full_path).resolve()
        try:
            requested_file.relative_to(static_root)
        except ValueError:
            raise HTTPException(status_code=404, detail="Not Found")

        if full_path and requested_file.is_file():
            return FileResponse(requested_file)

        requested_name = Path(full_path).name
        looks_like_file = bool(Path(requested_name).suffix) or requested_name.startswith(
            "."
        )
        accept = request.headers.get("accept", "")
        accepts_html = not accept or "*/*" in accept or "text/html" in accept
        if looks_like_file or not accepts_html:
            raise HTTPException(status_code=404, detail="Not Found")

        index_path = static_root / "index.html"
        if index_path.is_file():
            return FileResponse(index_path)
        raise HTTPException(status_code=404, detail="Web application is not built.")

    return app


def _enqueue_managed_processing(
    store: EvidenceStore,
    worker: ProcessingWorker,
    service: DocumentProcessingService,
    knowledge_base_id: str,
    document_id: str,
    operation: str,
) -> dict:
    _require_document(store, knowledge_base_id, document_id)
    if not service.mineru_configured():
        _raise_api_error(
            503,
            "document_processing_not_configured",
            "文档解析服务尚未配置。请在运行状态中确认服务配置后再重新读取。",
        )
    try:
        service.ensure_knowledge_base_ready(knowledge_base_id)
    except KnowledgeBaseRecoveryBlockedError:
        _raise_api_error(
            503,
            "knowledge_base_recovery_blocked",
            "The knowledge base is blocked pending storage recovery.",
        )
    try:
        job = enqueue_document_processing(
            store, knowledge_base_id, document_id, operation=operation
        )
    except ProcessingJobAlreadyActiveError:
        _raise_api_error(
            409,
            "processing_job_active",
            "The document already has an active processing job.",
        )
    worker.notify()
    queued = _require_document(store, knowledge_base_id, document_id)
    return {
        "operation": operation,
        "document": _document_payload(store, queued),
        "job": _processing_job_payload(store, job),
    }


def _require_knowledge_base(
    store: EvidenceStore, knowledge_base_id: str
) -> KnowledgeBase:
    knowledge_base = store.get_knowledge_base(knowledge_base_id)
    if knowledge_base is None:
        _raise_api_error(404, "knowledge_base_not_found", "Knowledge base not found.")
    return knowledge_base


def _require_document(
    store: EvidenceStore, knowledge_base_id: str, document_id: str
) -> Document:
    _require_knowledge_base(store, knowledge_base_id)
    document = store.get_document(knowledge_base_id, document_id)
    if document is None:
        _raise_api_error(404, "document_not_found", "Document not found.")
    return document


def _retrieval_warning(message: str) -> dict[str, str]:
    if "Reranker" in message:
        code = "reranker_unavailable"
    elif "索引未完整" in message or "查询模型不兼容" in message:
        code = "embedding_index_incompatible"
    elif "查询向量生成失败" in message:
        code = "embedding_request_failed"
    elif "Embedding Provider" in message:
        code = "embedding_unavailable"
    else:
        code = "retrieval_degraded"
    return {"code": code, "message": message}


def _knowledge_base_payload(
    knowledge_base: KnowledgeBase,
    document_stats: dict[str, int] | None = None,
) -> dict:
    document_stats = document_stats or {
        "document_count": 0,
        "ready_document_count": 0,
        "processing_document_count": 0,
        "failed_document_count": 0,
    }
    return {
        "id": knowledge_base.id,
        "name": knowledge_base.name,
        "description": knowledge_base.description,
        "created_at": knowledge_base.created_at,
        "updated_at": knowledge_base.updated_at,
        **document_stats,
    }


def _document_payload(
    store: EvidenceStore,
    document: Document,
    *,
    stats: dict[str, int] | None = None,
) -> dict:
    if stats is None:
        stats = store.list_document_graph_stats(
            document.knowledge_base_id,
            document.id,
        ).get(
            document.id,
            {
                "blocks": 0,
                "assets": 0,
                "chunks": 0,
                "index_records": 0,
                "embedding_records": 0,
                "embedding_configurations": 0,
            },
        )
    index_record_count = stats.get("index_records", 0)
    embedding_record_count = stats.get("embedding_records", 0)
    embedding_configuration_count = stats.get("embedding_configurations", 0)
    embedding_ready = (
        index_record_count > 0
        and embedding_record_count == index_record_count
        and embedding_configuration_count == 1
    )
    embedding_status = (
        "ready"
        if embedding_ready
        else "fallback_keyword"
        if index_record_count > 0
        else "not_indexed"
    )
    latest_job = store.latest_processing_job(document.id)
    return {
        "id": document.id,
        "knowledge_base_id": document.knowledge_base_id,
        "original_filename": document.original_filename,
        "display_name": document.display_name,
        "source_type": document.source_type,
        "page_count": document.page_count,
        "processing_status": document.processing_status,
        "processing_error": (
            "Document processing failed." if document.processing_error else None
        ),
        "parser_provider": document.parser_provider,
        "parser_version": document.parser_version,
        "created_at": document.created_at,
        "updated_at": document.updated_at,
        "stats": {
            key: stats.get(key, 0)
            for key in (
                "blocks",
                "assets",
                "chunks",
                "index_records",
                "embedding_records",
            )
        },
        "embedding_record_count": embedding_record_count,
        "embedding_ready": embedding_ready,
        "embedding_status": embedding_status,
        "embedding_warning": (
            "Embedding 索引未完整就绪，当前文档仍可使用关键词检索。"
            if embedding_status == "fallback_keyword"
            else None
        ),
        "file_url": (
            f"/api/knowledge-bases/{document.knowledge_base_id}/documents/"
            f"{document.id}/file"
        ),
        "web_url": (
            f"/knowledge-bases/{document.knowledge_base_id}/documents/{document.id}"
        ),
        "latest_job": (
            _processing_job_payload(store, latest_job) if latest_job is not None else None
        ),
    }


def _processing_job_payload(store: EvidenceStore, job: ProcessingJob) -> dict:
    error = None
    if job.error_code:
        error = {
            "code": job.error_code,
            "stage": job.stage,
            "message": job.error_message or "文档处理失败。",
            "retryable": bool(job.retryable),
            "diagnostic_id": job.id,
        }
    return {
        "id": job.id,
        "knowledge_base_id": job.knowledge_base_id,
        "document_id": job.document_id,
        "operation": (
            "normalize_upgrade"
            if is_normalization_upgrade_operation(job.operation)
            else job.operation
        ),
        "status": job.status,
        "stage": job.stage,
        "attempt": job.attempt,
        "queue_position": store.processing_job_queue_position(job.id),
        "error": error,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "updated_at": job.updated_at,
    }


async def _read_upload(
    request: Request, *, max_upload_bytes: int
) -> dict[str, Any]:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_upload_bytes:
            _raise_api_error(
                413,
                "upload_too_large",
                "The upload exceeds the configured size limit.",
            )
        body.extend(chunk)
    if not body:
        _raise_api_error(400, "empty_upload", "Upload body is empty.")
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type:
        return _parse_multipart_upload(bytes(body), content_type)
    filename = request.headers.get("x-filename") or "document.pdf"
    display_name = request.query_params.get("display_name")
    return {"filename": filename, "content": bytes(body), "display_name": display_name}


def _parse_multipart_upload(body: bytes, content_type: str) -> dict[str, Any]:
    match = re.search(r"boundary=(?:\"([^\"]+)\"|([^;]+))", content_type)
    if match is None:
        _raise_api_error(400, "invalid_multipart", "Missing multipart boundary.")
    boundary_value = (match.group(1) or match.group(2)).strip()
    try:
        boundary = boundary_value.encode("ascii")
    except UnicodeEncodeError:
        _raise_api_error(400, "invalid_multipart", "Invalid multipart boundary.")
    if not boundary or len(boundary) > 70:
        _raise_api_error(400, "invalid_multipart", "Invalid multipart boundary.")
    delimiter = b"--" + boundary
    if not body.startswith(delimiter):
        _raise_api_error(400, "invalid_multipart", "Malformed multipart upload.")
    result: dict[str, Any] = {}
    cursor = 0
    while True:
        if body[cursor : cursor + len(delimiter)] != delimiter:
            _raise_api_error(400, "invalid_multipart", "Malformed multipart upload.")
        cursor += len(delimiter)
        if body[cursor : cursor + 2] == b"--":
            cursor += 2
            if body[cursor:] not in {b"", b"\r\n"}:
                _raise_api_error(400, "invalid_multipart", "Malformed multipart upload.")
            break
        if body[cursor : cursor + 2] != b"\r\n":
            _raise_api_error(400, "invalid_multipart", "Malformed multipart upload.")
        header_start = cursor + 2
        header_end = body.find(b"\r\n\r\n", header_start)
        if header_end < 0:
            _raise_api_error(400, "invalid_multipart", "Malformed multipart upload.")
        content_start = header_end + 4
        next_boundary = body.find(b"\r\n" + delimiter, content_start)
        if next_boundary < 0:
            _raise_api_error(400, "invalid_multipart", "Malformed multipart upload.")
        header_bytes = body[header_start:header_end]
        content = body[content_start:next_boundary]
        cursor = next_boundary + 2
        headers = header_bytes.decode("utf-8", errors="replace")
        name_match = re.search(r'name="([^"]+)"', headers)
        if name_match is None:
            continue
        field_name = name_match.group(1)
        filename_match = re.search(r'filename="([^"]*)"', headers)
        if filename_match is not None:
            if not content:
                _raise_api_error(400, "empty_upload", "Uploaded file is empty.")
            result["filename"] = filename_match.group(1) or "document.pdf"
            result["content"] = content
        elif field_name == "display_name":
            result["display_name"] = content.decode("utf-8", errors="replace")
    if "content" not in result:
        _raise_api_error(400, "missing_file", "No file part was found.")
    return result


def _safe_upload_filename(filename: str) -> str:
    normalized = filename.replace("\\", "/")
    safe_name = Path(normalized).name.strip()
    if not safe_name or safe_name in {".", ".."} or len(safe_name) > 255:
        _raise_api_error(422, "invalid_filename", "Uploaded filename is invalid.")
    return safe_name


def _managed_preview_path(managed_root: Path, value: str) -> Path | None:
    root = managed_root.resolve()
    candidate = Path(value)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError, OSError):
        return None
    if candidate.is_symlink() or not resolved.is_file() or resolved.suffix.lower() != ".pdf":
        return None
    return resolved


def _if_none_match_matches(value: str | None, etag: str) -> bool:
    if not value:
        return False
    for candidate in value.split(","):
        token = candidate.strip()
        if token == "*" or token == etag or token == f"W/{etag}":
            return True
    return False


def _raise_api_error(status_code: int, code: str, message: str):
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


async def _read_bounded_json_object(
    request: Request,
    *,
    invalid_code: str,
    invalid_message: str,
    allow_empty: bool = False,
) -> dict[str, Any]:
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > SETTINGS_REQUEST_MAX_BYTES:
                _raise_api_error(
                    413,
                    "request_body_too_large",
                    "Request body exceeds the allowed size.",
                )
            body.extend(chunk)
        if allow_empty and not body:
            return {}
        value = json.loads(body.decode("utf-8"))
    except HTTPException:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        _raise_api_error(422, invalid_code, invalid_message)
    if not isinstance(value, dict):
        _raise_api_error(422, invalid_code, invalid_message)
    return value


class RuntimeConfiguredApp:
    """Lazily build the default ASGI app when the server actually uses it."""

    def __init__(self) -> None:
        self._app: FastAPI | None = None

    def _load(self) -> FastAPI:
        if self._app is None:
            runtime = load_runtime_config()
            self._app = create_app(runtime_config=runtime)
        return self._app

    async def __call__(self, scope, receive, send) -> None:
        await self._load()(scope, receive, send)

    def openapi(self) -> dict[str, Any]:
        return self._load().openapi()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._load(), name)


app = RuntimeConfiguredApp()
