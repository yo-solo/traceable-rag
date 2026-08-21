import { ShieldCheck } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";

import { ApiError, api } from "../../api/client";
import { requireSourceContextResponse } from "../../api/guards";
import type { SourceContextResponse } from "../../api/types";
import { AsyncState } from "../../components/AsyncState";
import { ImageAssetLightbox, ImageAssetPreview } from "../viewer/ImageAssetPreview";
import { ContinuousSourcePdfViewer } from "./ContinuousSourcePdfViewer";
import { parseSourceInsight } from "./sourceInsight";

function sourceErrorMessage(error: unknown) {
  if (error instanceof ApiError && error.status === 410) {
    return "这条来源已失效：原文可能已被重新处理或删除。为避免误引，系统不会把它跳转到新内容。";
  }
  if (error instanceof ApiError && error.status === 404) {
    return "没有找到这条来源。请检查链接是否完整，或重新执行检索。";
  }
  return "暂时无法载入来源。请确认服务可用后重试。";
}

export function SourceVerificationPage() {
  const { sourceId = "" } = useParams();
  const { hash } = useLocation();
  const [data, setData] = useState<SourceContextResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retryToken, setRetryToken] = useState(0);
  const [assetOpen, setAssetOpen] = useState(false);
  const requestEpoch = useRef(0);
  const insight = parseSourceInsight(hash);

  useEffect(() => {
    window.scrollTo(0, 0);
    setAssetOpen(false);
  }, [sourceId]);

  useEffect(() => {
    const controller = new AbortController();
    const epoch = ++requestEpoch.current;
    setLoading(true);
    setError(null);
    setData(null);
    void api.get<unknown>(
      `/api/sources/${encodeURIComponent(sourceId)}/context?before=5&after=5`,
      { signal: controller.signal },
    )
      .then((value) => {
        if (controller.signal.aborted || epoch !== requestEpoch.current) {
          return;
        }
        setData(requireSourceContextResponse(value));
      })
      .catch((loadError: unknown) => {
        if (!controller.signal.aborted && epoch === requestEpoch.current) {
          setError(sourceErrorMessage(loadError));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted && epoch === requestEpoch.current) {
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [retryToken, sourceId]);

  if (loading) {
    return (
      <main className="source-route-state">
        <AsyncState loading loadingLabel="正在载入原始 PDF 与引用定位…">
          <span />
        </AsyncState>
      </main>
    );
  }

  if (error || !data) {
    return (
      <main className="source-route-state" aria-labelledby="source-error-title">
        <div className="source-route-error" role="alert">
          <ShieldCheck aria-hidden="true" size={26} />
          <div>
            <h1 id="source-error-title">无法核验这条来源</h1>
            <p>{error ?? "来源内容暂不可用。"}</p>
            <div>
              <button onClick={() => setRetryToken((value) => value + 1)} type="button">
                重试
              </button>
              <Link to="/knowledge-bases">前往知识库</Link>
            </div>
          </div>
        </div>
      </main>
    );
  }

  const { source } = data;
  const knowledgeBaseUrl = [
    `/knowledge-bases/${encodeURIComponent(source.knowledge_base_id)}`,
    `/documents/${encodeURIComponent(source.document_id)}`,
    `?page=${source.human_page_number}`,
  ].join("");
  const assetLabel = source.asset?.modality === "chart" ? "图表" : "图片";
  const assetCaption = source.asset?.caption || source.text;

  return (
    <main className={`source-route-page${source.asset ? " source-route-page--visual" : ""}`}>
      <ContinuousSourcePdfViewer
        contextLocations={[...data.before, ...data.after].map((block) => ({
          humanPageNumber: block.human_page_number,
          bbox: block.bbox,
          pageSize: block.page_size,
          blockType: block.block_type,
        }))}
        documentName={source.document_name}
        fileUrl={source.file_url}
        insight={insight}
        knowledgeBaseUrl={knowledgeBaseUrl}
        target={{
          humanPageNumber: source.human_page_number,
          bbox: source.bbox,
          pageSize: source.page_size,
        }}
      />
      {source.asset ? (
        <aside className="source-visual-asset-panel" aria-labelledby="source-visual-asset-title">
          <header>
            <span>引用{assetLabel}</span>
            <h1 id="source-visual-asset-title">图片与原始 PDF 对照核验</h1>
          </header>
          <ImageAssetPreview
            asset={source.asset}
            assetLabel={assetLabel}
            caption={assetCaption}
            onOpen={() => setAssetOpen(true)}
          />
          <p className="source-visual-asset-panel__note">
            图题用于判断相关性；图内结构、数值和趋势请以原图与 PDF 高亮区域为准。
          </p>
        </aside>
      ) : null}
      {assetOpen && source.asset ? (
        <ImageAssetLightbox
          asset={source.asset}
          assetLabel={assetLabel}
          caption={assetCaption}
          onClose={() => setAssetOpen(false)}
        />
      ) : null}
    </main>
  );
}
