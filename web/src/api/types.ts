export type ApiErrorDetail = {
  code?: string;
  message?: string;
};

export type ApiErrorPayload = {
  detail?: ApiErrorDetail | string;
  message?: string;
};

export type ApiRequestOptions = {
  signal?: AbortSignal;
  headers?: HeadersInit;
};

export type ApiJsonRequestOptions = ApiRequestOptions & {
  body?: unknown;
};

export type ApiUploadOptions = ApiRequestOptions & {
  displayName?: string;
  fieldName?: string;
};

export type DocumentProcessingStatus =
  | "uploading"
  | "uploaded"
  | "queued"
  | "parsing"
  | "parsed"
  | "indexing"
  | "ready"
  | "failed";

export type ProcessingJobStage =
  | "queued"
  | "converting"
  | "page_check"
  | "mineru_submit"
  | "mineru_poll"
  | "mineru_download"
  | "normalizing"
  | "indexing"
  | "persist"
  | "complete";

export type ProcessingJob = {
  id: string;
  knowledge_base_id: string;
  document_id: string;
  operation: "process" | "parse" | "index" | "normalize_upgrade";
  status: "queued" | "running" | "succeeded" | "failed";
  stage: ProcessingJobStage;
  attempt: number;
  queue_position: number | null;
  error: {
    code: string;
    stage: ProcessingJobStage;
    message: string;
    retryable: boolean;
    diagnostic_id: string;
  } | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  updated_at: string | null;
};

export type KnowledgeBase = {
  id: string;
  name: string;
  description: string;
  created_at: string | null;
  updated_at: string | null;
};

export type KnowledgeBaseSummary = KnowledgeBase & {
  document_count: number;
  ready_document_count: number;
  processing_document_count: number;
  failed_document_count: number;
};

export type KnowledgeBaseDocument = {
  id: string;
  knowledge_base_id: string;
  original_filename: string;
  display_name: string;
  source_type: string;
  page_count: number;
  processing_status: DocumentProcessingStatus;
  processing_error: string | null;
  parser_provider: string;
  parser_version: string | null;
  created_at: string | null;
  updated_at: string | null;
  stats: {
    blocks: number;
    assets: number;
    chunks: number;
    index_records: number;
    embedding_records: number;
  };
  embedding_record_count: number;
  embedding_ready: boolean;
  embedding_status: "not_indexed" | "fallback_keyword" | "ready";
  embedding_warning: string | null;
  file_url: string;
  web_url: string;
  latest_job: ProcessingJob | null;
};

export type KnowledgeBaseListResponse = {
  knowledge_bases: KnowledgeBaseSummary[];
};

export type KnowledgeBaseResponse = {
  knowledge_base: KnowledgeBase;
};

export type DocumentListResponse = {
  documents: KnowledgeBaseDocument[];
};

export type DocumentResponse = {
  document: KnowledgeBaseDocument;
};

export type UploadDocumentResponse = DocumentResponse & {
  job: ProcessingJob;
};

export type ManagedProcessResponse = DocumentResponse & {
  operation: "parse" | "index";
  job: ProcessingJob;
};

export type DocumentBlock = {
  id: string;
  document_id: string;
  page_idx: number;
  human_page_number: number;
  block_idx: number;
  block_type: string;
  text: string | null;
  html: string | null;
  asset_id: string | null;
  bbox: [number, number, number, number] | null;
  page_size: [number, number] | null;
  bbox_by_page?: Record<string, [number, number, number, number][]>;
  bbox_page_size_by_page?: Record<string, [number, number]>;
  is_discarded: boolean;
  metadata: Record<string, unknown>;
};

export type DocumentPageMetadata = {
  page_idx: number;
  human_page_number: number;
  page_size: [number, number] | null;
  block_count: number;
};

export type VisualAssetType = "image" | "chart";

export type VisualAssetReference = {
  asset_id: string;
  modality: VisualAssetType;
  asset_url: string | null;
  preview_url: string | null;
};

export type VisualAssetDetail = VisualAssetReference & {
  content_type: string | null;
  file_hash: string | null;
  width: number | null;
  height: number | null;
  caption: string | null;
};

export type DocumentVisualAssetDetail = VisualAssetDetail & {
  block_id: string;
};

export type DocumentBlocksResponse = {
  blocks: DocumentBlock[];
  pages: DocumentPageMetadata[];
  assets: Record<string, DocumentVisualAssetDetail>;
};

export type RetrievalWarning = {
  code: string;
  message: string;
};

export type RetrievalSource = {
  source_id: string;
  source_url: string;
  text: string;
  document_id: string;
  document_name: string;
  human_page_number: number;
  bbox: [number, number, number, number] | null;
  score: number;
  asset?: VisualAssetReference;
};

export type RetrievalResponse = {
  query: string;
  knowledge_base_ids: string[];
  document_ids: string[] | null;
  mode_requested: "auto" | "keyword";
  pipeline_used: "keyword" | "dense" | "dense_reranker";
  degraded: boolean;
  warnings: RetrievalWarning[];
  results: RetrievalSource[];
};

export type SourceContextItem = {
  text: string;
  human_page_number: number;
  bbox: [number, number, number, number] | null;
  page_size: [number, number] | null;
  block_type: string;
};

export type SourceContextSource = Omit<SourceContextItem, "block_type"> & {
  source_id: string;
  source_url: string;
  document_id: string;
  document_name: string;
  knowledge_base_id: string;
  title_path: string[];
  file_url: string;
  asset?: VisualAssetDetail;
};

export type SourceContextResponse = {
  source: SourceContextSource;
  before: SourceContextItem[];
  after: SourceContextItem[];
  truncated: boolean;
};

export type RetrievalMode = "keyword" | "embedding";

export type SafeSettings = {
  default_top_k: number;
  default_retrieval_mode: RetrievalMode;
  default_use_embedding: boolean;
  public_base_url: string;
  default_knowledge_base_id: string | null;
};

export type SettingsPersistence = {
  status: "ok" | "degraded";
  message: string | null;
};

export type RuntimeFailure = {
  document_id: string;
  display_name: string;
  summary: string;
  updated_at: string;
};

export type RuntimeStatus = {
  service: { healthy: boolean };
  counts: {
    knowledge_bases: number;
    documents: number;
    chunks: number;
    index_records: number;
  };
  recent_failures: RuntimeFailure[];
};

export type ProviderStatus = {
  configured: boolean;
};

export type SkillStatus = {
  template_available: boolean;
  installed: boolean;
};

export type ApiReferenceExample = {
  label: string;
  method: "GET" | "POST" | "PATCH" | "DELETE";
  path: string;
  body: Record<string, unknown> | null;
};

export type SettingsResponse = {
  settings: SafeSettings;
  persistence: SettingsPersistence;
  runtime: RuntimeStatus;
  providers: {
    mineru: ProviderStatus;
    embedding: ProviderStatus;
  };
  skill: SkillStatus;
  developer: {
    openapi_url: string;
    examples: ApiReferenceExample[];
  };
};

export type SettingsPatchResponse = {
  settings: SafeSettings;
  persistence: SettingsPersistence;
};

export type DiagnosticTarget = "service" | "mineru" | "embedding";

export type DiagnosticResult = {
  target: DiagnosticTarget;
  ok: boolean;
  code: string;
  message: string;
  action: string | null;
};

export type DiagnosticResponse = {
  diagnostic: DiagnosticResult;
};

export type ProviderRole = "mineru" | "embedding" | "reranker" | "llm";

export type ProviderAdapter =
  | "mineru_official"
  | "openai_compatible"
  | "siliconflow"
  | "siliconflow_qwen"
  | "cohere"
  | "jina";

export type ProviderProfileStatus =
  | "not_configured"
  | "incomplete"
  | "configured"
  | "unavailable";

export type ProviderLastTestStatus = "success" | "failed" | "not_supported" | null;

export type ProviderProfileSource =
  | "stored"
  | "environment"
  | "environment_legacy"
  | "default";

export type ProviderProfileOptions = {
  timeout?: number;
  language?: string;
  batch_size?: number;
  query_prompt?: string;
};

export type ProviderProfile = {
  role: ProviderRole;
  adapter: ProviderAdapter;
  source: ProviderProfileSource | null;
  display_name: string;
  base_url: string | null;
  model: string | null;
  options: ProviderProfileOptions;
  configured: boolean;
  secret_present: boolean;
  status: ProviderProfileStatus;
  last_test_status: ProviderLastTestStatus;
  last_tested_at: string | null;
};

export type ProviderProfilesResponse = {
  storage_available: boolean;
  profiles: Record<ProviderRole, ProviderProfile>;
};

export type ProviderProfileMutation = {
  adapter: ProviderAdapter;
  display_name: string | null;
  base_url: string | null;
  model: string | null;
  options: ProviderProfileOptions;
  api_key?: string | null;
};

export type ProviderTestResult = {
  role: ProviderRole;
  ok: boolean;
  status: "success" | "failed" | "not_supported";
  message: string;
  tested_at: string;
};
