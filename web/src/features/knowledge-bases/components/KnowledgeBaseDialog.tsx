import { AlertTriangle, X } from "lucide-react";
import {
  type FormEvent,
  type RefObject,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

import type { KnowledgeBase } from "../../../api/types";
import { useModalFocus } from "../../../components/useModalFocus";

type EditorMode = "create" | "rename";

type KnowledgeBaseEditorDialogProps = {
  mode: EditorMode;
  knowledgeBase?: KnowledgeBase;
  busy?: boolean;
  error?: string | null;
  onClose: () => void;
  onSubmit: (value: { name: string; description: string }) => Promise<void> | void;
  returnFocusFallbackRef?: RefObject<HTMLElement | null>;
};

type KnowledgeBaseDeleteDialogProps = {
  knowledgeBase: KnowledgeBase;
  documentCount: number;
  busy?: boolean;
  error?: string | null;
  requireNameConfirmation?: boolean;
  onClose: () => void;
  onConfirm: (confirmName?: string) => Promise<void> | void;
  returnFocusFallbackRef?: RefObject<HTMLElement | null>;
};

export function KnowledgeBaseEditorDialog({
  mode,
  knowledgeBase,
  busy = false,
  error,
  onClose,
  onSubmit,
  returnFocusFallbackRef,
}: KnowledgeBaseEditorDialogProps) {
  const titleId = useId();
  const [name, setName] = useState(knowledgeBase?.name ?? "");
  const [description, setDescription] = useState(knowledgeBase?.description ?? "");
  const title = mode === "create" ? "新建知识库" : "重命名知识库";
  const nameInputRef = useRef<HTMLInputElement>(null);
  const requestClose = () => {
    if (!busy) {
      onClose();
    }
  };
  const modal = useModalFocus(requestClose, nameInputRef, busy, returnFocusFallbackRef);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalizedName = name.trim();
    if (!normalizedName) {
      return;
    }
    await onSubmit({ name: normalizedName, description: description.trim() });
  }

  return createPortal(
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
            <p className="modal-dialog__context">知识库管理</p>
            <h2 id={titleId}>{title}</h2>
          </div>
          <button
            aria-label="关闭"
            className="icon-button"
            disabled={busy}
            onClick={requestClose}
            type="button"
          >
            <X aria-hidden="true" size={18} />
          </button>
        </div>

        <form className="modal-dialog__form" onSubmit={handleSubmit}>
          <label className="field-label">
            <span>名称</span>
            <input
              maxLength={120}
              onChange={(event) => setName(event.target.value)}
              ref={nameInputRef}
              required
              type="text"
              value={name}
            />
          </label>

          <label className="field-label">
            <span>描述（可选）</span>
            <textarea
              maxLength={500}
              onChange={(event) => setDescription(event.target.value)}
              rows={3}
              value={description}
            />
          </label>

          {error ? <p className="form-error" role="alert">{error}</p> : null}

          <div className="modal-dialog__actions">
            <button className="ghost-button" disabled={busy} onClick={requestClose} type="button">
              取消
            </button>
            <button className="primary-button" disabled={busy || !name.trim()} type="submit">
              {busy ? "正在保存" : mode === "create" ? "创建知识库" : "保存更改"}
            </button>
          </div>
        </form>
      </section>
    </div>,
    document.body,
  );
}

export function KnowledgeBaseDeleteDialog({
  knowledgeBase,
  documentCount,
  busy = false,
  error,
  requireNameConfirmation = false,
  onClose,
  onConfirm,
  returnFocusFallbackRef,
}: KnowledgeBaseDeleteDialogProps) {
  const titleId = useId();
  const [confirmName, setConfirmName] = useState("");
  const requiresName = documentCount > 0 || requireNameConfirmation;
  const isConfirmed = !requiresName || confirmName === knowledgeBase.name;
  const confirmInputRef = useRef<HTMLInputElement>(null);
  const cancelButtonRef = useRef<HTMLButtonElement>(null);
  const requestClose = () => {
    if (!busy) {
      onClose();
    }
  };
  const modal = useModalFocus(
    requestClose,
    requiresName ? confirmInputRef : cancelButtonRef,
    busy,
    returnFocusFallbackRef,
  );

  useEffect(() => {
    if (requiresName) {
      confirmInputRef.current?.focus();
    }
  }, [requiresName]);

  return createPortal(
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
            <h2 id={titleId}>删除知识库</h2>
          </div>
          <button
            aria-label="关闭"
            className="icon-button"
            disabled={busy}
            onClick={requestClose}
            type="button"
          >
            <X aria-hidden="true" size={18} />
          </button>
        </div>

        <div className="danger-callout">
          <AlertTriangle aria-hidden="true" size={20} />
          <div>
            <strong>{knowledgeBase.name}</strong>
            <p>
              {documentCount > 0
                ? `将同时删除 ${documentCount} 个文档及其相关搜索内容。`
                : requireNameConfirmation
                  ? "服务检测到仍有关联数据，需要输入完整名称确认。"
                  : "这个知识库没有文档，可以直接删除。"}
            </p>
          </div>
        </div>

        {requiresName ? (
          <label className="field-label">
            <span>输入知识库名称以确认</span>
            <input
              autoComplete="off"
              onChange={(event) => setConfirmName(event.target.value)}
              ref={confirmInputRef}
              type="text"
              value={confirmName}
            />
          </label>
        ) : null}

        <p className="modal-dialog__hint">
          历史问答记录不会被删除；相关来源会标记为不可访问。
        </p>
        {error ? <p className="form-error" role="alert">{error}</p> : null}

        <div className="modal-dialog__actions">
          <button
            className="ghost-button"
            disabled={busy}
            onClick={requestClose}
            ref={cancelButtonRef}
            type="button"
          >
            取消
          </button>
          <button
            className="danger-button"
            disabled={busy || !isConfirmed}
            onClick={() => onConfirm(requiresName ? confirmName : undefined)}
            type="button"
          >
            {busy ? "正在删除" : requiresName ? "永久删除" : "确认删除"}
          </button>
        </div>
      </section>
    </div>,
    document.body,
  );
}
