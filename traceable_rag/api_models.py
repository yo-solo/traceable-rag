"""Pydantic request and response models for the public HTTP API."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from .settings import normalize_public_base_url


KnowledgeBaseId = Annotated[str, Field(min_length=1, max_length=255)]


class KnowledgeBaseCreateRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "name": "某季度财报知识库",
                    "description": "用于归档某季度财务报告和经营分析资料",
                }
            ]
        },
    )

    name: str = Field(description="知识库显示名称。", min_length=1, max_length=120)
    description: str = Field(
        default="", description="知识库用途说明。", max_length=1000
    )

    @field_validator("name", "description", mode="before")
    @classmethod
    def strip_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class KnowledgeBasePatchRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "name": "某季度财报知识库",
                    "description": "更新后的知识库说明",
                }
            ]
        },
    )

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)

    @field_validator("name", "description", mode="before")
    @classmethod
    def strip_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def require_change(self):
        if self.name is None and self.description is None:
            raise ValueError("At least one field is required")
        return self


class KnowledgeBaseDeleteRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"confirm_name": "某季度财报知识库"}]},
    )

    confirm_name: str = Field(
        description="非空知识库删除确认，必须与当前名称完全一致。",
        min_length=1,
        max_length=120,
    )


class DocumentPatchRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"display_name": "某季度财务报告.pdf"}]},
    )

    display_name: str = Field(min_length=1, max_length=255)

    @field_validator("display_name", mode="before")
    @classmethod
    def strip_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class ManagedProcessRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"model_version": "vlm", "language": "ch"}]},
    )

    model_version: str = Field(
        default="vlm",
        description="兼容字段；当前队列使用部署时配置的解析模型。",
        min_length=1,
        max_length=80,
    )
    language: str = Field(
        default="ch",
        description="兼容字段；当前队列使用部署时配置的解析语言。",
        min_length=1,
        max_length=20,
    )


class RetrievalRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "query": "本季度营业收入和净利润分别是多少？",
                    "knowledge_base_ids": ["knowledge-base-id"],
                    "mode": "auto",
                    "top_k": 8,
                    "document_ids": None,
                }
            ]
        },
    )

    query: str = Field(min_length=1, max_length=4000)
    knowledge_base_ids: list[KnowledgeBaseId] = Field(min_length=1, max_length=50)
    mode: Literal["auto", "keyword"] = "auto"
    top_k: int = Field(default=8, ge=1, le=50)
    document_ids: list[str] | None = Field(default=None, min_length=1, max_length=50)

    @field_validator("query", mode="before")
    @classmethod
    def strip_retrieval_query(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("knowledge_base_ids", "document_ids")
    @classmethod
    def normalize_retrieval_scope(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("Scope ids cannot be empty")
        return list(dict.fromkeys(normalized))


class RetrievalWarningModel(BaseModel):
    code: str
    message: str


class VisualAssetReferenceModel(BaseModel):
    asset_id: str
    modality: Literal["image", "chart"]
    asset_url: str | None
    preview_url: str | None


class VisualAssetDetailModel(VisualAssetReferenceModel):
    content_type: str | None
    file_hash: str | None
    width: int | None
    height: int | None
    caption: str | None


class DocumentVisualAssetDetailModel(VisualAssetDetailModel):
    block_id: str


class RetrievalSourceModel(BaseModel):
    source_id: str
    source_url: str
    text: str
    document_id: str
    document_name: str
    human_page_number: int
    bbox: list[float] | None
    score: float
    asset: VisualAssetReferenceModel | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "source_id": "src_0123456789abcdef0123456789abcdef",
                    "source_url": "/sources/src_0123456789abcdef0123456789abcdef",
                    "text": "本季度营业收入和净利润均较上年同期增长，具体金额见合并利润表。",
                    "document_id": "document-id",
                    "document_name": "某季度财务报告.pdf",
                    "human_page_number": 12,
                    "bbox": [72, 160, 510, 240],
                    "score": 0.82,
                }
            ]
        }
    )


class RetrievalApiResponseModel(BaseModel):
    query: str
    knowledge_base_ids: list[str]
    document_ids: list[str] | None
    mode_requested: Literal["auto", "keyword"]
    pipeline_used: Literal["keyword", "dense", "dense_reranker"]
    degraded: bool
    warnings: list[RetrievalWarningModel]
    results: list[RetrievalSourceModel]


class SourceContextItemModel(BaseModel):
    text: str
    human_page_number: int
    bbox: list[float] | None
    page_size: list[float] | None
    block_type: str


class SourceContextSourceModel(BaseModel):
    source_id: str
    source_url: str
    text: str
    document_id: str
    document_name: str
    knowledge_base_id: str
    human_page_number: int
    bbox: list[float] | None
    page_size: list[float] | None
    title_path: list[str]
    file_url: str
    asset: VisualAssetDetailModel | None = None


class SourceContextResponseModel(BaseModel):
    source: SourceContextSourceModel
    before: list[SourceContextItemModel]
    after: list[SourceContextItemModel]
    truncated: bool

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "source": {
                        "source_id": "src_0123456789abcdef0123456789abcdef",
                        "source_url": "/sources/src_0123456789abcdef0123456789abcdef",
                        "text": "本季度营业收入和净利润均较上年同期增长，具体金额见合并利润表。",
                        "document_id": "document-id",
                        "document_name": "某季度财务报告.pdf",
                        "knowledge_base_id": "knowledge-base-id",
                        "human_page_number": 12,
                        "bbox": [72, 160, 510, 240],
                        "page_size": [612, 792],
                        "title_path": ["主要财务指标"],
                        "file_url": "/api/knowledge-bases/knowledge-base-id/documents/document-id/file",
                    },
                    "before": [],
                    "after": [],
                    "truncated": False,
                }
            ]
        }
    )


class ApiErrorDetailModel(BaseModel):
    code: str = Field(description="稳定、可供程序判断的错误码。")
    message: str = Field(description="不包含密钥和内部路径的安全错误说明。")


class ApiErrorResponseModel(BaseModel):
    detail: ApiErrorDetailModel


class HealthResponseModel(BaseModel):
    ok: bool
    knowledge_bases: int = Field(ge=0)
    documents: int = Field(ge=0)

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"ok": True, "knowledge_bases": 2, "documents": 6}]
        }
    )


class KnowledgeBasePayloadResponse(BaseModel):
    id: str
    name: str
    description: str
    created_at: str | None
    updated_at: str | None
    document_count: int = Field(default=0, ge=0)
    ready_document_count: int = Field(default=0, ge=0)
    processing_document_count: int = Field(default=0, ge=0)
    failed_document_count: int = Field(default=0, ge=0)


class KnowledgeBaseResponseModel(BaseModel):
    knowledge_base: KnowledgeBasePayloadResponse


class KnowledgeBaseListResponseModel(BaseModel):
    knowledge_bases: list[KnowledgeBasePayloadResponse]


class KnowledgeBaseDeleteResponseModel(BaseModel):
    deleted: Literal[True]
    knowledge_base_id: str


class DocumentDeleteResponseModel(BaseModel):
    deleted: Literal[True]
    document_id: str


class ProcessingJobErrorResponse(BaseModel):
    code: str
    stage: str
    message: str
    retryable: bool
    diagnostic_id: str


class ProcessingJobPayloadResponse(BaseModel):
    id: str
    knowledge_base_id: str
    document_id: str
    operation: Literal["process", "parse", "index", "normalize_upgrade"]
    status: Literal["queued", "running", "succeeded", "failed"]
    stage: str
    attempt: int = Field(ge=0)
    queue_position: int | None = Field(default=None, ge=1)
    error: ProcessingJobErrorResponse | None
    created_at: str | None
    started_at: str | None
    finished_at: str | None
    updated_at: str | None


class DocumentBlockResponse(BaseModel):
    id: str
    document_id: str
    page_idx: int = Field(ge=0)
    human_page_number: int = Field(ge=1)
    block_idx: int = Field(ge=0)
    block_type: str
    text: str | None
    html: str | None
    asset_id: str | None
    bbox: list[float] | None
    page_size: list[float] | None
    bbox_by_page: dict[str, list[list[float]]]
    bbox_page_size_by_page: dict[str, list[float]]
    metadata: dict[str, Any]


class DocumentPageMetadataResponse(BaseModel):
    page_idx: int = Field(ge=0)
    human_page_number: int = Field(ge=1)
    page_size: list[float] | None
    block_count: int = Field(ge=0)


class DocumentBlocksResponseModel(BaseModel):
    blocks: list[DocumentBlockResponse]
    pages: list[DocumentPageMetadataResponse]
    assets: dict[str, DocumentVisualAssetDetailModel]


class DocumentGraphStatsResponse(BaseModel):
    blocks: int
    assets: int
    chunks: int
    index_records: int
    embedding_records: int


class DocumentPayloadResponse(BaseModel):
    id: str
    knowledge_base_id: str
    original_filename: str
    display_name: str
    source_type: str
    page_count: int
    processing_status: Literal[
        "uploading", "uploaded", "queued", "parsing", "parsed", "indexing", "ready", "failed"
    ]
    processing_error: str | None
    parser_provider: str
    parser_version: str | None
    created_at: str | None
    updated_at: str | None
    stats: DocumentGraphStatsResponse
    embedding_record_count: int
    embedding_ready: bool
    embedding_status: Literal["not_indexed", "fallback_keyword", "ready"]
    embedding_warning: str | None
    file_url: str
    web_url: str
    latest_job: ProcessingJobPayloadResponse | None


class DocumentResponseModel(BaseModel):
    document: DocumentPayloadResponse


class ProcessingJobResponseModel(BaseModel):
    job: ProcessingJobPayloadResponse


class ProcessingJobListResponseModel(BaseModel):
    jobs: list[ProcessingJobPayloadResponse]


class UploadDocumentResponseModel(DocumentResponseModel):
    job: ProcessingJobPayloadResponse


class DocumentListResponseModel(BaseModel):
    documents: list[DocumentPayloadResponse]


class ManagedProcessResponseModel(DocumentResponseModel):
    operation: Literal["parse", "index"]
    job: ProcessingJobPayloadResponse


class SettingsPatchRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "default_top_k": 8,
                    "default_retrieval_mode": "embedding",
                    "default_use_embedding": True,
                    "public_base_url": "http://127.0.0.1:8000",
                    "default_knowledge_base_id": "knowledge-base-id",
                }
            ]
        },
    )

    default_top_k: int | None = Field(default=None, ge=1, le=50)
    default_retrieval_mode: Literal["keyword", "embedding"] | None = None
    default_use_embedding: bool | None = None
    public_base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    default_knowledge_base_id: str | None = Field(default=None, max_length=255)

    @field_validator("public_base_url", mode="before")
    @classmethod
    def validate_public_base_url(cls, value: Any) -> Any:
        if value is None:
            return value
        return normalize_public_base_url(value)

    @field_validator("default_knowledge_base_id", mode="before")
    @classmethod
    def normalize_knowledge_base_id(cls, value: Any) -> Any:
        if value is None:
            return value
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Default knowledge base id cannot be empty")
        return value.strip()

    @model_validator(mode="after")
    def require_change(self):
        if not self.model_fields_set:
            raise ValueError("At least one settings field is required")
        for field_name in self.model_fields_set - {"default_knowledge_base_id"}:
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class DiagnosticRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"target": "embedding"}]},
    )

    target: Literal["service", "mineru", "embedding"]


class SafeSettingsResponse(BaseModel):
    default_top_k: int = Field(ge=1, le=50)
    default_retrieval_mode: Literal["keyword", "embedding"]
    default_use_embedding: bool
    public_base_url: str
    default_knowledge_base_id: str | None


class SettingsPersistenceResponse(BaseModel):
    status: Literal["ok", "degraded"]
    message: str | None


class RuntimeServiceResponse(BaseModel):
    healthy: bool


class RuntimeCountsResponse(BaseModel):
    knowledge_bases: int = Field(ge=0)
    documents: int = Field(ge=0)
    chunks: int = Field(ge=0)
    index_records: int = Field(ge=0)


class RuntimeFailureResponse(BaseModel):
    document_id: str
    display_name: str
    summary: str
    updated_at: str


class RuntimeStatusResponse(BaseModel):
    service: RuntimeServiceResponse
    counts: RuntimeCountsResponse
    recent_failures: list[RuntimeFailureResponse]


class ProviderStatusResponse(BaseModel):
    configured: bool


class ProvidersResponse(BaseModel):
    mineru: ProviderStatusResponse
    embedding: ProviderStatusResponse


ProviderRole = Literal["mineru", "embedding", "reranker", "llm"]


class ProviderProfileWriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter: str | None = Field(default=None, min_length=1, max_length=80)
    display_name: str | None = Field(default=None, max_length=120)
    base_url: str | None = Field(default=None, max_length=2048)
    model: str | None = Field(default=None, max_length=500)
    options: dict[str, Any] | None = None
    api_key: SecretStr | None = Field(default=None, max_length=64 * 1024)

    @field_validator("adapter", "display_name", "base_url", "model", mode="before")
    @classmethod
    def strip_provider_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class ProviderProfileResponse(BaseModel):
    role: ProviderRole
    adapter: str
    display_name: str
    base_url: str | None
    model: str | None
    options: dict[str, Any]
    source: Literal["stored", "environment", "environment_legacy", "default"]
    configured: bool
    secret_present: bool
    status: Literal["configured", "incomplete", "not_configured", "unavailable"]
    last_test_status: Literal["success", "failed", "not_supported"] | None
    last_tested_at: str | None


class ProviderProfilesMapResponse(BaseModel):
    mineru: ProviderProfileResponse
    embedding: ProviderProfileResponse
    reranker: ProviderProfileResponse
    llm: ProviderProfileResponse


class ProviderProfilesResponse(BaseModel):
    storage_available: bool
    profiles: ProviderProfilesMapResponse


class ProviderTestResultResponse(BaseModel):
    role: ProviderRole
    ok: bool
    status: Literal["success", "failed", "not_supported"]
    message: str
    tested_at: str


class SkillStatusResponse(BaseModel):
    template_available: bool
    installed: bool


class ApiReferenceExampleResponse(BaseModel):
    label: str
    method: Literal["GET", "POST", "PATCH", "DELETE"]
    path: str
    body: dict[str, Any] | None


class DeveloperReferenceResponse(BaseModel):
    openapi_url: str
    examples: list[ApiReferenceExampleResponse]


class SettingsResponseModel(BaseModel):
    settings: SafeSettingsResponse
    persistence: SettingsPersistenceResponse
    runtime: RuntimeStatusResponse
    providers: ProvidersResponse
    skill: SkillStatusResponse
    developer: DeveloperReferenceResponse


class SettingsPatchResponseModel(BaseModel):
    settings: SafeSettingsResponse
    persistence: SettingsPersistenceResponse


class DiagnosticResultResponse(BaseModel):
    target: Literal["service", "mineru", "embedding"]
    ok: bool
    code: str
    message: str
    action: str | None


class DiagnosticResponseModel(BaseModel):
    diagnostic: DiagnosticResultResponse
