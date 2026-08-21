import pdfWorkerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import type {
  PDFDocumentLoadingTask,
  PDFDocumentProxy,
  PDFPageProxy,
  RenderTask,
} from "pdfjs-dist";
import {
  Check,
  ChevronLeft,
  ChevronRight,
  Copy,
  Database,
  ExternalLink,
  FileWarning,
  Focus,
  Maximize2,
  Minus,
  MoreHorizontal,
  Plus,
  Sparkles,
  X,
} from "lucide-react";
import {
  type CSSProperties,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Link } from "react-router-dom";

import { scaleAndClipBBox, type PageSize } from "../viewer/bbox";

type TargetLocation = {
  humanPageNumber: number;
  bbox: [number, number, number, number] | null;
  pageSize: [number, number] | null;
};

type ContextLocation = TargetLocation & {
  blockType: string;
};

type ContinuousSourcePdfViewerProps = {
  documentName: string;
  fileUrl: string;
  insight: string | null;
  knowledgeBaseUrl: string;
  target: TargetLocation;
  contextLocations: ContextLocation[];
};

type PageMetrics = {
  width: number;
  height: number;
};

type TargetMetrics = {
  element: HTMLButtonElement;
  height: number;
};

const MIN_ZOOM = 0.5;
const MAX_ZOOM = 2;
const ZOOM_STEP = 0.25;
const DEFAULT_PAGE_RATIO = 1.414;
const ACTIVE_PAGE_RADIUS = 2;

function clamp(value: number, min: number, max: number) {
  return Math.max(min, Math.min(max, value));
}

function boundedPage(page: number, pageCount: number) {
  return clamp(Math.trunc(page) || 1, 1, Math.max(1, pageCount));
}

function boundedZoom(zoom: number) {
  return clamp(Math.round(zoom * 100) / 100, MIN_ZOOM, MAX_ZOOM);
}

function ContinuousPdfPage({
  active,
  contextLocations,
  documentName,
  pageNumber,
  pdf,
  stageWidth,
  target,
  zoom,
  onMetrics,
  onTargetReady,
  insight,
  insightMinimized,
  onInsightMinimizedChange,
  renderRevision,
}: {
  active: boolean;
  contextLocations: ContextLocation[];
  documentName: string;
  pageNumber: number;
  pdf: PDFDocumentProxy;
  stageWidth: number;
  target: TargetLocation;
  zoom: number;
  onMetrics: (page: number, metrics: PageMetrics) => void;
  onTargetReady: (metrics: TargetMetrics | null) => void;
  insight: string | null;
  insightMinimized: boolean;
  onInsightMinimizedChange: (value: boolean) => void;
  renderRevision: number;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [renderedSize, setRenderedSize] = useState<PageSize | null>(null);
  const [renderError, setRenderError] = useState(false);
  const isTargetPage = pageNumber === target.humanPageNumber;
  const targetPosition = useMemo(
    () => isTargetPage && renderedSize
      ? scaleAndClipBBox(target.bbox, target.pageSize, renderedSize)
      : null,
    [isTargetPage, renderedSize, target.bbox, target.pageSize],
  );
  const contextPositions = useMemo(() => {
    if (!renderedSize) {
      return [];
    }
    return contextLocations.flatMap((location, index) => {
      if (location.humanPageNumber !== pageNumber) {
        return [];
      }
      const position = scaleAndClipBBox(location.bbox, location.pageSize, renderedSize);
      return position ? [{ index, location, position }] : [];
    });
  }, [contextLocations, pageNumber, renderedSize]);

  useLayoutEffect(() => {
    if (!active) {
      setRenderedSize(null);
      setRenderError(false);
      return;
    }
    const canvas = canvasRef.current;
    if (!canvas) {
      return;
    }
    canvas.width = 0;
    canvas.height = 0;
    canvas.style.width = "";
    canvas.style.height = "";
  }, [active, renderRevision, stageWidth, zoom]);

  useEffect(() => {
    if (!active || !canvasRef.current) {
      return;
    }
    let alive = true;
    let pageProxy: PDFPageProxy | null = null;
    let renderTask: RenderTask | null = null;
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
        const availableWidth = Math.max(240, stageWidth - 40);
        const fitScale = clamp(availableWidth / baseViewport.width, 0.25, 1.5);
        const baseSize = nextPage.getViewport({ scale: fitScale });
        onMetrics(pageNumber, { width: baseSize.width, height: baseSize.height });
        const viewport = nextPage.getViewport({ scale: fitScale * zoom });
        const canvas = canvasRef.current;
        const context = canvas.getContext("2d");
        if (!context) {
          throw new Error("Canvas 2D context unavailable");
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
          setRenderedSize([viewport.width, viewport.height]);
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
  }, [active, onMetrics, pageNumber, pdf, renderRevision, stageWidth, zoom]);

  const registerTarget = useCallback((element: HTMLButtonElement | null) => {
    if (!isTargetPage) {
      return;
    }
    onTargetReady(element && targetPosition
      ? { element, height: targetPosition.height }
      : null);
  }, [isTargetPage, onTargetReady, targetPosition]);

  const insightStyle = useMemo<CSSProperties | undefined>(() => {
    if (!targetPosition || !renderedSize) {
      return undefined;
    }
    const roomAbove = targetPosition.top;
    const roomBelow = renderedSize[1] - targetPosition.top - targetPosition.height;
    const useAbove = roomAbove > roomBelow && roomAbove >= 112;
    const left = clamp(targetPosition.left, 12, Math.max(12, renderedSize[0] - 332));
    return useAbove
      ? { left, bottom: renderedSize[1] - targetPosition.top + 10 }
      : { left, top: targetPosition.top + targetPosition.height + 10 };
  }, [renderedSize, targetPosition]);

  return (
    <div className="source-pdf-page__surface">
      {active ? (
        <>
          <canvas aria-label={`${documentName} PDF 第 ${pageNumber} 页`} ref={canvasRef} role="img" />
          {renderError ? (
            <div className="source-pdf-page__error" role="alert">这一页暂时无法显示。</div>
          ) : null}
          {contextPositions.map(({ index, location, position }) => (
            <span
              aria-hidden="true"
              className="source-pdf-context-bbox"
              data-block-type={location.blockType}
              key={`${location.humanPageNumber}-${index}`}
              style={position}
            />
          ))}
          {isTargetPage && targetPosition ? (
            <button
              aria-label="引用原文高亮区域"
              className="source-pdf-highlight"
              ref={registerTarget}
              style={targetPosition}
              type="button"
            />
          ) : null}
          {isTargetPage && targetPosition && insightStyle ? (
            <InsightBubble
              insight={insight}
              insightStyle={insightStyle}
              minimized={insightMinimized}
              onMinimizedChange={onInsightMinimizedChange}
            />
          ) : null}
        </>
      ) : (
        <div className="source-pdf-page__placeholder">第 {pageNumber} 页</div>
      )}
      <span className="source-pdf-page__number">{pageNumber}</span>
    </div>
  );
}

function InsightBubble({
  insight,
  insightStyle,
  minimized,
  onMinimizedChange,
}: {
  insight: string | null;
  insightStyle: CSSProperties;
  minimized: boolean;
  onMinimizedChange: (value: boolean) => void;
}) {
  if (!insight) {
    return null;
  }
  if (minimized) {
    return (
      <button
        aria-label="展开关键提示"
        className="source-insight-marker"
        onClick={() => onMinimizedChange(false)}
        style={insightStyle}
        type="button"
      >
        <Sparkles aria-hidden="true" size={15} />
      </button>
    );
  }
  return (
    <aside className="source-insight-bubble" style={insightStyle}>
      <div>
        <Sparkles aria-hidden="true" size={15} />
        <strong>关键提示</strong>
      </div>
      <button aria-label="最小化关键提示" onClick={() => onMinimizedChange(true)} type="button">
        <X aria-hidden="true" size={15} />
      </button>
      <p>{insight}</p>
    </aside>
  );
}

export function ContinuousSourcePdfViewer({
  contextLocations,
  documentName,
  fileUrl,
  insight,
  knowledgeBaseUrl,
  target,
}: ContinuousSourcePdfViewerProps) {
  const stageRef = useRef<HTMLDivElement>(null);
  const pageRefs = useRef(new Map<number, HTMLDivElement>());
  const targetElementRef = useRef<HTMLButtonElement | null>(null);
  const scrollFrameRef = useRef<number | null>(null);
  const targetLocateFrameRef = useRef<number | null>(null);
  const targetLocateRequestRef = useRef(0);
  const autoFocusAppliedRef = useRef(false);
  const pendingTargetLocateRef = useRef(false);
  const pageInactiveRef = useRef(false);
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [pageCount, setPageCount] = useState(Math.max(1, target.humanPageNumber));
  const [currentPage, setCurrentPage] = useState(target.humanPageNumber);
  const [stageWidth, setStageWidth] = useState(720);
  const [zoom, setZoom] = useState(1);
  const [pageMetrics, setPageMetrics] = useState<Record<number, PageMetrics>>({});
  const [loadError, setLoadError] = useState(false);
  const [retryToken, setRetryToken] = useState(0);
  const [copied, setCopied] = useState(false);
  const [insightMinimized, setInsightMinimized] = useState(false);
  const [renderRevision, setRenderRevision] = useState(0);

  useEffect(() => {
    let alive = true;
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
    setLoadError(false);
    autoFocusAppliedRef.current = false;
    pendingTargetLocateRef.current = false;
    void (async () => {
      try {
        const pdfjs = await import("pdfjs-dist");
        if (!alive) {
          return;
        }
        pdfjs.GlobalWorkerOptions.workerSrc = pdfWorkerUrl;
        loadingTask = pdfjs.getDocument({ url: fileUrl });
        const loadedPdf = await loadingTask.promise;
        if (!alive) {
          await destroyLoadingTask();
          return;
        }
        setPageCount(loadedPdf.numPages);
        setPdf(loadedPdf);
      } catch {
        if (alive) {
          setLoadError(true);
        }
      }
    })();
    return () => {
      alive = false;
      void destroyLoadingTask();
    };
  }, [fileUrl, retryToken]);

  useLayoutEffect(() => {
    const stage = stageRef.current;
    if (!stage) {
      return;
    }
    const update = () => {
      setStageWidth(stage.clientWidth || 720);
    };
    update();
    if (typeof ResizeObserver === "undefined") {
      return;
    }
    const observer = new ResizeObserver(update);
    observer.observe(stage);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const markInactive = () => {
      pageInactiveRef.current = true;
    };
    const restoreVisiblePages = () => {
      if (document.visibilityState !== "visible") {
        markInactive();
        return;
      }
      if (!pageInactiveRef.current) {
        return;
      }
      pageInactiveRef.current = false;
      const stage = stageRef.current;
      if (stage) {
        setStageWidth(stage.clientWidth || 720);
      }
      setRenderRevision((value) => value + 1);
    };
    const handleVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        restoreVisiblePages();
      } else {
        markInactive();
      }
    };

    if (document.visibilityState !== "visible") {
      markInactive();
    }

    document.addEventListener("visibilitychange", handleVisibilityChange);
    window.addEventListener("blur", markInactive);
    window.addEventListener("focus", restoreVisiblePages);
    window.addEventListener("pagehide", markInactive);
    window.addEventListener("pageshow", restoreVisiblePages);
    return () => {
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      window.removeEventListener("blur", markInactive);
      window.removeEventListener("focus", restoreVisiblePages);
      window.removeEventListener("pagehide", markInactive);
      window.removeEventListener("pageshow", restoreVisiblePages);
    };
  }, []);

  const registerPage = useCallback((page: number, element: HTMLDivElement | null) => {
    if (element) {
      pageRefs.current.set(page, element);
    } else {
      pageRefs.current.delete(page);
    }
  }, []);

  const locatePage = useCallback((page: number, behavior: ScrollBehavior = "smooth") => {
    const stage = stageRef.current;
    const element = pageRefs.current.get(boundedPage(page, pageCount));
    if (!stage || !element) {
      return;
    }
    stage.scrollTo({
      top: Math.max(0, element.offsetTop - 12),
      behavior,
    });
  }, [pageCount]);

  const locateTarget = useCallback((behavior: ScrollBehavior = "smooth") => {
    const stage = stageRef.current;
    const targetElement = targetElementRef.current;
    const targetPage = pageRefs.current.get(target.humanPageNumber);
    if (!stage || !targetPage) {
      return;
    }
    const targetTop = targetElement
      ? targetPage.offsetTop + targetElement.offsetTop
      : targetPage.offsetTop;
    const targetHeight = targetElement?.offsetHeight ?? 0;
    const targetLeft = targetElement
      ? targetPage.offsetLeft + targetElement.offsetLeft
      : targetPage.offsetLeft;
    const targetWidth = targetElement?.offsetWidth ?? 0;
    stage.scrollTo({
      top: Math.max(0, targetTop - (stage.clientHeight - targetHeight) / 2),
      left: Math.max(0, targetLeft - (stage.clientWidth - targetWidth) / 2),
      behavior,
    });
  }, [target.humanPageNumber]);

  const locateTargetStably = useCallback(() => {
    const requestId = targetLocateRequestRef.current + 1;
    targetLocateRequestRef.current = requestId;
    if (targetLocateFrameRef.current !== null) {
      window.cancelAnimationFrame(targetLocateFrameRef.current);
    }
    let frame = 0;
    const align = () => {
      if (requestId !== targetLocateRequestRef.current) {
        return;
      }
      locateTarget("auto");
      frame += 1;
      if (frame < 18) {
        targetLocateFrameRef.current = window.requestAnimationFrame(align);
      } else {
        targetLocateFrameRef.current = null;
      }
    };
    targetLocateFrameRef.current = window.requestAnimationFrame(align);
  }, [locateTarget]);

  const handleTargetReady = useCallback((metrics: TargetMetrics | null) => {
    targetElementRef.current = metrics?.element ?? null;
    if (!metrics) {
      return;
    }
    if (pendingTargetLocateRef.current) {
      pendingTargetLocateRef.current = false;
      locateTargetStably();
      return;
    }
    if (autoFocusAppliedRef.current) {
      return;
    }
    autoFocusAppliedRef.current = true;
    locateTargetStably();
  }, [locateTargetStably]);

  const handleMetrics = useCallback((page: number, metrics: PageMetrics) => {
    setPageMetrics((current) => {
      const previous = current[page];
      if (previous?.width === metrics.width && previous.height === metrics.height) {
        return current;
      }
      return { ...current, [page]: metrics };
    });
    if (autoFocusAppliedRef.current && currentPage === target.humanPageNumber) {
      window.requestAnimationFrame(locateTargetStably);
    }
  }, [currentPage, locateTargetStably, target.humanPageNumber]);

  const updateCurrentPage = useCallback(() => {
    const stage = stageRef.current;
    if (!stage) {
      return;
    }
    const center = stage.scrollTop + stage.clientHeight / 2;
    let nextPage = currentPage;
    let nearestDistance = Number.POSITIVE_INFINITY;
    pageRefs.current.forEach((element, page) => {
      const pageCenter = element.offsetTop + element.offsetHeight / 2;
      const distance = Math.abs(pageCenter - center);
      if (distance < nearestDistance) {
        nearestDistance = distance;
        nextPage = page;
      }
    });
    if (nextPage !== currentPage) {
      setCurrentPage(nextPage);
    }
  }, [currentPage]);

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
    targetLocateRequestRef.current += 1;
    if (scrollFrameRef.current !== null) {
      window.cancelAnimationFrame(scrollFrameRef.current);
    }
    if (targetLocateFrameRef.current !== null) {
      window.cancelAnimationFrame(targetLocateFrameRef.current);
    }
  }, []);

  async function copyLink() {
    try {
      await navigator.clipboard.writeText(window.location.href);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1_500);
    } catch {
      setCopied(false);
    }
  }

  function fitToWidth() {
    if (zoom === 1) {
      locateTargetStably();
      return;
    }
    pendingTargetLocateRef.current = true;
    setZoom(1);
  }

  const pages = useMemo(
    () => Array.from({ length: pageCount }, (_, index) => index + 1),
    [pageCount],
  );
  const fallbackMetrics = pageMetrics[target.humanPageNumber];
  const estimatedWidth = fallbackMetrics?.width ?? Math.max(240, stageWidth - 40);
  const estimatedHeight = fallbackMetrics?.height ?? estimatedWidth * DEFAULT_PAGE_RATIO;
  return (
    <section className="source-workspace" aria-label="原文核验工作台">
      <header className="source-toolbar">
        <div className="source-toolbar__identity">
          <strong title={documentName}>{documentName}</strong>
          <span>第 {currentPage} 页</span>
        </div>
        <div className="source-toolbar__controls" role="toolbar" aria-label="PDF 查看控制">
          <button aria-label="上一页" data-tooltip="上一页" disabled={currentPage <= 1} onClick={() => locatePage(currentPage - 1)} type="button">
            <ChevronLeft aria-hidden="true" size={17} />
          </button>
          <label className="source-toolbar__page">
            <span className="visually-hidden">当前页码</span>
            <input
              aria-label="当前页码"
              max={pageCount}
              min={1}
              onChange={(event) => locatePage(Number(event.target.value))}
              type="number"
              value={currentPage}
            />
            <span>/ {pageCount}</span>
          </label>
          <button aria-label="下一页" data-tooltip="下一页" disabled={currentPage >= pageCount} onClick={() => locatePage(currentPage + 1)} type="button">
            <ChevronRight aria-hidden="true" size={17} />
          </button>
          <span className="source-toolbar__divider" aria-hidden="true" />
          <span className="source-toolbar__zoom-controls">
            <button aria-label="缩小" data-tooltip="缩小 PDF" disabled={zoom <= MIN_ZOOM} onClick={() => setZoom((value) => boundedZoom(value - ZOOM_STEP))} type="button">
              <Minus aria-hidden="true" size={16} />
            </button>
            <output aria-label="当前缩放比例">{Math.round(zoom * 100)}%</output>
            <button aria-label="放大" data-tooltip="放大 PDF" disabled={zoom >= MAX_ZOOM} onClick={() => setZoom((value) => boundedZoom(value + ZOOM_STEP))} type="button">
              <Plus aria-hidden="true" size={16} />
            </button>
          </span>
          <button aria-label="适合宽度" className="source-toolbar__labeled-control" data-tooltip="按窗口宽度适配 PDF" onClick={fitToWidth} type="button">
            <Maximize2 aria-hidden="true" size={16} />
            <span>适应</span>
          </button>
          <button aria-label="定位引用" className="source-toolbar__labeled-control" data-tooltip="定位并居中当前引用" onClick={locateTargetStably} type="button">
            <Focus aria-hidden="true" size={17} />
            <span>定位</span>
          </button>
        </div>
        <div className="source-toolbar__actions">
          <Link className="source-toolbar__knowledge-link" data-tooltip="打开文档的知识库详情页" to={knowledgeBaseUrl}>
            <Database aria-hidden="true" size={16} />
            <span>在知识库中查看</span>
          </Link>
          <details className="source-toolbar__menu">
            <summary aria-label="更多操作" data-tooltip="更多操作">
              <MoreHorizontal aria-hidden="true" size={19} />
            </summary>
            <div>
              <button onClick={() => void copyLink()} type="button">
                {copied ? <Check aria-hidden="true" size={16} /> : <Copy aria-hidden="true" size={16} />}
                {copied ? "已复制" : "复制引用链接"}
              </button>
              <a href={fileUrl} rel="noreferrer" target="_blank">
                <ExternalLink aria-hidden="true" size={16} />
                打开原始 PDF
              </a>
            </div>
          </details>
        </div>
      </header>

      <div className="source-pdf-stage" onScroll={handleScroll} ref={stageRef}>
        {loadError ? (
          <div className="source-pdf-failure" role="alert">
            <FileWarning aria-hidden="true" size={26} />
            <h1>无法载入原始 PDF</h1>
            <p>请重试，或前往知识库确认文档是否仍然可用。</p>
            <div>
              <button onClick={() => setRetryToken((value) => value + 1)} type="button">重试</button>
              <Link to={knowledgeBaseUrl}>在知识库中查看</Link>
            </div>
          </div>
        ) : !pdf ? (
          <div className="source-pdf-loading" role="status">正在载入原始 PDF…</div>
        ) : (
          <div className="source-pdf-pages">
            {pages.map((page) => {
              const metrics = pageMetrics[page];
              const width = metrics?.width ?? estimatedWidth;
              const height = metrics?.height ?? estimatedHeight;
              const active = Math.abs(page - currentPage) <= ACTIVE_PAGE_RADIUS || page === target.humanPageNumber;
              return (
                <div
                  className="source-pdf-page"
                  data-page={page}
                  key={page}
                  ref={(element) => registerPage(page, element)}
                  style={{ width: width * zoom, minHeight: height * zoom }}
                >
                  <ContinuousPdfPage
                    active={active}
                    contextLocations={contextLocations}
                    documentName={documentName}
                    insight={insight}
                    insightMinimized={insightMinimized}
                    onInsightMinimizedChange={setInsightMinimized}
                    onMetrics={handleMetrics}
                    onTargetReady={handleTargetReady}
                    pageNumber={page}
                    pdf={pdf}
                    renderRevision={renderRevision}
                    stageWidth={stageWidth}
                    target={target}
                    zoom={zoom}
                  />
                </div>
              );
            })}
          </div>
        )}
        {pdf && !target.bbox ? (
          <div className="source-location-failure" role="status">
            <Focus aria-hidden="true" size={17} />
            无法定位引用原文：该来源没有可靠的页面坐标。
          </div>
        ) : null}
      </div>
    </section>
  );
}
