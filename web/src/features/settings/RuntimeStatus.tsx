import {
  AlertTriangle,
  CheckCircle2,
  Database,
  FileText,
  Layers3,
  RefreshCw,
  Server,
} from "lucide-react";

import type {
  DiagnosticResult,
  DiagnosticTarget,
  ProviderStatus,
  RuntimeStatus as RuntimeStatusValue,
} from "../../api/types";

type RuntimeStatusProps = {
  runtime: RuntimeStatusValue;
  providers: {
    mineru: ProviderStatus;
    embedding: ProviderStatus;
  };
  activeDiagnostic: DiagnosticTarget | null;
  diagnosticError: { target: DiagnosticTarget; message: string } | null;
  diagnosticResult: DiagnosticResult | null;
  onDiagnostic: (target: DiagnosticTarget) => void;
};

function ProviderRow({
  activeDiagnostic,
  diagnosticError,
  diagnosticResult,
  label,
  onDiagnostic,
  provider,
  target,
}: {
  activeDiagnostic: DiagnosticTarget | null;
  diagnosticError: RuntimeStatusProps["diagnosticError"];
  diagnosticResult: DiagnosticResult | null;
  label: string;
  onDiagnostic: (target: DiagnosticTarget) => void;
  provider: ProviderStatus;
  target: "mineru" | "embedding";
}) {
  const result = diagnosticResult?.target === target ? diagnosticResult : null;
  const error = diagnosticError?.target === target ? diagnosticError : null;
  const testing = activeDiagnostic === target;
  const actionLabel = `${label}服务`;

  return (
    <article className="provider-row">
      <div className="provider-row__identity">
        <span
          aria-hidden="true"
          className={`provider-row__indicator${provider.configured ? " is-configured" : ""}`}
        />
        <div>
          <h3>{actionLabel}</h3>
          <p>{provider.configured ? "已配置" : "尚未配置"}</p>
          {target === "embedding" && provider.configured ? (
            <p>实际查询仍可能因内容尚未准备完成而自动使用精确匹配。</p>
          ) : null}
        </div>
      </div>
      <button
        className="secondary-button"
        disabled={testing}
        onClick={() => onDiagnostic(target)}
        type="button"
      >
        <RefreshCw aria-hidden="true" className={testing ? "is-spinning" : ""} size={14} />
        {testing ? "测试中…" : "测试连接"}
      </button>
      {error ? (
        <div className="provider-diagnostic provider-diagnostic--error" role="alert">
          <AlertTriangle aria-hidden="true" size={15} />
          <div>
            <strong>{actionLabel}连接测试失败，请检查服务设置后重试。</strong>
            <button onClick={() => onDiagnostic(target)} type="button">
              重新测试
            </button>
          </div>
        </div>
      ) : result ? (
        <div
          aria-live={result.ok ? "polite" : "assertive"}
          className={`provider-diagnostic${result.ok ? " is-success" : " is-warning"}`}
          role={result.ok ? "status" : "alert"}
        >
          {result.ok ? (
            <CheckCircle2 aria-hidden="true" size={15} />
          ) : (
            <AlertTriangle aria-hidden="true" size={15} />
          )}
          <div>
            <strong>{result.ok ? `${actionLabel}连接正常。` : `${actionLabel}暂不可用。`}</strong>
            {!result.ok ? <p>请检查服务设置，或稍后重新测试。</p> : null}
          </div>
        </div>
      ) : null}
    </article>
  );
}

export function RuntimeStatus({
  runtime,
  providers,
  activeDiagnostic,
  diagnosticError,
  diagnosticResult,
  onDiagnostic,
}: RuntimeStatusProps) {
  const metrics = [
    { label: "知识库", value: runtime.counts.knowledge_bases, icon: Database },
    { label: "文档", value: runtime.counts.documents, icon: FileText },
    { label: "可搜索内容", value: runtime.counts.chunks, icon: Layers3 },
    { label: "搜索记录", value: runtime.counts.index_records, icon: Server },
  ];

  return (
    <section className="settings-section runtime-status" aria-labelledby="runtime-status-title">
      <header className="settings-section__header">
        <div>
          <h2 id="runtime-status-title">运行状态</h2>
          <p>查看文档处理和查询功能是否可用；连接测试只在你点击时运行。</p>
        </div>
        <span className={`runtime-health${runtime.service.healthy ? "" : " is-unhealthy"}`}>
          {runtime.service.healthy ? (
            <CheckCircle2 aria-hidden="true" size={15} />
          ) : (
            <AlertTriangle aria-hidden="true" size={15} />
          )}
          {runtime.service.healthy ? "服务正常" : "服务异常"}
        </span>
      </header>

      {!runtime.service.healthy ? (
        <div className="runtime-status__warning" role="alert">
          <AlertTriangle aria-hidden="true" size={16} />
          <div>
            <strong>运行统计暂不可用。</strong>
            <p>请稍后重试；如果问题持续存在，请联系系统管理员。</p>
          </div>
        </div>
      ) : null}

      <div className="runtime-metrics" aria-label="运行统计">
        {metrics.map(({ label, value, icon: Icon }) => (
          <div className="runtime-metric" key={label}>
            <Icon aria-hidden="true" size={17} />
            <span>{label}</span>
            <strong>{value}</strong>
          </div>
        ))}
      </div>

      <div className="runtime-status__details">
        <section aria-labelledby="provider-status-title">
          <div className="settings-subheading">
            <h3 id="provider-status-title">处理服务状态</h3>
            <p>这里只显示是否可用，不显示任何密钥或认证信息。</p>
          </div>
          <div className="provider-list">
            <ProviderRow
              activeDiagnostic={activeDiagnostic}
              diagnosticError={diagnosticError}
              diagnosticResult={diagnosticResult}
              label="文档读取"
              onDiagnostic={onDiagnostic}
              provider={providers.mineru}
              target="mineru"
            />
            <ProviderRow
              activeDiagnostic={activeDiagnostic}
              diagnosticError={diagnosticError}
              diagnosticResult={diagnosticResult}
              label="语义检索"
              onDiagnostic={onDiagnostic}
              provider={providers.embedding}
              target="embedding"
            />
          </div>
        </section>

        <section aria-labelledby="failed-jobs-title">
          <div className="settings-subheading">
            <h3 id="failed-jobs-title">最近失败任务</h3>
            <p>显示近期未完成的文档任务和可采取的操作。</p>
          </div>
          {runtime.recent_failures.length === 0 ? (
            <div className="runtime-empty">
              <CheckCircle2 aria-hidden="true" size={17} />
              最近没有失败任务
            </div>
          ) : (
            <ul className="runtime-failures">
              {runtime.recent_failures.map((failure) => (
                <li key={failure.document_id}>
                  <AlertTriangle aria-hidden="true" size={15} />
                  <div>
                    <strong>{failure.display_name}</strong>
                    <p>{failure.summary}</p>
                    <time dateTime={failure.updated_at}>{failure.updated_at}</time>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </section>
  );
}
