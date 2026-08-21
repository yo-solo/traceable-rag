import DOMPurify from "dompurify";
import katex from "katex";
import { Fragment, memo, type ReactNode, useMemo } from "react";

import type { DocumentBlock, VisualAssetDetail } from "../../api/types";
import { ImageAssetPreview } from "./ImageAssetPreview";

const MATH_EXPRESSION = /(\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\$[^$\n]+?\$|\\\([^\n]+?\\\))/g;
const FORMULA_BLOCK_TYPES = new Set([
  "formula",
  "equation",
  "equation_interline",
  "interline_equation",
]);
const MINERU_ALLOWED_TAGS = [
  "div",
  "p",
  "br",
  "span",
  "strong",
  "b",
  "em",
  "i",
  "u",
  "s",
  "sub",
  "sup",
  "blockquote",
  "pre",
  "code",
  "ul",
  "ol",
  "li",
  "dl",
  "dt",
  "dd",
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "hr",
  "table",
  "caption",
  "colgroup",
  "col",
  "thead",
  "tbody",
  "tfoot",
  "tr",
  "th",
  "td",
];
const MINERU_ALLOWED_ATTRIBUTES = [
  "abbr",
  "colspan",
  "headers",
  "rowspan",
  "scope",
  "span",
];

function sanitizeMineruHtml(html: string) {
  return DOMPurify.sanitize(html, {
    ALLOWED_ATTR: MINERU_ALLOWED_ATTRIBUTES,
    ALLOWED_TAGS: MINERU_ALLOWED_TAGS,
    ALLOW_ARIA_ATTR: false,
    ALLOW_DATA_ATTR: false,
    KEEP_CONTENT: true,
  });
}

function mathToken(token: string) {
  if (token.startsWith("$$") && token.endsWith("$$")) {
    return { displayMode: true, source: token.slice(2, -2) };
  }
  if (token.startsWith("\\[") && token.endsWith("\\]")) {
    return { displayMode: true, source: token.slice(2, -2) };
  }
  if (token.startsWith("$") && token.endsWith("$")) {
    return { displayMode: false, source: token.slice(1, -1) };
  }
  if (token.startsWith("\\(") && token.endsWith("\\)")) {
    return { displayMode: false, source: token.slice(2, -2) };
  }
  return null;
}

function renderMath(
  source: string,
  displayMode: boolean,
  key: number,
  fallback = source,
): ReactNode {
  try {
    const html = katex.renderToString(source, {
      displayMode,
      output: "htmlAndMathml",
      strict: "ignore",
      throwOnError: true,
    });
    const sanitized = DOMPurify.sanitize(html, {
      USE_PROFILES: { html: true },
    });
    return displayMode ? (
      <div
        className="block-content__math block-content__math--display"
        dangerouslySetInnerHTML={{ __html: sanitized }}
        key={key}
      />
    ) : (
      <span
        className="block-content__math block-content__math--inline"
        dangerouslySetInnerHTML={{ __html: sanitized }}
        key={key}
      />
    );
  } catch {
    return <Fragment key={key}>{fallback}</Fragment>;
  }
}

function RichText({ text }: { text: string }) {
  const content = useMemo(() => {
    const parts = text.split(MATH_EXPRESSION);
    return parts.map((part, index) => {
      const token = mathToken(part);
      return token
        ? renderMath(token.source, token.displayMode, index, part)
        : <Fragment key={index}>{part}</Fragment>;
    });
  }, [text]);
  return (
    <div className="block-content block-content--text">
      {content}
    </div>
  );
}

type BlockContentProps = {
  asset?: VisualAssetDetail | null;
  block: DocumentBlock;
  onOpenAsset?: () => void;
};

export const BlockContent = memo(function BlockContent({
  asset,
  block,
  onOpenAsset,
}: BlockContentProps) {
  const sanitizedHtml = useMemo(
    () => block.html ? sanitizeMineruHtml(block.html) : null,
    [block.html],
  );

  const blockType = block.block_type.toLowerCase();
  if (blockType === "image" || blockType === "chart") {
    return (
      <div className="block-content block-content--image-asset">
        <ImageAssetPreview
          asset={asset}
          assetLabel={blockType === "chart" ? "图表" : "图片"}
          caption={block.text}
          onOpen={onOpenAsset}
        />
      </div>
    );
  }

  if (block.html) {
    return (
      <div
        className="block-content block-content--html"
        dangerouslySetInnerHTML={{ __html: sanitizedHtml ?? "" }}
      />
    );
  }

  if (block.text) {
    if (FORMULA_BLOCK_TYPES.has(blockType)) {
      const source = block.text.trim();
      const token = mathToken(source);
      if (!token && source.split(MATH_EXPRESSION).length > 1) {
        return <RichText text={block.text} />;
      }
      return (
        <div className="block-content block-content--formula">
          {renderMath(token?.source ?? source, true, 0, source)}
        </div>
      );
    }
    return <RichText text={block.text} />;
  }

  return <p className="block-content block-content--empty">这项内容暂时无法显示。</p>;
});
