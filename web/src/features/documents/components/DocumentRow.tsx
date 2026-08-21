import {
  FileText,
  LoaderCircle,
  Pencil,
  RefreshCw,
  RotateCcw,
  Trash2,
} from "lucide-react";
import { Link } from "react-router-dom";

import type {
  DocumentProcessingStatus,
  KnowledgeBaseDocument,
} from "../../../api/types";

type DocumentAction = "rename" | "delete" | "parse" | "index" | null;

type DocumentRowProps = {
  document: KnowledgeBaseDocument;
  busyAction?: DocumentAction;
  disabled?: boolean;
  onRename: (document: KnowledgeBaseDocument) => void;
  onDelete: (document: KnowledgeBaseDocument) => void;
  onParse: (document: KnowledgeBaseDocument) => void;
  onIndex: (document: KnowledgeBaseDocument) => void;
};

const STATUS_COPY: Record<
  DocumentProcessingStatus,
  { label: string; tone: "neutral" | "pending" | "active" | "success" | "danger" }
> = {
  uploading: { label: "上传中", tone: "active" },
  uploaded: { label: "等待读取", tone: "pending" },
  queued: { label: "等待处理", tone: "pending" },
  parsing: { label: "读取内容", tone: "active" },
  parsed: { label: "等待准备搜索", tone: "pending" },
  indexing: { label: "准备搜索", tone: "active" },
  ready: { label: "可使用", tone: "success" },
  failed: { label: "处理未完成", tone: "danger" },
};

const JOB_STAGE_LABELS: Record<string, string> = {
  queued: "等待处理",
  converting: "正在转换 PDF",
  page_check: "正在检查页数",
  mineru_submit: "正在准备读取内容",
  mineru_poll: "正在读取文档内容",
  mineru_download: "正在接收文档内容",
  normalizing: "正在整理文档内容",
  indexing: "正在准备搜索",
  persist: "正在保存文档内容",
  complete: "处理完成",
};

function formatUpdatedAt(value: string | null) {
  if (!value) {
    return "时间未知";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function fileExtension(filename: string) {
  const extension = filename.split(".").pop();
  return extension && extension !== filename ? extension.toUpperCase() : "文档";
}

export function DocumentRow({
  document,
  busyAction = null,
  disabled = false,
  onRename,
  onDelete,
  onParse,
  onIndex,
}: DocumentRowProps) {
  const status = STATUS_COPY[document.processing_status];
  const busy = disabled || busyAction !== null;
  const canOpen = ["parsed", "indexing", "ready"].includes(document.processing_status) || document.page_count > 0;
  const showParse = ["uploaded", "failed", "ready"].includes(document.processing_status);
  const showIndex = ["parsed", "failed", "ready"].includes(document.processing_status);
  const retryable = document.latest_job?.error?.retryable !== false;
  const parseLabel =
    document.processing_status === "uploaded"
      ? "读取内容"
      : document.processing_status === "ready"
        ? "重新读取"
        : "重试读取";
  const indexLabel =
    document.processing_status === "parsed"
      ? "准备搜索"
      : document.processing_status === "ready"
        ? "重新准备"
        : "重试准备";

  return (
    <tr className={`document-row document-row--${status.tone}`}>
      <td className="document-row__identity">
        <div className="document-file-icon" aria-hidden="true">
          <FileText size={21} strokeWidth={1.7} />
          <span>{fileExtension(document.original_filename)}</span>
        </div>
        <div className="document-row__name-block">
          {canOpen ? (
            <Link className="document-name-link" to={document.web_url}>
              {document.display_name}
            </Link>
          ) : (
            <strong className="document-name-static">{document.display_name}</strong>
          )}
          <span title={document.original_filename}>{document.original_filename}</span>
        </div>
      </td>

      <td>
        <span className={`status-label status-label--${status.tone}`}>
          {status.tone === "active" ? (
            <LoaderCircle className="status-label__spinner" aria-hidden="true" size={14} />
          ) : null}
          {status.label}
        </span>
        {document.latest_job && ["queued", "running"].includes(document.latest_job.status) ? (
          <p className="document-row__job-stage">
            {JOB_STAGE_LABELS[document.latest_job.stage] ?? "正在处理"}
            {document.latest_job.queue_position
              ? ` · 队列第 ${document.latest_job.queue_position} 位`
              : ""}
          </p>
        ) : null}
        {document.processing_status === "failed" ? (
          <p className="document-row__failure">
            {document.latest_job?.error?.message ??
              "文档处理未完成。请重试读取内容；如果问题持续存在，可在运行状态中查看服务情况。"}
          </p>
        ) : null}
      </td>

      <td className="document-row__metric">
        <strong>{document.page_count || "—"}</strong>
        <span>页</span>
      </td>
      <td className="document-row__metric">
        <strong>{document.stats.blocks}</strong>
        <span>项内容</span>
      </td>
      <td className="document-row__metric">
        <strong>{document.stats.chunks}</strong>
        <span>段可搜索内容</span>
      </td>
      <td className="document-row__updated">{formatUpdatedAt(document.updated_at)}</td>

      <td>
        <div className="document-row__actions">
          {showParse && retryable ? (
            <button
              aria-label={`${parseLabel}${document.display_name}`}
              className="row-action-button row-action-button--primary"
              disabled={busy}
              onClick={() => onParse(document)}
              title={parseLabel}
              type="button"
            >
              <RotateCcw aria-hidden="true" size={16} />
              <span>{busyAction === "parse" ? "处理中" : parseLabel}</span>
            </button>
          ) : null}
          {showIndex && retryable ? (
            <button
              aria-label={`${indexLabel}${document.display_name}`}
              className="row-action-button row-action-button--primary"
              disabled={busy}
              onClick={() => onIndex(document)}
              title={indexLabel}
              type="button"
            >
              <RefreshCw aria-hidden="true" size={16} />
              <span>{busyAction === "index" ? "准备中" : indexLabel}</span>
            </button>
          ) : null}
          <button
            aria-label={`重命名${document.display_name}`}
            className="icon-button"
            disabled={busy}
            onClick={() => onRename(document)}
            title="重命名"
            type="button"
          >
            <Pencil aria-hidden="true" size={16} />
          </button>
          <button
            aria-label={`删除${document.display_name}`}
            className="icon-button icon-button--danger"
            disabled={busy}
            onClick={() => onDelete(document)}
            title="删除文档"
            type="button"
          >
            <Trash2 aria-hidden="true" size={16} />
          </button>
        </div>
      </td>
    </tr>
  );
}
