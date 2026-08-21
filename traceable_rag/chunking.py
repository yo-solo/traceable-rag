from __future__ import annotations

import re
import uuid

from .models import Block, Chunk


DEFAULT_STRATEGY = "block_window_with_title_hints"
ATOMIC_TYPES = {"table", "image", "chart", "formula", "code", "algorithm"}
VISUAL_TYPES = {"image", "chart"}
SKIP_TYPES = {"header", "footer", "page_number"}


class BlockWindowChunker:
    def __init__(self, target_tokens: int = 450, max_tokens: int = 700, min_tokens: int = 80):
        self.target_tokens = target_tokens
        self.max_tokens = max(max_tokens, target_tokens)
        self.min_tokens = min_tokens

    def chunk(self, document_id: str, blocks: list[Block]) -> list[Chunk]:
        chunks: list[Chunk] = []
        current: list[Block] = []
        current_tokens = 0
        title_context: str | None = None

        for block in blocks:
            if block.is_discarded or block.block_type in SKIP_TYPES:
                continue

            if block.block_type in VISUAL_TYPES:
                if current:
                    chunks.append(
                        self._make_chunk(
                            document_id,
                            len(chunks),
                            current,
                            title_context,
                        )
                    )
                    current = []
                    current_tokens = 0
                # Captionless visuals remain browseable Blocks/Assets but are
                # deliberately absent from Retrieval and Source Context.
                if _block_text(block):
                    chunks.append(
                        self._make_chunk(
                            document_id,
                            len(chunks),
                            [block],
                            title_context,
                            include_title_in_index=False,
                        )
                    )
                continue

            if block.block_type == "title":
                if current and current_tokens >= self.min_tokens:
                    chunks.append(self._make_chunk(document_id, len(chunks), current, title_context))
                    current = []
                    current_tokens = 0
                title_context = _block_text(block) or title_context
                current.append(block)
                current_tokens += _estimate_tokens(_block_text(block))
                continue

            block_tokens = _estimate_tokens(_block_text(block))
            page_changed = current and current[-1].page_idx != block.page_idx
            would_overflow = current and current_tokens + block_tokens > self.max_tokens
            soft_boundary = current_tokens >= self.target_tokens and page_changed

            if current and (would_overflow or soft_boundary):
                chunks.append(self._make_chunk(document_id, len(chunks), current, title_context))
                current = []
                current_tokens = 0

            current.append(block)
            current_tokens += block_tokens

            if block.block_type in ATOMIC_TYPES and current_tokens >= self.target_tokens:
                chunks.append(self._make_chunk(document_id, len(chunks), current, title_context))
                current = []
                current_tokens = 0

        if current:
            chunks.append(self._make_chunk(document_id, len(chunks), current, title_context))
        return chunks

    def _make_chunk(
        self,
        document_id: str,
        chunk_idx: int,
        blocks: list[Block],
        title_context: str | None,
        *,
        include_title_in_index: bool = True,
    ) -> Chunk:
        chunk_text = "\n".join(text for text in (_block_text(block) for block in blocks) if text)
        indexing_text = "\n".join(
            part
            for part in [title_context if include_title_in_index else None, chunk_text]
            if part
        )
        page_range = sorted({block.page_idx for block in blocks})
        bbox_by_page: dict[str, list[list[float]]] = {}
        for block in blocks:
            if block.bbox:
                bbox_by_page.setdefault(str(block.page_idx), []).append(block.bbox)
        modality_mix = sorted({_modality(block.block_type) for block in blocks})
        return Chunk(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{document_id}:chunk:{chunk_idx}:{','.join(b.id for b in blocks)}")),
            document_id=document_id,
            chunk_idx=chunk_idx,
            strategy=DEFAULT_STRATEGY,
            block_ids=[block.id for block in blocks],
            title_context=title_context,
            inferred_heading_path=[],
            chunk_text=chunk_text,
            indexing_text=indexing_text,
            page_range=page_range,
            bbox_by_page=bbox_by_page,
            modality_mix=modality_mix,
            token_count=_estimate_tokens(indexing_text),
            strategy_version="block_window_with_title_hints.v2",
        )


def _block_text(block: Block) -> str:
    if block.html and block.block_type == "table":
        return _html_to_text(block.html)
    return block.text or ""


def _html_to_text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    ascii_words = re.findall(r"[A-Za-z0-9_]+", text)
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", text)
    other = max(0, len(text) - sum(len(word) for word in ascii_words) - len(cjk_chars))
    return len(ascii_words) + len(cjk_chars) + other // 4


def _modality(block_type: str) -> str:
    if block_type == "table":
        return "table"
    if block_type == "image":
        return "image"
    if block_type == "chart":
        return "chart"
    if block_type == "formula":
        return "formula"
    if block_type in {"code", "algorithm"}:
        return "code"
    return "text"
