import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ContinuousSourcePdfViewer } from "./ContinuousSourcePdfViewer";

const pdfMocks = vi.hoisted(() => ({
  getDocument: vi.fn(),
  loadingTasks: [] as Array<{
    destroy: ReturnType<typeof vi.fn>;
    promise: Promise<unknown>;
  }>,
  pageCleanup: vi.fn(),
  pageRender: vi.fn(() => ({ promise: Promise.resolve(), cancel: vi.fn() })),
  trackLoadingTask<T>(promise: Promise<T>) {
    const task = {
      promise,
      destroy: vi.fn(async () => undefined),
    };
    this.loadingTasks.push(task);
    return task;
  },
}));

vi.mock("pdfjs-dist", () => ({
  GlobalWorkerOptions: { workerSrc: "" },
  getDocument: pdfMocks.getDocument,
}));

function makePdf(pageCount = 10) {
  return {
    numPages: pageCount,
    getPage: vi.fn(async () => ({
      getViewport: ({ scale }: { scale: number }) => ({ width: 600 * scale, height: 800 * scale }),
      render: pdfMocks.pageRender,
      cleanup: pdfMocks.pageCleanup,
    })),
  };
}

function renderViewer(bbox: [number, number, number, number] | null = [60, 200, 540, 300]) {
  return render(
    <MemoryRouter>
      <ContinuousSourcePdfViewer
        contextLocations={[
          { humanPageNumber: 5, bbox: [60, 80, 540, 150], pageSize: [600, 800], blockType: "paragraph" },
          { humanPageNumber: 5, bbox: [60, 340, 540, 420], pageSize: [600, 800], blockType: "table" },
          { humanPageNumber: 6, bbox: [60, 60, 540, 130], pageSize: [600, 800], blockType: "paragraph" },
        ]}
        documentName="示例监管文件.pdf"
        fileUrl="/api/file"
        insight="关键数字之间的差额解释了最终结论。"
        knowledgeBaseUrl="/knowledge-bases/kb-1/documents/doc-1?page=5"
        target={{ humanPageNumber: 5, bbox, pageSize: [600, 800] }}
      />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  pdfMocks.loadingTasks.length = 0;
  pdfMocks.pageRender.mockClear();
  const pdf = makePdf();
  pdfMocks.getDocument.mockImplementation(() =>
    pdfMocks.trackLoadingTask(Promise.resolve(pdf)),
  );
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(
    { setTransform: vi.fn() } as unknown as CanvasRenderingContext2D,
  );
  Object.defineProperty(HTMLElement.prototype, "scrollTo", {
    configurable: true,
    value: vi.fn(),
  });
  vi.stubGlobal("scrollTo", vi.fn());
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    callback(0);
    return 1;
  });
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  pdfMocks.getDocument.mockReset();
});

describe("ContinuousSourcePdfViewer", () => {
  it("renders a compact PDF-first workspace with lazy nearby pages and an insight bubble", async () => {
    renderViewer();

    expect(screen.getByText("示例监管文件.pdf")).toBeVisible();
    expect(screen.getByRole("link", { name: "在知识库中查看" })).toHaveAttribute(
      "href", "/knowledge-bases/kb-1/documents/doc-1?page=5",
    );
    expect(await screen.findByRole("button", { name: "引用原文高亮区域" })).toBeVisible();
    expect(document.querySelectorAll(".source-pdf-context-bbox")).toHaveLength(3);
    expect(screen.getByText("关键数字之间的差额解释了最终结论。")).toBeVisible();
    expect(screen.queryByLabelText("示例监管文件.pdf PDF 第 1 页")).not.toBeInTheDocument();
    expect(screen.getByLabelText("示例监管文件.pdf PDF 第 5 页")).toBeInTheDocument();
  });

  it("opens at fit width instead of magnifying a short target block", async () => {
    renderViewer();

    await screen.findByRole("button", { name: "引用原文高亮区域" });
    expect(screen.getByLabelText("当前缩放比例")).toHaveTextContent("100%");
  });

  it("provides visible labels and hover explanations for the compact toolbar", async () => {
    renderViewer();

    expect(screen.getByRole("button", { name: "放大" })).toHaveAttribute("data-tooltip", "放大 PDF");
    expect(screen.getByRole("button", { name: "适合宽度" })).toHaveTextContent("适应");
    expect(screen.getByRole("button", { name: "定位引用" })).toHaveTextContent("定位");
    expect(screen.getByRole("link", { name: "在知识库中查看" })).toHaveAttribute(
      "data-tooltip", "打开文档的知识库详情页",
    );
  });

  it("minimizes and restores the insight without changing the locate control", async () => {
    const user = userEvent.setup();
    renderViewer();
    await screen.findByText("关键数字之间的差额解释了最终结论。");

    await user.click(screen.getByRole("button", { name: "最小化关键提示" }));
    expect(screen.queryByText("关键数字之间的差额解释了最终结论。")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "展开关键提示" })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "定位引用" }));
    expect(screen.getByRole("button", { name: "展开关键提示" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "展开关键提示" }));
    expect(screen.getByText("关键数字之间的差额解释了最终结论。")).toBeVisible();
  });

  it("shows a location failure instead of inventing a highlight when bbox is missing", async () => {
    renderViewer(null);
    expect(await screen.findByText(/无法定位引用原文/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "引用原文高亮区域" })).not.toBeInTheDocument();
    expect(screen.queryByText("关键数字之间的差额解释了最终结论。")).not.toBeInTheDocument();
  });

  it("rerenders only the active canvases when a background source tab becomes visible again", async () => {
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    renderViewer(null);

    await waitFor(() => expect(pdfMocks.pageRender).toHaveBeenCalledTimes(5));
    expect(screen.queryByLabelText("示例监管文件.pdf PDF 第 1 页")).not.toBeInTheDocument();

    visibility.mockReturnValue("hidden");
    fireEvent(document, new Event("visibilitychange"));
    expect(pdfMocks.pageRender).toHaveBeenCalledTimes(5);

    visibility.mockReturnValue("visible");
    fireEvent(document, new Event("visibilitychange"));

    await waitFor(() => expect(pdfMocks.pageRender).toHaveBeenCalledTimes(10));
    expect(screen.queryByLabelText("示例监管文件.pdf PDF 第 1 页")).not.toBeInTheDocument();
  });

  it("copies the complete insight link from the compact actions menu", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    renderViewer();
    await screen.findByText("关键数字之间的差额解释了最终结论。");

    fireEvent.click(screen.getByLabelText("更多操作"));
    fireEvent.click(screen.getByRole("button", { name: "复制引用链接" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(window.location.href));
    expect(screen.getByRole("link", { name: "打开原始 PDF" })).toHaveAttribute("href", "/api/file");
  });

  it("destroys the PDF loading task once when leaving the source viewer", async () => {
    const { unmount } = renderViewer();
    await screen.findByRole("button", { name: "引用原文高亮区域" });

    unmount();

    expect(pdfMocks.loadingTasks).toHaveLength(1);
    expect(pdfMocks.loadingTasks[0]?.destroy).toHaveBeenCalledTimes(1);
  });
});
