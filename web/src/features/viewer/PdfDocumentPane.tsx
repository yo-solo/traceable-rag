import pdfWorkerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import type {
  PDFDocumentLoadingTask,
  PDFDocumentProxy,
  PDFPageProxy,
  RenderTask,
} from "pdfjs-dist";
import {
  ChevronLeft,
  ChevronRight,
  FileWarning,
  LocateFixed,
  Minus,
  Plus,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import type { DocumentBlock } from "../../api/types";
import { scaleAndClipBBox, type PageSize } from "./bbox";
import { blockLocations, type BlockLocation } from "./blockLocations";

type PdfDocumentPaneProps = {
  blocks: DocumentBlock[];
  currentPage: number;
  documentName: string;
  fileUrl: string | null;
  highlightedBlockIds?: readonly string[];
  pageCount: number;
  zoom: number;
  hoveredBlockId?: string | null;
  selectedBlockId?: string | null;
  onHover?: (blockId: string | null) => void;
  onPageChange?: (page: number) => void;
  onSelect?: (block: DocumentBlock) => void;
  onZoomChange?: (zoom: number) => void;
  unavailableMessage?: string;
};

type PageMetrics = {
  width: number;
  height: number;
};

const MIN_ZOOM = 0.5;
const MAX_ZOOM = 2;
const ZOOM_STEP = 0.25;
const ACTIVE_PAGE_RADIUS = 2;
const DEFAULT_PAGE_RATIO = 1.414;

function boundedPage(page: number, pageCount: number) {
  return Math.max(1, Math.min(pageCount, Math.trunc(page) || 1));
}

function boundedZoom(zoom: number) {
  return Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, Math.round(zoom * 100) / 100));
}

function ContinuousDocumentPage({
  active,
  locations,
  documentName,
  highlightedIds,
  hoveredBlockId,
  onHover,
  onMetrics,
  onSelect,
  pageNumber,
  pdf,
  registerOverlay,
  selectedBlockId,
  stageWidth,
  zoom,
}: {
  active: boolean;
  locations: Array<{ block: DocumentBlock; location: BlockLocation }>;
  documentName: string;
  highlightedIds: Set<string>;
  hoveredBlockId?: string | null;
  onHover?: (blockId: string | null) => void;
  onMetrics: (page: number, metrics: PageMetrics) => void;
  onSelect?: (block: DocumentBlock) => void;
  pageNumber: number;
  pdf: PDFDocumentProxy;
  registerOverlay: (
    blockId: string,
    pageNumber: number,
    element: HTMLButtonElement | null,
  ) => void;
  selectedBlockId?: string | null;
  stageWidth: number;
  zoom: number;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [renderedPageSize, setRenderedPageSize] = useState<PageSize | null>(null);
  const [renderError, setRenderError] = useState(false);

  useLayoutEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) {
      return;
    }
    canvas.width = 0;
    canvas.height = 0;
    canvas.style.width = "";
    canvas.style.height = "";
    if (!active) {
      setRenderedPageSize(null);
      setRenderError(false);
    }
    return () => {
      canvas.width = 0;
      canvas.height = 0;
      canvas.style.width = "";
      canvas.style.height = "";
    };
  }, [active, stageWidth, zoom]);

  useEffect(() => {
    if (!active || !canvasRef.current) {
      return;
    }
    let alive = true;
    let pageProxy: PDFPageProxy | null = null;
    let renderTask: RenderTask | null = null;
    setRenderedPageSize(null);
    setRenderError(false);

    void (async () => {
      try {
        const nextPage = await pdf.getPage(pageNumber);
        pageProxy = nextPage;
        if (!alive || !canvasRef.current) {
          nextPage.cleanup?.();
          return;
        }
        const baseViewport = nextPage.getViewport({ scale: 1 });
        const availableWidth = Math.max(240, stageWidth - 48);
        const fitScale = Math.min(1.5, Math.max(0.25, availableWidth / baseViewport.width));
        const fittedViewport = nextPage.getViewport({ scale: fitScale });
        onMetrics(pageNumber, { width: fittedViewport.width, height: fittedViewport.height });
        const viewport = nextPage.getViewport({ scale: fitScale * zoom });
        const canvas = canvasRef.current;
        const context = canvas.getContext("2d");
        if (!context) {
          throw new Error("浏览器没有提供 Canvas 2D 上下文");
        }
        const pixelRatio = Math.max(1, window.devicePixelRatio || 1);
        canvas.width = Math.ceil(viewport.width * pixelRatio);
        canvas.height = Math.ceil(viewport.height * pixelRatio);
        canvas.style.width = `${viewport.width}px`;
        canvas.style.height = `${viewport.height}px`;
        context.setTransform?.(pixelRatio, 0, 0, pixelRatio, 0, 0);
        renderTask = nextPage.render({ canvas, canvasContext: context, viewport });
        await renderTask.promise;
        if (alive) {
          setRenderedPageSize([viewport.width, viewport.height]);
        }
      } catch (error) {
        if (alive && !(error instanceof Error && error.name === "RenderingCancelledException")) {
          setRenderError(true);
        }
      }
    })();

    return () => {
      alive = false;
      renderTask?.cancel();
      pageProxy?.cleanup?.();
    };
  }, [active, onMetrics, pageNumber, pdf, stageWidth, zoom]);

  const positionedBlocks = useMemo(() => {
    if (!renderedPageSize) {
      return [];
    }
    return locations.flatMap(({ block, location }) => {
      const position = scaleAndClipBBox(location.bbox, location.pageSize, renderedPageSize);
      return position ? [{ block, location, position }] : [];
    });
  }, [locations, renderedPageSize]);

  return (
    <div className="pdf-page__surface">
      {active ? (
        <>
          <canvas
            aria-label={`${documentName} PDF 第 ${pageNumber} 页`}
            ref={canvasRef}
            role="img"
          />
          {renderError ? (
            <div className="pdf-page__error" role="alert">这一页暂时无法显示。</div>
          ) : null}
          <div className="pdf-overlay" aria-label={`第 ${pageNumber} 页定位区域`}>
            {positionedBlocks.map(({ block, location, position }, locationIndex) => (
              <button
                aria-label={`定位第 ${pageNumber} 页的内容 ${block.block_idx + 1}`}
                aria-pressed={selectedBlockId === block.id}
                className={[
                  "pdf-bbox",
                  hoveredBlockId === block.id ? "is-hovered" : "",
                  selectedBlockId === block.id ? "is-selected" : "",
                  highlightedIds.has(block.id) ? "is-supporting" : "",
                ].filter(Boolean).join(" ")}
                data-content-id={block.id}
                data-content-type={block.block_type}
                data-coordinate-summary={location.bbox.map((value) => Math.round(value)).join(",")}
                data-document-id={block.document_id}
                data-page={pageNumber}
                key={`${block.id}:${pageNumber}:${locationIndex}`}
                onClick={() => onSelect?.({
                  ...block,
                  page_idx: location.pageIdx,
                  human_page_number: pageNumber,
                  bbox: location.bbox,
                  page_size: location.pageSize,
                })}
                onMouseEnter={() => onHover?.(block.id)}
                onMouseLeave={() => onHover?.(null)}
                ref={(element) => registerOverlay(block.id, pageNumber, element)}
                style={position}
                type="button"
              >
                <span className="visually-hidden">{block.text ?? block.block_type}</span>
              </button>
            ))}
          </div>
        </>
      ) : (
        <div className="pdf-page__placeholder">第 {pageNumber} 页</div>
      )}
      <span className="pdf-page__number">{pageNumber}</span>
    </div>
  );
}

export function PdfDocumentPane({
  blocks,
  currentPage,
  documentName,
  fileUrl,
  highlightedBlockIds = [],
  pageCount,
  zoom,
  hoveredBlockId,
  selectedBlockId,
  onHover,
  onPageChange,
  onSelect,
  onZoomChange,
  unavailableMessage,
}: PdfDocumentPaneProps) {
  const pageStageRef = useRef<HTMLDivElement>(null);
  const pageRefs = useRef(new Map<number, HTMLDivElement>());
  const overlayRefs = useRef(new Map<string, Map<number, HTMLButtonElement>>());
  const scrollFrameRef = useRef<number | null>(null);
  const locateFrameRef = useRef<number | null>(null);
  const reportedPageRef = useRef<number | null>(null);
  const initialPageLocatedRef = useRef(false);
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [loadedPageCount, setLoadedPageCount] = useState<number | null>(null);
  const [stageWidth, setStageWidth] = useState(720);
  const [pageMetrics, setPageMetrics] = useState<Record<number, PageMetrics>>({});
  const [loadError, setLoadError] = useState<string | null>(null);
  const effectivePageCount = Math.max(1, loadedPageCount ?? pageCount);
  const page = boundedPage(currentPage, effectivePageCount);
  const pages = useMemo(
    () => Array.from({ length: effectivePageCount }, (_, index) => index + 1),
    [effectivePageCount],
  );
  const locationsByPage = useMemo(() => {
    const grouped = new Map<number, Array<{ block: DocumentBlock; location: BlockLocation }>>();
    blocks.forEach((block) => {
      blockLocations(block).forEach((location) => {
        const humanPage = location.pageIdx + 1;
        const current = grouped.get(humanPage) ?? [];
        current.push({ block, location });
        grouped.set(humanPage, current);
      });
    });
    return grouped;
  }, [blocks]);
  const highlightedIds = useMemo(
    () => new Set(highlightedBlockIds),
    [highlightedBlockIds],
  );

  useEffect(() => {
    let active = true;
    let loadingTask: PDFDocumentLoadingTask | null = null;
    let destroyPromise: Promise<void> | null = null;
    const destroyLoadingTask = () => {
      if (!loadingTask) {
        return null;
      }
      destroyPromise ??= loadingTask.destroy().catch(() => undefined);
      return destroyPromise;
    };
    setPdf(null);
    setLoadedPageCount(null);
    setPageMetrics({});
    setLoadError(null);
    initialPageLocatedRef.current = false;

    if (!fileUrl) {
      return () => {
        active = false;
      };
    }

    void (async () => {
      try {
        const pdfjs = await import("pdfjs-dist");
        if (!active) {
          return;
        }
        pdfjs.GlobalWorkerOptions.workerSrc = pdfWorkerUrl;
        loadingTask = pdfjs.getDocument({ url: fileUrl });
        const nextPdf = await loadingTask.promise;
        if (!active) {
          await destroyLoadingTask();
          return;
        }
        setLoadedPageCount(nextPdf.numPages);
        setPdf(nextPdf);
      } catch (error) {
        if (active) {
          void error;
          setLoadError("原始文档暂时无法载入。请确认文档仍然可用，然后重试。");
        }
      }
    })();

    return () => {
      active = false;
      void destroyLoadingTask();
    };
  }, [fileUrl]);

  useLayoutEffect(() => {
    const stage = pageStageRef.current;
    if (!stage) {
      return;
    }
    const updateWidth = (width: number) => {
      if (Number.isFinite(width) && width > 0) {
        setStageWidth(width);
      }
    };
    updateWidth(stage.clientWidth);
    if (typeof ResizeObserver === "undefined") {
      return;
    }
    const observer = new ResizeObserver((entries) => {
      updateWidth(entries[0]?.contentRect.width ?? stage.clientWidth);
    });
    observer.observe(stage);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (page !== currentPage) {
      reportedPageRef.current = page;
      onPageChange?.(page);
    }
  }, [currentPage, onPageChange, page]);

  const registerPage = useCallback((pageNumber: number, element: HTMLDivElement | null) => {
    if (element) {
      pageRefs.current.set(pageNumber, element);
    } else {
      pageRefs.current.delete(pageNumber);
    }
  }, []);

  const registerOverlay = useCallback((
    blockId: string,
    pageNumber: number,
    element: HTMLButtonElement | null,
  ) => {
    if (element) {
      const byPage = overlayRefs.current.get(blockId) ?? new Map<number, HTMLButtonElement>();
      byPage.set(pageNumber, element);
      overlayRefs.current.set(blockId, byPage);
    } else {
      const byPage = overlayRefs.current.get(blockId);
      byPage?.delete(pageNumber);
      if (byPage?.size === 0) {
        overlayRefs.current.delete(blockId);
      }
    }
  }, []);

  const locatePage = useCallback((pageNumber: number, behavior: ScrollBehavior = "smooth") => {
    const stage = pageStageRef.current;
    const element = pageRefs.current.get(boundedPage(pageNumber, effectivePageCount));
    if (!stage || !element) {
      return;
    }
    stage.scrollTo({
      top: Math.max(0, element.offsetTop - 12),
      behavior,
    });
  }, [effectivePageCount]);

  const navigateToPage = useCallback((pageNumber: number) => {
    const nextPage = boundedPage(pageNumber, effectivePageCount);
    reportedPageRef.current = nextPage;
    onPageChange?.(nextPage);
    window.requestAnimationFrame(() => locatePage(nextPage));
  }, [effectivePageCount, locatePage, onPageChange]);

  const locateSelectedBlock = useCallback(() => {
    if (!selectedBlockId) {
      return;
    }
    if (locateFrameRef.current !== null) {
      window.cancelAnimationFrame(locateFrameRef.current);
    }
    let frame = 0;
    const align = () => {
      const overlays = overlayRefs.current.get(selectedBlockId);
      const stage = pageStageRef.current;
      const selected = blocks.find((block) => block.id === selectedBlockId);
      const primaryPage = selected ? selected.page_idx + 1 : 1;
      const overlay = (
        overlays?.get(page) ??
        overlays?.get(primaryPage) ??
        overlays?.values().next().value ??
        null
      );
      const overlayPage = overlays
        ? Array.from(overlays.entries()).find(([, candidate]) => candidate === overlay)?.[0]
        : null;
      const pageElement = overlayPage ? pageRefs.current.get(overlayPage) : null;
      if (stage && overlay && pageElement) {
        const top = pageElement.offsetTop + overlay.offsetTop;
        const left = pageElement.offsetLeft + overlay.offsetLeft;
        stage.scrollTo({
          top: Math.max(0, top - (stage.clientHeight - overlay.offsetHeight) / 2),
          left: Math.max(0, left - (stage.clientWidth - overlay.offsetWidth) / 2),
          behavior: "auto",
        });
      }
      frame += 1;
      if (!overlay && frame < 18) {
        locateFrameRef.current = window.requestAnimationFrame(align);
      } else {
        locateFrameRef.current = null;
      }
    };
    locateFrameRef.current = window.requestAnimationFrame(align);
  }, [blocks, page, selectedBlockId]);

  useEffect(() => {
    if (!pdf) {
      return;
    }
    if (selectedBlockId) {
      locateSelectedBlock();
      return;
    }
    if (!initialPageLocatedRef.current || reportedPageRef.current !== page) {
      initialPageLocatedRef.current = true;
      reportedPageRef.current = page;
      window.requestAnimationFrame(() => locatePage(page, "auto"));
    }
  }, [locatePage, locateSelectedBlock, page, pdf, selectedBlockId]);

  const handleMetrics = useCallback((pageNumber: number, metrics: PageMetrics) => {
    setPageMetrics((current) => {
      const previous = current[pageNumber];
      if (previous?.width === metrics.width && previous.height === metrics.height) {
        return current;
      }
      return { ...current, [pageNumber]: metrics };
    });
  }, []);

  const updateCurrentPage = useCallback(() => {
    const stage = pageStageRef.current;
    if (!stage) {
      return;
    }
    const center = stage.scrollTop + stage.clientHeight / 2;
    let nextPage = page;
    let nearestDistance = Number.POSITIVE_INFINITY;
    pageRefs.current.forEach((element, pageNumber) => {
      const pageCenter = element.offsetTop + element.offsetHeight / 2;
      const distance = Math.abs(pageCenter - center);
      if (distance < nearestDistance) {
        nearestDistance = distance;
        nextPage = pageNumber;
      }
    });
    if (nextPage !== page) {
      reportedPageRef.current = nextPage;
      onPageChange?.(nextPage);
    }
  }, [onPageChange, page]);

  const handleScroll = useCallback(() => {
    if (scrollFrameRef.current !== null) {
      return;
    }
    scrollFrameRef.current = window.requestAnimationFrame(() => {
      scrollFrameRef.current = null;
      updateCurrentPage();
    });
  }, [updateCurrentPage]);

  useEffect(() => () => {
    if (scrollFrameRef.current !== null) {
      window.cancelAnimationFrame(scrollFrameRef.current);
    }
    if (locateFrameRef.current !== null) {
      window.cancelAnimationFrame(locateFrameRef.current);
    }
  }, []);

  const selectedBlock = selectedBlockId
    ? blocks.find((block) => block.id === selectedBlockId) ?? null
    : null;
  const selectedHasNoPosition = Boolean(
    selectedBlock && blockLocations(selectedBlock).length === 0,
  );
  const unlocatedCount = blocks.filter(
    (block) => block.page_idx === page - 1 && blockLocations(block).length === 0,
  ).length;
  const zoomPercent = Math.round(zoom * 100);
  const fallbackMetrics = pageMetrics[page];
  const estimatedWidth = fallbackMetrics?.width ?? Math.max(240, stageWidth - 48);
  const estimatedHeight = fallbackMetrics?.height ?? estimatedWidth * DEFAULT_PAGE_RATIO;

  return (
    <section className="viewer-panel viewer-panel--pdf" aria-labelledby="pdf-panel-title">
      <header className="viewer-panel__header viewer-panel__header--pdf">
        <div>
          <p>上下滚动浏览，点击高亮查看对应内容</p>
          <h2 id="pdf-panel-title">原始文档</h2>
        </div>
        <div className="pdf-toolbar" aria-label="PDF 查看控制" role="toolbar">
          <button
            aria-label="上一页"
            disabled={page <= 1}
            onClick={() => navigateToPage(page - 1)}
            type="button"
          >
            <ChevronLeft aria-hidden="true" size={17} />
          </button>
          <label className="pdf-toolbar__page">
            <span className="visually-hidden">当前页码</span>
            <input
              aria-label="当前页码"
              max={effectivePageCount}
              min={1}
              onChange={(event) => navigateToPage(Number(event.target.value))}
              type="number"
              value={page}
            />
            <span>/ {effectivePageCount}</span>
          </label>
          <button
            aria-label="下一页"
            disabled={page >= effectivePageCount}
            onClick={() => navigateToPage(page + 1)}
            type="button"
          >
            <ChevronRight aria-hidden="true" size={17} />
          </button>
          <span className="pdf-toolbar__divider" aria-hidden="true" />
          <button
            aria-label="缩小"
            disabled={zoom <= MIN_ZOOM}
            onClick={() => onZoomChange?.(boundedZoom(zoom - ZOOM_STEP))}
            type="button"
          >
            <Minus aria-hidden="true" size={16} />
          </button>
          <output aria-label="当前缩放比例" className="pdf-toolbar__zoom">
            {zoomPercent}%
          </output>
          <button
            aria-label="放大"
            disabled={zoom >= MAX_ZOOM}
            onClick={() => onZoomChange?.(boundedZoom(zoom + ZOOM_STEP))}
            type="button"
          >
            <Plus aria-hidden="true" size={16} />
          </button>
        </div>
      </header>

      <div className="pdf-stage pdf-stage--continuous" onScroll={handleScroll} ref={pageStageRef}>
        {loadError ? (
          <div className="viewer-inline-error" role="alert">
            <FileWarning aria-hidden="true" size={20} />
            <p>{loadError}</p>
          </div>
        ) : null}
        {!fileUrl ? (
          <div className="pdf-stage__unavailable" role="note">
            <FileWarning aria-hidden="true" size={24} />
            <div>
              <strong>原始文档暂不可用</strong>
              <p>{unavailableMessage ?? "该来源当前不可访问，仍可查看已经保存的引用内容。"}</p>
            </div>
          </div>
        ) : null}
        {fileUrl && !pdf && !loadError ? (
          <div className="pdf-stage__loading" role="status">正在载入原始文档…</div>
        ) : null}
        {pdf ? (
          <div className="pdf-pages">
            {pages.map((pageNumber) => {
              const metrics = pageMetrics[pageNumber];
              const width = metrics?.width ?? estimatedWidth;
              const height = metrics?.height ?? estimatedHeight;
              const active = Math.abs(pageNumber - page) <= ACTIVE_PAGE_RADIUS;
              return (
                <div
                  className="pdf-page"
                  data-page={pageNumber}
                  key={pageNumber}
                  ref={(element) => registerPage(pageNumber, element)}
                  style={{ width: width * zoom, minHeight: height * zoom }}
                >
                  <ContinuousDocumentPage
                    active={active}
                    locations={locationsByPage.get(pageNumber) ?? []}
                    documentName={documentName}
                    highlightedIds={highlightedIds}
                    hoveredBlockId={hoveredBlockId}
                    onHover={onHover}
                    onMetrics={handleMetrics}
                    onSelect={onSelect}
                    pageNumber={pageNumber}
                    pdf={pdf}
                    registerOverlay={registerOverlay}
                    selectedBlockId={selectedBlockId}
                    stageWidth={stageWidth}
                    zoom={zoom}
                  />
                </div>
              );
            })}
          </div>
        ) : null}
        {selectedHasNoPosition ? (
          <div className="pdf-location-notice" role="status">
            <LocateFixed aria-hidden="true" size={17} />
            <div>
              <strong>暂无对应原文位置</strong>
              <span>这项内容没有完整的页面位置信息。</span>
            </div>
          </div>
        ) : unlocatedCount > 0 ? (
          <p className="pdf-unlocated-note">
            本页有 {unlocatedCount} 项内容暂时无法在原文中标出位置。
          </p>
        ) : null}
      </div>
    </section>
  );
}
