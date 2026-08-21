import { ArrowLeft, FileUp, X } from "lucide-react";
import {
  type ChangeEvent,
  type FormEvent,
  type RefObject,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { Link, useParams } from "react-router-dom";

import { ApiError, api } from "../../api/client";
import {
  requireDocumentListResponse,
  requireDocumentResponse,
  requireKnowledgeBaseResponse,
  requireManagedProcessResponse,
  requireUploadDocumentResponse,
} from "../../api/guards";
import type {
  KnowledgeBase,
  KnowledgeBaseDocument,
} from "../../api/types";
import { AsyncState } from "../../components/AsyncState";
import { useModalFocus } from "../../components/useModalFocus";
import { DocumentRow } from "../documents/components/DocumentRow";
import { useDocumentPolling } from "../documents/useDocumentPolling";

type DocumentDialog =
  | { type: "rename"; document: KnowledgeBaseDocument }
  | { type: "delete"; document: KnowledgeBaseDocument }
  | null;

type BusyDocumentAction = {
  documentId: string;
  action: "rename" | "delete" | "parse" | "index";
} | null;

type UploadAttempt = {
  id: string;
  filename: string;
  status: "uploading" | "failed";
  error?: string;
};

function actionableMutationError(error: unknown, action: "upload" | "rename" | "delete" | "parse" | "index") {
  if (
    error instanceof ApiError &&
    error.code === "document_processing_not_configured"
  ) {
    return "文档解析服务尚未配置。请先在运行状态中确认服务配置。";
  }
  if (action === "parse") {
    return "未能读取文档内容。请检查文件后重试；如果问题持续存在，可在运行状态中检查文档处理服务。";
  }
  if (action === "index") {
    return "文档暂时无法用于搜索。请确认内容已读取完成，然后重试。";
  }
  if (action === "upload") {
    return "上传未完成。请确认文件格式、大小和名称后重新选择。";
  }
  return `${action === "rename" ? "重命名" : "删除"}未完成。请确认文档当前没有正在处理，然后重试。`;
}

export function KnowledgeBaseDetailPage() {
  const { knowledgeBaseId = "" } = useParams();
  const [knowledgeBase, setKnowledgeBase] = useState<KnowledgeBase | null>(null);
  const [documents, setDocuments] = useState<KnowledgeBaseDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadAttempts, setUploadAttempts] = useState<UploadAttempt[]>([]);
  const [dialog, setDialog] = useState<DocumentDialog>(null);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<BusyDocumentAction>(null);
  const pageControllerRef = useRef<AbortController | null>(null);
  const pageEpochRef = useRef(0);
  const documentSequenceRef = useRef(0);
  const pageHeadingRef = useRef<HTMLHeadingElement>(null);

  function isCurrent(epoch: number, signal: AbortSignal) {
    return epoch === pageEpochRef.current && !signal.aborted;
  }

  function currentScope() {
    const controller = pageControllerRef.current;
    return controller && !controller.signal.aborted
      ? { controller, epoch: pageEpochRef.current }
      : null;
  }

  const loadPage = useCallback(async (signal: AbortSignal, epoch: number) => {
    const requestSequence = ++documentSequenceRef.current;
    setLoading(true);
    setLoadError(null);
    try {
      const [rawKnowledgeBaseResponse, rawDocumentsResponse] = await Promise.all([
        api.get<unknown>(`/api/knowledge-bases/${knowledgeBaseId}`, { signal }),
        api.get<unknown>(`/api/knowledge-bases/${knowledgeBaseId}/documents`, { signal }),
      ]);
      const knowledgeBaseResponse = requireKnowledgeBaseResponse(rawKnowledgeBaseResponse);
      const documentsResponse = requireDocumentListResponse(rawDocumentsResponse);
      if (
        isCurrent(epoch, signal) &&
        requestSequence === documentSequenceRef.current
      ) {
        setKnowledgeBase(knowledgeBaseResponse.knowledge_base);
        setDocuments(documentsResponse.documents);
      }
    } catch (error) {
      if (signal.aborted || (error instanceof DOMException && error.name === "AbortError")) {
        return;
      }
      if (epoch === pageEpochRef.current) {
        if (error instanceof ApiError && error.status === 404) {
          setLoadError("知识库不存在。下一步：返回知识库列表并选择一个仍然可用的知识库。");
        } else {
          setLoadError("暂时无法读取知识库中的文档，请稍后重试。");
        }
      }
    } finally {
      if (epoch === pageEpochRef.current) {
        setLoading(false);
      }
    }
  }, [knowledgeBaseId]);

  const requestDocuments = useCallback(async (refreshErrorMessage: string) => {
    const controller = pageControllerRef.current;
    const epoch = pageEpochRef.current;
    if (!controller || controller.signal.aborted) {
      return false;
    }
    const requestSequence = ++documentSequenceRef.current;
    try {
      const response = requireDocumentListResponse(
        await api.get<unknown>(`/api/knowledge-bases/${knowledgeBaseId}/documents`, {
          signal: controller.signal,
        }),
      );
      if (
        isCurrent(epoch, controller.signal) &&
        requestSequence === documentSequenceRef.current
      ) {
        setDocuments(response.documents);
      }
      return true;
    } catch (error) {
      if (
        !controller.signal.aborted &&
        epoch === pageEpochRef.current &&
        requestSequence === documentSequenceRef.current
      ) {
        setMutationError(refreshErrorMessage);
      }
      return false;
    }
  }, [knowledgeBaseId]);

  const refreshDocuments = useCallback(async () => {
    await requestDocuments(
      "状态刷新失败。下一步：确认服务仍在运行；页面会继续轮询，也可以稍后手动重试。",
    );
  }, [requestDocuments]);

  useEffect(() => {
    const epoch = pageEpochRef.current + 1;
    pageEpochRef.current = epoch;
    documentSequenceRef.current = 0;
    const controller = new AbortController();
    pageControllerRef.current = controller;
    setKnowledgeBase(null);
    setDocuments([]);
    setLoadError(null);
    setMutationError(null);
    setUploading(false);
    setUploadAttempts([]);
    setDialog(null);
    setDialogError(null);
    setBusyAction(null);
    void loadPage(controller.signal, epoch);
    return () => {
      controller.abort();
      if (pageEpochRef.current === epoch) {
        pageEpochRef.current = epoch + 1;
      }
      if (pageControllerRef.current === controller) {
        pageControllerRef.current = null;
      }
    };
  }, [loadPage]);

  const pageMutationBusy = busyAction !== null;
  useDocumentPolling(pageMutationBusy ? [] : documents, refreshDocuments);

  function replaceDocument(document: KnowledgeBaseDocument) {
    setDocuments((current) =>
      current.map((item) => (item.id === document.id ? document : item)),
    );
  }

  async function handleUpload(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? []);
    event.target.value = "";
    if (files.length === 0) {
      return;
    }
    const scope = currentScope();
    if (!scope) {
      return;
    }
    setUploading(true);
    setMutationError(null);
    const attempts = files.map((file) => ({
      id: `${file.name}-${file.size}-${file.lastModified}`,
      filename: file.name,
      status: "uploading" as const,
    }));
    setUploadAttempts((current) => [...current, ...attempts]);
    for (const [index, file] of files.entries()) {
      const attempt = attempts[index];
      try {
        const response = requireUploadDocumentResponse(
          await api.upload<unknown>(
            `/api/knowledge-bases/${knowledgeBaseId}/documents/upload`,
            file,
            {
              displayName: file.name.replace(/\.[^.]+$/, ""),
              signal: scope.controller.signal,
            },
          ),
        );
        if (isCurrent(scope.epoch, scope.controller.signal)) {
          setDocuments((current) => [
            response.document,
            ...current.filter((item) => item.id !== response.document.id),
          ]);
          setUploadAttempts((current) => current.filter((item) => item.id !== attempt.id));
        }
      } catch (error) {
        if (isCurrent(scope.epoch, scope.controller.signal)) {
          const message = actionableMutationError(error, "upload");
          setUploadAttempts((current) => current.map((item) =>
            item.id === attempt.id ? { ...item, status: "failed", error: message } : item,
          ));
        }
      }
    }
    if (scope.epoch === pageEpochRef.current) {
      setUploading(false);
    }
  }

  async function handleProcess(document: KnowledgeBaseDocument, action: "parse" | "index") {
    const scope = currentScope();
    if (!scope) {
      return;
    }
    const requestSequence = ++documentSequenceRef.current;
    setMutationError(null);
    setBusyAction({ documentId: document.id, action });
    replaceDocument({
      ...document,
      processing_status: "queued",
      processing_error: null,
    });
    try {
      const response = requireManagedProcessResponse(
        await api.post<unknown>(
          `/api/knowledge-bases/${knowledgeBaseId}/documents/${document.id}/${action}`,
          { signal: scope.controller.signal },
        ),
      );
      if (isCurrent(scope.epoch, scope.controller.signal) && requestSequence === documentSequenceRef.current) {
        replaceDocument(response.document);
      }
    } catch (error) {
      if (isCurrent(scope.epoch, scope.controller.signal)) {
        const actionError = actionableMutationError(error, action);
        setMutationError(actionError);
        await requestDocuments(
          `${actionError} 状态刷新失败。下一步：页面会保持处理中并继续轮询，请确认服务恢复后再试。`,
        );
      }
    } finally {
      if (scope.epoch === pageEpochRef.current) {
        setBusyAction(null);
      }
    }
  }

  async function handleRename(displayName: string) {
    if (!dialog || dialog.type !== "rename") {
      return;
    }
    const scope = currentScope();
    if (!scope) {
      return;
    }
    const requestSequence = ++documentSequenceRef.current;
    setDialogError(null);
    setBusyAction({ documentId: dialog.document.id, action: "rename" });
    try {
      const response = requireDocumentResponse(
        await api.patch<unknown>(
          `/api/knowledge-bases/${knowledgeBaseId}/documents/${dialog.document.id}`,
          {
            body: { display_name: displayName },
            signal: scope.controller.signal,
          },
        ),
      );
      if (isCurrent(scope.epoch, scope.controller.signal) && requestSequence === documentSequenceRef.current) {
        replaceDocument(response.document);
        setDialog(null);
      }
    } catch (error) {
      if (isCurrent(scope.epoch, scope.controller.signal)) {
        setDialogError(actionableMutationError(error, "rename"));
      }
    } finally {
      if (scope.epoch === pageEpochRef.current) {
        setBusyAction(null);
      }
    }
  }

  async function handleDelete() {
    if (!dialog || dialog.type !== "delete") {
      return;
    }
    const documentId = dialog.document.id;
    const scope = currentScope();
    if (!scope) {
      return;
    }
    const requestSequence = ++documentSequenceRef.current;
    setDialogError(null);
    setBusyAction({ documentId, action: "delete" });
    try {
      const response = await api.delete<unknown>(
        `/api/knowledge-bases/${knowledgeBaseId}/documents/${documentId}`,
        { signal: scope.controller.signal },
      );
      if (
        typeof response !== "object" ||
        response === null ||
        !("deleted" in response) ||
        response.deleted !== true
      ) {
        throw new Error("删除结果未确认");
      }
      if (isCurrent(scope.epoch, scope.controller.signal) && requestSequence === documentSequenceRef.current) {
        setDocuments((current) => current.filter((document) => document.id !== documentId));
        setDialog(null);
      }
    } catch (error) {
      if (isCurrent(scope.epoch, scope.controller.signal)) {
        setDialogError(actionableMutationError(error, "delete"));
      }
    } finally {
      if (scope.epoch === pageEpochRef.current) {
        setBusyAction(null);
      }
    }
  }

  return (
    <section className="feature-page knowledge-base-detail-page" aria-labelledby="knowledge-base-detail-title">
      <nav aria-label="面包屑" className="breadcrumb-navigation">
        <Link aria-label="知识库列表" to="/knowledge-bases">知识库</Link>
        <span aria-hidden="true">/</span>
        <span aria-current="page">{knowledgeBase?.name ?? "读取中"}</span>
      </nav>

      <AsyncState
        error={loadError}
        loading={loading}
        loadingLabel="正在读取文档列表"
        onRetry={() => {
          const scope = currentScope();
          if (scope) {
            void loadPage(scope.controller.signal, scope.epoch);
          }
        }}
      >
        {knowledgeBase ? (
          <>
            <div className="feature-page__header feature-page__header--detail">
              <div>
                <Link className="back-link" to="/knowledge-bases">
                  <ArrowLeft aria-hidden="true" size={15} />
                  全部知识库
                </Link>
                <h1 id="knowledge-base-detail-title" ref={pageHeadingRef} tabIndex={-1}>
                  {knowledgeBase.name}
                </h1>
                <p>{knowledgeBase.description || "这个知识库尚未添加说明。"}</p>
              </div>

              <label className={`upload-button${uploading ? " is-busy" : ""}`}>
                <FileUp aria-hidden="true" size={18} />
                <span>{uploading ? "正在上传" : "上传文档"}</span>
                <input
                  accept=".pdf,.doc,.docx,.ppt,.pptx,.jpg,.jpeg,.png"
                  aria-label="选择要上传的原始文档"
                  disabled={uploading}
                  multiple
                  onChange={handleUpload}
                  type="file"
                />
              </label>
            </div>

            {uploadAttempts.length > 0 ? (
              <section className="upload-attempts" aria-label="文件上传队列">
                {uploadAttempts.map((attempt) => (
                  <div className={`upload-attempt upload-attempt--${attempt.status}`} key={attempt.id}>
                    <div>
                      <strong>{attempt.filename}</strong>
                      <span>{attempt.status === "uploading" ? "正在上传" : "上传失败"}</span>
                    </div>
                    {attempt.error ? <p role="alert">{attempt.error}</p> : null}
                    {attempt.status === "failed" ? (
                      <button
                        className="ghost-button"
                        onClick={() => setUploadAttempts((current) =>
                          current.filter((item) => item.id !== attempt.id)
                        )}
                        type="button"
                      >
                        关闭
                      </button>
                    ) : null}
                  </div>
                ))}
              </section>
            ) : null}

            {mutationError ? (
              <div className="inline-action-error" role="alert">
                <strong>操作未完成</strong>
                <p>{mutationError}</p>
                <button className="ghost-button" onClick={() => setMutationError(null)} type="button">
                  关闭提示
                </button>
              </div>
            ) : null}

            <AsyncState
              empty={documents.length === 0}
              emptyDescription="上传 PDF、Word、PPT 或图片后，系统会自动读取内容并准备搜索。"
              emptyTitle="还没有文档"
            >
              <div className="document-table-shell">
                <table className="document-table">
                  <thead>
                    <tr>
                      <th scope="col">文档</th>
                      <th scope="col">处理状态</th>
                      <th scope="col">页数</th>
                      <th scope="col">内容</th>
                      <th scope="col">可搜索内容</th>
                      <th scope="col">更新时间</th>
                      <th scope="col"><span className="visually-hidden">操作</span></th>
                    </tr>
                  </thead>
                  <tbody>
                    {documents.map((document) => (
                      <DocumentRow
                        busyAction={
                          busyAction?.documentId === document.id ? busyAction.action : null
                        }
                        document={document}
                        disabled={pageMutationBusy}
                        key={document.id}
                        onDelete={(selected) => {
                          setDialogError(null);
                          setDialog({ type: "delete", document: selected });
                        }}
                        onIndex={(selected) => void handleProcess(selected, "index")}
                        onParse={(selected) => void handleProcess(selected, "parse")}
                        onRename={(selected) => {
                          setDialogError(null);
                          setDialog({ type: "rename", document: selected });
                        }}
                      />
                    ))}
                  </tbody>
                </table>
              </div>
            </AsyncState>
          </>
        ) : null}
      </AsyncState>

      {dialog?.type === "rename" ? (
        <DocumentRenameDialog
          busy={busyAction?.action === "rename"}
          document={dialog.document}
          error={dialogError}
          onClose={() => setDialog(null)}
          onSubmit={handleRename}
          returnFocusFallbackRef={pageHeadingRef}
        />
      ) : null}
      {dialog?.type === "delete" ? (
        <DocumentDeleteDialog
          busy={busyAction?.action === "delete"}
          document={dialog.document}
          error={dialogError}
          onClose={() => setDialog(null)}
          onConfirm={handleDelete}
          returnFocusFallbackRef={pageHeadingRef}
        />
      ) : null}
    </section>
  );
}

type DocumentRenameDialogProps = {
  document: KnowledgeBaseDocument;
  busy: boolean;
  error?: string | null;
  onClose: () => void;
  onSubmit: (displayName: string) => Promise<void> | void;
  returnFocusFallbackRef?: RefObject<HTMLElement | null>;
};

function DocumentRenameDialog({
  document,
  busy,
  error,
  onClose,
  onSubmit,
  returnFocusFallbackRef,
}: DocumentRenameDialogProps) {
  const titleId = useId();
  const [displayName, setDisplayName] = useState(document.display_name);
  const inputRef = useRef<HTMLInputElement>(null);
  const requestClose = () => {
    if (!busy) {
      onClose();
    }
  };
  const modal = useModalFocus(requestClose, inputRef, busy, returnFocusFallbackRef);

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (displayName.trim()) {
      void onSubmit(displayName.trim());
    }
  }

  return (
    <div className="modal-backdrop" role="presentation">
      <section
        aria-labelledby={titleId}
        aria-modal="true"
        className="modal-dialog"
        onKeyDown={modal.onKeyDown}
        ref={modal.dialogRef}
        role="dialog"
      >
        <div className="modal-dialog__header">
          <div>
            <p className="modal-dialog__context">文档管理</p>
            <h2 id={titleId}>重命名文档</h2>
          </div>
          <button aria-label="关闭" className="icon-button" disabled={busy} onClick={requestClose} type="button">
            <X aria-hidden="true" size={18} />
          </button>
        </div>
        <form className="modal-dialog__form" onSubmit={handleSubmit}>
          <label className="field-label">
            <span>文档名称</span>
            <input
              maxLength={255}
              onChange={(event) => setDisplayName(event.target.value)}
              ref={inputRef}
              required
              type="text"
              value={displayName}
            />
          </label>
          <p className="modal-dialog__hint">原始文件名保持不变，历史问答记录仍会使用当时保存的引用内容。</p>
          {error ? <p className="form-error" role="alert">{error}</p> : null}
          <div className="modal-dialog__actions">
            <button className="ghost-button" disabled={busy} onClick={requestClose} type="button">取消</button>
            <button className="primary-button" disabled={busy || !displayName.trim()} type="submit">
              {busy ? "正在保存" : "保存更改"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}

type DocumentDeleteDialogProps = {
  document: KnowledgeBaseDocument;
  busy: boolean;
  error?: string | null;
  onClose: () => void;
  onConfirm: () => Promise<void> | void;
  returnFocusFallbackRef?: RefObject<HTMLElement | null>;
};

function DocumentDeleteDialog({
  document,
  busy,
  error,
  onClose,
  onConfirm,
  returnFocusFallbackRef,
}: DocumentDeleteDialogProps) {
  const titleId = useId();
  const cancelButtonRef = useRef<HTMLButtonElement>(null);
  const requestClose = () => {
    if (!busy) {
      onClose();
    }
  };
  const modal = useModalFocus(
    requestClose,
    cancelButtonRef,
    busy,
    returnFocusFallbackRef,
  );
  return (
    <div className="modal-backdrop" role="presentation">
      <section
        aria-labelledby={titleId}
        aria-modal="true"
        className="modal-dialog modal-dialog--danger"
        onKeyDown={modal.onKeyDown}
        ref={modal.dialogRef}
        role="dialog"
      >
        <div className="modal-dialog__header">
          <div>
            <p className="modal-dialog__context">不可撤销操作</p>
            <h2 id={titleId}>删除文档</h2>
          </div>
          <button aria-label="关闭" className="icon-button" disabled={busy} onClick={requestClose} type="button">
            <X aria-hidden="true" size={18} />
          </button>
        </div>
        <p className="document-delete-copy">
          删除“{document.display_name}”会同时移除原文件、已读取内容和搜索数据。
        </p>
        <p className="modal-dialog__hint">历史问答记录会保留当时的引用内容，并把来源标记为不可打开。</p>
        {error ? <p className="form-error" role="alert">{error}</p> : null}
        <div className="modal-dialog__actions">
          <button
            className="ghost-button"
            disabled={busy}
            onClick={requestClose}
            ref={cancelButtonRef}
            type="button"
          >取消</button>
          <button className="danger-button" disabled={busy} onClick={() => void onConfirm()} type="button">
            {busy ? "正在删除" : "确认删除"}
          </button>
        </div>
      </section>
    </div>
  );
}
