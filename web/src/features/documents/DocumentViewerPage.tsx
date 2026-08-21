import { ArrowLeft, CalendarClock, FileCheck2, Layers3 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { ApiError, api } from "../../api/client";
import {
  requireDocumentBlocksResponse,
  requireDocumentResponse,
  requireKnowledgeBaseResponse,
} from "../../api/guards";
import type {
  DocumentBlocksResponse,
  KnowledgeBase,
  KnowledgeBaseDocument,
} from "../../api/types";
import { AsyncState } from "../../components/AsyncState";
import { DocumentEvidenceViewer } from "../viewer/DocumentEvidenceViewer";
import { blockAppearsOnPage } from "../viewer/blockLocations";
import {
  buildViewerSearch,
  parseViewerSearch,
  type ViewerDeepLink,
} from "../viewer/deepLink";

type ViewerPageData = {
  knowledgeBase: KnowledgeBase;
  document: KnowledgeBaseDocument;
  blockData: DocumentBlocksResponse;
};

function clampPage(page: number, pageCount: number) {
  return Math.max(1, Math.min(Math.max(1, pageCount), Math.trunc(page) || 1));
}

function loadErrorMessage(error: unknown) {
  if (error instanceof ApiError && error.status === 404) {
    return "文档或所属知识库不存在。下一步：返回知识库并选择一个仍然可用的文档。";
  }
  void error;
  return "暂时无法打开文档。请确认文档仍可使用，然后重试。";
}

export function DocumentViewerPage() {
  const { knowledgeBaseId = "", documentId = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const [data, setData] = useState<ViewerPageData | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [retryToken, setRetryToken] = useState(0);
  const requestEpochRef = useRef(0);
  const pageHeadingRef = useRef<HTMLHeadingElement>(null);
  const searchKey = searchParams.toString();
  const parsedLocation = useMemo(
    () => parseViewerSearch(searchKey ? `?${searchKey}` : ""),
    [searchKey],
  );

  useEffect(() => {
    const epoch = requestEpochRef.current + 1;
    requestEpochRef.current = epoch;
    const controller = new AbortController();
    setData(null);
    setLoading(true);
    setLoadError(null);

    void (async () => {
      try {
        const [rawKnowledgeBase, rawDocument, rawBlocks] = await Promise.all([
          api.get<unknown>(`/api/knowledge-bases/${knowledgeBaseId}`, {
            signal: controller.signal,
          }),
          api.get<unknown>(
            `/api/knowledge-bases/${knowledgeBaseId}/documents/${documentId}`,
            { signal: controller.signal },
          ),
          api.get<unknown>(
            `/api/knowledge-bases/${knowledgeBaseId}/documents/${documentId}/blocks`,
            { signal: controller.signal },
          ),
        ]);
        const knowledgeBase = requireKnowledgeBaseResponse(rawKnowledgeBase).knowledge_base;
        const document = requireDocumentResponse(rawDocument).document;
        const blockData = requireDocumentBlocksResponse(rawBlocks);
        if (
          document.knowledge_base_id !== knowledgeBaseId ||
          document.id !== documentId ||
          blockData.blocks.some((block) => block.document_id !== document.id)
        ) {
          throw new Error("服务返回了不属于当前知识库或文档的数据");
        }
        if (controller.signal.aborted || epoch !== requestEpochRef.current) {
          return;
        }
        setData({
          knowledgeBase,
          document,
          blockData,
        });
      } catch (error) {
        if (
          controller.signal.aborted ||
          (error instanceof DOMException && error.name === "AbortError") ||
          epoch !== requestEpochRef.current
        ) {
          return;
        }
        setLoadError(loadErrorMessage(error));
      } finally {
        if (!controller.signal.aborted && epoch === requestEpochRef.current) {
          setLoading(false);
        }
      }
    })();

    return () => {
      controller.abort();
      if (requestEpochRef.current === epoch) {
        requestEpochRef.current = epoch + 1;
      }
    };
  }, [documentId, knowledgeBaseId, retryToken]);

  useEffect(() => {
    if (data) {
      pageHeadingRef.current?.focus();
    }
  }, [data]);

  const effectiveLocation = useMemo<ViewerDeepLink>(() => {
    if (!data) {
      return parsedLocation;
    }
    const requestedBlockId = parsedLocation.blockId;
    const selectedBlock = requestedBlockId
      ? data.blockData.blocks.find((block) => block.id === requestedBlockId)
      : undefined;
    const requestedPage = clampPage(parsedLocation.page, data.document.page_count);
    return {
      page: selectedBlock
        ? (
          blockAppearsOnPage(selectedBlock, requestedPage)
            ? requestedPage
            : selectedBlock.human_page_number
        )
        : requestedPage,
      blockId: selectedBlock?.id,
    };
  }, [data, parsedLocation]);

  useEffect(() => {
    if (!data) {
      return;
    }
    const currentSearch = searchKey ? `?${searchKey}` : "";
    const normalizedSearch = buildViewerSearch(effectiveLocation, currentSearch);
    if (normalizedSearch !== currentSearch) {
      setSearchParams(new URLSearchParams(normalizedSearch), { replace: true });
    }
  }, [data, effectiveLocation, searchKey, setSearchParams]);

  function handleLocationChange(location: ViewerDeepLink) {
    const nextSearch = buildViewerSearch(location, searchKey ? `?${searchKey}` : "");
    setSearchParams(new URLSearchParams(nextSearch), { replace: true });
  }

  if (loading) {
    return (
      <section className="document-viewer-page">
        <AsyncState loading loadingLabel="正在载入原始文档与文档内容…">
          <span />
        </AsyncState>
      </section>
    );
  }

  if (loadError || !data) {
    return (
      <section className="document-viewer-error" aria-labelledby="viewer-error-title">
        <h1 id="viewer-error-title">无法打开文档</h1>
        <p>{loadError ?? "文档内容暂不可用。请返回知识库并重新选择文档。"}</p>
        <div className="document-viewer-error__actions">
          <button
            className="secondary-button"
            onClick={() => setRetryToken((value) => value + 1)}
            type="button"
          >
            重试
          </button>
          <Link className="ghost-button" to={`/knowledge-bases/${knowledgeBaseId}`}>
            返回知识库
          </Link>
        </div>
      </section>
    );
  }

  const { knowledgeBase, document, blockData } = data;

  return (
    <section className="feature-page document-viewer-page" aria-labelledby="document-viewer-title">
      <nav aria-label="面包屑" className="breadcrumb-navigation">
        <Link to="/knowledge-bases">知识库</Link>
        <span aria-hidden="true">/</span>
        <Link to={`/knowledge-bases/${knowledgeBase.id}`}>{knowledgeBase.name}</Link>
        <span aria-hidden="true">/</span>
        <span title={document.display_name}>{document.display_name}</span>
      </nav>

      <header className="document-viewer-heading">
        <div>
          <Link className="back-link" to={`/knowledge-bases/${knowledgeBase.id}`}>
            <ArrowLeft aria-hidden="true" size={15} />
            返回文档列表
          </Link>
          <h1
            id="document-viewer-title"
            ref={pageHeadingRef}
            tabIndex={-1}
            title={document.display_name}
          >
            {document.display_name}
          </h1>
          <p title={document.original_filename}>{document.original_filename}</p>
        </div>
        <dl className="document-viewer-summary">
          <div>
            <FileCheck2 aria-hidden="true" size={18} />
            <dt>页面</dt>
            <dd>{document.page_count}</dd>
          </div>
          <div>
            <Layers3 aria-hidden="true" size={18} />
            <dt>内容</dt>
            <dd>{blockData.blocks.length}</dd>
          </div>
          <div>
            <CalendarClock aria-hidden="true" size={18} />
            <dt>状态</dt>
            <dd>{document.processing_status === "ready" ? "可使用" : "已读取"}</dd>
          </div>
        </dl>
      </header>

      {blockData.blocks.length === 0 ? (
        <AsyncState
          empty
          emptyDescription="文档已存在，但暂时没有可查看的内容。请返回文档列表重新读取。"
          emptyTitle="没有可查看的内容"
        >
          <span />
        </AsyncState>
      ) : (
        <DocumentEvidenceViewer
          assets={blockData.assets}
          blocks={blockData.blocks}
          documentName={document.display_name}
          fileUrl={document.file_url}
          location={effectiveLocation}
          onLocationChange={handleLocationChange}
          pageCount={document.page_count}
          pages={blockData.pages}
        />
      )}
    </section>
  );
}
