import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { appRoutes } from "../../app/router";

vi.mock("../viewer/DocumentEvidenceViewer", () => ({
  DocumentEvidenceViewer: ({
    assets,
    blocks,
    documentName,
    fileUrl,
    location,
    onLocationChange,
  }: {
    assets: Record<string, unknown>;
    blocks: Array<{ id: string }>;
    documentName: string;
    fileUrl: string;
    location: { page: number; blockId?: string };
    onLocationChange: (location: {
      page: number;
      blockId?: string;
    }) => void;
  }) => (
    <div data-testid="viewer-stub">
      <span>{documentName}</span>
      <span>{fileUrl}</span>
      <span>viewer-page-{location.page}</span>
      <span
        data-selected-content-id={location.blockId ?? "none"}
        data-testid="viewer-selection"
      >
        {location.blockId ? "已选择内容" : "未选择内容"}
      </span>
      <span data-testid="viewer-content-count">共 {blocks.length} 项内容</span>
      <span data-testid="viewer-asset-count">共 {Object.keys(assets).length} 个图片资产</span>
      <button
        onClick={() =>
          onLocationChange({
            page: 1,
            blockId: "block-1",
          })
        }
        type="button"
      >
        模拟选择段落
      </button>
    </div>
  ),
}));

function knowledgeBase(id = "kb-1", name = "ViDoRe 示例知识库") {
  return {
    id,
    name,
    description: "用于图表与复杂版式核验",
    created_at: "2026-07-20T09:00:00Z",
    updated_at: "2026-07-21T09:30:00Z",
  };
}

function documentFixture(id = "doc-1", displayName = "欧盟收入不平等趋势") {
  return {
    id,
    knowledge_base_id: "kb-1",
    original_filename: `${displayName}.pdf`,
    display_name: displayName,
    source_type: "pdf",
    page_count: 2,
    processing_status: "ready",
    processing_error: null,
    parser_provider: "mineru",
    parser_version: "vlm",
    created_at: "2026-07-20T09:00:00Z",
    updated_at: "2026-07-21T09:30:00Z",
    stats: {
      blocks: 2,
      assets: 0,
      chunks: 1,
      index_records: 1,
      embedding_records: 1,
    },
    embedding_record_count: 1,
    embedding_ready: true,
    embedding_status: "ready",
    embedding_warning: null,
    file_url: `/api/knowledge-bases/kb-1/documents/${id}/file`,
    web_url: `/knowledge-bases/kb-1/documents/${id}`,
  };
}

const blockFixture = (id: string, pageIdx: number, blockIdx: number) => ({
  id,
  document_id: "doc-1",
  page_idx: pageIdx,
  human_page_number: pageIdx + 1,
  block_idx: blockIdx,
  block_type: "paragraph",
  text: `文档内容 ${id}`,
  html: null,
  asset_id: null,
  bbox: [10, 20, 100, 120],
  page_size: [1000, 1400],
  is_discarded: false,
  metadata: {},
});

const blocksPayload = {
  blocks: [blockFixture("block-1", 0, 0), blockFixture("block-2", 1, 1)],
  pages: [
    { page_idx: 0, human_page_number: 1, page_size: [1000, 1400], block_count: 1 },
    { page_idx: 1, human_page_number: 2, page_size: [1000, 1400], block_count: 1 },
  ],
};

const publicBlocksPayload = {
  ...blocksPayload,
  blocks: blocksPayload.blocks.map(({ is_discarded: _discarded, ...block }) => block),
};

const imageBlocksPayload = {
  blocks: [{
    ...blockFixture("image-block", 0, 0),
    block_type: "image",
    text: "图 1 合成系统流程图",
    asset_id: "asset-image",
  }],
  pages: [
    { page_idx: 0, human_page_number: 1, page_size: [1000, 1400], block_count: 1 },
  ],
  assets: {
    "asset-image": {
      asset_id: "asset-image",
      block_id: "image-block",
      modality: "image",
      asset_url: "/api/assets/asset-image",
      preview_url: "/api/assets/asset-image?variant=preview",
      content_type: "image/png",
      file_hash: "image-hash",
      width: 640,
      height: 320,
      caption: "图 1 合成系统流程图",
    },
  },
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

function renderRoute(initialEntry: string) {
  const router = createMemoryRouter(appRoutes, { initialEntries: [initialEntry] });
  return {
    router,
    user: userEvent.setup(),
    ...render(<RouterProvider router={router} />),
  };
}

function deferredResponse() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((promiseResolve) => {
    resolve = promiseResolve;
  });
  return { promise, resolve };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("DocumentViewerPage", () => {
  it("loads the document contract and applies then updates a page/block deep link", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = requestPath(input);
      if (path === "/api/knowledge-bases/kb-1") {
        return jsonResponse({ knowledge_base: knowledgeBase() });
      }
      if (path === "/api/knowledge-bases/kb-1/documents/doc-1") {
        return jsonResponse({ document: documentFixture() });
      }
      if (path === "/api/knowledge-bases/kb-1/documents/doc-1/blocks") {
        return jsonResponse(publicBlocksPayload);
      }
      throw new Error(`Unhandled request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { router, user } = renderRoute(
      "/knowledge-bases/kb-1/documents/doc-1?page=2&block=block-2",
    );

    const heading = await screen.findByRole("heading", { name: "欧盟收入不平等趋势" });
    expect(heading).toBeInTheDocument();
    expect(heading).toHaveAttribute("title", "欧盟收入不平等趋势");
    expect(screen.getByTitle("欧盟收入不平等趋势.pdf")).toHaveTextContent("欧盟收入不平等趋势.pdf");
    expect(screen.getByRole("navigation", { name: "面包屑" })).toHaveTextContent(
      "知识库/ViDoRe 示例知识库/欧盟收入不平等趋势",
    );
    expect(screen.getByText("viewer-page-2")).toBeInTheDocument();
    expect(screen.getByTestId("viewer-selection")).toHaveAttribute(
      "data-selected-content-id",
      "block-2",
    );
    expect(screen.getByTestId("viewer-content-count")).toHaveTextContent("共 2 项内容");
    const userFacingContent = [
      document.body.textContent ?? "",
      ...Array.from(document.querySelectorAll("[aria-label]"), (element) =>
        element.getAttribute("aria-label") ?? ""),
    ].join(" ");
    expect(userFacingContent).not.toMatch(
      /\b(?:Trace|Citation|Block|Evidence|bbox|supporting|manual_query|Embedding|MinerU|Provider)\b/i,
    );

    await user.click(screen.getByRole("button", { name: "模拟选择段落" }));
    const params = new URLSearchParams(router.state.location.search);
    expect(Object.fromEntries(params)).toEqual({
      page: "1",
      block: "block-1",
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });

  it("removes legacy Evidence locator parameters without calling the deleted API", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = requestPath(input);
      if (path === "/api/knowledge-bases/kb-1") {
        return jsonResponse({ knowledge_base: knowledgeBase() });
      }
      if (path === "/api/knowledge-bases/kb-1/documents/doc-1") {
        return jsonResponse({ document: documentFixture() });
      }
      if (path === "/api/knowledge-bases/kb-1/documents/doc-1/blocks") {
        return jsonResponse(blocksPayload);
      }
      throw new Error(`Unhandled request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { router } = renderRoute(
      "/knowledge-bases/kb-1/documents/doc-1?page=2&block_token=stable-token&evidence=evidence-2",
    );

    expect(await screen.findByRole("heading", { name: "欧盟收入不平等趋势" })).toBeInTheDocument();
    expect(screen.getByText("viewer-page-2")).toBeInTheDocument();
    expect(screen.getByTestId("viewer-selection")).toHaveAttribute(
      "data-selected-content-id",
      "none",
    );
    expect(fetchMock).toHaveBeenCalledTimes(3);
    const params = new URLSearchParams(router.state.location.search);
    expect(params.has("block_token")).toBe(false);
    expect(params.has("evidence")).toBe(false);
    expect(params.has("block")).toBe(false);
  });

  it("passes the scoped image asset map into the document viewer", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = requestPath(input);
      if (path === "/api/knowledge-bases/kb-1") {
        return jsonResponse({ knowledge_base: knowledgeBase() });
      }
      if (path === "/api/knowledge-bases/kb-1/documents/doc-1") {
        return jsonResponse({ document: documentFixture() });
      }
      if (path === "/api/knowledge-bases/kb-1/documents/doc-1/blocks") {
        return jsonResponse(imageBlocksPayload);
      }
      throw new Error(`Unhandled request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    renderRoute("/knowledge-bases/kb-1/documents/doc-1");

    expect(await screen.findByTestId("viewer-asset-count")).toHaveTextContent("共 1 个图片资产");
    expect(screen.getByTestId("viewer-content-count")).toHaveTextContent("共 1 项内容");
  });

  it("aborts old route requests and never lets their late response replace the new document", async () => {
    const oldDocument = deferredResponse();
    const oldBlocks = deferredResponse();
    const oldSignals: AbortSignal[] = [];
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      if (path === "/api/knowledge-bases/kb-1") {
        return Promise.resolve(jsonResponse({ knowledge_base: knowledgeBase() }));
      }
      if (path === "/api/knowledge-bases/kb-1/documents/doc-1") {
        oldSignals.push(init?.signal as AbortSignal);
        return oldDocument.promise;
      }
      if (path === "/api/knowledge-bases/kb-1/documents/doc-1/blocks") {
        oldSignals.push(init?.signal as AbortSignal);
        return oldBlocks.promise;
      }
      if (path === "/api/knowledge-bases/kb-1/documents/doc-2") {
        return Promise.resolve(jsonResponse({ document: documentFixture("doc-2", "新文档") }));
      }
      if (path === "/api/knowledge-bases/kb-1/documents/doc-2/blocks") {
        return Promise.resolve(jsonResponse({ ...blocksPayload, blocks: [] }));
      }
      throw new Error(`Unhandled request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { router } = renderRoute("/knowledge-bases/kb-1/documents/doc-1");

    await waitFor(() => expect(oldSignals).toHaveLength(2));
    await router.navigate("/knowledge-bases/kb-1/documents/doc-2?page=1");

    expect(await screen.findByRole("heading", { name: "新文档" })).toBeInTheDocument();
    expect(oldSignals.every((signal) => signal.aborted)).toBe(true);

    oldDocument.resolve(jsonResponse({ document: documentFixture("doc-1", "旧文档") }));
    oldBlocks.resolve(jsonResponse(blocksPayload));
    await Promise.resolve();
    expect(screen.queryByRole("heading", { name: "旧文档" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "新文档" })).toBeInTheDocument();
  });

  it("rejects malformed content payloads with an actionable error and recovers on retry", async () => {
    let blocksAttempt = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = requestPath(input);
      if (path === "/api/knowledge-bases/kb-1") {
        return jsonResponse({ knowledge_base: knowledgeBase() });
      }
      if (path === "/api/knowledge-bases/kb-1/documents/doc-1") {
        return jsonResponse({ document: documentFixture() });
      }
      if (path === "/api/knowledge-bases/kb-1/documents/doc-1/blocks") {
        blocksAttempt += 1;
        return blocksAttempt === 1
          ? jsonResponse({ blocks: [{ id: "broken" }], pages: [] })
          : jsonResponse(blocksPayload);
      }
      throw new Error(`Unhandled request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { user } = renderRoute("/knowledge-bases/kb-1/documents/doc-1");

    expect(await screen.findByRole("heading", { name: "无法打开文档" })).toBeInTheDocument();
    expect(screen.getByText("暂时无法打开文档。请确认文档仍可使用，然后重试。")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重试" }));

    expect(await screen.findByRole("heading", { name: "欧盟收入不平等趋势" })).toBeInTheDocument();
    expect(screen.getByTestId("viewer-content-count")).toHaveTextContent("共 2 项内容");
  });
});
