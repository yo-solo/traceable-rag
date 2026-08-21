import DOMPurify from "dompurify";
import katex from "katex";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  DocumentBlock,
  DocumentPageMetadata,
  DocumentVisualAssetDetail,
} from "../../api/types";
import { BlockContent } from "./BlockContent";
import { blockTypeLabel } from "./BlockListPane";
import { DocumentEvidenceViewer } from "./DocumentEvidenceViewer";
import { PdfDocumentPane } from "./PdfDocumentPane";

const pdfMocks = vi.hoisted(() => {
  const renderCancel = vi.fn();
  const pageCleanup = vi.fn();
  const proxies: Array<{
    getPage: ReturnType<typeof vi.fn>;
  }> = [];
  const loadingTasks: Array<{
    destroy: ReturnType<typeof vi.fn>;
    promise: Promise<unknown>;
  }> = [];
  const trackLoadingTask = <T,>(promise: Promise<T>) => {
    const task = {
      promise,
      destroy: vi.fn(async () => undefined),
    };
    loadingTasks.push(task);
    return task;
  };

  return {
    getDocument: vi.fn(() => {
      const proxy = {
        numPages: 2,
        getPage: vi.fn(async () => ({
          getViewport: ({ scale }: { scale: number }) => ({
            width: 600 * scale,
            height: 800 * scale,
          }),
          render: vi.fn(() => ({
            promise: Promise.resolve(),
            cancel: renderCancel,
          })),
          cleanup: pageCleanup,
        })),
      };
      proxies.push(proxy);
      return trackLoadingTask(Promise.resolve(proxy));
    }),
    loadingTasks,
    pageCleanup,
    proxies,
    renderCancel,
    trackLoadingTask,
  };
});

vi.mock("pdfjs-dist", () => ({
  GlobalWorkerOptions: { workerSrc: "" },
  getDocument: pdfMocks.getDocument,
}));

const blocks: DocumentBlock[] = [
  {
    id: "block-1",
    document_id: "doc-1",
    page_idx: 0,
    human_page_number: 1,
    block_idx: 0,
    block_type: "paragraph",
    text: "第一段收入不平等趋势说明",
    html: null,
    asset_id: null,
    bbox: [20, 40, 180, 160],
    page_size: [200, 400],
    is_discarded: false,
    metadata: {},
  },
  {
    id: "block-2",
    document_id: "doc-1",
    page_idx: 1,
    human_page_number: 2,
    block_idx: 1,
    block_type: "formula",
    text: "拟合公式 $P_t = L_t + \\epsilon_t$",
    html: null,
    asset_id: null,
    bbox: [40, 80, 190, 240],
    page_size: [200, 400],
    is_discarded: false,
    metadata: {},
  },
  {
    id: "block-3",
    document_id: "doc-1",
    page_idx: 1,
    human_page_number: 2,
    block_idx: 2,
    block_type: "paragraph",
    text: "没有坐标的完整长段落，末尾内容也必须展示，不能因为卡片高度或行数限制而被截断。",
    html: null,
    asset_id: null,
    bbox: null,
    page_size: null,
    is_discarded: false,
    metadata: {},
  },
];

const pages: DocumentPageMetadata[] = [
  { page_idx: 0, human_page_number: 1, page_size: [200, 400], block_count: 1 },
  { page_idx: 1, human_page_number: 2, page_size: [200, 400], block_count: 2 },
];

const imageBlock: DocumentBlock = {
  ...blocks[0],
  id: "image-block",
  block_type: "image",
  text: "图 1 合成系统流程图",
  asset_id: "asset-image",
};

const imageAsset: DocumentVisualAssetDetail = {
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
};

beforeEach(() => {
  pdfMocks.getDocument.mockClear();
  pdfMocks.pageCleanup.mockClear();
  pdfMocks.renderCancel.mockClear();
  pdfMocks.loadingTasks.length = 0;
  pdfMocks.proxies.length = 0;
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(
    {} as CanvasRenderingContext2D,
  );
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
    configurable: true,
    value: vi.fn(),
  });
  Object.defineProperty(HTMLElement.prototype, "scrollTo", {
    configurable: true,
    value: vi.fn(),
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("DocumentEvidenceViewer", () => {
  it("synchronizes hover and selection between document content and original-page highlights", async () => {
    const user = userEvent.setup();
    const onLocationChange = vi.fn();
    render(
      <DocumentEvidenceViewer
        blocks={[...blocks]}
        documentName="欧盟收入不平等趋势"
        fileUrl="/api/doc-1/file"
        initialLocation={{ page: 1 }}
        onLocationChange={onLocationChange}
        pages={[...pages]}
      />,
    );

    const firstCard = screen.getByRole("button", {
      name: "第 1 页的段落 1",
    });
    const firstHighlight = await screen.findByRole("button", {
      name: "定位第 1 页的内容 1",
    });

    fireEvent.mouseEnter(firstCard);
    expect(firstCard).toHaveClass("is-hovered");
    expect(firstHighlight).toHaveClass("is-hovered");
    fireEvent.mouseLeave(firstCard);
    expect(firstCard).not.toHaveClass("is-hovered");
    expect(firstHighlight).not.toHaveClass("is-hovered");

    fireEvent.mouseEnter(firstHighlight);
    expect(firstHighlight).toHaveClass("is-hovered");
    expect(firstCard).toHaveClass("is-hovered");
    fireEvent.mouseLeave(firstHighlight);

    await user.click(firstCard);
    expect(firstCard).toHaveAttribute("aria-pressed", "true");
    expect(firstCard).toHaveClass("is-selected");
    expect(firstHighlight).toHaveClass("is-selected");
    expect(onLocationChange).toHaveBeenLastCalledWith({
      page: 1,
      blockId: "block-1",
    });
  });

  it("opens the original only from the image area while card clicks only locate the PDF", async () => {
    const user = userEvent.setup();
    const onLocationChange = vi.fn();
    const { container } = render(
      <DocumentEvidenceViewer
        assets={{ "asset-image": imageAsset }}
        blocks={[imageBlock]}
        documentName="项目报告"
        fileUrl="/api/doc-image/file"
        initialLocation={{ page: 1 }}
        onLocationChange={onLocationChange}
        pages={[{ ...pages[0], block_count: 1 }]}
      />,
    );

    const card = screen.getByRole("button", { name: "第 1 页的图片 1" });
    const imageTrigger = screen.getByRole("button", {
      name: "查看图片原图：图 1 合成系统流程图",
    });
    const preview = container.querySelector<HTMLImageElement>(".image-asset-preview img");
    expect(preview).not.toBeNull();
    expect(preview).toHaveAttribute("loading", "lazy");
    expect(preview).toHaveAttribute("src", imageAsset.preview_url);
    expect(preview).toHaveAttribute("width", "640");
    expect(screen.getByText("图 1 合成系统流程图")).toBeVisible();

    await user.click(card);
    expect(onLocationChange).toHaveBeenLastCalledWith({ page: 1, blockId: "image-block" });
    expect(screen.queryByRole("dialog", { name: "图片" })).not.toBeInTheDocument();

    await user.click(imageTrigger);
    const dialog = screen.getByRole("dialog", { name: "图片" });
    expect(within(dialog).getByRole("img", { name: "图 1 合成系统流程图" })).toHaveAttribute(
      "src",
      imageAsset.asset_url,
    );
    expect(within(dialog).getByRole("button", { name: "关闭原图" })).toHaveFocus();

    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(document.querySelector(".image-asset-lightbox-backdrop")).toHaveClass("is-closing");
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "图片" })).not.toBeInTheDocument();
    });
    expect(imageTrigger).toHaveFocus();
  });

  it("renders one cross-page content card and selects every bbox for that logical Block", async () => {
    const user = userEvent.setup();
    const crossPageBlock: DocumentBlock = {
      ...blocks[0],
      id: "cross-page-block",
      text: "跨页段落完整正文",
      bbox: [20, 300, 180, 390],
      bbox_by_page: {
        "0": [[20, 300, 180, 390]],
        "1": [[20, 10, 180, 90]],
      },
      bbox_page_size_by_page: {
        "0": [200, 400],
        "1": [200, 400],
      },
    };
    render(
      <DocumentEvidenceViewer
        blocks={[crossPageBlock]}
        documentName="跨页文档"
        fileUrl="/api/cross-page/file"
        initialLocation={{ page: 1 }}
        pages={[
          { ...pages[0], block_count: 1 },
          { ...pages[1], block_count: 0 },
        ]}
      />,
    );

    expect(screen.getAllByRole("button", { name: "第 1 页的段落 1" })).toHaveLength(1);
    expect(screen.getByText("段落").closest(".block-card__type")?.querySelector("svg")).not.toBeNull();
    const firstBox = await screen.findByRole("button", { name: "定位第 1 页的内容 1" });
    const secondBox = await screen.findByRole("button", { name: "定位第 2 页的内容 1" });

    await user.click(screen.getByRole("button", { name: "第 1 页的段落 1" }));
    expect(firstBox).toHaveClass("is-selected");
    expect(secondBox).toHaveClass("is-selected");
  });

  it("falls back from a broken preview to the original and then keeps a captioned placeholder", () => {
    const { container } = render(
      <BlockContent asset={imageAsset} block={imageBlock} />,
    );

    fireEvent.error(container.querySelector(".image-asset-preview img") as HTMLImageElement);
    expect(container.querySelector(".image-asset-preview img")).toHaveAttribute(
      "src",
      imageAsset.asset_url,
    );
    fireEvent.error(container.querySelector(".image-asset-preview img") as HTMLImageElement);
    expect(screen.getByText("图片加载失败")).toBeVisible();
    expect(screen.getByText("图 1 合成系统流程图")).toBeVisible();
  });

  it("keeps the merged caption and PDF-locatable card when the image asset is unavailable", () => {
    render(
      <DocumentEvidenceViewer
        blocks={[imageBlock]}
        documentName="缺图报告"
        fileUrl="/api/doc-missing-image/file"
        initialLocation={{ page: 1 }}
        pages={[{ ...pages[0], block_count: 1 }]}
      />,
    );

    expect(screen.getByText("图片文件暂不可用")).toBeVisible();
    expect(screen.getByText("图 1 合成系统流程图")).toBeVisible();
    expect(screen.getByRole("button", { name: "第 1 页的图片 1" })).toBeVisible();
  });

  it("moves to a clicked content card page and centers it on the first original-page click", async () => {
    const user = userEvent.setup();
    const onLocationChange = vi.fn();
    render(
      <DocumentEvidenceViewer
        blocks={[...blocks]}
        documentName="欧盟收入不平等趋势"
        fileUrl="/api/doc-1/file"
        initialLocation={{ page: 1 }}
        onLocationChange={onLocationChange}
        pages={[...pages]}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: "第 2 页的公式 2" }),
    );

    expect(screen.getByLabelText("当前页码")).toHaveValue(2);
    const secondHighlight = await screen.findByRole("button", {
      name: "定位第 2 页的内容 2",
    });
    const blockList = screen.getByTestId("block-list");
    const secondCard = screen.getByRole("button", { name: "第 2 页的公式 2" });
    Object.defineProperty(blockList, "clientHeight", {
      configurable: true,
      value: 400,
    });
    vi.spyOn(blockList, "getBoundingClientRect").mockImplementation(() => ({
      bottom: 500,
      height: 400,
      left: 0,
      right: 400,
      top: 100,
      width: 400,
      x: 0,
      y: 100,
      toJSON: () => ({}),
    }));
    vi.spyOn(secondCard, "getBoundingClientRect").mockImplementation(() => ({
      bottom: 700 - blockList.scrollTop,
      height: 100,
      left: 0,
      right: 400,
      top: 600 - blockList.scrollTop,
      width: 400,
      x: 0,
      y: 600 - blockList.scrollTop,
      toJSON: () => ({}),
    }));
    await user.click(secondHighlight);

    await waitFor(() => expect(blockList.scrollTop).toBe(350));
    expect(onLocationChange).toHaveBeenLastCalledWith({
      page: 2,
      blockId: "block-2",
    });
  });

  it("centers a block supplied by the initial deep link without requiring a PDF click", async () => {
    render(
      <DocumentEvidenceViewer
        blocks={[...blocks]}
        documentName="欧盟收入不平等趋势"
        fileUrl="/api/doc-1/file"
        location={{ page: 2, blockId: "block-2" }}
        pages={[...pages]}
      />,
    );

    const blockList = screen.getByTestId("block-list");
    const secondCard = screen.getByRole("button", { name: "第 2 页的公式 2" });
    Object.defineProperty(blockList, "clientHeight", {
      configurable: true,
      value: 400,
    });
    vi.spyOn(blockList, "getBoundingClientRect").mockImplementation(() => ({
      bottom: 500,
      height: 400,
      left: 0,
      right: 400,
      top: 100,
      width: 400,
      x: 0,
      y: 100,
      toJSON: () => ({}),
    }));
    vi.spyOn(secondCard, "getBoundingClientRect").mockImplementation(() => ({
      bottom: 700 - blockList.scrollTop,
      height: 100,
      left: 0,
      right: 400,
      top: 600 - blockList.scrollTop,
      width: 400,
      x: 0,
      y: 600 - blockList.scrollTop,
      toJSON: () => ({}),
    }));

    await waitFor(() => expect(blockList.scrollTop).toBe(350));
    expect(secondCard).toHaveClass("is-selected");
  });

  it("keeps paging and zoom controls in bounds and fully renders content without a source position", async () => {
    const user = userEvent.setup();
    render(
      <DocumentEvidenceViewer
        blocks={[...blocks]}
        documentName="欧盟收入不平等趋势"
        fileUrl="/api/doc-1/file"
        initialLocation={{ page: 2 }}
        pages={[...pages]}
      />,
    );

    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "上一页" }));
    expect(screen.getByLabelText("当前页码")).toHaveValue(1);
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();

    for (let index = 0; index < 20; index += 1) {
      await user.click(screen.getByRole("button", { name: "缩小" }));
    }
    expect(screen.getByText("50%")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "缩小" })).toBeDisabled();

    await user.click(
      screen.getByRole("button", { name: "第 2 页的段落 3" }),
    );
    expect(screen.getByText("暂无对应原文位置")).toBeInTheDocument();
    expect(
      screen.getByText(
        "没有坐标的完整长段落，末尾内容也必须展示，不能因为卡片高度或行数限制而被截断。",
      ),
    ).toBeInTheDocument();
  });

  it("marks only declared reference content and skips PDF loading for snapshot-only sources", () => {
    render(
      <DocumentEvidenceViewer
        blocks={[...blocks]}
        documentName="已删除文档快照"
        fileUrl={null}
        highlightedBlockIds={["block-2"]}
        initialLocation={{ page: 2, blockId: "block-2" }}
        pages={[...pages]}
        pdfUnavailableMessage="实时 PDF 不可用，未发起文件请求。"
      />,
    );

    const firstCard = screen.getByRole("button", { name: "第 1 页的段落 1" });
    const secondCard = screen.getByRole("button", { name: "第 2 页的公式 2" });
    expect(firstCard).not.toHaveClass("is-supporting");
    expect(secondCard).toHaveClass("is-supporting");
    expect(secondCard).toHaveTextContent("引用依据");
    expect(screen.getByText("实时 PDF 不可用，未发起文件请求。")).toBeInTheDocument();
    expect(pdfMocks.getDocument).not.toHaveBeenCalled();
  });

  it("uses each complete content card as the selection control and keeps the PDF image separate", async () => {
    render(
      <DocumentEvidenceViewer
        blocks={[
          {
            ...blocks[0],
            block_type: "table",
            html: "<table><tbody><tr><td>可访问表格</td></tr></tbody></table>",
            text: null,
          },
        ]}
        documentName="可访问文档"
        fileUrl="/api/doc-accessible/file"
        initialLocation={{ page: 1 }}
        pages={[{ ...pages[0], block_count: 1 }]}
      />,
    );

    const card = screen.getByRole("button", { name: "第 1 页的表格 1" });
    expect(card.tagName).toBe("ARTICLE");
    expect(card).toHaveAttribute("tabindex", "0");
    expect(within(card).getByRole("table")).toHaveTextContent("可访问表格");

    const pdfImage = await screen.findByRole("img", {
      name: "可访问文档 PDF 第 1 页",
    });
    const highlightControl = await screen.findByRole("button", {
      name: "定位第 1 页的内容 1",
    });
    expect(pdfImage.contains(highlightControl)).toBe(false);
  });

  it("does not re-sanitize or re-render math across 100+ content items on hover and selection", async () => {
    const user = userEvent.setup();
    const manyBlocks = Array.from({ length: 120 }, (_, index): DocumentBlock => ({
      ...blocks[0],
      id: `many-block-${index}`,
      block_idx: index,
      block_type: index % 2 === 0 ? "table" : "formula",
      html: index % 2 === 0
        ? `<table><tbody><tr><td>表格 ${index}</td></tr></tbody></table>`
        : null,
      text: index % 2 === 0 ? null : `公式 ${index} $P_${index}=L_${index}$`,
      bbox: null,
      page_size: null,
    }));
    const sanitizeSpy = vi.spyOn(DOMPurify, "sanitize");
    const katexSpy = vi.spyOn(katex, "renderToString");

    render(
      <DocumentEvidenceViewer
        blocks={manyBlocks}
        documentName="大型研究资料"
        fileUrl="/api/doc-many-blocks/file"
        initialLocation={{ page: 1 }}
        pages={[{ ...pages[0], block_count: manyBlocks.length }]}
      />,
    );

    const firstControl = screen.getByRole("button", { name: "第 1 页的表格 1" });
    const initialSanitizeCalls = sanitizeSpy.mock.calls.length;
    const initialKatexCalls = katexSpy.mock.calls.length;
    expect(initialSanitizeCalls).toBeGreaterThanOrEqual(120);
    expect(initialKatexCalls).toBe(60);

    fireEvent.mouseEnter(firstControl);
    await waitFor(() => expect(firstControl).toHaveClass("is-hovered"));
    await user.click(firstControl);
    expect(firstControl).toHaveAttribute("aria-pressed", "true");

    expect(sanitizeSpy.mock.calls.length - initialSanitizeCalls).toBeLessThanOrEqual(2);
    expect(katexSpy.mock.calls.length - initialKatexCalls).toBeLessThanOrEqual(1);
  });

  it("keeps technical identifiers in data attributes without exposing implementation terms", async () => {
    const { container } = render(
      <DocumentEvidenceViewer
        blocks={[...blocks]}
        documentName="欧盟收入不平等趋势"
        fileUrl="/api/doc-1/file"
        initialLocation={{ page: 1 }}
        pages={[...pages]}
      />,
    );

    const card = screen.getByRole("button", { name: "第 1 页的段落 1" });
    const highlight = await screen.findByRole("button", {
      name: "定位第 1 页的内容 1",
    });
    for (const element of [card, highlight]) {
      expect(element).toHaveAttribute("data-content-id", "block-1");
      expect(element).toHaveAttribute("data-content-type", "paragraph");
      expect(element).toHaveAttribute("data-page", "1");
      expect(element).toHaveAttribute("data-document-id", "doc-1");
      expect(element).toHaveAttribute("data-coordinate-summary", "20,40,180,160");
    }

    const userFacingContent = [
      container.textContent ?? "",
      ...Array.from(container.querySelectorAll("[aria-label]"), (element) =>
        element.getAttribute("aria-label") ?? ""),
    ].join(" ");
    expect(userFacingContent).not.toMatch(
      /\b(?:Trace|Citation|Block|Evidence|bbox|supporting|manual_query|Embedding|MinerU|Provider)\b/i,
    );
  });
});

describe("natural content type labels", () => {
  it.each([
    ["title", "标题"],
    ["paragraph", "段落"],
    ["list", "列表"],
    ["index", "目录"],
    ["reference", "参考文献"],
    ["image", "图片"],
    ["chart", "图表"],
    ["table", "表格"],
    ["formula", "公式"],
    ["equation", "公式"],
    ["code", "代码"],
    ["algorithm", "算法"],
    ["footnote", "脚注"],
    ["content", "原文内容"],
    ["future-parser-type", "原文内容"],
  ])("maps %s to %s", (type, label) => {
    expect(blockTypeLabel(type)).toBe(label);
  });
});

describe("BlockContent", () => {
  it("uses a strict MinerU allowlist without external resources or UI-overriding styles", () => {
    const fetchSpy = vi.spyOn(window, "fetch");
    const { container } = render(
      <BlockContent
        block={{
          ...blocks[0],
          block_type: "table",
          text: null,
          html: [
            '<div style="position:fixed;inset:0;z-index:999999">覆盖内容</div>',
            '<a href="https://example.com/steal">外部链接</a>',
            '<img src="https://example.com/tracker.png" srcset="https://example.com/tracker-2x.png 2x">',
            '<form action="https://example.com/submit"><input name="secret"></form>',
            '<iframe src="https://example.com/frame"></iframe>',
            '<table onmouseover="alert(1)"><tbody><tr><td colspan="2" rowspan="3">安全表格</td></tr></tbody></table>',
            '<script>alert(1)</script>',
          ].join(""),
        }}
      />,
    );

    expect(screen.getByRole("table")).toHaveTextContent("安全表格");
    expect(screen.getByRole("cell")).toHaveAttribute("colspan", "2");
    expect(screen.getByRole("cell")).toHaveAttribute("rowspan", "3");
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("[style]")).toBeNull();
    expect(container.querySelector("a, img, form, input, iframe")).toBeNull();
    expect(container.querySelector("[src], [srcset], [href]")).toBeNull();
    expect(screen.getByRole("table")).not.toHaveAttribute("onmouseover");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("renders bare formula blocks and common inline or display LaTeX delimiters", () => {
    const { rerender } = render(
      <BlockContent
        block={{
          ...blocks[1],
          text: "P_t = L_t + \\epsilon_t",
        }}
      />,
    );
    expect(document.querySelector(".block-content--formula .katex-display")).not.toBeNull();

    rerender(
      <BlockContent
        block={{
          ...blocks[0],
          text: "模型满足 \\(y_t=x_t+\\epsilon_t\\)，并且 $R^2>0.9$。",
        }}
      />,
    );
    expect(document.querySelectorAll(".block-content__math--inline .katex")).toHaveLength(2);

    rerender(
      <BlockContent
        block={{
          ...blocks[0],
          text: "目标函数为 \\[L(s)=\\sum_i P(s_i)\\]，用于评估结果。",
        }}
      />,
    );
    expect(document.querySelector(".block-content__math--display .katex-display")).not.toBeNull();
  });

  it("falls back to the original source when LaTeX parsing fails", () => {
    render(
      <BlockContent
        block={{
          ...blocks[0],
          text: "失败公式 $\\notARealCommand{x}$ 保留原文",
        }}
      />,
    );
    expect(screen.getByText(/\$\\notARealCommand\{x\}\$/)).toBeInTheDocument();
  });
});

describe("PdfDocumentPane lifecycle", () => {
  it("renders nearby PDF pages in one vertically scrollable document surface", async () => {
    render(
      <PdfDocumentPane
        blocks={[...blocks]}
        currentPage={1}
        documentName="连续浏览文档"
        fileUrl="/api/doc-continuous/file"
        pageCount={2}
        zoom={1}
      />,
    );

    expect(screen.getByText("上下滚动浏览，点击高亮查看对应内容")).toBeVisible();
    expect(await screen.findByLabelText("连续浏览文档 PDF 第 1 页")).toBeInTheDocument();
    expect(screen.getByLabelText("连续浏览文档 PDF 第 2 页")).toBeInTheDocument();
    expect(document.querySelectorAll(".pdf-pages .pdf-page")).toHaveLength(2);
  });

  it("does not report a missing location before the PDF page finishes rendering", async () => {
    let finishRender!: () => void;
    const renderPromise = new Promise<void>((resolve) => {
      finishRender = resolve;
    });
    pdfMocks.getDocument.mockImplementationOnce(() => {
      const proxy = {
        numPages: 2,
        getPage: vi.fn(async () => ({
          getViewport: ({ scale }: { scale: number }) => ({
            width: 600 * scale,
            height: 800 * scale,
          }),
          render: vi.fn(() => ({
            promise: renderPromise,
            cancel: pdfMocks.renderCancel,
          })),
          cleanup: pdfMocks.pageCleanup,
        })),
      };
      pdfMocks.proxies.push(proxy);
      return pdfMocks.trackLoadingTask(Promise.resolve(proxy));
    });

    render(
      <PdfDocumentPane
        blocks={[...blocks]}
        currentPage={1}
        documentName="正在渲染的文档"
        fileUrl="/api/doc-rendering/file"
        pageCount={2}
        selectedBlockId="block-1"
        zoom={1}
      />,
    );

    await waitFor(() => expect(pdfMocks.proxies).toHaveLength(1));
    expect(screen.queryByText("无可定位区域")).not.toBeInTheDocument();
    expect(screen.queryByText(/缺少完整坐标/)).not.toBeInTheDocument();

    finishRender();
    expect(
      await screen.findByRole("button", { name: "定位第 1 页的内容 1" }),
    ).toBeInTheDocument();
  });

  it("uses the incoming page count while a replacement PDF is still loading", async () => {
    const { rerender } = render(
      <PdfDocumentPane
        blocks={[...blocks]}
        currentPage={1}
        documentName="文档一"
        fileUrl="/api/doc-1/file"
        pageCount={2}
        zoom={1}
      />,
    );

    await waitFor(() => expect(pdfMocks.proxies).toHaveLength(1));
    expect(screen.getByLabelText("当前页码")).toHaveAttribute("max", "2");

    const replacementProxy = {
      numPages: 5,
      getPage: vi.fn(async () => ({
        getViewport: ({ scale }: { scale: number }) => ({
          width: 600 * scale,
          height: 800 * scale,
        }),
        render: vi.fn(() => ({
          promise: Promise.resolve(),
          cancel: pdfMocks.renderCancel,
        })),
        cleanup: pdfMocks.pageCleanup,
      })),
    };
    let finishLoading!: (proxy: typeof replacementProxy) => void;
    const loadingPromise = new Promise<typeof replacementProxy>((resolve) => {
      finishLoading = resolve;
    });
    pdfMocks.getDocument.mockImplementationOnce(() =>
      pdfMocks.trackLoadingTask(loadingPromise),
    );

    rerender(
      <PdfDocumentPane
        blocks={[...blocks]}
        currentPage={1}
        documentName="文档二"
        fileUrl="/api/doc-2/file"
        pageCount={5}
        zoom={1}
      />,
    );

    expect(screen.getByLabelText("当前页码")).toHaveAttribute("max", "5");

    pdfMocks.proxies.push(replacementProxy);
    finishLoading(replacementProxy);
    await waitFor(() => expect(replacementProxy.getPage).toHaveBeenCalledWith(1));
  });

  it("clears the rendered Canvas immediately when the PDF becomes unavailable", async () => {
    const { rerender } = render(
      <PdfDocumentPane
        blocks={[...blocks]}
        currentPage={1}
        documentName="文档一"
        fileUrl="/api/doc-1/file"
        pageCount={2}
        zoom={1}
      />,
    );
    const canvas = await screen.findByRole("img", { name: "文档一 PDF 第 1 页" }) as HTMLCanvasElement;
    await waitFor(() => expect(canvas.width).toBeGreaterThan(0));
    expect(canvas.style.width).not.toBe("");

    rerender(
      <PdfDocumentPane
        blocks={[...blocks]}
        currentPage={1}
        documentName="文档一"
        fileUrl={null}
        pageCount={2}
        zoom={1}
      />,
    );

    expect(canvas.width).toBe(0);
    expect(canvas.height).toBe(0);
    expect(canvas.style.width).toBe("");
    expect(canvas.style.height).toBe("");
  });

  it("clears the previous Canvas while a replacement PDF is still loading", async () => {
    const { rerender } = render(
      <PdfDocumentPane
        blocks={[...blocks]}
        currentPage={1}
        documentName="文档一"
        fileUrl="/api/doc-1/file"
        pageCount={2}
        zoom={1}
      />,
    );
    const canvas = await screen.findByRole("img", { name: "文档一 PDF 第 1 页" }) as HTMLCanvasElement;
    await waitFor(() => expect(canvas.width).toBeGreaterThan(0));

    const replacementLoading = new Promise<never>(() => undefined);
    pdfMocks.getDocument.mockImplementationOnce(() =>
      pdfMocks.trackLoadingTask(replacementLoading),
    );
    rerender(
      <PdfDocumentPane
        blocks={[...blocks]}
        currentPage={1}
        documentName="文档二"
        fileUrl="/api/doc-2/file"
        pageCount={2}
        zoom={1}
      />,
    );

    expect(canvas.width).toBe(0);
    expect(canvas.height).toBe(0);
    expect(canvas.style.width).toBe("");
    expect(canvas.style.height).toBe("");
  });

  it("caps rendering and navigation at the actual PDF page count", async () => {
    const onPageChange = vi.fn();
    pdfMocks.getDocument.mockImplementationOnce(() => {
      const proxy = {
        numPages: 1,
        getPage: vi.fn(async () => ({
          getViewport: ({ scale }: { scale: number }) => ({
            width: 600 * scale,
            height: 800 * scale,
          }),
          render: vi.fn(() => ({
            promise: Promise.resolve(),
            cancel: pdfMocks.renderCancel,
          })),
          cleanup: pdfMocks.pageCleanup,
        })),
      };
      pdfMocks.proxies.push(proxy);
      return pdfMocks.trackLoadingTask(Promise.resolve(proxy));
    });

    render(
      <PdfDocumentPane
        blocks={[...blocks]}
        currentPage={2}
        documentName="页数不一致的文档"
        fileUrl="/api/doc-page-mismatch/file"
        onPageChange={onPageChange}
        pageCount={2}
        zoom={1}
      />,
    );

    await waitFor(() => expect(onPageChange).toHaveBeenCalledWith(1));
    expect(pdfMocks.proxies[0]?.getPage).toHaveBeenCalledWith(1);
    expect(pdfMocks.proxies[0]?.getPage).not.toHaveBeenCalledWith(2);
    expect(screen.getByLabelText("当前页码")).toHaveAttribute("max", "1");
  });

  it("refits and rerenders the page when the PDF panel width changes", async () => {
    let resizeCallback!: ResizeObserverCallback;
    const observe = vi.fn();
    const disconnect = vi.fn();
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(callback: ResizeObserverCallback) {
          resizeCallback = callback;
        }

        observe = observe;
        disconnect = disconnect;
      },
    );

    render(
      <PdfDocumentPane
        blocks={[...blocks]}
        currentPage={1}
        documentName="可响应尺寸的文档"
        fileUrl="/api/doc-resize/file"
        pageCount={2}
        zoom={1}
      />,
    );

    await waitFor(() => expect(pdfMocks.proxies).toHaveLength(1));
    const getPage = pdfMocks.proxies[0]?.getPage;
    await waitFor(() => expect(getPage).toHaveBeenCalledTimes(2));
    expect(observe).toHaveBeenCalled();

    resizeCallback(
      [{ contentRect: { width: 920 } } as ResizeObserverEntry],
      {} as ResizeObserver,
    );

    await waitFor(() => expect(getPage).toHaveBeenCalledTimes(4));
  });

  it("cancels active renders and destroys each loading task once on route or file changes", async () => {
    const { rerender, unmount } = render(
      <PdfDocumentPane
        blocks={[...blocks]}
        currentPage={1}
        documentName="文档一"
        fileUrl="/api/doc-1/file"
        pageCount={2}
        zoom={1}
      />,
    );

    await waitFor(() => expect(pdfMocks.proxies).toHaveLength(1));
    await waitFor(() => expect(screen.getByLabelText("文档一 PDF 第 1 页")).toBeInTheDocument());
    rerender(
      <PdfDocumentPane
        blocks={[...blocks]}
        currentPage={1}
        documentName="文档二"
        fileUrl="/api/doc-2/file"
        pageCount={2}
        zoom={1}
      />,
    );

    await waitFor(() => expect(pdfMocks.proxies).toHaveLength(2));
    expect(pdfMocks.renderCancel).toHaveBeenCalled();
    expect(pdfMocks.loadingTasks[0]?.destroy).toHaveBeenCalledTimes(1);

    unmount();
    expect(pdfMocks.loadingTasks[1]?.destroy).toHaveBeenCalledTimes(1);
  });

  it("destroys a still-loading task once when the viewer unmounts", async () => {
    const pendingPdf = new Promise<never>(() => undefined);
    pdfMocks.getDocument.mockImplementationOnce(() =>
      pdfMocks.trackLoadingTask(pendingPdf),
    );
    const { unmount } = render(
      <PdfDocumentPane
        blocks={[...blocks]}
        currentPage={1}
        documentName="尚未载入完成的文档"
        fileUrl="/api/doc-pending/file"
        pageCount={2}
        zoom={1}
      />,
    );

    await waitFor(() => expect(pdfMocks.loadingTasks).toHaveLength(1));
    unmount();
    expect(pdfMocks.loadingTasks[0]?.destroy).toHaveBeenCalledTimes(1);
  });
});
