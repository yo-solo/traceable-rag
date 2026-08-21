import type { ProviderAdapter, ProviderRole } from "../../api/types";

export type ProviderAdapterOption = {
  value: ProviderAdapter;
  label: string;
  defaultBaseUrl: string;
  defaultModel: string;
  baseUrlPlaceholder?: string;
  modelPlaceholder?: string;
};

export type ProviderRoleConfig = {
  title: string;
  eyebrow: string;
  description: string;
  optionalNote?: string;
  adapters: ProviderAdapterOption[];
};

export const PROVIDER_ROLES = ["mineru", "embedding", "reranker", "llm"] as const;

export const PROVIDER_CONFIG: Record<ProviderRole, ProviderRoleConfig> = {
  mineru: {
    title: "文档解析",
    eyebrow: "MinerU",
    description: "用于上传 PDF 或 Office 文档后的自动解析。已有可检索内容时可以稍后再配置。",
    adapters: [{
      value: "mineru_official",
      label: "MinerU 官方服务",
      defaultBaseUrl: "https://mineru.net",
      defaultModel: "vlm",
    }],
  },
  embedding: {
    title: "语义检索",
    eyebrow: "Embedding",
    description: "生成向量索引并支持语义召回。未配置时仍可使用关键词检索。",
    adapters: [
      {
        value: "openai_compatible",
        label: "OpenAI-compatible",
        defaultBaseUrl: "",
        defaultModel: "",
        baseUrlPlaceholder: "例如 https://api.example.com",
        modelPlaceholder: "填写该服务的 Embedding 模型名",
      },
      {
        value: "siliconflow",
        label: "SiliconFlow",
        defaultBaseUrl: "https://api.siliconflow.cn",
        defaultModel: "",
        modelPlaceholder: "例如 BAAI/bge-m3",
      },
    ],
  },
  reranker: {
    title: "结果重排",
    eyebrow: "Reranker",
    description: "对语义召回候选进行二次排序；未配置时语义检索仍可在无重排模式下运行。",
    adapters: [
      {
        value: "siliconflow_qwen",
        label: "SiliconFlow · Qwen",
        defaultBaseUrl: "https://api.siliconflow.cn",
        defaultModel: "Qwen/Qwen3-Reranker-8B",
      },
      {
        value: "cohere",
        label: "Cohere",
        defaultBaseUrl: "https://api.cohere.com",
        defaultModel: "rerank-v3.5",
      },
      {
        value: "jina",
        label: "Jina AI",
        defaultBaseUrl: "https://api.jina.ai",
        defaultModel: "jina-reranker-v2-base-multilingual",
      },
    ],
  },
  llm: {
    title: "语言模型",
    eyebrow: "LLM",
    description: "预留给后续增强能力的模型连接。",
    optionalNote: "可选；当前版本不使用此配置生成答案。",
    adapters: [
      {
        value: "openai_compatible",
        label: "OpenAI-compatible",
        defaultBaseUrl: "",
        defaultModel: "",
        baseUrlPlaceholder: "例如 https://api.example.com",
        modelPlaceholder: "填写该服务的模型名",
      },
      {
        value: "siliconflow",
        label: "SiliconFlow",
        defaultBaseUrl: "https://api.siliconflow.cn",
        defaultModel: "",
        modelPlaceholder: "填写要连接的模型名",
      },
    ],
  },
};

export function providerAdapterOption(role: ProviderRole, adapter: ProviderAdapter) {
  return PROVIDER_CONFIG[role].adapters.find((option) => option.value === adapter)
    ?? PROVIDER_CONFIG[role].adapters[0];
}
