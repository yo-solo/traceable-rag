import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RetrievalPage } from "./RetrievalPage";

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

const knowledgeBases = [
  {
    id: "kb-policy",
    name: "合规政策库",
    description: "公开政策与制度文件",
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
    document_count: 2,
    ready_document_count: 2,
    processing_document_count: 0,
    failed_document_count: 0,
  },
  {
    id: "kb-guide",
    name: "操作指南库",
    description: "运行手册与流程说明",
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
    document_count: 1,
    ready_document_count: 1,
    processing_document_count: 0,
    failed_document_count: 0,
  },
];

function retrievalResponse(overrides: Record<string, unknown> = {}) {
  return {
    query: "数据保留期限",
    knowledge_base_ids: ["kb-policy", "kb-guide"],
    document_ids: null,
    mode_requested: "auto",
    pipeline_used: "dense_reranker",
    degraded: true,
    warnings: [{ code: "reranker_unavailable", message: "Reranker 当前不可用，已回退 Dense 检索。" }],
    results: [
      {
        source_id: "src_0123456789abcdef0123456789abcdef",
        source_url: "/sources/src_0123456789abcdef0123456789abcdef",
        text: "个人数据应在实现处理目的所需的期限内保存，并定期复核保留必要性。",
        document_id: "policy-2026",
        document_name: "数据处理政策",
        human_page_number: 12,
        bbox: [72, 160, 510, 240],
        score: 0.8234,
      },
    ],
    ...overrides,
  };
}

function renderPage() {
  return {
    user: userEvent.setup(),
    ...render(
      <MemoryRouter>
        <RetrievalPage />
      </MemoryRouter>,
    ),
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("RetrievalPage", () => {
  it("submits the selected scope and displays actual pipeline, warning, and source link", async () => {
    let body: unknown;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = requestMethod(input, init);
      if (path === "/api/knowledge-bases" && method === "GET") {
        return Promise.resolve(jsonResponse({ knowledge_bases: knowledgeBases }));
      }
      if (path === "/api/retrieval" && method === "POST") {
        body = requestJson(init);
        return Promise.resolve(jsonResponse(retrievalResponse()));
      }
      throw new Error(`Unhandled request: ${method} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { user } = renderPage();

    await user.click(await screen.findByRole("checkbox", { name: /合规政策库/ }));
    await user.click(screen.getByRole("checkbox", { name: /操作指南库/ }));
    await user.type(screen.getByRole("textbox", { name: "检索问题" }), "数据保留期限");
    await user.click(screen.getByRole("button", { name: "运行检索" }));

    await waitFor(() => {
      expect(body).toEqual({
        query: "数据保留期限",
        knowledge_base_ids: ["kb-policy", "kb-guide"],
        mode: "auto",
        top_k: 8,
      });
    });
    expect(screen.getByText(/实际管线：Dense \+ Reranker/)).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Reranker 当前不可用，已回退 Dense 检索。");
    expect(screen.getByText("数据处理政策 · 第 12 页")).toBeInTheDocument();
    expect(screen.getByText("相关性分数 0.823")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /数据处理政策/ })).toHaveAttribute(
      "href",
      "/sources/src_0123456789abcdef0123456789abcdef",
    );
  });

  it("allows keyword mode and a changed top-k", async () => {
    let body: unknown;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = requestMethod(input, init);
      if (path === "/api/knowledge-bases" && method === "GET") {
        return Promise.resolve(jsonResponse({ knowledge_bases: knowledgeBases }));
      }
      if (path === "/api/retrieval" && method === "POST") {
        body = requestJson(init);
        return Promise.resolve(jsonResponse(retrievalResponse({ pipeline_used: "keyword", degraded: false, warnings: [] })));
      }
      throw new Error(`Unhandled request: ${method} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { user } = renderPage();

    await user.click(await screen.findByRole("checkbox", { name: /合规政策库/ }));
    await user.type(screen.getByRole("textbox", { name: "检索问题" }), "保留期限");
    await user.click(screen.getByText("检索参数"));
    await user.click(screen.getByRole("radio", { name: "关键词检索" }));
    const topK = screen.getByRole("spinbutton", { name: "候选来源数量" });
    await user.clear(topK);
    await user.type(topK, "12");
    await user.click(screen.getByRole("button", { name: "运行检索" }));

    await waitFor(() => {
      expect(body).toEqual({
        query: "保留期限",
        knowledge_base_ids: ["kb-policy"],
        mode: "keyword",
        top_k: 12,
      });
    });
    expect(screen.getByText(/实际管线：关键词检索/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("marks image and chart retrieval results without loading their binary assets", async () => {
    const visualResult = {
      ...retrievalResponse({ degraded: false, warnings: [] }),
      results: [{
        source_id: "src_image",
        source_url: "/sources/src_image",
        text: "图 1 合成系统流程图",
        document_id: "policy-2026",
        document_name: "项目报告",
        human_page_number: 3,
        bbox: [72, 160, 510, 400],
        score: 0.91,
        asset: {
          asset_id: "asset-chart",
          modality: "chart",
          asset_url: "/api/assets/asset-chart",
          preview_url: "/api/assets/asset-chart?variant=preview",
        },
      }],
    };
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = requestMethod(input, init);
      if (path === "/api/knowledge-bases" && method === "GET") {
        return Promise.resolve(jsonResponse({ knowledge_bases: knowledgeBases }));
      }
      if (path === "/api/retrieval" && method === "POST") {
        return Promise.resolve(jsonResponse(visualResult));
      }
      throw new Error(`Unhandled request: ${method} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { user } = renderPage();

    await user.click(await screen.findByRole("checkbox", { name: /合规政策库/ }));
    await user.type(screen.getByRole("textbox", { name: "检索问题" }), "合成系统流程图");
    await user.click(screen.getByRole("button", { name: "运行检索" }));

    expect(await screen.findByText("图表")).toBeVisible();
    expect(screen.getByRole("link", { name: /项目报告/ })).toHaveAttribute(
      "href",
      "/sources/src_image",
    );
    expect(document.querySelector(".citation-card img")).toBeNull();
  });

  it("shows a safe error when the retrieval response is malformed", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = requestMethod(input, init);
      if (path === "/api/knowledge-bases" && method === "GET") {
        return Promise.resolve(jsonResponse({ knowledge_bases: knowledgeBases }));
      }
      if (path === "/api/retrieval" && method === "POST") {
        return Promise.resolve(jsonResponse({ results: [] }));
      }
      throw new Error(`Unhandled request: ${method} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { user } = renderPage();

    await user.click(await screen.findByRole("checkbox", { name: /合规政策库/ }));
    await user.type(screen.getByRole("textbox", { name: "检索问题" }), "保留期限");
    await user.click(screen.getByRole("button", { name: "运行检索" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("检索失败，请检查服务状态后重试。");
  });
});
