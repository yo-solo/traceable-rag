import { Save, Settings2 } from "lucide-react";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { ApiError, api } from "../../api/client";
import {
  requireDiagnosticResponse,
  requireKnowledgeBaseListResponse,
  requireSettingsPatchResponse,
  requireSettingsResponse,
} from "../../api/guards";
import type {
  DiagnosticResult,
  DiagnosticTarget,
  KnowledgeBaseSummary,
  SafeSettings,
  SettingsResponse,
} from "../../api/types";
import { AsyncState } from "../../components/AsyncState";
import { ProviderManager } from "../providers/ProviderManager";
import { ApiReference } from "./ApiReference";
import { RuntimeStatus } from "./RuntimeStatus";
import { SkillStatus } from "./SkillStatus";

type SettingsTab = "preferences" | "providers" | "runtime" | "developer";

type SettingsDraft = Omit<SafeSettings, "default_top_k"> & {
  default_top_k: string;
};

function toDraft(settings: SafeSettings): SettingsDraft {
  return { ...settings, default_top_k: String(settings.default_top_k) };
}

function normalizeDefaultKnowledgeBase(
  settings: SafeSettings,
  knowledgeBases: KnowledgeBaseSummary[],
): SafeSettings {
  const defaultId = settings.default_knowledge_base_id;
  if (defaultId && !knowledgeBases.some((knowledgeBase) => knowledgeBase.id === defaultId)) {
    return { ...settings, default_knowledge_base_id: null };
  }
  return settings;
}

function errorMessage(error: unknown, fallback: string) {
  void error;
  return fallback;
}

function validatedSettings(draft: SettingsDraft): SafeSettings | null {
  const defaultTopK = Number(draft.default_top_k);
  if (!Number.isSafeInteger(defaultTopK) || defaultTopK < 1 || defaultTopK > 50) {
    return null;
  }
  try {
    const url = new URL(draft.public_base_url.trim());
    if (
      !["http:", "https:"].includes(url.protocol) ||
      url.username ||
      url.password ||
      url.search ||
      url.hash
    ) {
      return null;
    }
  } catch {
    return null;
  }
  return {
    ...draft,
    default_top_k: defaultTopK,
    public_base_url: draft.public_base_url.trim().replace(/\/$/, ""),
  };
}

export function SettingsPage() {
  const [response, setResponse] = useState<SettingsResponse | null>(null);
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseSummary[]>([]);
  const [draft, setDraft] = useState<SettingsDraft | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loadRetry, setLoadRetry] = useState(0);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState(false);
  const [activeDiagnostic, setActiveDiagnostic] = useState<DiagnosticTarget | null>(null);
  const [diagnosticError, setDiagnosticError] = useState<{
    target: DiagnosticTarget;
    message: string;
  } | null>(null);
  const [diagnosticResult, setDiagnosticResult] = useState<DiagnosticResult | null>(null);
  const [activeTab, setActiveTab] = useState<SettingsTab>("preferences");
  const loadEpoch = useRef(0);
  const saveEpoch = useRef(0);
  const diagnosticEpoch = useRef(0);
  const saveController = useRef<AbortController | null>(null);
  const diagnosticController = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const epoch = ++loadEpoch.current;
    setLoading(true);
    setLoadError(null);
    void Promise.all([
      api.get<unknown>("/api/settings", { signal: controller.signal }),
      api.get<unknown>("/api/knowledge-bases", { signal: controller.signal }),
    ])
      .then(([settingsValue, knowledgeBaseValue]) => {
        if (controller.signal.aborted || epoch !== loadEpoch.current) {
          return;
        }
        const settings = requireSettingsResponse(settingsValue);
        const knowledgeBaseResponse = requireKnowledgeBaseListResponse(knowledgeBaseValue);
        const loadedKnowledgeBases = knowledgeBaseResponse.knowledge_bases;
        const normalizedSettings = normalizeDefaultKnowledgeBase(
          settings.settings,
          loadedKnowledgeBases,
        );
        setResponse({ ...settings, settings: normalizedSettings });
        setKnowledgeBases(loadedKnowledgeBases);
        setDraft(toDraft(normalizedSettings));
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted && epoch === loadEpoch.current) {
          setLoadError(errorMessage(error, "设置与运行状态加载失败，请重试。"));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted && epoch === loadEpoch.current) {
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [loadRetry]);

  useEffect(
    () => () => {
      saveController.current?.abort();
      diagnosticController.current?.abort();
    },
    [],
  );

  async function saveSettings() {
    if (!draft) {
      return;
    }
    const settings = validatedSettings(draft);
    if (!settings) {
      setSaveError("请检查参考内容数量和服务地址；服务地址需要使用有效的 HTTP(S) 地址。");
      setSaveSuccess(false);
      return;
    }
    saveController.current?.abort();
    const controller = new AbortController();
    const epoch = ++saveEpoch.current;
    saveController.current = controller;
    setSaving(true);
    setSaveError(null);
    setSaveSuccess(false);
    try {
      const value = await api.patch<unknown>("/api/settings", {
        signal: controller.signal,
        body: settings,
      });
      if (controller.signal.aborted || epoch !== saveEpoch.current) {
        return;
      }
      const saved = requireSettingsPatchResponse(value);
      const normalizedSettings = normalizeDefaultKnowledgeBase(saved.settings, knowledgeBases);
      setDraft(toDraft(normalizedSettings));
      setResponse((current) =>
        current
          ? { ...current, settings: normalizedSettings, persistence: saved.persistence }
          : current,
      );
      setSaveSuccess(true);
    } catch (error) {
      if (!controller.signal.aborted && epoch === saveEpoch.current) {
        setSaveError(errorMessage(error, "设置保存失败，请检查服务状态后重试。"));
      }
    } finally {
      if (!controller.signal.aborted && epoch === saveEpoch.current) {
        setSaving(false);
      }
    }
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    void saveSettings();
  }

  async function runDiagnostic(target: DiagnosticTarget) {
    diagnosticController.current?.abort();
    const controller = new AbortController();
    const epoch = ++diagnosticEpoch.current;
    diagnosticController.current = controller;
    setActiveDiagnostic(target);
    setDiagnosticError(null);
    setDiagnosticResult(null);
    try {
      const value = await api.post<unknown>("/api/settings/diagnostics", {
        signal: controller.signal,
        body: { target },
      });
      if (!controller.signal.aborted && epoch === diagnosticEpoch.current) {
        setDiagnosticResult(requireDiagnosticResponse(value).diagnostic);
      }
    } catch (error) {
      if (!controller.signal.aborted && epoch === diagnosticEpoch.current) {
        setDiagnosticError({
          target,
          message: errorMessage(error, "连接测试失败，请稍后重试。"),
        });
      }
    } finally {
      if (!controller.signal.aborted && epoch === diagnosticEpoch.current) {
        setActiveDiagnostic(null);
      }
    }
  }

  return (
    <section className="feature-page settings-page" aria-labelledby="settings-page-title">
      <header className="feature-page__header settings-page__header">
        <div>
          <h1 id="settings-page-title">设置</h1>
          <p>调整日常使用偏好，查看系统是否可用，或获取开发者接入信息。</p>
        </div>
      </header>

      <AsyncState
        error={loadError}
        loading={loading}
        loadingLabel="正在加载设置与运行状态…"
        onRetry={() => setLoadRetry((value) => value + 1)}
      >
        {response && draft ? (
          <div className="settings-page__content">
            <div className="settings-tabs" aria-label="设置分类" role="tablist">
              {([
                ["preferences", "使用设置"],
                ["providers", "外部服务"],
                ["runtime", "运行状态"],
                ["developer", "开发者接入"],
              ] as const).map(([value, label]) => (
                <button
                  aria-controls={`settings-panel-${value}`}
                  aria-selected={activeTab === value}
                  className={activeTab === value ? "is-selected" : ""}
                  id={`settings-tab-${value}`}
                  key={value}
                  onClick={() => setActiveTab(value)}
                  role="tab"
                  type="button"
                >
                  {label}
                </button>
              ))}
            </div>

            {response.persistence.status === "degraded" ? (
              <div className="settings-persistence-warning" role="alert">
                <strong>部分设置暂时无法读取</strong>
                <p>当前已使用可用的默认设置，你仍可继续使用并重新保存。</p>
              </div>
            ) : null}

            {activeTab === "preferences" ? (
              <form
                aria-busy={saving}
                aria-labelledby="settings-tab-preferences"
                className="settings-section settings-form"
                id="settings-panel-preferences"
                noValidate
                onSubmit={handleSubmit}
                role="tabpanel"
              >
                <header className="settings-section__header">
                  <div>
                    <h2>默认使用方式</h2>
                    <p>这些选项会用于新的查询，不影响已经保存的问答记录。</p>
                  </div>
                  <Settings2 aria-hidden="true" size={21} />
                </header>

                <div className="settings-form__grid">
                  <label className="settings-field">
                    <span>默认参考内容数量</span>
                    <input
                      aria-label="默认参考内容数量"
                      disabled={saving}
                      max={50}
                      min={1}
                      onChange={(event) => {
                        setDraft((current) =>
                          current ? { ...current, default_top_k: event.target.value } : current,
                        );
                        setSaveSuccess(false);
                      }}
                      type="number"
                      value={draft.default_top_k}
                    />
                    <small>安全范围 1–50。</small>
                  </label>
                  <label className="settings-field">
                    <span>默认检索模式</span>
                    <select
                      aria-label="默认检索模式"
                      disabled={saving}
                      onChange={(event) => {
                        setDraft((current) =>
                          current
                            ? {
                                ...current,
                                default_retrieval_mode: event.target.value as SafeSettings["default_retrieval_mode"],
                                default_use_embedding: event.target.value === "embedding",
                              }
                            : current,
                        );
                        setSaveSuccess(false);
                      }}
                      value={draft.default_retrieval_mode}
                    >
                      <option value="keyword">精确匹配</option>
                      <option value="embedding">语义检索</option>
                    </select>
                  </label>
                  <label className="settings-field">
                    <span>默认知识库</span>
                    <select
                      aria-label="默认知识库"
                      disabled={saving}
                      onChange={(event) => {
                        setDraft((current) =>
                          current
                            ? {
                                ...current,
                                default_knowledge_base_id: event.target.value || null,
                              }
                            : current,
                        );
                        setSaveSuccess(false);
                      }}
                      value={draft.default_knowledge_base_id ?? ""}
                    >
                      <option value="">不预选</option>
                      {knowledgeBases.map((knowledgeBase) => (
                        <option key={knowledgeBase.id} value={knowledgeBase.id}>
                          {knowledgeBase.name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="settings-toggle settings-field--wide">
                    <input
                      aria-label="默认启用语义检索"
                      checked={draft.default_use_embedding}
                      disabled={saving}
                      onChange={(event) => {
                        setDraft((current) =>
                          current
                            ? {
                                ...current,
                                default_use_embedding: event.target.checked,
                                default_retrieval_mode: event.target.checked
                                  ? "embedding"
                                  : "keyword",
                              }
                            : current,
                        );
                        setSaveSuccess(false);
                      }}
                      type="checkbox"
                    />
                    <span>
                      <strong>默认启用语义检索</strong>
                      <small>如果语义检索暂不可用，仍会自动使用精确匹配。</small>
                    </span>
                  </label>
                </div>

                {saveError ? (
                  <div className="settings-save-message settings-save-message--error" role="alert">
                    <p>{saveError}</p>
                    <button disabled={saving} onClick={() => void saveSettings()} type="button">
                      重试保存设置
                    </button>
                  </div>
                ) : saveSuccess ? (
                  <div className="settings-save-message is-success" role="status">
                    设置已保存。
                  </div>
                ) : null}

                <div className="settings-form__actions">
                  <button className="primary-button" disabled={saving} type="submit">
                    <Save aria-hidden="true" size={15} />
                    {saving ? "正在保存…" : "保存设置"}
                  </button>
                </div>
              </form>
            ) : null}

            {activeTab === "runtime" ? (
              <div
                aria-labelledby="settings-tab-runtime"
                id="settings-panel-runtime"
                role="tabpanel"
              >
                <RuntimeStatus
                  activeDiagnostic={activeDiagnostic}
                  diagnosticError={diagnosticError}
                  diagnosticResult={diagnosticResult}
                  onDiagnostic={(target) => void runDiagnostic(target)}
                  providers={response.providers}
                  runtime={response.runtime}
                />
              </div>
            ) : null}

            {activeTab === "providers" ? (
              <div
                aria-labelledby="settings-tab-providers"
                id="settings-panel-providers"
                role="tabpanel"
              >
                <ProviderManager context="settings" />
              </div>
            ) : null}

            {activeTab === "developer" ? (
              <div
                aria-labelledby="settings-tab-developer"
                className="settings-developer-stack"
                id="settings-panel-developer"
                role="tabpanel"
              >
                <form className="settings-section developer-base-url" onSubmit={handleSubmit}>
                  <header className="settings-section__header">
                    <div>
                      <h2>公共访问地址</h2>
                      <p>用于生成 API、OpenAPI 和 Codex Skill 中可从外部访问的链接。</p>
                    </div>
                  </header>
                  <div className="settings-form__grid">
                    <label className="settings-field settings-field--wide">
                      <span>服务地址</span>
                      <input
                        aria-label="服务地址"
                        disabled={saving}
                        onChange={(event) => {
                          setDraft((current) =>
                            current ? { ...current, public_base_url: event.target.value } : current,
                          );
                          setSaveSuccess(false);
                        }}
                        spellCheck={false}
                        type="url"
                        value={draft.public_base_url}
                      />
                      <small>使用不包含账号、密码、查询参数或片段的 HTTP(S) 地址。</small>
                    </label>
                  </div>
                  <div className="settings-form__actions">
                    <button className="primary-button" disabled={saving} type="submit">
                      <Save aria-hidden="true" size={15} />
                      {saving ? "正在保存…" : "保存地址"}
                    </button>
                  </div>
                </form>
                <SkillStatus publicBaseUrl={draft.public_base_url} status={response.skill} />
                <ApiReference
                  openApiUrl={response.developer.openapi_url}
                />
              </div>
            ) : null}
          </div>
        ) : (
          <span />
        )}
      </AsyncState>
    </section>
  );
}
