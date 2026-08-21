import { ApiReferenceReact, type ReferenceProps } from "@scalar/api-reference-react";
import "@scalar/api-reference-react/style.css";
import { ExternalLink, FileJson2 } from "lucide-react";
import { useMemo } from "react";

import { createApiReferenceFetch } from "./requestPolicy";

const API_GUIDE_URL =
  "https://github.com/yo-solo/traceable-rag/blob/main/docs/api.md";

const SCALAR_CUSTOM_CSS = `
  :root {
    --scalar-font: var(--font-ui);
    --scalar-font-code: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    --scalar-color-accent: var(--color-accent);
    --scalar-background-1: #ffffff;
    --scalar-background-2: #f8f8fc;
    --scalar-background-3: #f1f0fb;
    --scalar-color-1: var(--color-ink);
    --scalar-color-2: var(--color-muted);
    --scalar-color-3: #8b8ca0;
    --scalar-border-color: var(--color-border);
    --scalar-radius: 12px;
  }
  .scalar-app {
    --scalar-custom-header-height: 94px;
    min-height: 760px;
  }
  .scalar-app .references-layout {
    min-height: 760px;
  }
  .references-developer-tools {
    display: none !important;
  }
  .scalar-api-reference .z-overlay:has(.scalar-container) {
    display: none !important;
  }
  .scalar-api-reference .code-snippet > .scalar-code-block > .custom-scroll {
    box-sizing: border-box;
    padding-right: 7rem !important;
  }
  .scalar-api-reference .response-card .scalar-card-checkbox {
    display: none !important;
  }
`;

export function ApiReferencePage() {
  const origin = window.location.origin;
  const customFetch = useMemo(
    () => createApiReferenceFetch(window.fetch.bind(window), origin),
    [origin],
  );
  const configuration = useMemo<ReferenceProps["configuration"]>(
    () => ({
      url: "/openapi.json",
      servers: [{ url: origin, description: "当前服务" }],
      layout: "modern",
      theme: "none",
      localization: { locale: "zh-CN" },
      withDefaultFonts: false,
      agent: { disabled: true },
      mcp: { disabled: true },
      telemetry: false,
      persistAuth: false,
      showDeveloperTools: "never",
      hideClientButton: true,
      hideDarkModeToggle: true,
      forceDarkModeState: "light",
      hideTestRequestButton: true,
      hideModels: true,
      showOperationId: true,
      documentDownloadType: "json",
      defaultHttpClient: { targetKey: "shell", clientKey: "curl" },
      orderSchemaPropertiesBy: "preserve",
      customCss: SCALAR_CUSTOM_CSS,
      customFetch,
      metaData: {
        title: "API 参考 · Traceable RAG",
        description: "Traceable RAG 完整 OpenAPI 参考与多语言调用示例。",
      },
    }),
    [customFetch, origin],
  );

  return (
    <section className="api-reference-page" aria-labelledby="api-reference-page-title">
      <header className="api-reference-page__header">
        <div className="api-reference-page__intro">
          <span className="api-reference-page__eyebrow">Developer API</span>
          <h1 id="api-reference-page-title">API 参考</h1>
          <p>
            浏览全部接口、请求与响应示例，以及多语言调用代码。页面不发送任何在线调试请求。
          </p>
        </div>
        <div className="api-reference-page__actions">
          <a className="ghost-button" href="/openapi.json" target="_blank" rel="noreferrer">
            <FileJson2 aria-hidden="true" size={16} />
            OpenAPI JSON
            <ExternalLink aria-hidden="true" size={13} />
          </a>
          <a className="ghost-button" href={API_GUIDE_URL} target="_blank" rel="noreferrer">
            手写接入指南
            <ExternalLink aria-hidden="true" size={13} />
          </a>
        </div>
      </header>

      <div className="api-reference-page__surface">
        <ApiReferenceReact configuration={configuration} />
      </div>
    </section>
  );
}
