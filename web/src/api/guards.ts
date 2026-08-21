import type {
  DocumentBlock,
  DocumentBlocksResponse,
  DocumentListResponse,
  DocumentPageMetadata,
  DocumentProcessingStatus,
  DocumentResponse,
  DocumentVisualAssetDetail,
  KnowledgeBase,
  KnowledgeBaseDocument,
  KnowledgeBaseListResponse,
  KnowledgeBaseResponse,
  KnowledgeBaseSummary,
  ManagedProcessResponse,
  ProcessingJob,
  ProcessingJobStage,
  UploadDocumentResponse,
  RetrievalResponse,
  RetrievalSource,
  RetrievalWarning,
  SourceContextItem,
  SourceContextResponse,
  SourceContextSource,
  ApiReferenceExample,
  DiagnosticResponse,
  DiagnosticResult,
  DiagnosticTarget,
  ProviderAdapter,
  ProviderProfile,
  ProviderProfileOptions,
  ProviderProfilesResponse,
  ProviderProfileSource,
  ProviderProfileStatus,
  ProviderRole,
  ProviderStatus,
  ProviderTestResult,
  RuntimeFailure,
  RuntimeStatus,
  SafeSettings,
  SettingsPatchResponse,
  SettingsPersistence,
  SettingsResponse,
  SkillStatus,
  VisualAssetDetail,
  VisualAssetReference,
  VisualAssetType,
} from "./types";

const DOCUMENT_STATUSES = new Set<DocumentProcessingStatus>([
  "uploading",
  "uploaded",
  "queued",
  "parsing",
  "parsed",
  "indexing",
  "ready",
  "failed",
]);

const PROCESSING_JOB_STAGES = new Set<ProcessingJobStage>([
  "queued", "converting", "page_check", "mineru_submit", "mineru_poll",
  "mineru_download", "normalizing", "indexing", "persist", "complete",
]);

const VISUAL_ASSET_TYPES = new Set<VisualAssetType>(["image", "chart"]);

const PROVIDER_ROLES = new Set<ProviderRole>([
  "mineru", "embedding", "reranker", "llm",
]);

const PROVIDER_PROFILE_STATUSES = new Set<ProviderProfileStatus>([
  "not_configured", "incomplete", "configured", "unavailable",
]);

const PROVIDER_PROFILE_SOURCES = new Set<ProviderProfileSource>([
  "stored", "environment", "environment_legacy", "default",
]);

const PROVIDER_ADAPTERS: Record<ProviderRole, Set<ProviderAdapter>> = {
  mineru: new Set(["mineru_official"]),
  embedding: new Set(["openai_compatible", "siliconflow"]),
  reranker: new Set(["siliconflow_qwen", "cohere", "jina"]),
  llm: new Set(["openai_compatible", "siliconflow"]),
};

const PROVIDER_OPTION_KEYS: Record<ProviderRole, Set<keyof ProviderProfileOptions>> = {
  mineru: new Set(["timeout", "language"]),
  embedding: new Set(["timeout", "batch_size", "query_prompt"]),
  reranker: new Set(["timeout"]),
  llm: new Set(["timeout"]),
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isCount(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isOptionalNullableNonEmptyString(value: unknown): value is string | null | undefined {
  return value === undefined || value === null || isNonEmptyString(value);
}

function isOptionalNullableString(value: unknown): value is string | null | undefined {
  return value === undefined || isNullableString(value);
}

function isOptionalNullablePositiveInteger(value: unknown): value is number | null | undefined {
  return value === undefined || value === null || (
    Number.isSafeInteger(value) && (value as number) > 0
  );
}

function requireProviderOptions(value: unknown, role: ProviderRole): ProviderProfileOptions {
  if (!isRecord(value)) {
    throw malformed("外部服务高级选项");
  }
  const allowedKeys = PROVIDER_OPTION_KEYS[role];
  if (Object.keys(value).some((key) => !allowedKeys.has(key as keyof ProviderProfileOptions))) {
    throw malformed("外部服务高级选项");
  }
  if (
    (value.timeout !== undefined && (
      !Number.isSafeInteger(value.timeout) || (value.timeout as number) < 1 || (value.timeout as number) > 600
    )) ||
    (value.batch_size !== undefined && (
      !Number.isSafeInteger(value.batch_size) || (value.batch_size as number) < 1 || (value.batch_size as number) > 256
    )) ||
    (value.language !== undefined && (
      !isNonEmptyString(value.language) || value.language.length > 20
    )) ||
    (value.query_prompt !== undefined && (
      typeof value.query_prompt !== "string" || value.query_prompt.length > 4000
    ))
  ) {
    throw malformed("外部服务高级选项");
  }
  return {
    ...(value.timeout === undefined ? {} : { timeout: value.timeout as number }),
    ...(value.language === undefined ? {} : { language: value.language }),
    ...(value.batch_size === undefined ? {} : { batch_size: value.batch_size as number }),
    ...(value.query_prompt === undefined ? {} : { query_prompt: value.query_prompt }),
  };
}

function requireProcessingJob(value: unknown): ProcessingJob {
  if (
    !isRecord(value) ||
    !isNonEmptyString(value.id) ||
    !isNonEmptyString(value.knowledge_base_id) ||
    !isNonEmptyString(value.document_id) ||
    !["process", "parse", "index", "normalize_upgrade"].includes(String(value.operation)) ||
    !["queued", "running", "succeeded", "failed"].includes(String(value.status)) ||
    typeof value.stage !== "string" ||
    !PROCESSING_JOB_STAGES.has(value.stage as ProcessingJobStage) ||
    !isNonNegativeInteger(value.attempt) ||
    (value.queue_position !== null && !isNonNegativeInteger(value.queue_position)) ||
    (value.error !== null && !isRecord(value.error))
  ) {
    throw malformed("处理任务");
  }
  return value as ProcessingJob;
}

function numberTuple(value: unknown, length: 2 | 4, positive = false): number[] | null {
  if (
    !Array.isArray(value) ||
    value.length !== length ||
    !value.every((item) =>
      typeof item === "number" && Number.isFinite(item) && (!positive || item > 0),
    )
  ) {
    return null;
  }
  return value;
}

function malformed(entity: string): Error {
  return new Error(`服务响应格式不正确：缺少有效的${entity}。`);
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(isNonEmptyString);
}

function isNullableNonEmptyString(value: unknown): value is string | null {
  return value === null || isNonEmptyString(value);
}

function isBBoxMap(value: unknown): value is Record<string, [number, number, number, number][]> {
  return isRecord(value) && Object.values(value).every(
    (boxes) => Array.isArray(boxes) && boxes.every((box) => numberTuple(box, 4) !== null),
  );
}

function isPageSizeMap(value: unknown): value is Record<string, [number, number]> {
  return isRecord(value) && Object.values(value).every(
    (pageSize) => numberTuple(pageSize, 2, true) !== null,
  );
}

function requireRetrievalWarning(value: unknown): RetrievalWarning {
  if (
    !isRecord(value) ||
    !isNonEmptyString(value.code) ||
    !isNonEmptyString(value.message)
  ) {
    throw malformed("检索降级提示");
  }
  return value as RetrievalWarning;
}

function requireVisualAssetReference(
  value: unknown,
  entity = "图片资产",
): VisualAssetReference {
  if (
    !isRecord(value) ||
    !isNonEmptyString(value.asset_id) ||
    typeof value.modality !== "string" ||
    !VISUAL_ASSET_TYPES.has(value.modality as VisualAssetType) ||
    !isOptionalNullableNonEmptyString(value.asset_url) ||
    !isOptionalNullableNonEmptyString(value.preview_url) ||
    (isNonEmptyString(value.preview_url) && !isNonEmptyString(value.asset_url))
  ) {
    throw malformed(entity);
  }
  return {
    asset_id: value.asset_id,
    modality: value.modality as VisualAssetType,
    asset_url: value.asset_url ?? null,
    preview_url: value.preview_url ?? null,
  };
}

function requireDocumentVisualAssetDetail(value: unknown): DocumentVisualAssetDetail {
  const detail = requireVisualAssetDetail(value);
  if (!isRecord(value) || !isNonEmptyString(value.block_id)) {
    throw malformed("文档图片资产关联");
  }
  return { ...detail, block_id: value.block_id };
}

function requireVisualAssetDetail(value: unknown): VisualAssetDetail {
  const reference = requireVisualAssetReference(value);
  if (
    !isRecord(value) ||
    !isOptionalNullableNonEmptyString(value.content_type) ||
    !isOptionalNullableNonEmptyString(value.file_hash) ||
    !isOptionalNullablePositiveInteger(value.width) ||
    !isOptionalNullablePositiveInteger(value.height) ||
    !isOptionalNullableString(value.caption) ||
    ((value.width === undefined || value.width === null) !==
      (value.height === undefined || value.height === null))
  ) {
    throw malformed("图片资产详情");
  }
  return {
    ...reference,
    content_type: value.content_type ?? null,
    file_hash: value.file_hash ?? null,
    width: value.width ?? null,
    height: value.height ?? null,
    caption: value.caption ?? null,
  };
}

function optionalVisualAsset(
  value: Record<string, unknown>,
  detailed: boolean,
): { asset?: VisualAssetReference | VisualAssetDetail } {
  const rawAsset = value.asset;
  const assetMissing = rawAsset === undefined || rawAsset === null;
  if (assetMissing) {
    return {};
  }
  const asset = detailed
    ? requireVisualAssetDetail(rawAsset)
    : requireVisualAssetReference(rawAsset);
  return { asset };
}

function requireRetrievalSource(value: unknown): RetrievalSource {
  if (
    !isRecord(value) ||
    !isNonEmptyString(value.source_id) ||
    !isNonEmptyString(value.source_url) ||
    !isNonEmptyString(value.text) ||
    !isNonEmptyString(value.document_id) ||
    !isNonEmptyString(value.document_name) ||
    !Number.isSafeInteger(value.human_page_number) ||
    (value.human_page_number as number) < 1 ||
    (value.bbox !== null && numberTuple(value.bbox, 4) === null) ||
    typeof value.score !== "number" ||
    !Number.isFinite(value.score)
  ) {
    throw malformed("检索来源");
  }
  return {
    ...value,
    ...optionalVisualAsset(value, false),
  } as RetrievalSource;
}

export function requireRetrievalResponse(value: unknown): RetrievalResponse {
  if (
    !isRecord(value) ||
    !isNonEmptyString(value.query) ||
    !isStringArray(value.knowledge_base_ids) ||
    (value.document_ids !== null && !isStringArray(value.document_ids)) ||
    (value.mode_requested !== "auto" && value.mode_requested !== "keyword") ||
    !["keyword", "dense", "dense_reranker"].includes(String(value.pipeline_used)) ||
    typeof value.degraded !== "boolean" ||
    !Array.isArray(value.warnings) ||
    !Array.isArray(value.results)
  ) {
    throw malformed("Retrieval 响应");
  }
  return {
    ...value,
    warnings: value.warnings.map(requireRetrievalWarning),
    results: value.results.map(requireRetrievalSource),
  } as RetrievalResponse;
}

function requireSourceContextItem(value: unknown): SourceContextItem {
  if (
    !isRecord(value) ||
    typeof value.text !== "string" ||
    !Number.isSafeInteger(value.human_page_number) ||
    (value.human_page_number as number) < 1 ||
    (value.bbox !== null && numberTuple(value.bbox, 4) === null) ||
    (value.page_size !== null && numberTuple(value.page_size, 2, true) === null) ||
    !isNonEmptyString(value.block_type)
  ) {
    throw malformed("Source 上下文");
  }
  return value as SourceContextItem;
}

function requireSourceContextSource(value: unknown): SourceContextSource {
  if (
    !isRecord(value) ||
    !isNonEmptyString(value.source_id) ||
    !isNonEmptyString(value.source_url) ||
    typeof value.text !== "string" ||
    !isNonEmptyString(value.document_id) ||
    !isNonEmptyString(value.document_name) ||
    !isNonEmptyString(value.knowledge_base_id) ||
    !Number.isSafeInteger(value.human_page_number) ||
    (value.human_page_number as number) < 1 ||
    (value.bbox !== null && numberTuple(value.bbox, 4) === null) ||
    (value.page_size !== null && numberTuple(value.page_size, 2, true) === null) ||
    !isStringArray(value.title_path) ||
    !isNonEmptyString(value.file_url)
  ) {
    throw malformed("Source 原文");
  }
  return {
    ...value,
    ...optionalVisualAsset(value, true),
  } as SourceContextSource;
}

export function requireSourceContextResponse(value: unknown): SourceContextResponse {
  if (
    !isRecord(value) ||
    !Array.isArray(value.before) ||
    !Array.isArray(value.after) ||
    typeof value.truncated !== "boolean"
  ) {
    throw malformed("Source Context 响应");
  }
  return {
    source: requireSourceContextSource(value.source),
    before: value.before.map(requireSourceContextItem),
    after: value.after.map(requireSourceContextItem),
    truncated: value.truncated,
  };
}

export function requireKnowledgeBase(value: unknown): KnowledgeBase {
  if (
    !isRecord(value) ||
    !isNonEmptyString(value.id) ||
    !isNonEmptyString(value.name) ||
    typeof value.description !== "string" ||
    (value.created_at !== null && typeof value.created_at !== "string") ||
    (value.updated_at !== null && typeof value.updated_at !== "string")
  ) {
    throw malformed("知识库实体");
  }
  return value as KnowledgeBase;
}

export function requireKnowledgeBaseSummary(value: unknown): KnowledgeBaseSummary {
  const knowledgeBase = requireKnowledgeBase(value);
  if (
    !isRecord(value) ||
    !isCount(value.document_count) ||
    !isCount(value.ready_document_count) ||
    !isCount(value.processing_document_count) ||
    !isCount(value.failed_document_count)
  ) {
    throw malformed("知识库统计字段");
  }
  return knowledgeBase as KnowledgeBaseSummary;
}

export function requireKnowledgeBaseListResponse(value: unknown): KnowledgeBaseListResponse {
  if (!isRecord(value) || !Array.isArray(value.knowledge_bases)) {
    throw malformed("知识库列表");
  }
  return { knowledge_bases: value.knowledge_bases.map(requireKnowledgeBaseSummary) };
}

export function requireKnowledgeBaseResponse(value: unknown): KnowledgeBaseResponse {
  if (!isRecord(value)) {
    throw malformed("知识库响应");
  }
  return { knowledge_base: requireKnowledgeBase(value.knowledge_base) };
}

export function requireDocument(value: unknown): KnowledgeBaseDocument {
  if (
    !isRecord(value) ||
    !isNonEmptyString(value.id) ||
    !isNonEmptyString(value.knowledge_base_id) ||
    !isNonEmptyString(value.original_filename) ||
    !isNonEmptyString(value.display_name) ||
    typeof value.source_type !== "string" ||
    !isCount(value.page_count) ||
    typeof value.processing_status !== "string" ||
    !DOCUMENT_STATUSES.has(value.processing_status as DocumentProcessingStatus) ||
    (value.processing_error !== null && typeof value.processing_error !== "string") ||
    typeof value.parser_provider !== "string" ||
    (value.parser_version !== null && typeof value.parser_version !== "string") ||
    (value.created_at !== null && typeof value.created_at !== "string") ||
    (value.updated_at !== null && typeof value.updated_at !== "string") ||
    typeof value.file_url !== "string" ||
    typeof value.web_url !== "string" ||
    !isRecord(value.stats) ||
    !isCount(value.stats.blocks) ||
    !isCount(value.stats.assets) ||
    !isCount(value.stats.chunks) ||
    !isCount(value.stats.index_records) ||
    !isCount(value.stats.embedding_records) ||
    !isCount(value.embedding_record_count) ||
    value.embedding_record_count !== value.stats.embedding_records ||
    typeof value.embedding_ready !== "boolean" ||
    (value.embedding_status !== "not_indexed" &&
      value.embedding_status !== "fallback_keyword" &&
      value.embedding_status !== "ready") ||
    !isNullableString(value.embedding_warning)
  ) {
    throw malformed("文档实体");
  }
  return {
    ...value,
    latest_job: value.latest_job == null ? null : requireProcessingJob(value.latest_job),
  } as KnowledgeBaseDocument;
}

export function requireDocumentListResponse(value: unknown): DocumentListResponse {
  if (!isRecord(value) || !Array.isArray(value.documents)) {
    throw malformed("文档列表");
  }
  return { documents: value.documents.map(requireDocument) };
}

export function requireDocumentResponse(value: unknown): DocumentResponse {
  if (!isRecord(value)) {
    throw malformed("文档响应");
  }
  return { document: requireDocument(value.document) };
}

export function requireUploadDocumentResponse(value: unknown): UploadDocumentResponse {
  const response = requireDocumentResponse(value);
  if (!isRecord(value)) {
    throw malformed("上传响应");
  }
  return { ...response, job: requireProcessingJob(value.job) };
}

export function requireManagedProcessResponse(value: unknown): ManagedProcessResponse {
  const response = requireDocumentResponse(value);
  if (
    !isRecord(value) ||
    (value.operation !== "parse" && value.operation !== "index")
  ) {
    throw malformed("处理响应");
  }
  return { ...response, operation: value.operation, job: requireProcessingJob(value.job) };
}

const RETRIEVAL_MODES = new Set(["keyword", "embedding"]);
const DIAGNOSTIC_TARGETS = new Set<DiagnosticTarget>(["service", "mineru", "embedding"]);
const API_METHODS = new Set(["GET", "POST", "PATCH", "DELETE"]);

function requireSafeSettings(value: unknown): SafeSettings {
  if (
    !isRecord(value) ||
    !Number.isSafeInteger(value.default_top_k) ||
    (value.default_top_k as number) < 1 ||
    (value.default_top_k as number) > 50 ||
    typeof value.default_retrieval_mode !== "string" ||
    !RETRIEVAL_MODES.has(value.default_retrieval_mode) ||
    typeof value.default_use_embedding !== "boolean" ||
    !isNonEmptyString(value.public_base_url) ||
    !isNullableNonEmptyString(value.default_knowledge_base_id)
  ) {
    throw malformed("安全设置");
  }
  return {
    default_top_k: value.default_top_k as number,
    default_retrieval_mode: value.default_retrieval_mode as SafeSettings["default_retrieval_mode"],
    default_use_embedding: value.default_use_embedding,
    public_base_url: value.public_base_url,
    default_knowledge_base_id: value.default_knowledge_base_id,
  };
}

function requireSettingsPersistence(value: unknown): SettingsPersistence {
  if (
    !isRecord(value) ||
    (value.status !== "ok" && value.status !== "degraded") ||
    !isNullableString(value.message)
  ) {
    throw malformed("设置持久化状态");
  }
  return { status: value.status, message: value.message };
}

function requireRuntimeFailure(value: unknown): RuntimeFailure {
  if (
    !isRecord(value) ||
    !isNonEmptyString(value.document_id) ||
    !isNonEmptyString(value.display_name) ||
    !isNonEmptyString(value.summary) ||
    !isNonEmptyString(value.updated_at)
  ) {
    throw malformed("失败任务摘要");
  }
  return {
    document_id: value.document_id,
    display_name: value.display_name,
    summary: value.summary,
    updated_at: value.updated_at,
  };
}

function requireRuntimeStatus(value: unknown): RuntimeStatus {
  if (
    !isRecord(value) ||
    !isRecord(value.service) ||
    typeof value.service.healthy !== "boolean" ||
    !isRecord(value.counts) ||
    !isNonNegativeInteger(value.counts.knowledge_bases) ||
    !isNonNegativeInteger(value.counts.documents) ||
    !isNonNegativeInteger(value.counts.chunks) ||
    !isNonNegativeInteger(value.counts.index_records) ||
    !Array.isArray(value.recent_failures)
  ) {
    throw malformed("运行状态");
  }
  return {
    service: { healthy: value.service.healthy },
    counts: {
      knowledge_bases: value.counts.knowledge_bases,
      documents: value.counts.documents,
      chunks: value.counts.chunks,
      index_records: value.counts.index_records,
    },
    recent_failures: value.recent_failures.map(requireRuntimeFailure),
  };
}

function requireProviderStatus(value: unknown): ProviderStatus {
  if (!isRecord(value) || typeof value.configured !== "boolean") {
    throw malformed("Provider 状态");
  }
  return {
    configured: value.configured,
  };
}

function requireSkillStatus(value: unknown): SkillStatus {
  if (
    !isRecord(value) ||
    typeof value.template_available !== "boolean" ||
    typeof value.installed !== "boolean"
  ) {
    throw malformed("Skill 状态");
  }
  return {
    template_available: value.template_available,
    installed: value.installed,
  };
}

function requireApiReferenceExample(value: unknown): ApiReferenceExample {
  if (
    !isRecord(value) ||
    !isNonEmptyString(value.label) ||
    typeof value.method !== "string" ||
    !API_METHODS.has(value.method) ||
    !isNonEmptyString(value.path) ||
    (value.body !== null && !isRecord(value.body))
  ) {
    throw malformed("API 示例");
  }
  return {
    label: value.label,
    method: value.method as ApiReferenceExample["method"],
    path: value.path,
    body: value.body,
  };
}

export function requireSettingsResponse(value: unknown): SettingsResponse {
  if (
    !isRecord(value) ||
    !isRecord(value.providers) ||
    !isRecord(value.developer) ||
    !isNonEmptyString(value.developer.openapi_url) ||
    !Array.isArray(value.developer.examples)
  ) {
    throw malformed("设置响应");
  }
  return {
    settings: requireSafeSettings(value.settings),
    persistence: requireSettingsPersistence(value.persistence),
    runtime: requireRuntimeStatus(value.runtime),
    providers: {
      mineru: requireProviderStatus(value.providers.mineru),
      embedding: requireProviderStatus(value.providers.embedding),
    },
    skill: requireSkillStatus(value.skill),
    developer: {
      openapi_url: value.developer.openapi_url,
      examples: value.developer.examples.map(requireApiReferenceExample),
    },
  };
}

export function requireSettingsPatchResponse(value: unknown): SettingsPatchResponse {
  if (!isRecord(value)) {
    throw malformed("设置保存响应");
  }
  return {
    settings: requireSafeSettings(value.settings),
    persistence: requireSettingsPersistence(value.persistence),
  };
}

function requireDiagnosticResult(value: unknown): DiagnosticResult {
  if (
    !isRecord(value) ||
    typeof value.target !== "string" ||
    !DIAGNOSTIC_TARGETS.has(value.target as DiagnosticTarget) ||
    typeof value.ok !== "boolean" ||
    !isNonEmptyString(value.code) ||
    !isNonEmptyString(value.message) ||
    !isNullableString(value.action)
  ) {
    throw malformed("诊断响应");
  }
  return {
    target: value.target as DiagnosticTarget,
    ok: value.ok,
    code: value.code,
    message: value.message,
    action: value.action,
  };
}

export function requireDiagnosticResponse(value: unknown): DiagnosticResponse {
  if (!isRecord(value)) {
    throw malformed("诊断响应");
  }
  return { diagnostic: requireDiagnosticResult(value.diagnostic) };
}

export function requireProviderProfile(
  value: unknown,
  expectedRole?: ProviderRole,
): ProviderProfile {
  if (
    !isRecord(value) ||
    typeof value.role !== "string" ||
    !PROVIDER_ROLES.has(value.role as ProviderRole) ||
    (expectedRole !== undefined && value.role !== expectedRole) ||
    typeof value.adapter !== "string" ||
    !PROVIDER_ADAPTERS[value.role as ProviderRole].has(value.adapter as ProviderAdapter) ||
    (value.source !== undefined && (
      typeof value.source !== "string" ||
      !PROVIDER_PROFILE_SOURCES.has(value.source as ProviderProfileSource)
    )) ||
    !isNonEmptyString(value.display_name) ||
    !isNullableNonEmptyString(value.base_url) ||
    !isNullableNonEmptyString(value.model) ||
    !isRecord(value.options) ||
    typeof value.configured !== "boolean" ||
    typeof value.secret_present !== "boolean" ||
    typeof value.status !== "string" ||
    !PROVIDER_PROFILE_STATUSES.has(value.status as ProviderProfileStatus) ||
    ![null, "success", "failed", "not_supported"].includes(value.last_test_status as string | null) ||
    !isNullableNonEmptyString(value.last_tested_at)
  ) {
    throw malformed("外部服务配置");
  }
  return {
    role: value.role as ProviderRole,
    adapter: value.adapter as ProviderAdapter,
    source: (value.source as ProviderProfileSource | undefined) ?? null,
    display_name: value.display_name,
    base_url: value.base_url,
    model: value.model,
    options: requireProviderOptions(value.options, value.role as ProviderRole),
    configured: value.configured,
    secret_present: value.secret_present,
    status: value.status as ProviderProfileStatus,
    last_test_status: value.last_test_status as ProviderProfile["last_test_status"],
    last_tested_at: value.last_tested_at ?? null,
  };
}

export function requireProviderProfilesResponse(value: unknown): ProviderProfilesResponse {
  if (
    !isRecord(value) ||
    typeof value.storage_available !== "boolean" ||
    !isRecord(value.profiles)
  ) {
    throw malformed("外部服务配置列表");
  }
  return {
    storage_available: value.storage_available,
    profiles: {
      mineru: requireProviderProfile(value.profiles.mineru, "mineru"),
      embedding: requireProviderProfile(value.profiles.embedding, "embedding"),
      reranker: requireProviderProfile(value.profiles.reranker, "reranker"),
      llm: requireProviderProfile(value.profiles.llm, "llm"),
    },
  };
}

export function requireProviderTestResult(value: unknown): ProviderTestResult {
  if (
    !isRecord(value) ||
    typeof value.role !== "string" ||
    !PROVIDER_ROLES.has(value.role as ProviderRole) ||
    typeof value.ok !== "boolean" ||
    !["success", "failed", "not_supported"].includes(String(value.status)) ||
    !isNonEmptyString(value.message) ||
    !isNonEmptyString(value.tested_at)
  ) {
    throw malformed("外部服务测试结果");
  }
  return {
    role: value.role as ProviderRole,
    ok: value.ok,
    status: value.status as ProviderTestResult["status"],
    message: value.message,
    tested_at: value.tested_at,
  };
}

export function requireDocumentBlock(value: unknown): DocumentBlock {
  if (
    !isRecord(value) ||
    !isNonEmptyString(value.id) ||
    !isNonEmptyString(value.document_id) ||
    !isNonNegativeInteger(value.page_idx) ||
    value.human_page_number !== value.page_idx + 1 ||
    !isNonNegativeInteger(value.block_idx) ||
    !isNonEmptyString(value.block_type) ||
    !isNullableString(value.text) ||
    !isNullableString(value.html) ||
    !isNullableString(value.asset_id) ||
    (value.is_discarded !== undefined && typeof value.is_discarded !== "boolean") ||
    !isRecord(value.metadata) ||
    (value.bbox !== null && numberTuple(value.bbox, 4) === null) ||
    (value.page_size !== null && numberTuple(value.page_size, 2, true) === null) ||
    (value.bbox_by_page !== undefined && !isBBoxMap(value.bbox_by_page)) ||
    (
      value.bbox_page_size_by_page !== undefined &&
      !isPageSizeMap(value.bbox_page_size_by_page)
    )
  ) {
    throw malformed("文档 Block");
  }
  const pageKey = String(value.page_idx);
  return {
    ...value,
    bbox_by_page: value.bbox_by_page ?? (
      value.bbox === null ? {} : { [pageKey]: [value.bbox] }
    ),
    bbox_page_size_by_page: value.bbox_page_size_by_page ?? (
      value.page_size === null ? {} : { [pageKey]: value.page_size }
    ),
    is_discarded: value.is_discarded ?? false,
  } as DocumentBlock;
}

export function requireDocumentPageMetadata(value: unknown): DocumentPageMetadata {
  if (
    !isRecord(value) ||
    !isNonNegativeInteger(value.page_idx) ||
    value.human_page_number !== value.page_idx + 1 ||
    !isNonNegativeInteger(value.block_count) ||
    (value.page_size !== null && numberTuple(value.page_size, 2, true) === null)
  ) {
    throw malformed("文档页面元数据");
  }
  return value as DocumentPageMetadata;
}

export function requireDocumentBlocksResponse(value: unknown): DocumentBlocksResponse {
  if (!isRecord(value) || !Array.isArray(value.blocks) || !Array.isArray(value.pages)) {
    throw malformed("文档 Blocks 响应");
  }
  const blocks = value.blocks.map(requireDocumentBlock);
  const pages = value.pages.map(requireDocumentPageMetadata);
  if (new Set(blocks.map((block) => block.id)).size !== blocks.length) {
    throw malformed("唯一 Block 标识");
  }
  const rawAssets = value.assets ?? {};
  if (!isRecord(rawAssets)) {
    throw malformed("文档图片资产映射");
  }
  const assets = Object.fromEntries(
    Object.entries(rawAssets).map(([assetId, rawAsset]) => {
      const asset = requireDocumentVisualAssetDetail(rawAsset);
      if (asset.asset_id !== assetId) {
        throw malformed("图片资产标识");
      }
      return [assetId, asset];
    }),
  );
  const blocksById = new Map(blocks.map((block) => [block.id, block]));
  for (const asset of Object.values(assets)) {
    const block = blocksById.get(asset.block_id);
    if (
      !block ||
      block.asset_id !== asset.asset_id ||
      block.block_type.toLowerCase() !== asset.modality
    ) {
      throw malformed("图片资产与文档内容的关联");
    }
  }
  return { blocks, pages, assets };
}
