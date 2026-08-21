import {
  BarChart3,
  BookOpen,
  Code2,
  FileText,
  Heading,
  Image as ImageIcon,
  List,
  ListTree,
  MapPinOff,
  Pilcrow,
  Quote,
  Sigma,
  Table2,
  Type,
  Workflow,
  type LucideIcon,
} from "lucide-react";
import { memo, type KeyboardEvent, type Ref, useCallback, useState } from "react";

import type { DocumentBlock, DocumentVisualAssetDetail } from "../../api/types";
import { BlockContent } from "./BlockContent";
import { ImageAssetLightbox } from "./ImageAssetPreview";

const BLOCK_TYPE_LABELS: Record<string, string> = {
  title: "标题",
  paragraph: "段落",
  text: "文本",
  table: "表格",
  table_body: "表格",
  image: "图片",
  formula: "公式",
  equation: "公式",
  code: "代码",
  list: "列表",
  index: "目录",
  reference: "参考文献",
  chart: "图表",
  algorithm: "算法",
  footnote: "脚注",
  content: "原文内容",
};

const BLOCK_TYPE_ICONS: Record<string, LucideIcon> = {
  title: Heading,
  paragraph: Pilcrow,
  text: Type,
  table: Table2,
  table_body: Table2,
  image: ImageIcon,
  formula: Sigma,
  equation: Sigma,
  equation_interline: Sigma,
  interline_equation: Sigma,
  code: Code2,
  list: List,
  index: ListTree,
  reference: BookOpen,
  chart: BarChart3,
  algorithm: Workflow,
  footnote: Quote,
  content: FileText,
};

export function blockTypeLabel(blockType: string) {
  return BLOCK_TYPE_LABELS[blockType.toLowerCase()] ?? "原文内容";
}

function coordinateSummary(block: DocumentBlock) {
  return block.bbox ? block.bbox.map((value) => Math.round(value)).join(",") : "unavailable";
}

type BlockListPaneProps = {
  assets?: Record<string, DocumentVisualAssetDetail>;
  blocks: DocumentBlock[];
  highlightedBlockIds?: readonly string[];
  hoveredBlockId?: string | null;
  selectedBlockId?: string | null;
  onHover?: (blockId: string | null) => void;
  onSelect?: (block: DocumentBlock) => void;
  registerBlockRef?: (blockId: string, element: HTMLElement | null) => void;
};

type BlockCardProps = {
  asset?: DocumentVisualAssetDetail;
  block: DocumentBlock;
  hovered: boolean;
  supporting: boolean;
  index: number;
  selected: boolean;
  onHover?: (blockId: string | null) => void;
  onOpenAsset?: (
    asset: DocumentVisualAssetDetail,
    block: DocumentBlock,
    typeLabel: string,
  ) => void;
  onSelect?: (block: DocumentBlock) => void;
  registerBlockRef?: (blockId: string, element: HTMLElement | null) => void;
};

const BlockCard = memo(function BlockCard({
  asset,
  block,
  hovered,
  supporting,
  index,
  selected,
  onHover,
  onOpenAsset,
  onSelect,
  registerBlockRef,
}: BlockCardProps) {
  const typeLabel = blockTypeLabel(block.block_type);
  const TypeIcon = BLOCK_TYPE_ICONS[block.block_type.toLowerCase()] ?? FileText;
  const setCardRef = useCallback(
    (element: HTMLElement | null) => registerBlockRef?.(block.id, element),
    [block.id, registerBlockRef],
  );
  const activate = useCallback(() => {
    onSelect?.(block);
  }, [block, onSelect]);
  const openAsset = useCallback(() => {
    onSelect?.(block);
    if (asset) {
      onOpenAsset?.(asset, block, typeLabel);
    }
  }, [asset, block, onOpenAsset, onSelect, typeLabel]);

  return (
    <article
      aria-label={`第 ${block.human_page_number} 页的${typeLabel} ${index + 1}`}
      aria-pressed={selected}
      className={[
        "block-card",
        hovered ? "is-hovered" : "",
        selected ? "is-selected" : "",
        supporting ? "is-supporting" : "",
        block.is_discarded ? "is-discarded" : "",
      ].filter(Boolean).join(" ")}
      data-content-id={block.id}
      data-content-type={block.block_type}
      data-coordinate-summary={coordinateSummary(block)}
      data-document-id={block.document_id}
      data-page={block.human_page_number}
      data-parser-discarded={block.is_discarded ? "true" : "false"}
      onClick={activate}
      onKeyDown={(event: KeyboardEvent<HTMLElement>) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          activate();
        }
      }}
      onMouseEnter={() => onHover?.(block.id)}
      onMouseLeave={() => onHover?.(null)}
      ref={setCardRef as Ref<HTMLElement>}
      role="button"
      tabIndex={0}
    >
      <div className="block-card__meta">
        <span className="block-card__index">内容 {index + 1}</span>
        <span className="block-card__type">
          <TypeIcon aria-hidden="true" size={13} strokeWidth={1.8} />
          {typeLabel}
        </span>
        {supporting ? <span className="block-card__supporting">引用依据</span> : null}
        <span>第 {block.human_page_number} 页</span>
        {!block.bbox || !block.page_size ? (
          <span className="block-card__unlocated">
            <MapPinOff aria-hidden="true" size={13} />
            暂无原文位置
          </span>
        ) : null}
      </div>
      <BlockContent asset={asset} block={block} onOpenAsset={asset ? openAsset : undefined} />
    </article>
  );
});

export const BlockListPane = memo(function BlockListPane({
  assets = {},
  blocks,
  highlightedBlockIds = [],
  hoveredBlockId,
  selectedBlockId,
  onHover,
  onSelect,
  registerBlockRef,
}: BlockListPaneProps) {
  const highlightedIds = new Set(highlightedBlockIds);
  const [activeAsset, setActiveAsset] = useState<{
    asset: DocumentVisualAssetDetail;
    block: DocumentBlock;
    typeLabel: string;
  } | null>(null);
  const openAsset = useCallback((
    asset: DocumentVisualAssetDetail,
    block: DocumentBlock,
    typeLabel: string,
  ) => {
    setActiveAsset({ asset, block, typeLabel });
  }, []);
  return (
    <section className="viewer-panel viewer-panel--blocks" aria-labelledby="blocks-panel-title">
      <header className="viewer-panel__header viewer-panel__header--blocks">
        <div>
          <p>按阅读顺序整理</p>
          <h2 id="blocks-panel-title">文档内容</h2>
        </div>
        <span>{blocks.length} 个</span>
      </header>
      <div className="block-list" data-testid="block-list">
        {blocks.map((block, index) => (
          <BlockCard
            asset={block.asset_id ? assets[block.asset_id] : undefined}
            block={block}
            hovered={hoveredBlockId === block.id}
            index={index}
            key={block.id}
            onHover={onHover}
            onOpenAsset={openAsset}
            onSelect={onSelect}
            registerBlockRef={registerBlockRef}
            selected={selectedBlockId === block.id}
            supporting={highlightedIds.has(block.id)}
          />
        ))}
      </div>
      {activeAsset ? (
        <ImageAssetLightbox
          asset={activeAsset.asset}
          assetLabel={activeAsset.typeLabel}
          caption={activeAsset.block.text}
          onClose={() => setActiveAsset(null)}
        />
      ) : null}
    </section>
  );
});
