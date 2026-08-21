from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

from .image_assets import VISUAL_ASSET_TYPES, visual_asset_payload
from .evidence_service import EvidenceSearchResult
from .models import Block
from .store import EvidenceStore


MAX_RETRIEVAL_TEXT_CHARS = 8_000
MAX_SOURCE_TEXT_CHARS = 8_000
MAX_CONTEXT_ITEM_CHARS = 2_000
MAX_CONTEXT_RESPONSE_TEXT_CHARS = 16_000
_SOURCE_ID_PATTERN = re.compile(r"src_[0-9a-f]{32}")


class SourceNotFoundError(LookupError):
    """Raised when a value has never identified a Source."""


class SourceGoneError(LookupError):
    """Raised when a once-valid Source no longer has a current document graph."""


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


class SourceService:
    def __init__(self, store: EvidenceStore):
        self.store = store

    def retrieval_sources(self, result: EvidenceSearchResult) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []
        for item in result.results:
            evidence_id = item["index_evidence_id"]
            record_id = result.record_id_by_evidence_id[evidence_id]
            supporting_block_ids = item.get("supporting_block_ids") or []
            if not supporting_block_ids:
                continue
            anchor = self.store.get_or_create_active_source_anchor(
                record_id, supporting_block_ids[0]
            )
            graph = self.store.load_active_source_graph(anchor.id)
            if graph is None:
                continue
            _anchor, document, _chunk, _record, block = graph
            text = result.indexing_text_by_evidence_id.get(
                evidence_id, item.get("chunk_text", "")
            )
            payload = {
                "source_id": anchor.id,
                "source_url": f"/sources/{anchor.id}",
                "text": text[:MAX_RETRIEVAL_TEXT_CHARS],
                "document_id": document.id,
                "document_name": document.display_name,
                "human_page_number": block.page_idx + 1,
                "bbox": block.bbox,
                "score": item.get("retrieval", {}).get("score", 0.0),
            }
            if block.block_type in VISUAL_ASSET_TYPES and block.asset_id:
                asset = self.store.get_asset(
                    document.knowledge_base_id,
                    document.id,
                    block.asset_id,
                )
                descriptor = (
                    visual_asset_payload(
                        document.knowledge_base_id,
                        asset,
                        detailed=False,
                    )
                    if asset
                    else None
                )
                if descriptor is not None:
                    payload["asset"] = descriptor
            sources.append(payload)
        return sources

    def context(self, source_id: str, *, before: int = 2, after: int = 2) -> dict:
        if _SOURCE_ID_PATTERN.fullmatch(source_id) is None:
            raise SourceNotFoundError(source_id)
        status = self.store.source_anchor_status(source_id)
        if status == "unknown":
            raise SourceNotFoundError(source_id)
        if status == "gone":
            raise SourceGoneError(source_id)

        graph = self.store.load_active_source_graph(source_id)
        context_blocks = self.store.list_source_context_blocks(
            source_id, before=before, after=after
        )
        if graph is None or context_blocks is None:
            raise SourceGoneError(source_id)
        _anchor, document, chunk, _record, source_block = graph
        selected_block, before_blocks, after_blocks = context_blocks
        if selected_block.id != source_block.id:
            raise SourceGoneError(source_id)

        truncated = False
        source_text, source_was_truncated = _bounded_text(
            _block_text(source_block), MAX_SOURCE_TEXT_CHARS
        )
        truncated = truncated or source_was_truncated
        remaining = max(0, MAX_CONTEXT_RESPONSE_TEXT_CHARS - len(source_text))

        before_payload, remaining, before_truncated = _context_payloads(
            before_blocks, remaining
        )
        after_payload, remaining, after_truncated = _context_payloads(
            after_blocks, remaining
        )
        truncated = truncated or before_truncated or after_truncated

        title_path = [value for value in chunk.inferred_heading_path if value]
        if not title_path and chunk.title_context:
            title_path = [chunk.title_context]
        source_payload = {
            "source_id": source_id,
            "source_url": f"/sources/{source_id}",
            "text": source_text,
            "document_id": document.id,
            "document_name": document.display_name,
            "knowledge_base_id": document.knowledge_base_id,
            "human_page_number": source_block.page_idx + 1,
            "bbox": source_block.bbox,
            "page_size": source_block.page_size,
            "title_path": title_path,
            "file_url": (
                f"/api/knowledge-bases/{document.knowledge_base_id}"
                f"/documents/{document.id}/file"
            ),
        }
        if source_block.block_type in VISUAL_ASSET_TYPES and source_block.asset_id:
            asset = self.store.get_asset(
                document.knowledge_base_id,
                document.id,
                source_block.asset_id,
            )
            descriptor = (
                visual_asset_payload(
                    document.knowledge_base_id,
                    asset,
                    detailed=True,
                )
                if asset
                else None
            )
            if descriptor is not None:
                source_payload["asset"] = descriptor
        return {
            "source": source_payload,
            "before": before_payload,
            "after": after_payload,
            "truncated": truncated,
        }


def _context_payloads(
    blocks: list[Block], remaining: int
) -> tuple[list[dict[str, Any]], int, bool]:
    payloads: list[dict[str, Any]] = []
    truncated = False
    for index, block in enumerate(blocks):
        if block.block_type in VISUAL_ASSET_TYPES and not _block_text(block):
            continue
        if remaining <= 0:
            return payloads, remaining, True
        text, item_truncated = _bounded_text(
            _block_text(block), min(MAX_CONTEXT_ITEM_CHARS, remaining)
        )
        payloads.append(
            {
                "text": text,
                "human_page_number": block.page_idx + 1,
                "bbox": block.bbox,
                "page_size": block.page_size,
                "block_type": block.block_type,
            }
        )
        remaining -= len(text)
        truncated = truncated or item_truncated
        if remaining <= 0 and index < len(blocks) - 1:
            truncated = True
            break
    return payloads, remaining, truncated


def _bounded_text(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def _block_text(block: Block) -> str:
    if block.text and block.text.strip():
        return block.text.strip()
    if not block.html:
        return ""
    parser = _HTMLTextExtractor()
    parser.feed(block.html)
    return "\n".join(parser.parts)
