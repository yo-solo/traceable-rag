import {
  AlertCircle,
  BookOpen,
  CheckCircle2,
  Clock3,
  MoreHorizontal,
  Pencil,
  Plus,
  Trash2,
} from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, api } from "../../api/client";
import {
  requireKnowledgeBaseListResponse,
  requireKnowledgeBaseResponse,
} from "../../api/guards";
import type { KnowledgeBaseSummary } from "../../api/types";
import { AsyncState } from "../../components/AsyncState";
import { usePolling } from "../documents/useDocumentPolling";
import {
  KnowledgeBaseDeleteDialog,
  KnowledgeBaseEditorDialog,
} from "./components/KnowledgeBaseDialog";

type ActiveDialog =
  | { type: "create" }
  | { type: "rename"; summary: KnowledgeBaseSummary }
  | { type: "delete"; summary: KnowledgeBaseSummary; requiresNameConfirmation?: boolean }
  | null;

function formatUpdatedAt(value: string | null) {
  if (!value) {
    return "更新时间未知";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return `更新于 ${value}`;
  }
  return `更新于 ${new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date)}`;
}

function summaryStatus(summary: KnowledgeBaseSummary) {
  if (summary.failed_document_count > 0) {
    return { label: "存在失败文档", tone: "danger" as const, icon: AlertCircle };
  }
  if (summary.processing_document_count > 0) {
    return { label: "处理中", tone: "active" as const, icon: Clock3 };
  }
  return { label: "正常", tone: "success" as const, icon: CheckCircle2 };
}

function mutationErrorMessage(error: unknown, action: string) {
  void error;
  return `${action}未完成。请检查名称是否重复，稍后再试。`;
}

export function KnowledgeBaseListPage() {
  const [summaries, setSummaries] = useState<KnowledgeBaseSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [activeMenuId, setActiveMenuId] = useState<string | null>(null);
  const [activeDialog, setActiveDialog] = useState<ActiveDialog>(null);
  const [dialogBusy, setDialogBusy] = useState(false);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const pageControllerRef = useRef<AbortController | null>(null);
  const pageEpochRef = useRef(0);
  const requestSequenceRef = useRef(0);
  const pageHeadingRef = useRef<HTMLHeadingElement>(null);

  const loadKnowledgeBases = useCallback(async (signal?: AbortSignal, epoch?: number) => {
    const requestSequence = ++requestSequenceRef.current;
    setLoading(true);
    setLoadError(null);
    try {
      const response = requireKnowledgeBaseListResponse(
        await api.get<unknown>("/api/knowledge-bases", {
          signal,
        }),
      );
      if (
        requestSequence === requestSequenceRef.current &&
        (epoch === undefined || epoch === pageEpochRef.current) &&
        !signal?.aborted
      ) {
        setSummaries(response.knowledge_bases);
      }
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        return;
      }
      if (epoch === undefined || epoch === pageEpochRef.current) {
        setLoadError("暂时无法读取知识库，请稍后重试。");
      }
    } finally {
      if (epoch === undefined || epoch === pageEpochRef.current) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    const epoch = pageEpochRef.current + 1;
    pageEpochRef.current = epoch;
    const controller = new AbortController();
    pageControllerRef.current = controller;
    setSummaries([]);
    void loadKnowledgeBases(controller.signal, epoch);
    return () => {
      controller.abort();
      if (pageEpochRef.current === epoch) {
        pageEpochRef.current = epoch + 1;
      }
      if (pageControllerRef.current === controller) {
        pageControllerRef.current = null;
      }
    };
  }, [loadKnowledgeBases]);

  const refreshKnowledgeBases = useCallback(async () => {
    const controller = pageControllerRef.current;
    const epoch = pageEpochRef.current;
    if (!controller || controller.signal.aborted) {
      return;
    }
    const requestSequence = ++requestSequenceRef.current;
    const response = requireKnowledgeBaseListResponse(
      await api.get<unknown>("/api/knowledge-bases", { signal: controller.signal }),
    );
    if (
      epoch === pageEpochRef.current &&
      requestSequence === requestSequenceRef.current &&
      !controller.signal.aborted
    ) {
      setSummaries(response.knowledge_bases);
    }
  }, []);

  usePolling(
    !dialogBusy && summaries.some((summary) => summary.processing_document_count > 0),
    refreshKnowledgeBases,
  );

  function openDialog(dialog: ActiveDialog) {
    setDialogError(null);
    setActiveDialog(dialog);
  }

  function currentScope() {
    const controller = pageControllerRef.current;
    return controller && !controller.signal.aborted
      ? { controller, epoch: pageEpochRef.current }
      : null;
  }

  async function handleCreate(value: { name: string; description: string }) {
    const scope = currentScope();
    if (!scope) {
      return;
    }
    const requestSequence = ++requestSequenceRef.current;
    setDialogBusy(true);
    setDialogError(null);
    try {
      const response = requireKnowledgeBaseResponse(
        await api.post<unknown>("/api/knowledge-bases", {
          body: value,
          signal: scope.controller.signal,
        }),
      );
      if (scope.epoch === pageEpochRef.current && requestSequence === requestSequenceRef.current) {
        setSummaries((current) => [
          ...current,
          {
            ...response.knowledge_base,
            document_count: 0,
            ready_document_count: 0,
            processing_document_count: 0,
            failed_document_count: 0,
          },
        ]);
        setActiveDialog(null);
      }
    } catch (error) {
      if (!scope.controller.signal.aborted && scope.epoch === pageEpochRef.current) {
        setDialogError(mutationErrorMessage(error, "创建知识库"));
      }
    } finally {
      if (scope.epoch === pageEpochRef.current) {
        setDialogBusy(false);
      }
    }
  }

  async function handleRename(value: { name: string; description: string }) {
    if (!activeDialog || activeDialog.type !== "rename") {
      return;
    }
    const knowledgeBaseId = activeDialog.summary.id;
    const scope = currentScope();
    if (!scope) {
      return;
    }
    const requestSequence = ++requestSequenceRef.current;
    setDialogBusy(true);
    setDialogError(null);
    try {
      const response = requireKnowledgeBaseResponse(
        await api.patch<unknown>(`/api/knowledge-bases/${knowledgeBaseId}`, {
          body: value,
          signal: scope.controller.signal,
        }),
      );
      if (scope.epoch === pageEpochRef.current && requestSequence === requestSequenceRef.current) {
        setSummaries((current) =>
          current.map((summary) =>
            summary.id === knowledgeBaseId ? { ...summary, ...response.knowledge_base } : summary,
          ),
        );
        setActiveDialog(null);
        setActiveMenuId(null);
      }
    } catch (error) {
      if (!scope.controller.signal.aborted && scope.epoch === pageEpochRef.current) {
        setDialogError(mutationErrorMessage(error, "重命名知识库"));
      }
    } finally {
      if (scope.epoch === pageEpochRef.current) {
        setDialogBusy(false);
      }
    }
  }

  async function handleDelete(confirmName?: string) {
    if (!activeDialog || activeDialog.type !== "delete") {
      return;
    }
    const knowledgeBaseId = activeDialog.summary.id;
    const scope = currentScope();
    if (!scope) {
      return;
    }
    const requestSequence = ++requestSequenceRef.current;
    setDialogBusy(true);
    setDialogError(null);
    try {
      const response = await api.delete<unknown>(`/api/knowledge-bases/${knowledgeBaseId}`, {
        body: confirmName ? { confirm_name: confirmName } : undefined,
        signal: scope.controller.signal,
      });
      if (
        typeof response !== "object" ||
        response === null ||
        !("deleted" in response) ||
        response.deleted !== true
      ) {
        throw new Error("删除结果未确认");
      }
      if (scope.epoch === pageEpochRef.current && requestSequence === requestSequenceRef.current) {
        setSummaries((current) => current.filter((summary) => summary.id !== knowledgeBaseId));
        setActiveDialog(null);
        setActiveMenuId(null);
      }
    } catch (error) {
      if (!scope.controller.signal.aborted && scope.epoch === pageEpochRef.current) {
        if (
          error instanceof ApiError &&
          error.status === 409 &&
          error.code === "knowledge_base_delete_confirmation_required"
        ) {
          setActiveDialog((current) =>
            current?.type === "delete"
              ? { ...current, requiresNameConfirmation: true }
              : current,
          );
          setDialogError("请输入完整知识库名称以确认删除。");
        } else {
          setDialogError(mutationErrorMessage(error, "删除知识库"));
        }
      }
    } finally {
      if (scope.epoch === pageEpochRef.current) {
        setDialogBusy(false);
      }
    }
  }

  return (
    <section className="feature-page knowledge-base-list-page" aria-labelledby="knowledge-base-title">
      <nav aria-label="面包屑" className="breadcrumb-navigation">
        <span aria-current="page">知识库</span>
      </nav>

      <div className="feature-page__header">
        <div>
          <h1 id="knowledge-base-title" ref={pageHeadingRef} tabIndex={-1}>知识库</h1>
          <p>按主题集中管理文档，让查询始终在清晰、可信的资料范围内进行。</p>
        </div>
        <button
          className="primary-button primary-button--large"
          onClick={() => openDialog({ type: "create" })}
          type="button"
        >
          <Plus aria-hidden="true" size={18} />
          新建知识库
        </button>
      </div>

      <AsyncState
        empty={!loading && !loadError && summaries.length === 0}
        emptyAction={
          <button className="primary-button" onClick={() => openDialog({ type: "create" })} type="button">
            <Plus aria-hidden="true" size={17} />
            创建第一个知识库
          </button>
        }
        emptyDescription="下一步：创建一个知识库，再上传需要核验的原始文档。"
        emptyTitle="还没有知识库"
        error={loadError}
        loading={loading}
        loadingLabel="正在读取知识库"
        onRetry={() => {
          const scope = currentScope();
          if (scope) {
            void loadKnowledgeBases(scope.controller.signal, scope.epoch);
          }
        }}
      >
        <div className="knowledge-base-grid">
          {summaries.map((summary) => (
            <KnowledgeBaseCard
              activeMenu={activeMenuId === summary.id}
              key={summary.id}
              onDelete={() => openDialog({ type: "delete", summary })}
              onRename={() => openDialog({ type: "rename", summary })}
              onToggleMenu={() =>
                setActiveMenuId((current) =>
                  current === summary.id ? null : summary.id,
                )
              }
              summary={summary}
            />
          ))}
        </div>
      </AsyncState>

      {activeDialog?.type === "create" ? (
        <KnowledgeBaseEditorDialog
          busy={dialogBusy}
          error={dialogError}
          mode="create"
          onClose={() => setActiveDialog(null)}
          onSubmit={handleCreate}
          returnFocusFallbackRef={pageHeadingRef}
        />
      ) : null}
      {activeDialog?.type === "rename" ? (
        <KnowledgeBaseEditorDialog
          busy={dialogBusy}
          error={dialogError}
          knowledgeBase={activeDialog.summary}
          mode="rename"
          onClose={() => setActiveDialog(null)}
          onSubmit={handleRename}
          returnFocusFallbackRef={pageHeadingRef}
        />
      ) : null}
      {activeDialog?.type === "delete" ? (
        <KnowledgeBaseDeleteDialog
          busy={dialogBusy}
          documentCount={activeDialog.summary.document_count}
          error={dialogError}
          knowledgeBase={activeDialog.summary}
          onClose={() => setActiveDialog(null)}
          onConfirm={handleDelete}
          requireNameConfirmation={activeDialog.requiresNameConfirmation}
          returnFocusFallbackRef={pageHeadingRef}
        />
      ) : null}
    </section>
  );
}

type KnowledgeBaseCardProps = {
  summary: KnowledgeBaseSummary;
  activeMenu: boolean;
  onToggleMenu: () => void;
  onRename: () => void;
  onDelete: () => void;
};

function KnowledgeBaseCard({
  summary,
  activeMenu,
  onToggleMenu,
  onRename,
  onDelete,
}: KnowledgeBaseCardProps) {
  const titleId = useId();
  const status = summaryStatus(summary);
  const StatusIcon = status.icon;
  const readyCount = summary.ready_document_count;

  return (
    <article aria-labelledby={titleId} className="knowledge-base-card">
      <div className="knowledge-base-card__topline">
        <div className="knowledge-base-card__icon" aria-hidden="true">
          <BookOpen size={24} strokeWidth={1.6} />
        </div>
        <div className="knowledge-base-card__menu-wrap">
          <button
            aria-expanded={activeMenu}
            aria-haspopup="menu"
            aria-label={`管理${summary.name}`}
            className="icon-button"
            onClick={onToggleMenu}
            type="button"
          >
            <MoreHorizontal aria-hidden="true" size={19} />
          </button>
          {activeMenu ? (
            <div className="context-menu" role="menu">
              <button onClick={onRename} role="menuitem" type="button">
                <Pencil aria-hidden="true" size={15} />
                重命名
              </button>
              <button className="context-menu__danger" onClick={onDelete} role="menuitem" type="button">
                <Trash2 aria-hidden="true" size={15} />
                删除
              </button>
            </div>
          ) : null}
        </div>
      </div>

      <Link className="knowledge-base-card__main" to={`/knowledge-bases/${summary.id}`}>
        <div>
          <h2 id={titleId}>{summary.name}</h2>
          <p>{summary.description || "尚未添加说明"}</p>
        </div>
        <dl className="knowledge-base-card__stats">
          <div>
            <dt>文档</dt>
            <dd>{summary.document_count} 个文档</dd>
          </div>
          <div>
            <dt>可使用</dt>
            <dd>{readyCount} 个文档</dd>
          </div>
        </dl>
        <div className="knowledge-base-card__footer">
          <span className={`status-label status-label--${status.tone}`}>
            <StatusIcon aria-hidden="true" size={14} />
            {status.label}
          </span>
          <span>{formatUpdatedAt(summary.updated_at)}</span>
        </div>
      </Link>
    </article>
  );
}
