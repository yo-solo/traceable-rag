import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { appRoutes } from "../../app/router";


vi.mock("./ContinuousSourcePdfViewer", () => ({
  ContinuousSourcePdfViewer: ({
    contextLocations,
    insight,
    knowledgeBaseUrl,
    target,
  }: {
    contextLocations: Array<{ humanPageNumber: number; bbox: number[] | null; blockType: string }>;
    insight: string | null;
    knowledgeBaseUrl: string;
    target: { humanPageNumber: number; bbox: number[] | null };
  }) => (
    <div
      data-bbox={target.bbox?.join(",") ?? "none"}
      data-context-count={contextLocations.length}
      data-insight={insight ?? "none"}
      data-page={target.humanPageNumber}
    >
      <span
        data-bbox={target.bbox?.join(",") ?? "none"}
        data-insight={insight ?? "none"}
        data-page={target.humanPageNumber}
      >
        PDF mock
      </span>
      <a href={knowledgeBaseUrl}>在知识库中查看</a>
    </div>
  ),
}));

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const sourceId = "src_0123456789abcdef0123456789abcdef";

function sourceResponse() {
  return {
    source: {
      source_id: sourceId,
      source_url: `/sources/${sourceId}`,
      text: "个人数据只应在实现处理目的所需的期限内保存。",
      document_id: "doc-1",
      document_name: "数据处理政策.pdf",
      knowledge_base_id: "kb-1",
      human_page_number: 12,
      bbox: [72, 160, 510, 240],
      page_size: [595, 842],
      title_path: ["数据治理", "保留期限"],
      file_url: "/api/knowledge-bases/kb-1/documents/doc-1/file",
    },
    before: [
      {
        text: "组织应定期复核处理目的。",
        human_page_number: 11,
        bbox: [70, 500, 510, 560],
        page_size: [595, 842],
        block_type: "paragraph",
      },
    ],
    after: [
      {
        text: "期限届满后应删除或匿名化。",
        human_page_number: 12,
        bbox: [72, 250, 510, 300],
        page_size: [595, 842],
        block_type: "paragraph",
      },
    ],
    truncated: false,
  };
}

function renderSource(fragment = "") {
  const router = createMemoryRouter(appRoutes, {
    initialEntries: [`/sources/${sourceId}${fragment}`],
  });
  return render(<RouterProvider router={router} />);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

beforeEach(() => {
  vi.stubGlobal("scrollTo", vi.fn());
});

describe("SourceVerificationPage", () => {
  it("opens the immersive PDF location without rendering source text panels", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(sourceResponse())));
    vi.stubGlobal("fetch", fetchMock);

    renderSource(
      `#insight=${encodeURIComponent("2014-15 年峰值后，欧盟收入不平等有所下降")}`,
    );

    expect(await screen.findByText("PDF mock")).toBeVisible();
    expect(screen.getByText("PDF mock")).toHaveAttribute("data-page", "12");
    expect(screen.getByText("PDF mock")).toHaveAttribute(
      "data-insight",
      "2014-15 年峰值后，欧盟收入不平等有所下降",
    );
    expect(screen.getByText("PDF mock")).toHaveAttribute("data-bbox", "72,160,510,240");
    expect(screen.getByText("PDF mock").parentElement).toHaveAttribute("data-context-count", "2");
    expect(screen.getByRole("link", { name: "在知识库中查看" })).toHaveAttribute(
      "href", "/knowledge-bases/kb-1/documents/doc-1?page=12",
    );
    expect(screen.queryByRole("link", { name: "Traceable RAG 首页" })).not.toBeInTheDocument();
    expect(screen.queryByText("个人数据只应在实现处理目的所需的期限内保存。")).not.toBeInTheDocument();
    expect(screen.queryByText("组织应定期复核处理目的。")).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/sources/${sourceId}/context?before=5&after=5`,
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it("keeps legacy source links working without an insight", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse(sourceResponse()))));
    renderSource();
    expect(await screen.findByText("PDF mock")).toHaveAttribute("data-insight", "none");
  });

  it("shows an image Source beside the continuous PDF viewer and opens its original", async () => {
    const user = userEvent.setup();
    const payload = sourceResponse();
    Object.assign(payload.source, {
      text: "图 1 合成系统流程图",
      asset: {
        asset_id: "asset-image",
        modality: "image",
        asset_url: "/api/assets/asset-image",
        preview_url: "/api/assets/asset-image?variant=preview",
        content_type: "image/png",
        file_hash: "image-hash",
        width: 640,
        height: 320,
        caption: "图 1 完整的合成系统流程图",
      },
    });
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse(payload))));

    renderSource();

    expect(await screen.findByText("PDF mock")).toBeVisible();
    expect(screen.getByRole("heading", { name: "图片与原始 PDF 对照核验" })).toBeVisible();
    expect(screen.getByText("图 1 完整的合成系统流程图")).toBeVisible();
    const preview = screen.getByRole("button", {
      name: "查看图片原图：图 1 完整的合成系统流程图",
    });
    expect(preview.querySelector("img")).toHaveAttribute("loading", "lazy");

    await user.click(preview);
    expect(screen.getByRole("dialog", { name: "图片" })).toBeVisible();
    expect(screen.getByText("PDF mock")).toBeVisible();
  });

  it("explains a stale Source without showing replacement content", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(jsonResponse({
        detail: { code: "source_gone", message: "Source is gone." },
      }, 410))),
    );

    renderSource();

    expect(await screen.findByRole("heading", { name: "无法核验这条来源" })).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("系统不会把它跳转到新内容");
    expect(screen.getByRole("link", { name: "前往知识库" })).toHaveAttribute("href", "/knowledge-bases");
    expect(screen.queryByText("个人数据只应")).not.toBeInTheDocument();
  });
});
