import { Expand, ImageOff, X } from "lucide-react";
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import type { VisualAssetDetail, VisualAssetReference } from "../../api/types";
import { useModalFocus } from "../../components/useModalFocus";

const SUPPORTED_IMAGE_CONTENT_TYPES = new Set([
  "image/jpeg",
  "image/png",
  "image/webp",
]);

type VisualAsset = VisualAssetReference | VisualAssetDetail;

function contentType(asset: VisualAsset) {
  return "content_type" in asset ? asset.content_type : null;
}

function dimensions(asset: VisualAsset) {
  if (!("width" in asset) || !asset.width || !asset.height) {
    return {};
  }
  return { width: asset.width, height: asset.height };
}

function normalizedCaption(asset: VisualAsset | null | undefined, caption?: string | null) {
  const preferred = caption?.trim();
  if (preferred) {
    return preferred;
  }
  if (asset && "caption" in asset) {
    return asset.caption?.trim() ?? "";
  }
  return "";
}

function supportsBrowserPreview(asset: VisualAsset) {
  const mime = contentType(asset);
  return mime === null || SUPPORTED_IMAGE_CONTENT_TYPES.has(mime);
}

export function canOpenVisualAsset(asset: VisualAsset | null | undefined) {
  return Boolean(
    asset?.asset_url &&
    supportsBrowserPreview(asset),
  );
}

type ImageAssetPreviewProps = {
  asset?: VisualAsset | null;
  assetLabel: string;
  caption?: string | null;
  onOpen?: () => void;
};

export function ImageAssetPreview({
  asset,
  assetLabel,
  caption,
  onOpen,
}: ImageAssetPreviewProps) {
  const resolvedCaption = normalizedCaption(asset, caption);
  const supported = asset ? supportsBrowserPreview(asset) : false;
  const initialSources = useMemo(
    () => [asset?.preview_url, asset?.asset_url].filter(
      (value, index, values): value is string =>
        Boolean(value) && values.indexOf(value) === index,
    ),
    [asset?.asset_url, asset?.preview_url],
  );
  const [failedSources, setFailedSources] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    setFailedSources(new Set());
  }, [asset?.asset_id, asset?.asset_url, asset?.preview_url]);

  const source = asset && supported
    ? initialSources.find((candidate) => !failedSources.has(candidate)) ?? null
    : null;
  const canOpen = Boolean(onOpen && canOpenVisualAsset(asset));

  let placeholder = "图片文件暂不可用";
  if (asset && !supported) {
    placeholder = "暂不支持预览此图片格式";
  } else if (initialSources.length > 0 && !source) {
    placeholder = "图片加载失败";
  }

  const media = source ? (
    <img
      {...dimensions(asset as VisualAsset)}
      alt={resolvedCaption ? "" : `${assetLabel}预览`}
      decoding="async"
      loading="lazy"
      onError={() => {
        setFailedSources((current) => new Set(current).add(source));
      }}
      src={source}
    />
  ) : (
    <span className="image-asset-preview__placeholder">
      <ImageOff aria-hidden="true" size={28} />
      <span>{placeholder}</span>
    </span>
  );

  return (
    <figure className="image-asset-preview">
      {canOpen ? (
        <button
          aria-label={`查看${assetLabel}原图${resolvedCaption ? `：${resolvedCaption}` : ""}`}
          className="image-asset-preview__trigger"
          onClick={(event) => {
            event.stopPropagation();
            onOpen?.();
          }}
          onKeyDown={(event) => event.stopPropagation()}
          type="button"
        >
          {media}
          <span className="image-asset-preview__zoom-hint">
            <Expand aria-hidden="true" size={14} />
            查看原图
          </span>
        </button>
      ) : (
        <div className="image-asset-preview__media">
          {media}
          {canOpenVisualAsset(asset) ? (
            <span className="image-asset-preview__zoom-hint">
              <Expand aria-hidden="true" size={14} />
              点击卡片查看原图
            </span>
          ) : null}
        </div>
      )}
      {resolvedCaption ? <figcaption>{resolvedCaption}</figcaption> : null}
    </figure>
  );
}

type ImageAssetLightboxProps = {
  asset: VisualAsset;
  assetLabel: string;
  caption?: string | null;
  onClose: () => void;
};

export function ImageAssetLightbox({
  asset,
  assetLabel,
  caption,
  onClose,
}: ImageAssetLightboxProps) {
  const titleId = useId();
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const closeTimerRef = useRef<number | null>(null);
  const [closing, setClosing] = useState(false);
  const requestClose = useCallback(() => {
    if (closing) {
      return;
    }
    setClosing(true);
    closeTimerRef.current = window.setTimeout(onClose, 160);
  }, [closing, onClose]);
  const modal = useModalFocus(requestClose, closeButtonRef);
  const [failed, setFailed] = useState(false);
  const resolvedCaption = normalizedCaption(asset, caption);

  useEffect(() => {
    setFailed(false);
    setClosing(false);
  }, [asset.asset_id, asset.asset_url]);

  useEffect(() => () => {
    if (closeTimerRef.current !== null) {
      window.clearTimeout(closeTimerRef.current);
    }
  }, []);

  if (!canOpenVisualAsset(asset) || !asset.asset_url) {
    return null;
  }

  return createPortal(
    <div
      className={[
        "modal-backdrop image-asset-lightbox-backdrop",
        closing ? "is-closing" : "",
      ].filter(Boolean).join(" ")}
      onClick={(event) => {
        if (event.target === event.currentTarget) {
          requestClose();
        }
      }}
      role="presentation"
    >
      <section
        aria-labelledby={titleId}
        aria-modal="true"
        className="image-asset-lightbox"
        onKeyDown={modal.onKeyDown}
        ref={modal.dialogRef}
        role="dialog"
      >
        <header className="image-asset-lightbox__header">
          <div>
            <p>原始图片</p>
            <h2 id={titleId}>{assetLabel}</h2>
          </div>
          <button
            aria-label="关闭原图"
            className="icon-button"
            onClick={requestClose}
            ref={closeButtonRef}
            type="button"
          >
            <X aria-hidden="true" size={20} />
          </button>
        </header>
        <div className="image-asset-lightbox__stage">
          {failed ? (
            <div className="image-asset-lightbox__error" role="status">
              <ImageOff aria-hidden="true" size={32} />
              <strong>原图加载失败</strong>
              <span>仍可关闭窗口并在 PDF 高亮位置核验。</span>
            </div>
          ) : (
            <img
              {...dimensions(asset)}
              alt={resolvedCaption || assetLabel}
              decoding="async"
              onError={() => setFailed(true)}
              src={asset.asset_url}
            />
          )}
        </div>
        {resolvedCaption ? <p className="image-asset-lightbox__caption">{resolvedCaption}</p> : null}
      </section>
    </div>,
    document.body,
  );
}
