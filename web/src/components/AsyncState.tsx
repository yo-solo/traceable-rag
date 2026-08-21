import type { ReactNode } from "react";

type AsyncStateProps = {
  children: ReactNode;
  loading?: boolean;
  loadingLabel?: string;
  error?: string | null;
  onRetry?: () => void;
  empty?: boolean;
  emptyTitle?: string;
  emptyDescription?: string;
  emptyAction?: ReactNode;
};

export function AsyncState({
  children,
  loading = false,
  loadingLabel = "正在加载",
  error,
  onRetry,
  empty = false,
  emptyTitle = "暂无内容",
  emptyDescription,
  emptyAction,
}: AsyncStateProps) {
  if (loading) {
    return (
      <div className="async-state async-state--loading" role="status" aria-live="polite">
        <span className="async-state__spinner" aria-hidden="true" />
        <span>{loadingLabel}</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="async-state async-state--error" role="alert">
        <strong>加载失败</strong>
        <p>{error}</p>
        {onRetry ? (
          <button className="secondary-button" type="button" onClick={onRetry}>
            重试
          </button>
        ) : null}
      </div>
    );
  }

  if (empty) {
    return (
      <div className="async-state async-state--empty">
        <h2>{emptyTitle}</h2>
        {emptyDescription ? <p>{emptyDescription}</p> : null}
        {emptyAction}
      </div>
    );
  }

  return children;
}
