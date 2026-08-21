import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { appRoutes } from "../../app/router";

type KnowledgeBaseFixture = {
  id: string;
  name: string;
  description: string;
  created_at: string;
  updated_at: string;
  document_count: number;
  ready_document_count: number;
  processing_document_count: number;
  failed_document_count: number;
};

type DocumentFixture = {
  id: string;
  knowledge_base_id: string;
  original_filename: string;
  display_name: string;
  source_type: string;
  page_count: number;
  processing_status: string;
  processing_error: string | null;
  parser_provider: string;
  parser_version: string | null;
  created_at: string;
  updated_at: string;
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
  latest_job: ProcessingJobFixture | null;
};

type ProcessingJobFixture = {
  id: string;
  knowledge_base_id: string;
  document_id: string;
  operation: "process" | "parse" | "index" | "normalize_upgrade";
  status: "queued" | "running" | "succeeded" | "failed";
  stage:
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
  attempt: number;
  queue_position: number | null;
  error: {
    code: string;
    stage: ProcessingJobFixture["stage"];
    message: string;
    retryable: boolean;
    diagnostic_id: string;
  } | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  updated_at: string;
};

const processingJobFixture = (
  documentId: string,
  overrides: Partial<ProcessingJobFixture> = {},
): ProcessingJobFixture => ({
  id: `job-${documentId}`,
  knowledge_base_id: "kb-1",
  document_id: documentId,
  operation: "process",
  status: "queued",
  stage: "queued",
  attempt: 1,
  queue_position: 1,
  error: null,
  created_at: "2026-07-21T09:30:00Z",
  started_at: null,
  finished_at: null,
  updated_at: "2026-07-21T09:30:00Z",
  ...overrides,
});

const knowledgeBase = (overrides: Partial<KnowledgeBaseFixture> = {}): KnowledgeBaseFixture => ({
  id: "kb-1",
  name: "ViDoRe 示例知识库",
  description: "用于图表与复杂版式核验",
  created_at: "2026-07-20T09:00:00Z",
  updated_at: "2026-07-21T09:30:00Z",
  document_count: 0,
  ready_document_count: 0,
  processing_document_count: 0,
  failed_document_count: 0,
  ...overrides,
});

type DocumentFixtureOverrides = Omit<Partial<DocumentFixture>, "stats"> & {
  stats?: Partial<DocumentFixture["stats"]>;
};

const documentFixture = (overrides: DocumentFixtureOverrides = {}): DocumentFixture => {
  const stats = {
    blocks: 48,
    assets: 3,
    chunks: 16,
    index_records: 16,
    embedding_records:
      overrides.stats?.embedding_records ??
      (overrides.stats?.index_records === undefined ? 16 : 0),
    ...overrides.stats,
  };
  const embeddingRecordCount = overrides.embedding_record_count ?? stats.embedding_records;
  const embeddingReady =
    overrides.embedding_ready ??
    (stats.index_records > 0 && embeddingRecordCount === stats.index_records);
  const embeddingStatus =
    overrides.embedding_status ??
    (embeddingReady ? "ready" : stats.index_records > 0 ? "fallback_keyword" : "not_indexed");
  return {
    id: "doc-1",
    knowledge_base_id: "kb-1",
    original_filename: "eu-joint-employment-report-2025-p97.pdf",
    display_name: "欧盟收入不平等趋势",
    source_type: "pdf",
    page_count: 12,
    processing_status: "ready",
    processing_error: null,
    parser_provider: "mineru",
    parser_version: "vlm",
    created_at: "2026-07-20T09:00:00Z",
    updated_at: "2026-07-21T09:30:00Z",
    embedding_record_count: embeddingRecordCount,
    embedding_ready: embeddingReady,
    embedding_status: embeddingStatus,
    embedding_warning:
      overrides.embedding_warning ??
      (embeddingStatus === "fallback_keyword" ? "已回退关键词检索。" : null),
    file_url: "/api/knowledge-bases/kb-1/documents/doc-1/file",
    web_url: "/knowledge-bases/kb-1/documents/doc-1",
    latest_job: null,
    ...overrides,
    stats,
  };
};

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function requestPath(input: RequestInfo | URL) {
  if (input instanceof Request) {
    return new URL(input.url, "http://localhost").pathname;
  }
  return new URL(String(input), "http://localhost").pathname;
}

function requestMethod(input: RequestInfo | URL, init?: RequestInit) {
  return (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
}

function requestJson(init?: RequestInit) {
  return init?.body ? JSON.parse(String(init.body)) : undefined;
}

function deferredResponse() {
  let resolve!: (response: Response) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<Response>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  return { promise, reject, resolve };
}

function renderRoute(initialEntry: string) {
  const router = createMemoryRouter(appRoutes, { initialEntries: [initialEntry] });
  return {
    router,
    user: userEvent.setup(),
    ...render(<RouterProvider router={router} />),
  };
}

const forbiddenUserTerms = /\b(?:Trace|Citation|Block|Evidence|bbox|supporting|manual_query|Embedding|MinerU|Provider)\b/i;

function expectNoForbiddenUserTerms(root: HTMLElement = document.body) {
  const accessibleCopy = Array.from(
    root.querySelectorAll<HTMLElement>("[aria-label], [aria-description], [title], [placeholder]"),
  ).flatMap((element) => [
    element.getAttribute("aria-label"),
    element.getAttribute("aria-description"),
    element.getAttribute("title"),
    element.getAttribute("placeholder"),
  ]).filter((value): value is string => Boolean(value));
  const userFacingCopy = [root.textContent ?? "", ...accessibleCopy]
    .join("\n")
    .replaceAll("Traceable RAG", "");

  expect(userFacingCopy).not.toMatch(forbiddenUserTerms);
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("knowledge base pages", () => {
  it("shows an actionable empty state and creates the first knowledge base", async () => {
    const bases: KnowledgeBaseFixture[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = requestMethod(input, init);

      if (path === "/api/knowledge-bases" && method === "GET") {
        return jsonResponse({ knowledge_bases: bases });
      }
      if (path === "/api/knowledge-bases" && method === "POST") {
        const body = requestJson(init);
        const created = knowledgeBase({
          id: "kb-created",
          name: body.name,
          description: body.description,
        });
        bases.push(created);
        return jsonResponse({ knowledge_base: created }, 201);
      }
      throw new Error(`Unhandled request: ${method} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { user } = renderRoute("/knowledge-bases");

    expect(await screen.findByRole("heading", { name: "还没有知识库" })).toBeInTheDocument();
    expect(screen.getByText("下一步：创建一个知识库，再上传需要核验的原始文档。")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "新建知识库" }));
    const dialog = screen.getByRole("dialog", { name: "新建知识库" });
    await user.type(within(dialog).getByRole("textbox", { name: "名称" }), "复杂版式资料");
    await user.type(
      within(dialog).getByRole("textbox", { name: "描述（可选）" }),
      "用于表格与图表核验",
    );
    await user.click(within(dialog).getByRole("button", { name: "创建知识库" }));

    expect(await screen.findByRole("heading", { name: "复杂版式资料" })).toBeInTheDocument();
    const createdCard = screen.getByRole("article", { name: "复杂版式资料" });
    expect(within(createdCard).getAllByText("0 个文档")).toHaveLength(2);
    expectNoForbiddenUserTerms();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/knowledge-bases",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          name: "复杂版式资料",
          description: "用于表格与图表核验",
        }),
      }),
    );
  });

  it("renames and deletes an empty knowledge base with a simple confirmation", async () => {
    let base = knowledgeBase();
    let deleted = false;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = requestMethod(input, init);
      if (path === "/api/knowledge-bases" && method === "GET") {
        return jsonResponse({ knowledge_bases: deleted ? [] : [base] });
      }
      if (path === "/api/knowledge-bases/kb-1" && method === "PATCH") {
        base = { ...base, ...requestJson(init) };
        return jsonResponse({ knowledge_base: base });
      }
      if (path === "/api/knowledge-bases/kb-1" && method === "DELETE") {
        expect(init?.body).toBeUndefined();
        deleted = true;
        return jsonResponse({ deleted: true, knowledge_base_id: "kb-1" });
      }
      throw new Error(`Unhandled request: ${method} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { user } = renderRoute("/knowledge-bases");

    const card = await screen.findByRole("article", { name: "ViDoRe 示例知识库" });
    await user.click(within(card).getByRole("button", { name: "管理ViDoRe 示例知识库" }));
    await user.click(screen.getByRole("menuitem", { name: "重命名" }));
    const renameDialog = screen.getByRole("dialog", { name: "重命名知识库" });
    const nameInput = within(renameDialog).getByRole("textbox", { name: "名称" });
    await user.clear(nameInput);
    await user.type(nameInput, "财务图表知识库");
    await user.click(within(renameDialog).getByRole("button", { name: "保存更改" }));

    expect(await screen.findByRole("heading", { name: "财务图表知识库" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "管理财务图表知识库" }));
    await user.click(screen.getByRole("menuitem", { name: "删除" }));
    const deleteDialog = screen.getByRole("dialog", { name: "删除知识库" });
    expect(within(deleteDialog).getByText("这个知识库没有文档，可以直接删除。")).toBeInTheDocument();
    await user.click(within(deleteDialog).getByRole("button", { name: "确认删除" }));

    expect(await screen.findByRole("heading", { name: "还没有知识库" })).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "知识库", level: 1 })).toHaveFocus();
    });
  });

  it("requires the exact knowledge base name before deleting a non-empty base", async () => {
    const base = knowledgeBase({
      document_count: 1,
      ready_document_count: 1,
    });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = requestMethod(input, init);
      if (path === "/api/knowledge-bases") {
        return jsonResponse({ knowledge_bases: [base] });
      }
      if (path === "/api/knowledge-bases/kb-1" && method === "DELETE") {
        expect(requestJson(init)).toEqual({ confirm_name: "ViDoRe 示例知识库" });
        return jsonResponse({ deleted: true, knowledge_base_id: "kb-1" });
      }
      throw new Error(`Unhandled request: ${method} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { user } = renderRoute("/knowledge-bases");

    const card = await screen.findByRole("article", { name: "ViDoRe 示例知识库" });
    expect(within(card).getAllByText("1 个文档")).toHaveLength(2);
    expect(within(card).getByText("正常")).toBeInTheDocument();
    await user.click(within(card).getByRole("button", { name: "管理ViDoRe 示例知识库" }));
    await user.click(screen.getByRole("menuitem", { name: "删除" }));

    const dialog = screen.getByRole("dialog", { name: "删除知识库" });
    expect(within(dialog).getByText("将同时删除 1 个文档及其相关搜索内容。")).toBeInTheDocument();
    const confirmButton = within(dialog).getByRole("button", { name: "永久删除" });
    expect(confirmButton).toBeDisabled();
    await user.type(within(dialog).getByRole("textbox", { name: "输入知识库名称以确认" }), "错误名称");
    expect(confirmButton).toBeDisabled();
    await user.clear(within(dialog).getByRole("textbox", { name: "输入知识库名称以确认" }));
    await user.type(
      within(dialog).getByRole("textbox", { name: "输入知识库名称以确认" }),
      "ViDoRe 示例知识库",
    );
    expect(confirmButton).toBeEnabled();
    await user.click(confirmButton);

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/knowledge-bases/kb-1",
        expect.objectContaining({
          method: "DELETE",
          body: JSON.stringify({ confirm_name: "ViDoRe 示例知识库" }),
        }),
      );
    });
  });

  it("keeps the knowledge base detail route refreshable and manages documents through the scoped API", async () => {
    const base = knowledgeBase();
    let documents: DocumentFixture[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = requestMethod(input, init);
      if (path === "/api/knowledge-bases/kb-1" && method === "GET") {
        return jsonResponse({ knowledge_base: base });
      }
      if (path === "/api/knowledge-bases/kb-1/documents" && method === "GET") {
        return jsonResponse({ documents });
      }
      if (path === "/api/knowledge-bases/kb-1/documents/upload" && method === "POST") {
        expect(init?.body).toBeInstanceOf(FormData);
        const form = init?.body as FormData;
        expect((form.get("file") as File).name).toBe("inheritance-reference.pdf");
        expect(form.has("result_dir")).toBe(false);
        const uploaded = documentFixture({
          id: "doc-uploaded",
          original_filename: "inheritance-reference.pdf",
          display_name: "继承关系参考表",
          source_type: "pdf",
          page_count: 0,
          processing_status: "queued",
          stats: { blocks: 0, assets: 0, chunks: 0, index_records: 0 },
          latest_job: processingJobFixture("doc-uploaded"),
        });
        documents = [uploaded];
        return jsonResponse({ document: uploaded, job: uploaded.latest_job }, 201);
      }
      if (path === "/api/knowledge-bases/kb-1/documents/doc-uploaded" && method === "PATCH") {
        documents = [{ ...documents[0], display_name: requestJson(init).display_name }];
        return jsonResponse({ document: documents[0] });
      }
      if (path === "/api/knowledge-bases/kb-1/documents/doc-uploaded" && method === "DELETE") {
        documents = [];
        return jsonResponse({ deleted: true, document_id: "doc-uploaded" });
      }
      throw new Error(`Unhandled request: ${method} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { user } = renderRoute("/knowledge-bases/kb-1");

    expect(await screen.findByRole("heading", { name: "ViDoRe 示例知识库" })).toBeInTheDocument();
    const breadcrumb = screen.getByRole("navigation", { name: "面包屑" });
    expect(within(breadcrumb).getByRole("link", { name: "知识库列表" })).toHaveAttribute(
      "href",
      "/knowledge-bases",
    );
    expect(within(breadcrumb).getByText("ViDoRe 示例知识库")).toBeInTheDocument();
    expect(
      screen.queryByRole("textbox", { name: /MinerU.*目录/i }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "还没有文档" })).toBeInTheDocument();
    const uploadInput = screen.getByLabelText("选择要上传的原始文档");
    expect(uploadInput).toHaveAttribute(
      "accept",
      ".pdf,.doc,.docx,.ppt,.pptx,.jpg,.jpeg,.png",
    );

    await user.upload(
      uploadInput,
      new File(["pdf"], "inheritance-reference.pdf", {
        type: "application/pdf",
      }),
    );
    const row = await screen.findByRole("row", { name: /继承关系参考表/ });
    expect(within(row).getByText("等待处理")).toBeInTheDocument();
    expect(within(row).getByText("等待处理 · 队列第 1 位")).toBeInTheDocument();
    expectNoForbiddenUserTerms();

    await user.click(within(row).getByRole("button", { name: "重命名继承关系参考表" }));
    const renameDialog = screen.getByRole("dialog", { name: "重命名文档" });
    const displayName = within(renameDialog).getByRole("textbox", { name: "文档名称" });
    await user.clear(displayName);
    await user.type(displayName, "多页继承关系参考表");
    await user.click(within(renameDialog).getByRole("button", { name: "保存更改" }));
    expect(await screen.findByText("多页继承关系参考表")).toBeInTheDocument();

    const renamedRow = screen.getByRole("row", { name: /多页继承关系参考表/ });
    await user.click(
      within(renamedRow).getByRole("button", { name: "删除多页继承关系参考表" }),
    );
    const deleteDialog = screen.getByRole("dialog", { name: "删除文档" });
    expect(within(deleteDialog).getByText(/历史问答记录会保留当时的引用内容/)).toBeInTheDocument();
    await user.click(within(deleteDialog).getByRole("button", { name: "确认删除" }));
    expect(await screen.findByRole("heading", { name: "还没有文档" })).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "ViDoRe 示例知识库", level: 1 })).toHaveFocus();
    });
  });

  it("accepts a background content upgrade job without exposing its internal operation", async () => {
    const base = knowledgeBase();
    const document = documentFixture({
      latest_job: processingJobFixture("doc-1", {
        operation: "normalize_upgrade",
        status: "failed",
        stage: "normalizing",
        queue_position: null,
        error: {
          code: "normalization_source_unavailable",
          stage: "normalizing",
          message: "internal upgrade source unavailable",
          retryable: false,
          diagnostic_id: "diag-upgrade",
        },
      }),
    });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = requestMethod(input, init);
      if (path === "/api/knowledge-bases/kb-1" && method === "GET") {
        return jsonResponse({ knowledge_base: base });
      }
      if (path === "/api/knowledge-bases/kb-1/documents" && method === "GET") {
        return jsonResponse({ documents: [document] });
      }
      throw new Error(`Unhandled request: ${method} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    renderRoute("/knowledge-bases/kb-1");

    expect(await screen.findByRole("link", { name: document.display_name })).toBeInTheDocument();
    expect(screen.getByText("可使用")).toBeInTheDocument();
    expectNoForbiddenUserTerms();
  });

  it("uploads multiple files independently and keeps later files moving after one failure", async () => {
    const base = knowledgeBase();
    let documents: DocumentFixture[] = [];
    const uploadedNames: string[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = requestMethod(input, init);
      if (path === "/api/knowledge-bases/kb-1" && method === "GET") {
        return jsonResponse({ knowledge_base: base });
      }
      if (path === "/api/knowledge-bases/kb-1/documents" && method === "GET") {
        return jsonResponse({ documents });
      }
      if (path === "/api/knowledge-bases/kb-1/documents/upload" && method === "POST") {
        const filename = ((init?.body as FormData).get("file") as File).name;
        uploadedNames.push(filename);
        if (filename === "重复名称.docx") {
          return jsonResponse(
            { detail: { code: "document_name_conflict", message: "文档名称已存在。" } },
            409,
          );
        }
        const job = processingJobFixture("doc-second", { queue_position: 2 });
        const document = documentFixture({
          id: "doc-second",
          original_filename: filename,
          display_name: "继续处理",
          processing_status: "queued",
          page_count: 0,
          stats: { blocks: 0, assets: 0, chunks: 0, index_records: 0 },
          latest_job: job,
        });
        documents = [document];
        return jsonResponse({ document, job }, 201);
      }
      throw new Error(`Unhandled request: ${method} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { user } = renderRoute("/knowledge-bases/kb-1");

    expect(await screen.findByRole("heading", { name: "还没有文档" })).toBeInTheDocument();
    await user.upload(screen.getByLabelText("选择要上传的原始文档"), [
      new File(["first"], "重复名称.docx"),
      new File(["second"], "继续处理.pdf"),
    ]);

    const queuedRow = await screen.findByRole("row", { name: /继续处理/ });
    expect(within(queuedRow).getByText("等待处理")).toBeInTheDocument();
    expect(within(queuedRow).getByText("等待处理 · 队列第 2 位")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("上传未完成。请确认文件格式、大小和名称后重新选择。");
    expect(uploadedNames).toEqual(["重复名称.docx", "继续处理.pdf"]);
  });

  it("explains missing document processing configuration before upload", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = requestPath(input);
        const method = requestMethod(input, init);
        if (path === "/api/knowledge-bases/kb-1") {
          return jsonResponse({ knowledge_base: knowledgeBase() });
        }
        if (path === "/api/knowledge-bases/kb-1/documents" && method === "GET") {
          return jsonResponse({ documents: [] });
        }
        if (path.endsWith("/documents/upload") && method === "POST") {
          return jsonResponse(
            {
              detail: {
                code: "document_processing_not_configured",
                message: "文档解析服务尚未配置。请在运行状态中确认服务配置后再上传。",
              },
            },
            503,
          );
        }
        throw new Error(`Unhandled request: ${method} ${path}`);
      }),
    );
    const { user } = renderRoute("/knowledge-bases/kb-1");

    await user.upload(
      await screen.findByLabelText("选择要上传的原始文档"),
      new File(["source"], "待处理.docx"),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "文档解析服务尚未配置。请先在运行状态中确认服务配置。",
    );
  });

  it("offers managed parse and index retries and explains the next step after a failure", async () => {
    const base = knowledgeBase();
    let documents = [
      documentFixture({
        id: "doc-failed",
        display_name: "失败文档",
        processing_status: "failed",
        processing_error: "Document processing failed.",
        stats: { blocks: 0, assets: 0, chunks: 0, index_records: 0 },
        latest_job: processingJobFixture("doc-failed", {
          status: "failed",
          stage: "mineru_submit",
          queue_position: null,
          error: {
            code: "mineru_queue_full",
            stage: "mineru_submit",
            message: "文档解析服务繁忙，请稍后重试。",
            retryable: true,
            diagnostic_id: "job-doc-failed",
          },
        }),
      }),
      documentFixture({
        id: "doc-unconfigured",
        display_name: "缺少配置文档",
        processing_status: "failed",
        processing_error: "Document processing failed.",
        stats: { blocks: 0, assets: 0, chunks: 0, index_records: 0 },
        latest_job: processingJobFixture("doc-unconfigured", {
          status: "failed",
          stage: "mineru_submit",
          queue_position: null,
          error: {
            code: "document_processing_not_configured",
            stage: "mineru_submit",
            message: "文档解析服务尚未配置。请在运行状态中确认服务配置后再重新读取。",
            retryable: false,
            diagnostic_id: "job-doc-unconfigured",
          },
        }),
      }),
      documentFixture({
        id: "doc-parsed",
        display_name: "待索引文档",
        processing_status: "parsed",
        stats: { blocks: 24, assets: 0, chunks: 8, index_records: 0 },
      }),
      documentFixture({
        id: "doc-ready",
        display_name: "已就绪文档",
        processing_status: "ready",
      }),
    ];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = requestMethod(input, init);
      if (path === "/api/knowledge-bases/kb-1") {
        return jsonResponse({ knowledge_base: base });
      }
      if (path === "/api/knowledge-bases/kb-1/documents" && method === "GET") {
        return jsonResponse({ documents });
      }
      if (path.endsWith("/doc-failed/parse") && method === "POST") {
        expect(init?.body).toBeUndefined();
        return jsonResponse(
          {
            detail: {
              code: "mineru_unavailable",
              message: "MinerU service is unavailable.",
            },
          },
          503,
        );
      }
      if (path.endsWith("/doc-failed/index") && method === "POST") {
        const indexed = {
          ...documents[0],
          processing_status: "queued",
          processing_error: null,
          latest_job: processingJobFixture("doc-failed", { operation: "index" }),
        };
        documents = documents.map((document) =>
          document.id === "doc-failed" ? indexed : document,
        );
        return jsonResponse({
          operation: "index",
          document: indexed,
          job: indexed.latest_job,
        });
      }
      if (path.endsWith("/doc-parsed/index") && method === "POST") {
        documents = documents.map((document) =>
          document.id === "doc-parsed"
            ? { ...document, processing_status: "ready", stats: { ...document.stats, index_records: 8 } }
            : document,
        );
        const queued = {
          ...documents[2],
          processing_status: "queued",
          latest_job: processingJobFixture("doc-parsed", { operation: "index" }),
        };
        documents = documents.map((document) =>
          document.id === "doc-parsed" ? queued : document,
        );
        return jsonResponse({
          operation: "index",
          document: queued,
          job: queued.latest_job,
        });
      }
      if (path.endsWith("/doc-ready/parse") && method === "POST") {
        const job = processingJobFixture("doc-ready", { operation: "parse" });
        return jsonResponse({
          operation: "parse",
          document: { ...documents[3], processing_status: "queued", latest_job: job },
          job,
        });
      }
      if (path.endsWith("/doc-ready/index") && method === "POST") {
        const job = processingJobFixture("doc-ready", { operation: "index" });
        return jsonResponse({
          operation: "index",
          document: { ...documents[3], processing_status: "queued", latest_job: job },
          job,
        });
      }
      throw new Error(`Unhandled request: ${method} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { user } = renderRoute("/knowledge-bases/kb-1");

    const failedRow = await screen.findByRole("row", { name: /失败文档/ });
    expect(within(failedRow).getByText("处理未完成")).toBeInTheDocument();
    expect(within(failedRow).getByText("文档解析服务繁忙，请稍后重试。")).toBeInTheDocument();
    expect(within(failedRow).queryByText(/诊断编号/)).not.toBeInTheDocument();

    const unconfiguredRow = screen.getByRole("row", { name: /缺少配置文档/ });
    expect(within(unconfiguredRow).getByText(/文档解析服务尚未配置/)).toBeInTheDocument();
    expect(within(unconfiguredRow).queryByRole("button", { name: /重试读取/ })).not.toBeInTheDocument();
    expect(within(unconfiguredRow).queryByRole("button", { name: /重试准备/ })).not.toBeInTheDocument();
    await user.click(within(failedRow).getByRole("button", { name: "重试读取失败文档" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "未能读取文档内容。请检查文件后重试",
    );
    await user.click(within(failedRow).getByRole("button", { name: "重试准备失败文档" }));
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/knowledge-bases/kb-1/documents/doc-failed/index",
        expect.objectContaining({ method: "POST", body: undefined }),
      );
    });

    const parsedRow = screen.getByRole("row", { name: /待索引文档/ });
    await user.click(within(parsedRow).getByRole("button", { name: "准备搜索待索引文档" }));
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/knowledge-bases/kb-1/documents/doc-parsed/index",
        expect.objectContaining({ method: "POST", body: undefined }),
      );
    });

    const readyRow = screen.getByRole("row", { name: /已就绪文档/ });
    await user.click(within(readyRow).getByRole("button", { name: "重新读取已就绪文档" }));
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/knowledge-bases/kb-1/documents/doc-ready/parse",
        expect.objectContaining({ method: "POST", body: undefined }),
      );
    });
  });

  it("refreshes a processing knowledge base card every two seconds and stops when ready", async () => {
    vi.useFakeTimers();
    let knowledgeBaseListCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      if (path === "/api/knowledge-bases") {
        knowledgeBaseListCalls += 1;
        return jsonResponse({
          knowledge_bases: [
            knowledgeBase(
              knowledgeBaseListCalls === 1
                ? { document_count: 1, processing_document_count: 1 }
                : { document_count: 1, ready_document_count: 1 },
            ),
          ],
        });
      }
      throw new Error(`Unhandled request: ${requestMethod(input, init)} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderRoute("/knowledge-bases");

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText("处理中")).toBeInTheDocument();
    expect(knowledgeBaseListCalls).toBe(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(screen.getByText("正常")).toBeInTheDocument();
    expect(knowledgeBaseListCalls).toBe(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(4_000);
    });
    expect(knowledgeBaseListCalls).toBe(2);
    expect(
      fetchMock.mock.calls.filter(([input]) => requestPath(input) === "/api/knowledge-bases"),
    ).toHaveLength(2);
    expect(
      fetchMock.mock.calls.some(([input]) => requestPath(input).includes("/documents")),
    ).toBe(false);
  });

  it("polls transitional document states every two seconds and stops after a terminal state", async () => {
    vi.useFakeTimers();
    const base = knowledgeBase();
    let documentListCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      if (path === "/api/knowledge-bases/kb-1") {
        return jsonResponse({ knowledge_base: base });
      }
      if (path === "/api/knowledge-bases/kb-1/documents") {
        documentListCalls += 1;
        return jsonResponse({
          documents: [
            documentFixture({
              processing_status: documentListCalls === 1 ? "parsing" : "ready",
            }),
          ],
        });
      }
      throw new Error(`Unhandled request: ${requestMethod(input, init)} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderRoute("/knowledge-bases/kb-1");

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText("读取内容")).toBeInTheDocument();
    expect(documentListCalls).toBe(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_999);
    });
    expect(documentListCalls).toBe(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(screen.getByText("可使用")).toBeInTheDocument();
    expect(documentListCalls).toBe(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(4_000);
    });
    expect(documentListCalls).toBe(2);
  });

  it("waits for a failed refresh to settle, then continues polling until ready", async () => {
    vi.useFakeTimers();
    const base = knowledgeBase();
    let documentListCalls = 0;
    let rejectRefresh: ((reason?: unknown) => void) | undefined;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      if (path === "/api/knowledge-bases/kb-1") {
        return jsonResponse({ knowledge_base: base });
      }
      if (path === "/api/knowledge-bases/kb-1/documents") {
        documentListCalls += 1;
        if (documentListCalls === 1) {
          return jsonResponse({
            documents: [documentFixture({ processing_status: "parsing" })],
          });
        }
        if (documentListCalls === 2) {
          return await new Promise<Response>((_resolve, reject) => {
            rejectRefresh = reject;
          });
        }
        return jsonResponse({ documents: [documentFixture({ processing_status: "ready" })] });
      }
      throw new Error(`Unhandled request: ${requestMethod(input, init)} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderRoute("/knowledge-bases/kb-1");

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText("读取内容")).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(documentListCalls).toBe(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(4_000);
    });
    expect(documentListCalls).toBe(2);

    await act(async () => {
      rejectRefresh?.(new TypeError("network down"));
      await Promise.resolve();
    });
    expect(screen.getByRole("alert")).toHaveTextContent("状态刷新失败");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_999);
    });
    expect(documentListCalls).toBe(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(screen.getByText("可使用")).toBeInTheDocument();
    expect(documentListCalls).toBe(3);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(4_000);
    });
    expect(documentListCalls).toBe(3);
  });

  it("refreshes server failed states after ready parse and parsed index errors", async () => {
    const base = knowledgeBase();
    let documents = [
      documentFixture({ id: "doc-ready", display_name: "待重新解析", processing_status: "ready" }),
      documentFixture({ id: "doc-parsed", display_name: "待建立索引", processing_status: "parsed" }),
    ];
    let documentListCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = requestMethod(input, init);
      if (path === "/api/knowledge-bases/kb-1") {
        return jsonResponse({ knowledge_base: base });
      }
      if (path === "/api/knowledge-bases/kb-1/documents" && method === "GET") {
        documentListCalls += 1;
        return jsonResponse({ documents });
      }
      if (path.endsWith("/doc-ready/parse") && method === "POST") {
        documents = documents.map((document) =>
          document.id === "doc-ready"
            ? { ...document, processing_status: "failed", processing_error: "parse failed" }
            : document,
        );
        return jsonResponse({ detail: { code: "parse_failed", message: "parse failed" } }, 500);
      }
      if (path.endsWith("/doc-parsed/index") && method === "POST") {
        documents = documents.map((document) =>
          document.id === "doc-parsed"
            ? { ...document, processing_status: "failed", processing_error: "index failed" }
            : document,
        );
        return jsonResponse({ detail: { code: "index_failed", message: "index failed" } }, 500);
      }
      throw new Error(`Unhandled request: ${method} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { user } = renderRoute("/knowledge-bases/kb-1");

    const readyRow = await screen.findByRole("row", { name: /待重新解析/ });
    await user.click(within(readyRow).getByRole("button", { name: "重新读取待重新解析" }));
    expect(await within(readyRow).findByText("处理未完成")).toBeInTheDocument();
    expect(within(readyRow).getByText(/文档处理未完成/)).toBeInTheDocument();

    const parsedRow = screen.getByRole("row", { name: /待建立索引/ });
    await user.click(within(parsedRow).getByRole("button", { name: "准备搜索待建立索引" }));
    expect(await within(parsedRow).findByText("处理未完成")).toBeInTheDocument();
    expect(within(parsedRow).getByText(/文档处理未完成/)).toBeInTheDocument();
    expect(documentListCalls).toBe(3);
  });

  it("keeps a failed process transitional when the authoritative refresh also fails", async () => {
    const base = knowledgeBase();
    let documentListCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = requestMethod(input, init);
      if (path === "/api/knowledge-bases/kb-1") {
        return jsonResponse({ knowledge_base: base });
      }
      if (path === "/api/knowledge-bases/kb-1/documents" && method === "GET") {
        documentListCalls += 1;
        if (documentListCalls > 1) {
          throw new TypeError("refresh offline");
        }
        return jsonResponse({ documents: [documentFixture({ processing_status: "ready" })] });
      }
      if (path.endsWith("/doc-1/parse") && method === "POST") {
        return jsonResponse({ detail: { code: "parse_failed", message: "parse failed" } }, 500);
      }
      throw new Error(`Unhandled request: ${method} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { user } = renderRoute("/knowledge-bases/kb-1");

    const row = await screen.findByRole("row", { name: /欧盟收入不平等趋势/ });
    await user.click(within(row).getByRole("button", { name: "重新读取欧盟收入不平等趋势" }));
    expect(await within(row).findByText("等待处理")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("状态刷新失败");
  });

  it("does not let a late knowledge base response overwrite a newer route", async () => {
    const kbResponseA = deferredResponse();
    const documentsResponseA = deferredResponse();
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = requestPath(input);
      if (path === "/api/knowledge-bases/kb-a") {
        return await kbResponseA.promise;
      }
      if (path === "/api/knowledge-bases/kb-a/documents") {
        return await documentsResponseA.promise;
      }
      if (path === "/api/knowledge-bases/kb-b") {
        return jsonResponse({ knowledge_base: knowledgeBase({ id: "kb-b", name: "知识库 B" }) });
      }
      if (path === "/api/knowledge-bases/kb-b/documents") {
        return jsonResponse({
          documents: [documentFixture({ knowledge_base_id: "kb-b", display_name: "B 文档" })],
        });
      }
      throw new Error(`Unhandled request: GET ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { router } = renderRoute("/knowledge-bases/kb-a");

    await act(async () => {
      await router.navigate("/knowledge-bases/kb-b");
    });
    expect(await screen.findByRole("heading", { name: "知识库 B" })).toBeInTheDocument();
    expect(screen.getByText("B 文档")).toBeInTheDocument();

    await act(async () => {
      kbResponseA.resolve(
        jsonResponse({ knowledge_base: knowledgeBase({ id: "kb-a", name: "知识库 A" }) }),
      );
      documentsResponseA.resolve(
        jsonResponse({ documents: [documentFixture({ display_name: "A 文档" })] }),
      );
      await Promise.resolve();
    });
    expect(screen.getByRole("heading", { name: "知识库 B" })).toBeInTheDocument();
    expect(screen.queryByText("A 文档")).not.toBeInTheDocument();
  });

  it("aborts and ignores a late upload response after switching knowledge bases", async () => {
    const uploadResponse = deferredResponse();
    let uploadSignal: AbortSignal | undefined;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      if (path === "/api/knowledge-bases/kb-a") {
        return jsonResponse({ knowledge_base: knowledgeBase({ id: "kb-a", name: "知识库 A" }) });
      }
      if (path === "/api/knowledge-bases/kb-a/documents" && requestMethod(input, init) === "GET") {
        return jsonResponse({ documents: [] });
      }
      if (path === "/api/knowledge-bases/kb-a/documents/upload") {
        uploadSignal = init?.signal ?? undefined;
        return await uploadResponse.promise;
      }
      if (path === "/api/knowledge-bases/kb-b") {
        return jsonResponse({ knowledge_base: knowledgeBase({ id: "kb-b", name: "知识库 B" }) });
      }
      if (path === "/api/knowledge-bases/kb-b/documents") {
        return jsonResponse({ documents: [] });
      }
      throw new Error(`Unhandled request: ${requestMethod(input, init)} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { router, user } = renderRoute("/knowledge-bases/kb-a");
    expect(await screen.findByRole("heading", { name: "知识库 A" })).toBeInTheDocument();

    await user.upload(
      screen.getByLabelText("选择要上传的原始文档"),
      new File(["docx"], "A文档.docx"),
    );
    await act(async () => {
      await router.navigate("/knowledge-bases/kb-b");
    });
    expect(await screen.findByRole("heading", { name: "知识库 B" })).toBeInTheDocument();
    expect(uploadSignal?.aborted).toBe(true);

    uploadResponse.resolve(
      jsonResponse({ document: documentFixture({ display_name: "迟到的 A 文档" }) }, 201),
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.queryByText("迟到的 A 文档")).not.toBeInTheDocument();
  });

  it("does not let an older poll response overwrite a newer rename mutation", async () => {
    vi.useFakeTimers();
    const pollResponse = deferredResponse();
    let documentListCalls = 0;
    const originalDocuments = [
      documentFixture({ id: "doc-target", display_name: "旧名称", processing_status: "ready" }),
      documentFixture({ id: "doc-worker", display_name: "后台文档", processing_status: "parsing" }),
    ];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = requestMethod(input, init);
      if (path === "/api/knowledge-bases/kb-1") {
        return jsonResponse({ knowledge_base: knowledgeBase() });
      }
      if (path === "/api/knowledge-bases/kb-1/documents" && method === "GET") {
        documentListCalls += 1;
        return documentListCalls === 1
          ? jsonResponse({ documents: originalDocuments })
          : await pollResponse.promise;
      }
      if (path === "/api/knowledge-bases/kb-1/documents/doc-target" && method === "PATCH") {
        return jsonResponse({
          document: { ...originalDocuments[0], display_name: requestJson(init).display_name },
        });
      }
      throw new Error(`Unhandled request: ${method} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderRoute("/knowledge-bases/kb-1");

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText("读取内容")).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(documentListCalls).toBe(2);

    fireEvent.click(screen.getByRole("button", { name: "重命名旧名称" }));
    fireEvent.change(screen.getByRole("textbox", { name: "文档名称" }), {
      target: { value: "新名称" },
    });
    await act(async () => {
      fireEvent.submit(screen.getByRole("textbox", { name: "文档名称" }).closest("form")!);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText("新名称")).toBeInTheDocument();

    pollResponse.resolve(jsonResponse({ documents: originalDocuments }));
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByText("新名称")).toBeInTheDocument();
    expect(screen.queryByText("旧名称")).not.toBeInTheDocument();
  });

  it("does not start a newer poll while a document mutation is still pending", async () => {
    vi.useFakeTimers();
    const renameResponse = deferredResponse();
    let documentListCalls = 0;
    const documents = [
      documentFixture({ id: "doc-target", display_name: "旧名称", processing_status: "ready" }),
      documentFixture({ id: "doc-worker", display_name: "后台文档", processing_status: "parsing" }),
    ];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = requestMethod(input, init);
      if (path === "/api/knowledge-bases/kb-1") {
        return jsonResponse({ knowledge_base: knowledgeBase() });
      }
      if (path === "/api/knowledge-bases/kb-1/documents" && method === "GET") {
        documentListCalls += 1;
        return jsonResponse({ documents });
      }
      if (path === "/api/knowledge-bases/kb-1/documents/doc-target" && method === "PATCH") {
        return await renameResponse.promise;
      }
      throw new Error(`Unhandled request: ${method} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderRoute("/knowledge-bases/kb-1");

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    fireEvent.click(screen.getByRole("button", { name: "重命名旧名称" }));
    fireEvent.change(screen.getByRole("textbox", { name: "文档名称" }), {
      target: { value: "新名称" },
    });
    fireEvent.submit(screen.getByRole("textbox", { name: "文档名称" }).closest("form")!);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(4_000);
    });
    expect(documentListCalls).toBe(1);

    renameResponse.resolve(
      jsonResponse({ document: { ...documents[0], display_name: "新名称" } }),
    );
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText("新名称")).toBeInTheDocument();
  });

  it("traps focus in knowledge base modals, closes with Escape, and restores the trigger", async () => {
    const base = knowledgeBase({ document_count: 1, ready_document_count: 1 });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ knowledge_bases: [base] })),
    );
    const { user } = renderRoute("/knowledge-bases");

    const createTrigger = await screen.findByRole("button", { name: "新建知识库" });
    await user.click(createTrigger);
    let dialog = screen.getByRole("dialog", { name: "新建知识库" });
    const nameInput = within(dialog).getByRole("textbox", { name: "名称" });
    expect(nameInput).toHaveFocus();
    expect(nameInput).toHaveAttribute("maxlength", "120");
    await user.type(nameInput, "知".repeat(121));
    expect(nameInput).toHaveValue("知".repeat(120));
    const closeButton = within(dialog).getByRole("button", { name: "关闭" });
    const submitButton = within(dialog).getByRole("button", { name: "创建知识库" });
    submitButton.focus();
    await user.tab();
    expect(closeButton).toHaveFocus();
    await user.tab({ shift: true });
    expect(submitButton).toHaveFocus();
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "新建知识库" })).not.toBeInTheDocument();
    await waitFor(() => expect(createTrigger).toHaveFocus());

    const manageButton = screen.getByRole("button", { name: "管理ViDoRe 示例知识库" });
    await user.click(manageButton);
    const renameTrigger = screen.getByRole("menuitem", { name: "重命名" });
    await user.click(renameTrigger);
    dialog = screen.getByRole("dialog", { name: "重命名知识库" });
    expect(dialog.parentElement?.parentElement).toBe(document.body);
    expect(within(dialog).getByRole("textbox", { name: "名称" })).toHaveFocus();
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(renameTrigger).toHaveFocus());

    const deleteTrigger = screen.getByRole("menuitem", { name: "删除" });
    await user.click(deleteTrigger);
    dialog = screen.getByRole("dialog", { name: "删除知识库" });
    expect(within(dialog).getByRole("textbox", { name: "输入知识库名称以确认" })).toHaveFocus();
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(deleteTrigger).toHaveFocus());
  });

  it("manages focus for document rename and delete modals", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = requestPath(input);
      if (path === "/api/knowledge-bases/kb-1") {
        return jsonResponse({ knowledge_base: knowledgeBase() });
      }
      if (path === "/api/knowledge-bases/kb-1/documents") {
        return jsonResponse({ documents: [documentFixture()] });
      }
      throw new Error(`Unhandled request: GET ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { user } = renderRoute("/knowledge-bases/kb-1");
    const row = await screen.findByRole("row", { name: /欧盟收入不平等趋势/ });

    const renameTrigger = within(row).getByRole("button", { name: "重命名欧盟收入不平等趋势" });
    await user.click(renameTrigger);
    let dialog = screen.getByRole("dialog", { name: "重命名文档" });
    expect(within(dialog).getByRole("textbox", { name: "文档名称" })).toHaveFocus();
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(renameTrigger).toHaveFocus());

    const deleteTrigger = within(row).getByRole("button", { name: "删除欧盟收入不平等趋势" });
    await user.click(deleteTrigger);
    dialog = screen.getByRole("dialog", { name: "删除文档" });
    expect(within(dialog).getByRole("button", { name: "取消" })).toHaveFocus();
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(deleteTrigger).toHaveFocus());
  });

  it("switches an empty knowledge base deletion to exact-name confirmation after a 409", async () => {
    const base = knowledgeBase();
    let deleteCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = requestMethod(input, init);
      if (path === "/api/knowledge-bases" && method === "GET") {
        return jsonResponse({ knowledge_bases: [base] });
      }
      if (path === "/api/knowledge-bases/kb-1" && method === "DELETE") {
        deleteCalls += 1;
        if (deleteCalls === 1) {
          expect(init?.body).toBeUndefined();
          return jsonResponse(
            {
              detail: {
                code: "knowledge_base_delete_confirmation_required",
                message: "confirmation required",
              },
            },
            409,
          );
        }
        expect(requestJson(init)).toEqual({ confirm_name: base.name });
        return jsonResponse({ deleted: true, knowledge_base_id: base.id });
      }
      throw new Error(`Unhandled request: ${method} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { user } = renderRoute("/knowledge-bases");

    const card = await screen.findByRole("article", { name: base.name });
    await user.click(within(card).getByRole("button", { name: `管理${base.name}` }));
    await user.click(screen.getByRole("menuitem", { name: "删除" }));
    await user.click(screen.getByRole("button", { name: "确认删除" }));

    const dialog = screen.getByRole("dialog", { name: "删除知识库" });
    const confirmInput = await within(dialog).findByRole("textbox", {
      name: "输入知识库名称以确认",
    });
    expect(confirmInput).toHaveFocus();
    await user.type(confirmInput, base.name);
    await user.click(within(dialog).getByRole("button", { name: "永久删除" }));
    expect(await screen.findByRole("heading", { name: "还没有知识库" })).toBeInTheDocument();
  });

  it("turns malformed list, create, and upload payloads into actionable errors", async () => {
    const listFetch = vi.fn(async () =>
      jsonResponse({
        knowledge_bases: [
          {
            id: "kb-malformed",
            name: "缺字段知识库",
            document_count: 0,
            ready_document_count: 0,
            processing_document_count: 0,
            failed_document_count: 0,
          },
        ],
      }),
    );
    vi.stubGlobal("fetch", listFetch);
    const listView = renderRoute("/knowledge-bases");
    expect(await screen.findByRole("alert")).toHaveTextContent("暂时无法读取知识库");
    listView.unmount();

    const createFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      if (path === "/api/knowledge-bases" && requestMethod(input, init) === "GET") {
        return jsonResponse({ knowledge_bases: [] });
      }
      if (path === "/api/knowledge-bases" && requestMethod(input, init) === "POST") {
        return jsonResponse({}, 201);
      }
      throw new Error(`Unhandled request: ${requestMethod(input, init)} ${path}`);
    });
    vi.stubGlobal("fetch", createFetch);
    const createView = renderRoute("/knowledge-bases");
    const createUser = createView.user;
    await createUser.click(await screen.findByRole("button", { name: "新建知识库" }));
    const createDialog = screen.getByRole("dialog", { name: "新建知识库" });
    await createUser.type(within(createDialog).getByRole("textbox", { name: "名称" }), "格式测试");
    await createUser.click(within(createDialog).getByRole("button", { name: "创建知识库" }));
    expect(await within(createDialog).findByRole("alert")).toHaveTextContent("创建知识库未完成");
    createView.unmount();

    const uploadFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      if (path === "/api/knowledge-bases/kb-1") {
        return jsonResponse({ knowledge_base: knowledgeBase() });
      }
      if (path === "/api/knowledge-bases/kb-1/documents" && requestMethod(input, init) === "GET") {
        return jsonResponse({ documents: [] });
      }
      if (path.endsWith("/upload")) {
        return jsonResponse(
          {
            document: {
              id: "doc-malformed",
              knowledge_base_id: "kb-1",
              display_name: "缺字段文档",
              processing_status: "ready",
              stats: {},
            },
          },
          201,
        );
      }
      throw new Error(`Unhandled request: ${requestMethod(input, init)} ${path}`);
    });
    vi.stubGlobal("fetch", uploadFetch);
    const uploadView = renderRoute("/knowledge-bases/kb-1");
    await uploadView.user.upload(
      await screen.findByLabelText("选择要上传的原始文档"),
      new File(["docx"], "格式测试.docx"),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("上传未完成");
    expect(screen.getByRole("heading", { name: "还没有文档" })).toBeInTheDocument();
  });

  it.each([
    ["updated_at", {}],
    ["created_at", 42],
    ["parser_version", {}],
  ])("rejects an otherwise valid document with an invalid %s field", async (field, value) => {
    const malformedDocument = {
      ...documentFixture(),
      [field]: value,
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = requestMethod(input, init);
      if (path === "/api/knowledge-bases/kb-1") {
        return jsonResponse({ knowledge_base: knowledgeBase() });
      }
      if (path === "/api/knowledge-bases/kb-1/documents" && method === "GET") {
        return jsonResponse({ documents: [malformedDocument] });
      }
      throw new Error(`Unhandled request: ${method} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    renderRoute("/knowledge-bases/kb-1");

    expect(await screen.findByRole("alert")).toHaveTextContent("暂时无法读取知识库中的文档");
    expect(screen.queryByRole("row", { name: /欧盟收入不平等趋势/ })).not.toBeInTheDocument();
  });

  it("rejects a managed process response that omits operation counts", async () => {
    let documentListCalls = 0;
    const failedDocument = documentFixture({
      processing_status: "failed",
      processing_error: "server failed",
    });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      const method = requestMethod(input, init);
      if (path === "/api/knowledge-bases/kb-1") {
        return jsonResponse({ knowledge_base: knowledgeBase() });
      }
      if (path === "/api/knowledge-bases/kb-1/documents" && method === "GET") {
        documentListCalls += 1;
        return jsonResponse({
          documents: [documentListCalls === 1 ? documentFixture() : failedDocument],
        });
      }
      if (path.endsWith("/doc-1/parse") && method === "POST") {
        return jsonResponse({ document: documentFixture(), operation: "parse" });
      }
      throw new Error(`Unhandled request: ${method} ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { user } = renderRoute("/knowledge-bases/kb-1");
    const row = await screen.findByRole("row", { name: /欧盟收入不平等趋势/ });

    await user.click(within(row).getByRole("button", { name: "重新读取欧盟收入不平等趋势" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("未能读取文档内容");
    expect(within(row).getByText("处理未完成")).toBeInTheDocument();
  });

  it("turns load failures into a concrete recovery action", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse(
          { detail: { code: "service_unavailable", message: "Service unavailable." } },
          503,
        ),
      ),
    );
    renderRoute("/knowledge-bases");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("暂时无法读取知识库，请稍后重试。");
    expect(within(alert).getByRole("button", { name: "重试" })).toBeInTheDocument();
  });
});
