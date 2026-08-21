import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { appRoutes } from "../../app/router";

const TEST_SECRET = "sk-test-never-render-this-value";
const USER_FACING_IMPLEMENTATION_TERMS =
  /\b(?:Trace|Citation|Block|Evidence|bbox|supporting|manual_query|Embedding|MinerU|Provider)\b/;

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function requestPath(input: RequestInfo | URL) {
  return new URL(input instanceof Request ? input.url : String(input), "http://localhost").pathname;
}

function requestMethod(input: RequestInfo | URL, init?: RequestInit) {
  return (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
}

function requestJson(init?: RequestInit) {
  return init?.body ? JSON.parse(String(init.body)) : undefined;
}

function deferredResponse() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((promiseResolve) => {
    resolve = promiseResolve;
  });
  return { promise, resolve };
}

const settingsPayload = {
  settings: {
    default_top_k: 5,
    default_retrieval_mode: "keyword",
    default_use_embedding: false,
    public_base_url: "http://127.0.0.1:8000",
    default_knowledge_base_id: null,
  },
  persistence: { status: "ok", message: null },
  runtime: {
    service: { healthy: true },
    counts: {
      knowledge_bases: 2,
      documents: 6,
      chunks: 48,
      index_records: 48,
    },
    recent_failures: [
      {
        document_id: "doc-failed",
        display_name: "ViDoRe 解析失败样例",
        summary: "文档处理失败，请检查服务日志后重试。",
        updated_at: "2026-07-22T08:00:00Z",
      },
    ],
  },
  providers: {
    mineru: { configured: true, source: "environment", api_key: TEST_SECRET },
    embedding: { configured: false, source: null },
  },
  skill: { template_available: true, installed: false },
  developer: {
    openapi_url: "/openapi.json",
    examples: [
      {
        label: "Agent Retrieval",
        method: "POST",
        path: "/api/retrieval",
        body: {
          query: "What happened to EU income inequality after its 2014-15 peak?",
          knowledge_base_ids: ["kb-east"],
          mode: "auto",
          top_k: 8,
        },
      },
      {
        label: "Source Context",
        method: "GET",
        path: "/api/sources/{source_id}/context?before=2&after=2",
        body: null,
      },
      {
        label: "Health",
        method: "GET",
        path: "/api/health",
        body: null,
      },
    ],
  },
  secret_debug_value: TEST_SECRET,
};

const knowledgeBasesPayload = {
  knowledge_bases: [
    {
      id: "default",
      name: "默认知识库",
      description: "",
      created_at: "2026-07-20T09:00:00Z",
      updated_at: "2026-07-20T09:00:00Z",
      document_count: 2,
      ready_document_count: 2,
      processing_document_count: 0,
      failed_document_count: 0,
    },
    {
      id: "kb-east",
      name: "ViDoRe 图表知识库",
      description: "复杂版式核验资料",
      created_at: "2026-07-20T09:00:00Z",
      updated_at: "2026-07-20T09:00:00Z",
      document_count: 4,
      ready_document_count: 3,
      processing_document_count: 0,
      failed_document_count: 1,
    },
  ],
};

function renderSettingsRoute() {
  const router = createMemoryRouter(appRoutes, { initialEntries: ["/settings"] });
  return {
    router,
    user: userEvent.setup(),
    ...render(<RouterProvider router={router} />),
  };
}

function settingsFetch(input: RequestInfo | URL, init?: RequestInit) {
  const path = requestPath(input);
  const method = requestMethod(input, init);
  if (path === "/api/settings" && method === "GET") {
    return Promise.resolve(jsonResponse(settingsPayload));
  }
  if (path === "/api/knowledge-bases" && method === "GET") {
    return Promise.resolve(jsonResponse(knowledgeBasesPayload));
  }
  throw new Error(`Unhandled request: ${method} ${path}`);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("SettingsPage", () => {
  it("opens user settings by default and separates runtime from developer access", async () => {
    vi.stubGlobal("fetch", vi.fn(settingsFetch));

    const { user } = renderSettingsRoute();

    expect(await screen.findByRole("heading", { name: "设置" })).toBeInTheDocument();
    const preferencesTab = screen.getByRole("tab", { name: "使用设置" });
    expect(screen.getByRole("tab", { name: "外部服务" })).toBeInTheDocument();
    const runtimeTab = screen.getByRole("tab", { name: "运行状态" });
    const developerTab = screen.getByRole("tab", { name: "开发者接入" });
    expect(preferencesTab).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("heading", { name: "默认使用方式" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "默认检索模式" })).toHaveTextContent(
      "精确匹配",
    );
    expect(screen.getByRole("combobox", { name: "默认检索模式" })).toHaveTextContent(
      "语义检索",
    );
    expect(document.body.textContent).not.toMatch(USER_FACING_IMPLEMENTATION_TERMS);

    await user.click(runtimeTab);
    expect(runtimeTab).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("heading", { name: "运行状态" })).toBeInTheDocument();
    expect(screen.getAllByText("48", { selector: "strong" })).toHaveLength(2);
    expect(screen.getByRole("heading", { name: "文档读取服务" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "语义检索服务" })).toBeInTheDocument();
    expect(screen.getByText("已配置")).toBeInTheDocument();
    expect(screen.getByText("尚未配置")).toBeInTheDocument();
    expect(screen.queryByText("环境变量")).not.toBeInTheDocument();
    expect(screen.queryByText("进程注入")).not.toBeInTheDocument();
    expect(screen.queryByText("服务端配置文件")).not.toBeInTheDocument();
    expect(screen.getByText("ViDoRe 解析失败样例")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(USER_FACING_IMPLEMENTATION_TERMS);

    await user.click(developerTab);
    expect(developerTab).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("heading", { name: "Codex Skill" })).toBeInTheDocument();
    expect(screen.getByText("Skill 模板可用")).toBeInTheDocument();
    expect(screen.getByText("尚未安装到用户目录")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "打开 API 参考" })).toHaveAttribute(
      "href",
      "/api-reference",
    );
    expect(screen.getByRole("link", { name: /JSON/ })).toHaveAttribute(
      "href",
      "/openapi.json",
    );
    expect(screen.getByText("完整 OpenAPI 工作台")).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(TEST_SECRET);
    expect(document.body).not.toHaveTextContent("Authorization");
    expect(screen.queryByLabelText(/API.?Key/i)).not.toBeInTheDocument();
  });

  it("explains that configured semantic search can still fall back per query", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      if (path === "/api/settings") {
        return Promise.resolve(
          jsonResponse({
            ...settingsPayload,
            providers: {
              ...settingsPayload.providers,
              embedding: { configured: true },
            },
          }),
        );
      }
      if (path === "/api/knowledge-bases") {
        return Promise.resolve(jsonResponse(knowledgeBasesPayload));
      }
      throw new Error(`Unhandled request: ${requestMethod(input, init)} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const { user } = renderSettingsRoute();

    await screen.findByRole("heading", { name: "设置" });
    await user.click(screen.getByRole("tab", { name: "运行状态" }));
    expect(await screen.findByRole("heading", { name: "语义检索服务" })).toBeInTheDocument();
    expect(screen.getByText(/实际查询仍可能因内容尚未准备完成而自动使用精确匹配/)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(USER_FACING_IMPLEMENTATION_TERMS);
  });

  it("renders an actionable safe fallback when runtime statistics are unavailable", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      if (path === "/api/settings") {
        return Promise.resolve(
          jsonResponse({
            ...settingsPayload,
            runtime: {
              service: { healthy: false },
              counts: {
                knowledge_bases: 0,
                documents: 0,
                chunks: 0,
                index_records: 0,
              },
              recent_failures: [],
            },
          }),
        );
      }
      if (path === "/api/knowledge-bases") {
        return Promise.resolve(jsonResponse(knowledgeBasesPayload));
      }
      throw new Error(`Unhandled request: ${requestMethod(input, init)} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const { user } = renderSettingsRoute();

    expect(await screen.findByRole("heading", { name: "设置" })).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "运行状态" }));
    expect(screen.getByRole("alert")).toHaveTextContent("运行统计暂不可用");
    expect(screen.getByRole("alert")).toHaveTextContent(
      "请稍后重试；如果问题持续存在，请联系系统管理员",
    );
    expect(document.body).not.toHaveTextContent(TEST_SECRET);
  });

  it("saves exactly the non-sensitive whitelist and reports success", async () => {
    let patchBody: unknown;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = requestMethod(input, init);
      if (path === "/api/settings" && method === "PATCH") {
        patchBody = requestJson(init);
        return Promise.resolve(
          jsonResponse({
            settings: patchBody,
            persistence: { status: "ok", message: null },
          }),
        );
      }
      return settingsFetch(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { user } = renderSettingsRoute();

    const topK = await screen.findByRole("spinbutton", { name: "默认参考内容数量" });
    await user.clear(topK);
    await user.type(topK, "12");
    await user.selectOptions(screen.getByRole("combobox", { name: "默认检索模式" }), "embedding");
    await user.selectOptions(
      screen.getByRole("combobox", { name: "默认知识库" }),
      "kb-east",
    );
    await user.click(screen.getByRole("tab", { name: "开发者接入" }));
    const baseUrl = screen.getByRole("textbox", { name: "服务地址" });
    await user.clear(baseUrl);
    await user.type(baseUrl, "https://rag.example.test");
    await user.click(screen.getByRole("button", { name: "保存地址" }));

    await waitFor(() =>
      expect(patchBody).toEqual({
        default_top_k: 12,
        default_retrieval_mode: "embedding",
        default_use_embedding: true,
        public_base_url: "https://rag.example.test",
        default_knowledge_base_id: "kb-east",
      }),
    );
    expect(JSON.stringify(patchBody)).not.toContain(TEST_SECRET);
  });

  it("keeps retrieval mode and embedding toggle synchronized while editing", async () => {
    vi.stubGlobal("fetch", vi.fn(settingsFetch));
    const { user } = renderSettingsRoute();

    const mode = await screen.findByRole("combobox", { name: "默认检索模式" });
    const toggle = screen.getByRole("checkbox", { name: "默认启用语义检索" });
    expect(mode).toHaveValue("keyword");
    expect(toggle).not.toBeChecked();

    await user.selectOptions(mode, "embedding");
    expect(toggle).toBeChecked();

    await user.click(toggle);
    expect(mode).toHaveValue("keyword");
    expect(toggle).not.toBeChecked();
  });

  it("disables the complete form while saving and restores editing after a failed retry", async () => {
    const pendingSave = deferredResponse();
    let patchBody: unknown;
    let saveAttempts = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = requestMethod(input, init);
      if (path === "/api/settings" && method === "PATCH") {
        saveAttempts += 1;
        patchBody = requestJson(init);
        if (saveAttempts === 1) {
          return pendingSave.promise;
        }
        return Promise.resolve(
          jsonResponse(
            { detail: { code: "settings_persistence_failed", message: "设置保存失败" } },
            500,
          ),
        );
      }
      return settingsFetch(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { user } = renderSettingsRoute();

    const topK = await screen.findByRole("spinbutton", { name: "默认参考内容数量" });
    await user.clear(topK);
    await user.type(topK, "12");
    const saveButton = screen.getByRole("button", { name: "保存设置" });
    await user.click(saveButton);
    const form = saveButton.closest("form");
    expect(form).not.toBeNull();
    expect(form).toHaveAttribute("aria-busy", "true");
    for (const control of form!.querySelectorAll("input, select, button")) {
      expect(control).toBeDisabled();
    }
    await user.type(topK, "9");
    expect(topK).toHaveValue(12);

    pendingSave.resolve(
      jsonResponse({
        settings: patchBody,
        persistence: { status: "ok", message: null },
      }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent("设置已保存");
    expect(topK).toHaveValue(12);
    expect(topK).toBeEnabled();
    expect(form).toHaveAttribute("aria-busy", "false");

    await user.clear(topK);
    await user.type(topK, "13");
    await user.click(screen.getByRole("button", { name: "保存设置" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("设置保存失败");
    expect(topK).toBeEnabled();
    expect(form).toHaveAttribute("aria-busy", "false");
  });

  it("clears a dangling default knowledge base before saving", async () => {
    let patchBody: unknown;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = requestMethod(input, init);
      if (path === "/api/settings" && method === "GET") {
        return Promise.resolve(
          jsonResponse({
            ...settingsPayload,
            settings: {
              ...settingsPayload.settings,
              default_knowledge_base_id: "deleted-kb",
            },
          }),
        );
      }
      if (path === "/api/knowledge-bases" && method === "GET") {
        return Promise.resolve(jsonResponse(knowledgeBasesPayload));
      }
      if (path === "/api/settings" && method === "PATCH") {
        patchBody = requestJson(init);
        return Promise.resolve(
          jsonResponse({
            settings: patchBody,
            persistence: { status: "ok", message: null },
          }),
        );
      }
      throw new Error(`Unhandled request: ${method} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { user } = renderSettingsRoute();

    expect(await screen.findByRole("combobox", { name: "默认知识库" })).toHaveValue("");
    await user.click(screen.getByRole("button", { name: "保存设置" }));
    await waitFor(() =>
      expect(patchBody).toMatchObject({ default_knowledge_base_id: null }),
    );
  });

  it("shows actionable diagnostic results, recovers from request errors and ignores stale responses", async () => {
    const staleMineru = deferredResponse();
    let embeddingAttempts = 0;
    let staleSignal: AbortSignal | null | undefined;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = requestMethod(input, init);
      if (path === "/api/settings/diagnostics" && method === "POST") {
        const body = requestJson(init) as { target: string };
        if (body.target === "mineru") {
          staleSignal = init?.signal;
          return staleMineru.promise;
        }
        embeddingAttempts += 1;
        if (embeddingAttempts === 1) {
          return Promise.resolve(
            jsonResponse(
              { detail: { code: "diagnostic_gateway_failed", message: "诊断网关不可用" } },
              503,
            ),
          );
        }
        return Promise.resolve(
          jsonResponse({
            diagnostic: {
              target: "embedding",
              ok: false,
              code: "provider_not_configured",
              message: "Embedding 尚未配置。",
              action: "在服务端配置 Embedding Provider 后重试。",
            },
          }),
        );
      }
      return settingsFetch(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { user } = renderSettingsRoute();

    await screen.findByRole("heading", { name: "设置" });
    await user.click(screen.getByRole("tab", { name: "运行状态" }));
    const documentService = screen.getByRole("heading", { name: "文档读取服务" }).closest("article");
    const semanticService = screen.getByRole("heading", { name: "语义检索服务" }).closest("article");
    expect(documentService).not.toBeNull();
    expect(semanticService).not.toBeNull();
    await user.click(within(documentService!).getByRole("button", { name: "测试连接" }));
    await user.click(within(semanticService!).getByRole("button", { name: "测试连接" }));
    expect(staleSignal?.aborted).toBe(true);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "语义检索服务连接测试失败，请检查服务设置后重试",
    );

    await user.click(within(semanticService!).getByRole("button", { name: "重新测试" }));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        "语义检索服务暂不可用",
      ),
    );
    staleMineru.resolve(
      jsonResponse({
        diagnostic: {
          target: "mineru",
          ok: false,
          code: "stale",
          message: TEST_SECRET,
          action: TEST_SECRET,
        },
      }),
    );
    await waitFor(() => expect(document.body).not.toHaveTextContent(TEST_SECRET));
    expect(screen.getByText("请检查服务设置，或稍后重新测试。")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(USER_FACING_IMPLEMENTATION_TERMS);
  });

  it("aborts in-flight loading on unmount and can recover from a malformed response", async () => {
    const pendingSettings = deferredResponse();
    let loadSignal: AbortSignal | null | undefined;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      if (path === "/api/settings") {
        loadSignal = init?.signal;
        return pendingSettings.promise;
      }
      if (path === "/api/knowledge-bases") {
        return Promise.resolve(jsonResponse(knowledgeBasesPayload));
      }
      throw new Error(`Unhandled request: ${requestMethod(input, init)} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const rendered = renderSettingsRoute();

    expect(await screen.findByRole("status")).toHaveTextContent("正在加载设置与运行状态");
    rendered.unmount();
    expect(loadSignal?.aborted).toBe(true);

    pendingSettings.resolve(jsonResponse({ settings: { default_top_k: TEST_SECRET } }));
  });
});
