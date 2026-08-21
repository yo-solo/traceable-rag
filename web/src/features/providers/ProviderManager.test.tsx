import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { appRoutes } from "../../app/router";

const ADMIN_TOKEN = "installer-admin-token-never-render";
const PROVIDER_SECRET = "sk-provider-secret-never-render";

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

function requestHeaders(init?: RequestInit) {
  return new Headers(init?.headers);
}

function requestJson(init?: RequestInit) {
  return init?.body ? JSON.parse(String(init.body)) : undefined;
}

const profileBase = {
  options: {},
  configured: false,
  secret_present: false,
  status: "not_configured",
  last_test_status: null,
  last_tested_at: null,
};

const profilesPayload = {
  storage_available: true,
  profiles: {
    mineru: {
      ...profileBase,
      role: "mineru",
      adapter: "mineru_official",
      source: "default",
      display_name: "MinerU",
      base_url: "https://mineru.net",
      model: "vlm",
    },
    embedding: {
      ...profileBase,
      role: "embedding",
      adapter: "siliconflow",
      source: "environment",
      display_name: "Embedding",
      base_url: "https://api.siliconflow.cn",
      model: "Qwen/Qwen3-Embedding-8B",
      options: {
        timeout: 60,
        batch_size: 16,
        query_prompt: "Represent this query for retrieval:",
      },
      configured: true,
      secret_present: true,
      status: "configured",
      api_key: PROVIDER_SECRET,
    },
    reranker: {
      ...profileBase,
      role: "reranker",
      adapter: "siliconflow_qwen",
      source: "stored",
      display_name: "Reranker",
      base_url: "https://api.siliconflow.cn",
      model: "Qwen/Qwen3-Reranker-8B",
      options: { timeout: 120 },
      configured: true,
      secret_present: true,
      status: "configured",
    },
    llm: {
      ...profileBase,
      role: "llm",
      adapter: "openai_compatible",
      source: "default",
      display_name: "LLM",
      base_url: null,
      model: null,
    },
  },
};

function renderSetup(initialEntry = `/setup?from=installer#token=${ADMIN_TOKEN}&source=installer`) {
  const router = createMemoryRouter(appRoutes, { initialEntries: [initialEntry] });
  return {
    router,
    user: userEvent.setup(),
    ...render(<RouterProvider router={router} />),
  };
}

afterEach(() => {
  window.sessionStorage.clear();
  vi.unstubAllGlobals();
});

describe("Provider setup and management", () => {
  it("opens as a non-blocking standalone setup page and consumes the installer token safely", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      expect(requestMethod(input, init)).toBe("GET");
      expect(requestPath(input)).toBe("/api/provider-profiles");
      expect(requestHeaders(init).has("X-Traceable-RAG-Admin-Token")).toBe(false);
      return Promise.resolve(jsonResponse(profilesPayload));
    });
    vi.stubGlobal("fetch", fetchMock);

    const { router } = renderSetup();

    expect(await screen.findByRole("heading", { name: "按需连接外部服务" })).toBeInTheDocument();
    await waitFor(() => expect(router.state.location.search).toBe("?from=installer"));
    await waitFor(() => expect(router.state.location.hash).toBe("#source=installer"));
    expect(screen.getByText(/先使用关键词检索/)).toBeInTheDocument();
    expect(screen.getByRole("alert", { name: "" })).toHaveTextContent("可能产生费用");
    expect(screen.getByText("可选；当前版本不使用此配置生成答案。")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "打开 MinerU 官方站点获取 Token" })).toHaveAttribute(
      "href",
      "https://mineru.net",
    );
    expect(screen.getByRole("link", { name: "打开 MinerU 官方站点获取 Token" })).toHaveAttribute(
      "rel",
      "noopener noreferrer",
    );
    expect(screen.getByText(/网页不会直接安装宿主 Codex Skill/)).toBeInTheDocument();
    expect(screen.getByText(/来源：运行环境（只读）/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存网页覆盖语义检索" })).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(ADMIN_TOKEN);
    expect(document.body).not.toHaveTextContent(PROVIDER_SECRET);
    for (const input of screen.getAllByLabelText(/API 密钥$/)) {
      expect(input).toHaveAttribute("type", "password");
      expect(input).toHaveValue("");
    }
  });

  it("saves a strict embedding profile with advanced options and never re-renders the secret", async () => {
    let savedBody: Record<string, unknown> | undefined;
    let savedHeaders: Headers | undefined;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = requestMethod(input, init);
      if (path === "/api/provider-profiles" && method === "GET") {
        return Promise.resolve(jsonResponse(profilesPayload));
      }
      if (path === "/api/provider-profiles/embedding" && method === "PUT") {
        savedBody = requestJson(init);
        savedHeaders = requestHeaders(init);
        return Promise.resolve(jsonResponse({
          ...profilesPayload.profiles.embedding,
          source: "stored",
          options: (savedBody as { options: unknown }).options,
          secret_present: true,
          api_key: PROVIDER_SECRET,
        }));
      }
      throw new Error(`Unhandled request: ${method} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { user } = renderSetup();

    const heading = await screen.findByRole("heading", { name: "语义检索" });
    const form = heading.closest("form");
    expect(form).not.toBeNull();
    await user.click(within(form!).getByText("高级选项"));
    const prompt = within(form!).getByRole("textbox", { name: "语义检索查询提示词" });
    await user.clear(prompt);
    await user.type(prompt, "Instruct: retrieve the best passage");
    const secret = within(form!).getByLabelText("语义检索API 密钥");
    await user.type(secret, PROVIDER_SECRET);
    await user.click(within(form!).getByRole("button", { name: "保存网页覆盖语义检索" }));

    await waitFor(() => expect(savedBody).toEqual({
      adapter: "siliconflow",
      display_name: "Embedding",
      base_url: "https://api.siliconflow.cn",
      model: "Qwen/Qwen3-Embedding-8B",
      options: {
        timeout: 60,
        batch_size: 16,
        query_prompt: "Instruct: retrieve the best passage",
      },
      api_key: PROVIDER_SECRET,
    }));
    expect(savedHeaders?.get("X-Traceable-RAG-Admin-Token")).toBe(ADMIN_TOKEN);
    expect(await screen.findByRole("status")).toHaveTextContent("密钥已安全提交");
    expect(screen.getByRole("status")).toHaveTextContent("可能需要重建向量索引");
    expect(screen.getByRole("status")).toHaveTextContent("暂时回退到关键词检索");
    expect(secret).toHaveValue("");
    expect(document.body).not.toHaveTextContent(PROVIDER_SECRET);
    expect(document.body).not.toHaveTextContent(ADMIN_TOKEN);
  });

  it("tests with the saved secret, deletes only stored profiles and refreshes the environment fallback", async () => {
    let testBody: Record<string, unknown> | undefined;
    let testHeaders: Headers | undefined;
    let deleteHeaders: Headers | undefined;
    let profileReads = 0;
    const restoredPayload = {
      ...profilesPayload,
      profiles: {
        ...profilesPayload.profiles,
        reranker: {
          ...profilesPayload.profiles.reranker,
          source: "environment",
          display_name: "Runtime Reranker",
        },
      },
    };
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = requestMethod(input, init);
      if (path === "/api/provider-profiles" && method === "GET") {
        profileReads += 1;
        expect(requestHeaders(init).has("X-Traceable-RAG-Admin-Token")).toBe(false);
        return Promise.resolve(jsonResponse(profileReads === 1 ? profilesPayload : restoredPayload));
      }
      if (path === "/api/provider-profiles/reranker/test" && method === "POST") {
        testBody = requestJson(init);
        testHeaders = requestHeaders(init);
        return Promise.resolve(jsonResponse({
          role: "reranker",
          ok: true,
          status: "success",
          message: PROVIDER_SECRET,
          tested_at: "2026-08-10T08:00:00Z",
        }));
      }
      if (path === "/api/provider-profiles/reranker" && method === "DELETE") {
        deleteHeaders = requestHeaders(init);
        return Promise.resolve(new Response(null, { status: 204 }));
      }
      throw new Error(`Unhandled request: ${method} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { user } = renderSetup();

    const heading = await screen.findByRole("heading", { name: "结果重排" });
    const form = heading.closest("form");
    expect(form).not.toBeNull();
    await user.click(within(form!).getByRole("button", { name: "测试结果重排连接" }));

    await waitFor(() => expect(testBody).toBeDefined());
    expect(testBody).not.toHaveProperty("api_key");
    expect(testHeaders?.get("X-Traceable-RAG-Admin-Token")).toBe(ADMIN_TOKEN);
    expect(await within(form!).findByRole("status")).toHaveTextContent("测试不会自动保存");
    expect(document.body).not.toHaveTextContent(PROVIDER_SECRET);

    await user.click(within(form!).getByRole("button", { name: "删除网页配置" }));
    await user.click(within(form!).getByRole("button", { name: "确认删除" }));

    await waitFor(() => expect(deleteHeaders?.get("X-Traceable-RAG-Admin-Token")).toBe(ADMIN_TOKEN));
    expect(await screen.findByText(/运行环境中的只读配置现已恢复生效/)).toBeInTheDocument();
    expect(within(form!).getByText(/来源：运行环境（只读）/)).toBeInTheDocument();
  });

  it("treats MinerU document verification guidance as informational instead of a failed probe", async () => {
    const mineruConfigured = {
      ...profilesPayload,
      profiles: {
        ...profilesPayload.profiles,
        mineru: {
          ...profilesPayload.profiles.mineru,
          source: "stored",
          configured: true,
          secret_present: true,
          status: "configured",
        },
      },
    };
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = requestMethod(input, init);
      if (path === "/api/provider-profiles" && method === "GET") {
        return Promise.resolve(jsonResponse(mineruConfigured));
      }
      if (path === "/api/provider-profiles/mineru/test" && method === "POST") {
        return Promise.resolve(jsonResponse({
          role: "mineru",
          ok: false,
          status: "not_supported",
          message: "requires a real document",
          tested_at: "2026-08-10T08:00:00Z",
        }));
      }
      throw new Error(`Unhandled request: ${method} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { user } = renderSetup();

    const heading = await screen.findByRole("heading", { name: "文档解析" });
    const form = heading.closest("form");
    expect(form).not.toBeNull();
    await user.click(within(form!).getByRole("button", { name: "查看验证方式" }));

    expect(await within(form!).findByRole("status")).toHaveTextContent("真实文档做端到端验证");
    expect(within(form!).queryByText(/连接测试未通过/)).not.toBeInTheDocument();
  });
});
