import {
  AlertTriangle,
  CheckCircle2,
  ExternalLink,
  FileText,
  KeyRound,
  MessageSquare,
  Save,
  Search,
  Settings,
  Trash2,
  Wifi,
} from "lucide-react";
import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";

import { ApiError, api } from "../../api/client";
import {
  requireProviderProfile,
  requireProviderProfilesResponse,
  requireProviderTestResult,
} from "../../api/guards";
import type {
  ProviderAdapter,
  ProviderProfile,
  ProviderProfileMutation,
  ProviderProfilesResponse,
  ProviderRole,
} from "../../api/types";
import { AsyncState } from "../../components/AsyncState";
import { providerAdminHeaders } from "./adminToken";
import {
  PROVIDER_CONFIG,
  PROVIDER_ROLES,
  providerAdapterOption,
} from "./providerConfig";

type ProviderManagerProps = {
  context?: "setup" | "settings";
};

type ProviderDraft = {
  adapter: ProviderAdapter;
  displayName: string;
  baseUrl: string;
  model: string;
  apiKey: string;
  timeout: string;
  language: string;
  batchSize: string;
  queryPrompt: string;
};

type OperationKind = "save" | "test" | "delete";

type ProviderOperation = {
  kind: OperationKind | null;
  error: string | null;
  success: string | null;
  testOk: boolean | null;
  notice?: string | null;
};

const ROLE_ICONS = {
  mineru: FileText,
  embedding: Search,
  reranker: Settings,
  llm: MessageSquare,
} satisfies Record<ProviderRole, typeof FileText>;

function operationState(): Record<ProviderRole, ProviderOperation> {
  return {
    mineru: { kind: null, error: null, success: null, testOk: null },
    embedding: { kind: null, error: null, success: null, testOk: null },
    reranker: { kind: null, error: null, success: null, testOk: null },
    llm: { kind: null, error: null, success: null, testOk: null },
  };
}

function draftFromProfile(profile: ProviderProfile): ProviderDraft {
  const defaults = providerAdapterOption(profile.role, profile.adapter);
  return {
    adapter: profile.adapter,
    displayName: profile.display_name,
    baseUrl: profile.base_url ?? defaults.defaultBaseUrl,
    model: profile.model ?? defaults.defaultModel,
    apiKey: "",
    timeout: profile.options.timeout === undefined ? "" : String(profile.options.timeout),
    language: profile.options.language ?? "",
    batchSize: profile.options.batch_size === undefined ? "" : String(profile.options.batch_size),
    queryPrompt: profile.options.query_prompt ?? "",
  };
}

function draftsFromProfiles(profiles: ProviderProfilesResponse["profiles"]) {
  return {
    mineru: draftFromProfile(profiles.mineru),
    embedding: draftFromProfile(profiles.embedding),
    reranker: draftFromProfile(profiles.reranker),
    llm: draftFromProfile(profiles.llm),
  } satisfies Record<ProviderRole, ProviderDraft>;
}

function normalizedOptional(value: string) {
  const normalized = value.trim();
  return normalized || null;
}

function mutationFromDraft(role: ProviderRole, draft: ProviderDraft): ProviderProfileMutation {
  const options: ProviderProfileMutation["options"] = {};
  if (draft.timeout.trim()) {
    options.timeout = Number(draft.timeout);
  }
  if (role === "mineru" && draft.language.trim()) {
    options.language = draft.language.trim();
  }
  if (role === "embedding") {
    if (draft.batchSize.trim()) {
      options.batch_size = Number(draft.batchSize);
    }
    if (draft.queryPrompt.trim()) {
      options.query_prompt = draft.queryPrompt.trim();
    }
  }
  const mutation: ProviderProfileMutation = {
    adapter: draft.adapter,
    display_name: normalizedOptional(draft.displayName),
    base_url: normalizedOptional(draft.baseUrl),
    model: normalizedOptional(draft.model),
    options,
  };
  const apiKey = draft.apiKey.trim();
  if (apiKey) {
    mutation.api_key = apiKey;
  }
  return mutation;
}

function mutationMatchesProfile(
  mutation: ProviderProfileMutation,
  profile: ProviderProfile,
) {
  const optionKeys = Object.keys(mutation.options) as Array<keyof typeof mutation.options>;
  const profileOptionKeys = Object.keys(profile.options);
  return (
    mutation.api_key === undefined &&
    mutation.adapter === profile.adapter &&
    mutation.display_name === profile.display_name &&
    mutation.base_url === profile.base_url &&
    mutation.model === profile.model &&
    optionKeys.length === profileOptionKeys.length &&
    optionKeys.every((key) => mutation.options[key] === profile.options[key])
  );
}

function validateBaseUrl(value: string) {
  if (!value.trim()) {
    return true;
  }
  try {
    const url = new URL(value.trim());
    return (
      ["http:", "https:"].includes(url.protocol) &&
      !url.username &&
      !url.password &&
      !url.search &&
      !url.hash
    );
  } catch {
    return false;
  }
}

function integerOptionError(value: string, minimum: number, maximum: number, label: string) {
  if (!value.trim()) {
    return null;
  }
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed >= minimum && parsed <= maximum
    ? null
    : `${label}需要是 ${minimum}–${maximum} 之间的整数。`;
}

function draftValidationMessage(
  role: ProviderRole,
  draft: ProviderDraft,
  profile: ProviderProfile,
  forTest: boolean,
) {
  if (!validateBaseUrl(draft.baseUrl)) {
    return "服务地址需要是有效的 HTTP(S) 地址，且不能包含账号、密码、查询参数或片段。";
  }
  const timeoutError = integerOptionError(draft.timeout, 1, 600, "超时时间");
  if (timeoutError) {
    return timeoutError;
  }
  if (role === "mineru" && draft.language.trim().length > 20) {
    return "解析语言不能超过 20 个字符。";
  }
  if (role === "embedding") {
    const batchSizeError = integerOptionError(draft.batchSize, 1, 256, "批量大小");
    if (batchSizeError) {
      return batchSizeError;
    }
    if (draft.queryPrompt.length > 4000) {
      return "查询提示词不能超过 4000 个字符。";
    }
  }
  if (forTest && !draft.baseUrl.trim()) {
    return "测试前请填写服务地址。";
  }
  if (forTest && !draft.model.trim()) {
    return "测试前请填写模型名称。";
  }
  if (forTest && !profile.secret_present && !draft.apiKey.trim()) {
    return "测试前请填写 API 密钥。";
  }
  return null;
}

function operationError(error: unknown, action: OperationKind) {
  if (error instanceof ApiError) {
    if (error.status === 403) {
      return "没有管理权限。请从安装器提供的带 token 链接重新打开本页。";
    }
    if (error.status === 422) {
      return "配置未通过校验，请检查适配器、服务地址和模型名称。";
    }
    if (error.status === 503) {
      return action === "test"
        ? "外部服务或测试通道暂不可用，请稍后重试。"
        : "配置存储暂不可用，请检查运行目录权限后重试。";
    }
  }
  if (action === "save") {
    return "配置保存失败，请检查服务状态后重试。";
  }
  if (action === "test") {
    return "连接测试失败，请检查网络、地址、模型和凭据。";
  }
  return "配置删除失败，请检查服务状态后重试。";
}

function statusLabel(profile: ProviderProfile) {
  switch (profile.status) {
    case "configured":
      return "已配置";
    case "incomplete":
      return "信息不完整";
    case "unavailable":
      return "暂不可用";
    default:
      return "未配置";
  }
}

function statusClass(profile: ProviderProfile) {
  if (profile.status === "configured") {
    return "is-configured";
  }
  if (profile.status === "incomplete") {
    return "is-incomplete";
  }
  if (profile.status === "unavailable") {
    return "is-unavailable";
  }
  return "is-empty";
}

function emptyProfile(role: ProviderRole, draft: ProviderDraft): ProviderProfile {
  const defaults = providerAdapterOption(role, draft.adapter);
  return {
    role,
    adapter: draft.adapter,
    source: "default",
    display_name: PROVIDER_CONFIG[role].eyebrow,
    base_url: defaults.defaultBaseUrl || null,
    model: defaults.defaultModel || null,
    options: {},
    configured: false,
    secret_present: false,
    status: "not_configured",
    last_test_status: null,
    last_tested_at: null,
  };
}

function formatTestTime(value: string | null) {
  if (!value) {
    return null;
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return null;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function sourceLabel(profile: ProviderProfile) {
  switch (profile.source) {
    case "stored":
      return "网页配置";
    case "environment":
      return "运行环境（只读）";
    case "environment_legacy":
      return "旧版环境变量（只读）";
    case "default":
      return "默认值";
    default:
      return "兼容配置";
  }
}

export function ProviderManager({ context = "settings" }: ProviderManagerProps) {
  const [response, setResponse] = useState<ProviderProfilesResponse | null>(null);
  const [drafts, setDrafts] = useState<Record<ProviderRole, ProviderDraft> | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loadRetry, setLoadRetry] = useState(0);
  const [operations, setOperations] = useState(operationState);
  const [confirmingDelete, setConfirmingDelete] = useState<ProviderRole | null>(null);
  const loadController = useRef<AbortController | null>(null);
  const operationControllers = useRef<Partial<Record<ProviderRole, AbortController>>>({});
  const operationEpochs = useRef<Record<ProviderRole, number>>({
    mineru: 0,
    embedding: 0,
    reranker: 0,
    llm: 0,
  });

  const loadProfiles = useCallback(() => {
    loadController.current?.abort();
    const controller = new AbortController();
    loadController.current = controller;
    setLoading(true);
    setLoadError(null);
    void api.get<unknown>("/api/provider-profiles", { signal: controller.signal })
      .then((value) => {
        if (controller.signal.aborted) {
          return;
        }
        const loaded = requireProviderProfilesResponse(value);
        setResponse(loaded);
        setDrafts(draftsFromProfiles(loaded.profiles));
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setLoadError("外部服务配置加载失败，请确认服务已启动并重试。");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      });
  }, []);

  useEffect(() => {
    loadProfiles();
    return () => {
      loadController.current?.abort();
      for (const controller of Object.values(operationControllers.current)) {
        controller?.abort();
      }
    };
  }, [loadProfiles, loadRetry]);

  function updateDraft(role: ProviderRole, update: Partial<ProviderDraft>) {
    setDrafts((current) => current
      ? { ...current, [role]: { ...current[role], ...update } }
      : current);
    setOperations((current) => ({
      ...current,
      [role]: current[role].kind
        ? current[role]
        : { kind: null, error: null, success: null, testOk: null },
    }));
    setConfirmingDelete((current) => current === role ? null : current);
  }

  function changeAdapter(role: ProviderRole, adapter: ProviderAdapter) {
    if (!drafts) {
      return;
    }
    const current = drafts[role];
    const previousDefaults = providerAdapterOption(role, current.adapter);
    const nextDefaults = providerAdapterOption(role, adapter);
    updateDraft(role, {
      adapter,
      baseUrl: !current.baseUrl.trim() || current.baseUrl === previousDefaults.defaultBaseUrl
        ? nextDefaults.defaultBaseUrl
        : current.baseUrl,
      model: !current.model.trim() || current.model === previousDefaults.defaultModel
        ? nextDefaults.defaultModel
        : current.model,
    });
  }

  function beginOperation(role: ProviderRole, kind: OperationKind) {
    operationControllers.current[role]?.abort();
    const controller = new AbortController();
    operationControllers.current[role] = controller;
    const epoch = ++operationEpochs.current[role];
    setOperations((current) => ({
      ...current,
      [role]: { kind, error: null, success: null, testOk: null },
    }));
    return { controller, epoch };
  }

  function operationIsCurrent(role: ProviderRole, epoch: number, controller: AbortController) {
    return !controller.signal.aborted && operationEpochs.current[role] === epoch;
  }

  function clearSecret(role: ProviderRole) {
    setDrafts((current) => current
      ? { ...current, [role]: { ...current[role], apiKey: "" } }
      : current);
  }

  async function saveProfile(role: ProviderRole) {
    if (!drafts || !response) {
      return;
    }
    const draft = drafts[role];
    const validationMessage = draftValidationMessage(role, draft, response.profiles[role], false);
    if (validationMessage) {
      setOperations((current) => ({
        ...current,
        [role]: {
          kind: null,
          error: validationMessage,
          success: null,
          testOk: null,
        },
      }));
      return;
    }
    const body = mutationFromDraft(role, draft);
    const { controller, epoch } = beginOperation(role, "save");
    clearSecret(role);
    try {
      const value = await api.put<unknown>(`/api/provider-profiles/${role}`, {
        signal: controller.signal,
        headers: providerAdminHeaders(),
        body,
      });
      if (!operationIsCurrent(role, epoch, controller)) {
        return;
      }
      const profile = requireProviderProfile(value, role);
      setResponse((current) => current
        ? { ...current, profiles: { ...current.profiles, [role]: profile } }
        : current);
      setDrafts((current) => current
        ? { ...current, [role]: draftFromProfile(profile) }
        : current);
      setOperations((current) => ({
        ...current,
        [role]: {
          kind: null,
          error: null,
          success: role === "embedding"
            ? "配置已保存。密钥已安全提交，之后不会在页面中回显。服务地址、适配器或模型变化后，既有文档可能需要重建向量索引；完成前可能显示待重建或暂时回退到关键词检索。"
            : "配置已保存。密钥已安全提交，之后不会在页面中回显。",
          testOk: null,
        },
      }));
    } catch (error) {
      if (operationIsCurrent(role, epoch, controller)) {
        setOperations((current) => ({
          ...current,
          [role]: {
            kind: null,
            error: operationError(error, "save"),
            success: null,
            testOk: null,
          },
        }));
      }
    }
  }

  async function testProfile(role: ProviderRole) {
    if (!drafts || !response) {
      return;
    }
    const draft = drafts[role];
    const validationMessage = draftValidationMessage(role, draft, response.profiles[role], true);
    if (validationMessage) {
      setOperations((current) => ({
        ...current,
        [role]: { kind: null, error: validationMessage, success: null, testOk: null },
      }));
      return;
    }
    const mutation = mutationFromDraft(role, draft);
    const body = mutationMatchesProfile(mutation, response.profiles[role]) ? {} : mutation;
    const { controller, epoch } = beginOperation(role, "test");
    clearSecret(role);
    try {
      const value = await api.post<unknown>(`/api/provider-profiles/${role}/test`, {
        signal: controller.signal,
        headers: providerAdminHeaders(),
        body,
      });
      if (!operationIsCurrent(role, epoch, controller)) {
        return;
      }
      const result = requireProviderTestResult(value);
      if (result.role !== role) {
        throw new Error("Provider role mismatch");
      }
      const requiresDocumentVerification = role === "mineru" && [
        "not_supported",
        "requires_document_e2e",
      ].includes(result.status);
      if (requiresDocumentVerification) {
        setOperations((current) => ({
          ...current,
          [role]: {
            kind: null,
            error: null,
            success: null,
            notice: "MinerU 需要真实文档做端到端验证。请先保存配置，再上传一份文档确认解析、轮询与结果下载完整可用。",
            testOk: null,
          },
        }));
        return;
      }
      setResponse((current) => current
        ? {
            ...current,
            profiles: {
              ...current.profiles,
              [role]: {
                ...current.profiles[role],
                last_test_status: result.ok ? "success" : "failed",
                last_tested_at: result.tested_at,
              },
            },
          }
        : current);
      setOperations((current) => ({
        ...current,
        [role]: {
          kind: null,
          error: result.ok ? null : "连接测试未通过，请检查地址、模型、凭据和服务商状态。",
          success: result.ok ? "连接测试成功。测试不会自动保存当前表单。" : null,
          testOk: result.ok,
        },
      }));
    } catch (error) {
      if (operationIsCurrent(role, epoch, controller)) {
        setOperations((current) => ({
          ...current,
          [role]: {
            kind: null,
            error: operationError(error, "test"),
            success: null,
            testOk: false,
          },
        }));
      }
    }
  }

  async function deleteProfile(role: ProviderRole) {
    if (!drafts || !response) {
      return;
    }
    const { controller, epoch } = beginOperation(role, "delete");
    setConfirmingDelete(null);
    clearSecret(role);
    let deleted = false;
    try {
      await api.delete<void>(`/api/provider-profiles/${role}`, {
        signal: controller.signal,
        headers: providerAdminHeaders(),
      });
      deleted = true;
      if (!operationIsCurrent(role, epoch, controller)) {
        return;
      }
      const refreshedValue = await api.get<unknown>("/api/provider-profiles", {
        signal: controller.signal,
      });
      if (!operationIsCurrent(role, epoch, controller)) {
        return;
      }
      const refreshed = requireProviderProfilesResponse(refreshedValue);
      setResponse(refreshed);
      setDrafts(draftsFromProfiles(refreshed.profiles));
      setOperations((current) => ({
        ...current,
        [role]: {
          kind: null,
          error: null,
          success: refreshed.profiles[role].source === "environment" || refreshed.profiles[role].source === "environment_legacy"
            ? "网页配置已删除，运行环境中的只读配置现已恢复生效。"
            : "网页保存的配置与密钥已删除。",
          testOk: null,
        },
      }));
    } catch (error) {
      if (operationIsCurrent(role, epoch, controller)) {
        if (deleted) {
          const profile = emptyProfile(role, drafts[role]);
          setResponse((current) => current
            ? { ...current, profiles: { ...current.profiles, [role]: profile } }
            : current);
          setDrafts((current) => current
            ? { ...current, [role]: draftFromProfile(profile) }
            : current);
        }
        setOperations((current) => ({
          ...current,
          [role]: {
            kind: null,
            error: deleted
              ? "网页配置已删除，但恢复来源的状态刷新失败。请重新加载页面确认当前配置。"
              : operationError(error, "delete"),
            success: null,
            testOk: null,
          },
        }));
      }
    }
  }

  function handleSave(event: FormEvent, role: ProviderRole) {
    event.preventDefault();
    void saveProfile(role);
  }

  return (
    <section
      aria-labelledby={`provider-manager-title-${context}`}
      className={`provider-manager provider-manager--${context}`}
    >
      <header className="provider-manager__header">
        <div>
          <span>{context === "setup" ? "可跳过的初始化步骤" : "运行时连接"}</span>
          <h2 id={`provider-manager-title-${context}`}>配置外部服务</h2>
          <p>
            四类配置彼此独立。即使暂不连接任何外部服务，也可以先用关键词检索已有内容。
          </p>
        </div>
        <KeyRound aria-hidden="true" size={24} />
      </header>

      <div className="provider-manager__cost-warning" role="alert">
        <AlertTriangle aria-hidden="true" size={19} />
        <div>
          <strong>测试会真实访问所选服务商</strong>
          <p>
            连接测试可能产生费用，并向外部服务传输最小测试数据。请先确认组织的数据与密钥使用规则。
          </p>
        </div>
      </div>

      <AsyncState
        error={loadError}
        loading={loading}
        loadingLabel="正在读取外部服务配置…"
        onRetry={() => setLoadRetry((value) => value + 1)}
      >
        {response && drafts ? (
          <>
            {!response.storage_available ? (
              <div className="provider-manager__storage-warning" role="alert">
                <AlertTriangle aria-hidden="true" size={18} />
                <div>
                  <strong>配置存储当前不可用</strong>
                  <p>可以填写并测试临时配置，但暂时无法保存或删除。</p>
                </div>
              </div>
            ) : null}

            <div className="provider-profile-grid">
              {PROVIDER_ROLES.map((role) => {
                const config = PROVIDER_CONFIG[role];
                const profile = response.profiles[role];
                const draft = drafts[role];
                const operation = operations[role];
                const adapter = providerAdapterOption(role, draft.adapter);
                const busy = operation.kind !== null;
                const canDelete = profile.source === "stored";
                const lastTestTime = formatTestTime(profile.last_tested_at);
                const Icon = ROLE_ICONS[role];
                return (
                  <form
                    aria-busy={busy}
                    aria-labelledby={`provider-profile-title-${role}`}
                    className="provider-profile-card"
                    key={role}
                    noValidate
                    onSubmit={(event) => handleSave(event, role)}
                  >
                    <header className="provider-profile-card__header">
                      <div className="provider-profile-card__icon" aria-hidden="true">
                        <Icon size={20} />
                      </div>
                      <div className="provider-profile-card__identity">
                        <span>{config.eyebrow}</span>
                        <h3 id={`provider-profile-title-${role}`}>{config.title}</h3>
                      </div>
                      <span className={`provider-profile-status ${statusClass(profile)}`}>
                        {statusLabel(profile)}
                      </span>
                    </header>

                    <div className="provider-profile-card__description">
                      <p>{config.description}</p>
                      {config.optionalNote ? <strong>{config.optionalNote}</strong> : null}
                      {profile.source === "environment" || profile.source === "environment_legacy" ? (
                        <span className="provider-profile-source-note">
                          当前来自只读运行环境；保存会创建一份网页配置并覆盖它。
                        </span>
                      ) : null}
                      {role === "mineru" ? (
                        <a
                          className="provider-official-link"
                          href="https://mineru.net"
                          rel="noopener noreferrer"
                          target="_blank"
                        >
                          打开 MinerU 官方站点获取 Token
                          <ExternalLink aria-hidden="true" size={12} />
                        </a>
                      ) : null}
                    </div>

                    <div className="provider-profile-fields">
                      <label className="provider-field">
                        <span>服务适配器</span>
                        <select
                          aria-label={`${config.title}服务适配器`}
                          disabled={busy}
                          onChange={(event) => changeAdapter(role, event.target.value as ProviderAdapter)}
                          value={draft.adapter}
                        >
                          {config.adapters.map((option) => (
                            <option key={option.value} value={option.value}>{option.label}</option>
                          ))}
                        </select>
                      </label>

                      <label className="provider-field">
                        <span>显示名称</span>
                        <input
                          aria-label={`${config.title}显示名称`}
                          disabled={busy}
                          onChange={(event) => updateDraft(role, { displayName: event.target.value })}
                          placeholder={config.eyebrow}
                          type="text"
                          value={draft.displayName}
                        />
                      </label>

                      <label className="provider-field provider-field--wide">
                        <span>服务地址</span>
                        <input
                          aria-label={`${config.title}服务地址`}
                          autoCapitalize="none"
                          disabled={busy}
                          onChange={(event) => updateDraft(role, { baseUrl: event.target.value })}
                          placeholder={adapter.baseUrlPlaceholder ?? adapter.defaultBaseUrl}
                          spellCheck={false}
                          type="url"
                          value={draft.baseUrl}
                        />
                      </label>

                      <label className="provider-field provider-field--wide">
                        <span>模型名称</span>
                        <input
                          aria-label={`${config.title}模型名称`}
                          autoCapitalize="none"
                          disabled={busy}
                          onChange={(event) => updateDraft(role, { model: event.target.value })}
                          placeholder={adapter.modelPlaceholder ?? adapter.defaultModel}
                          spellCheck={false}
                          type="text"
                          value={draft.model}
                        />
                      </label>

                      <label className="provider-field provider-field--wide">
                        <span>API 密钥</span>
                        <input
                          aria-label={`${config.title}API 密钥`}
                          autoComplete="new-password"
                          disabled={busy}
                          onChange={(event) => updateDraft(role, { apiKey: event.target.value })}
                          placeholder={profile.secret_present ? "已保存；留空则保留" : "输入服务商密钥"}
                          spellCheck={false}
                          type="password"
                          value={draft.apiKey}
                        />
                        <small>
                          {profile.secret_present
                            ? "已有密钥。留空保存或测试会继续使用原密钥；密钥不会回显。"
                            : "尚无已保存密钥。提交后输入框会清空，服务端也不会把密钥返回网页。"}
                        </small>
                      </label>

                      <details className="provider-advanced-options provider-field--wide">
                        <summary>高级选项</summary>
                        <div className="provider-advanced-options__grid">
                          <label className="provider-field">
                            <span>请求超时（秒）</span>
                            <input
                              aria-label={`${config.title}请求超时`}
                              disabled={busy}
                              inputMode="numeric"
                              max={600}
                              min={1}
                              onChange={(event) => updateDraft(role, { timeout: event.target.value })}
                              placeholder={role === "reranker" ? "120" : role === "llm" ? "30" : "60"}
                              type="number"
                              value={draft.timeout}
                            />
                          </label>

                          {role === "mineru" ? (
                            <label className="provider-field">
                              <span>解析语言</span>
                              <input
                                aria-label="文档解析语言"
                                disabled={busy}
                                maxLength={20}
                                onChange={(event) => updateDraft(role, { language: event.target.value })}
                                placeholder="例如 ch"
                                type="text"
                                value={draft.language}
                              />
                            </label>
                          ) : null}

                          {role === "embedding" ? (
                            <>
                              <label className="provider-field">
                                <span>批量大小</span>
                                <input
                                  aria-label="语义检索批量大小"
                                  disabled={busy}
                                  inputMode="numeric"
                                  max={256}
                                  min={1}
                                  onChange={(event) => updateDraft(role, { batchSize: event.target.value })}
                                  placeholder="16"
                                  type="number"
                                  value={draft.batchSize}
                                />
                              </label>
                              <label className="provider-field provider-field--wide">
                                <span>查询提示词（query_prompt）</span>
                                <textarea
                                  aria-label="语义检索查询提示词"
                                  disabled={busy}
                                  maxLength={4000}
                                  onChange={(event) => updateDraft(role, { queryPrompt: event.target.value })}
                                  placeholder="仅在模型要求时填写，例如 Qwen Embedding 的查询指令"
                                  rows={3}
                                  value={draft.queryPrompt}
                                />
                                <small>只作为非敏感模型参数保存；不要在这里填写密钥。</small>
                              </label>
                            </>
                          ) : null}
                        </div>
                      </details>
                    </div>

                    <div className="provider-profile-card__meta">
                      {profile.last_test_status ? (
                        <span className={
                          profile.last_test_status === "success"
                            ? "is-success"
                            : profile.last_test_status === "failed" ? "is-failed" : ""
                        }>
                          {profile.last_test_status === "success"
                            ? "上次测试成功"
                            : profile.last_test_status === "failed" ? "上次测试失败" : "需要文档验证"}
                          {lastTestTime ? ` · ${lastTestTime}` : ""}
                        </span>
                      ) : (
                        <span>尚未测试连接</span>
                      )}
                      <span>来源：{sourceLabel(profile)} · {profile.secret_present ? "密钥已保存" : "未保存密钥"}</span>
                    </div>

                    {operation.error ? (
                      <div className="provider-feedback provider-feedback--error" role="alert">
                        <AlertTriangle aria-hidden="true" size={16} />
                        <p>{operation.error}</p>
                      </div>
                    ) : null}
                    {operation.success ? (
                      <div className="provider-feedback provider-feedback--success" role="status">
                        <CheckCircle2 aria-hidden="true" size={16} />
                        <p>{operation.success}</p>
                      </div>
                    ) : null}
                    {operation.notice ? (
                      <div className="provider-feedback provider-feedback--notice" role="status">
                        <AlertTriangle aria-hidden="true" size={16} />
                        <p>{operation.notice}</p>
                      </div>
                    ) : null}

                    <footer className="provider-profile-card__actions">
                      <button
                        className="primary-button"
                        disabled={busy || !response.storage_available}
                        type="submit"
                      >
                        <Save aria-hidden="true" size={15} />
                        {operation.kind === "save"
                          ? "正在保存…"
                          : profile.source === "environment" || profile.source === "environment_legacy"
                            ? `保存网页覆盖${config.title}`
                            : `保存${config.title}配置`}
                      </button>
                      <button
                        className="ghost-button"
                        disabled={busy}
                        onClick={() => void testProfile(role)}
                        type="button"
                      >
                        <Wifi aria-hidden="true" size={15} />
                        {operation.kind === "test"
                          ? role === "mineru" ? "正在检查…" : "正在测试…"
                          : role === "mineru" ? "查看验证方式" : `测试${config.title}连接`}
                      </button>
                      {confirmingDelete === role ? (
                        <div className="provider-delete-confirmation" role="group" aria-label={`确认删除${config.title}配置`}>
                          <button
                            className="danger-button"
                            disabled={busy || !response.storage_available}
                            onClick={() => void deleteProfile(role)}
                            type="button"
                          >
                            确认删除
                          </button>
                          <button
                            className="ghost-button"
                            disabled={busy}
                            onClick={() => setConfirmingDelete(null)}
                            type="button"
                          >
                            取消
                          </button>
                        </div>
                      ) : (
                        <button
                          className="provider-delete-button"
                          disabled={busy || !canDelete || !response.storage_available}
                          onClick={() => setConfirmingDelete(role)}
                          type="button"
                        >
                          <Trash2 aria-hidden="true" size={14} />
                          删除网页配置
                        </button>
                      )}
                    </footer>
                  </form>
                );
              })}
            </div>
          </>
        ) : <span />}
      </AsyncState>
    </section>
  );
}
