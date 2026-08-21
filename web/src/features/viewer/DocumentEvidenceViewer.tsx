import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type {
  DocumentBlock,
  DocumentPageMetadata,
  DocumentVisualAssetDetail,
} from "../../api/types";
import { BlockListPane } from "./BlockListPane";
import { blockAppearsOnPage } from "./blockLocations";
import type { ViewerDeepLink } from "./deepLink";
import { PdfDocumentPane } from "./PdfDocumentPane";

type DocumentEvidenceViewerProps = {
  assets?: Record<string, DocumentVisualAssetDetail>;
  blocks: DocumentBlock[];
  documentName: string;
  fileUrl: string | null;
  highlightedBlockIds?: readonly string[];
  pages: DocumentPageMetadata[];
  pageCount?: number;
  pdfUnavailableMessage?: string;
  initialLocation?: ViewerDeepLink;
  location?: ViewerDeepLink;
  onLocationChange?: (location: ViewerDeepLink) => void;
};

function clampPage(page: number, pageCount: number) {
  return Math.max(1, Math.min(pageCount, Math.trunc(page) || 1));
}

const BLOCK_SCROLL_MAX_FRAMES = 12;
const BLOCK_SCROLL_STABLE_FRAMES = 2;
const BLOCK_SCROLL_TOLERANCE = 1;

export function DocumentEvidenceViewer({
  assets = {},
  blocks,
  documentName,
  fileUrl,
  highlightedBlockIds = [],
  pages,
  pageCount: providedPageCount,
  pdfUnavailableMessage,
  initialLocation = { page: 1 },
  location,
  onLocationChange,
}: DocumentEvidenceViewerProps) {
  const pageCount = useMemo(
    () => Math.max(
      1,
      providedPageCount ?? 0,
      ...pages.map((page) => page.human_page_number),
    ),
    [pages, providedPageCount],
  );
  const startingLocation = location ?? initialLocation;
  const [currentPage, setCurrentPage] = useState(() =>
    clampPage(startingLocation.page, pageCount),
  );
  const [zoom, setZoom] = useState(1);
  const [hoveredBlockId, setHoveredBlockId] = useState<string | null>(null);
  const [selectedBlockId, setSelectedBlockId] = useState<string | null>(
    startingLocation.blockId ?? null,
  );
  const blockRefs = useRef(new Map<string, HTMLElement>());
  const blockScrollFrameRef = useRef<number | null>(null);
  const blockScrollRequestRef = useRef(0);

  const scrollBlockIntoView = useCallback((blockId: string) => {
    const requestId = blockScrollRequestRef.current + 1;
    blockScrollRequestRef.current = requestId;
    if (blockScrollFrameRef.current !== null) {
      window.cancelAnimationFrame(blockScrollFrameRef.current);
    }

    let frameCount = 0;
    let stableFrameCount = 0;
    const align = () => {
      if (requestId !== blockScrollRequestRef.current) {
        return;
      }
      frameCount += 1;
      const block = blockRefs.current.get(blockId);
      const list = block?.closest<HTMLElement>(".block-list");
      if (block && list) {
        const blockRect = block.getBoundingClientRect();
        const listRect = list.getBoundingClientRect();
        const delta =
          blockRect.top -
          listRect.top -
          (list.clientHeight - blockRect.height) / 2;
        if (Math.abs(delta) > BLOCK_SCROLL_TOLERANCE) {
          list.scrollTop += delta;
          stableFrameCount = 0;
        } else {
          stableFrameCount += 1;
        }
      }

      if (
        frameCount < BLOCK_SCROLL_MAX_FRAMES &&
        stableFrameCount < BLOCK_SCROLL_STABLE_FRAMES
      ) {
        blockScrollFrameRef.current = window.requestAnimationFrame(align);
      } else {
        blockScrollFrameRef.current = null;
      }
    };

    blockScrollFrameRef.current = window.requestAnimationFrame(align);
  }, []);

  useEffect(() => {
    if (!location) {
      return;
    }
    setCurrentPage(clampPage(location.page, pageCount));
    setSelectedBlockId(
      location.blockId && blocks.some((block) => block.id === location.blockId)
        ? location.blockId
        : null,
    );
  }, [blocks, location, pageCount]);

  useEffect(() => {
    setCurrentPage((page) => clampPage(page, pageCount));
  }, [pageCount]);

  useEffect(() => {
    if (selectedBlockId) {
      scrollBlockIntoView(selectedBlockId);
    }
  }, [scrollBlockIntoView, selectedBlockId]);

  useEffect(() => () => {
    blockScrollRequestRef.current += 1;
    if (blockScrollFrameRef.current !== null) {
      window.cancelAnimationFrame(blockScrollFrameRef.current);
    }
  }, []);

  const publishLocation = useCallback((page: number, blockId: string | null) => {
    onLocationChange?.({
      page,
      blockId: blockId ?? undefined,
    });
  }, [onLocationChange]);

  const handlePageChange = useCallback((nextPage: number) => {
    const page = clampPage(nextPage, pageCount);
    const selected = selectedBlockId
      ? blocks.find((block) => block.id === selectedBlockId)
      : null;
    const nextSelectedBlockId =
      selected && blockAppearsOnPage(selected, page) ? selected.id : null;
    setCurrentPage(page);
    setSelectedBlockId(nextSelectedBlockId);
    publishLocation(page, nextSelectedBlockId);
  }, [blocks, pageCount, publishLocation, selectedBlockId]);

  const handleSelectBlock = useCallback((block: DocumentBlock, scrollBlock: boolean) => {
    const page = clampPage(block.page_idx + 1, pageCount);
    setCurrentPage(page);
    setSelectedBlockId(block.id);
    publishLocation(page, block.id);
    if (scrollBlock) {
      scrollBlockIntoView(block.id);
    }
  }, [pageCount, publishLocation, scrollBlockIntoView]);

  const handlePdfSelect = useCallback(
    (block: DocumentBlock) => handleSelectBlock(block, true),
    [handleSelectBlock],
  );
  const handleBlockSelect = useCallback(
    (block: DocumentBlock) => handleSelectBlock(block, false),
    [handleSelectBlock],
  );
  const registerBlockRef = useCallback((blockId: string, element: HTMLElement | null) => {
    if (element) {
      blockRefs.current.set(blockId, element);
    } else {
      blockRefs.current.delete(blockId);
    }
  }, []);

  return (
    <div className="document-evidence-viewer">
      <PdfDocumentPane
        blocks={blocks}
        currentPage={currentPage}
        documentName={documentName}
        fileUrl={fileUrl}
        highlightedBlockIds={highlightedBlockIds}
        hoveredBlockId={hoveredBlockId}
        onHover={setHoveredBlockId}
        onPageChange={handlePageChange}
        onSelect={handlePdfSelect}
        onZoomChange={setZoom}
        pageCount={pageCount}
        unavailableMessage={pdfUnavailableMessage}
        selectedBlockId={selectedBlockId}
        zoom={zoom}
      />
      <BlockListPane
        assets={assets}
        blocks={blocks}
        highlightedBlockIds={highlightedBlockIds}
        hoveredBlockId={hoveredBlockId}
        onHover={setHoveredBlockId}
        onSelect={handleBlockSelect}
        registerBlockRef={registerBlockRef}
        selectedBlockId={selectedBlockId}
      />
    </div>
  );
}
