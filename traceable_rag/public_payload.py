from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import unquote

from .models import Block


_MAX_PATH_DECODE_PASSES = 8
_MAX_PUBLIC_VALUE_DEPTH = 50
_INTERNAL_WEB_PATH_PREFIXES = ("/knowledge-bases", "/traces", "/api")
_PUBLIC_BLOCK_FIELDS = (
    "id",
    "document_id",
    "page_idx",
    "block_idx",
    "block_type",
    "text",
    "html",
    "asset_id",
    "bbox",
    "page_size",
    "metadata",
)
_PUBLIC_EVIDENCE_FIELDS = (
    "id",
    "evidence_id",
    "index_evidence_id",
    "knowledge_base_id",
    "document_id",
    "display_name",
    "original_filename",
    "chunk_id",
    "chunk_text",
    "supporting_block_ids",
    "supporting_blocks",
    "block_token",
    "page_idx",
    "human_page_number",
    "page_range",
    "bbox_by_page",
    "bbox_page_size_by_page",
    "modality",
    "retrieval",
    "title_context",
    "web_url",
)


def is_server_local_path(value: object) -> bool:
    """Return whether a string identifies a server-local or file-system path."""

    if not isinstance(value, str):
        return False
    candidate = value.strip()
    for _ in range(_MAX_PATH_DECODE_PASSES):
        decoded = unquote(candidate)
        if decoded == candidate:
            break
        candidate = decoded
    else:
        return True
    candidate = candidate.strip()
    if any(
        candidate == prefix or candidate.startswith(f"{prefix}/")
        for prefix in _INTERNAL_WEB_PATH_PREFIXES
    ):
        return False
    return (
        candidate.startswith(("/", "\\"))
        or candidate.casefold().startswith("file:")
        or (
            len(candidate) >= 3
            and candidate[0].isalpha()
            and candidate[1] == ":"
            and candidate[2] in "\\/"
        )
    )


def sanitize_public_value(value: object, *, depth: int = 0) -> object:
    """Recursively remove local path strings from public metadata values."""

    if depth > _MAX_PUBLIC_VALUE_DEPTH:
        return None
    if isinstance(value, Mapping):
        return {
            str(key): sanitize_public_value(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_public_value(item, depth=depth + 1) for item in value]
    if is_server_local_path(value):
        return None
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return None


def public_block_payload(
    block: Block | Mapping[str, object], *, include_discarded: bool = False
) -> dict:
    """Serialize a Block through the public whitelist, excluding source_ref."""

    if isinstance(block, Mapping):
        values = block
        payload = {field: values[field] for field in _PUBLIC_BLOCK_FIELDS if field in values}
        if "human_page_number" in values:
            payload["human_page_number"] = values["human_page_number"]
        if include_discarded and "is_discarded" in values:
            payload["is_discarded"] = values["is_discarded"]
    else:
        payload = {field: getattr(block, field) for field in _PUBLIC_BLOCK_FIELDS}
        if include_discarded:
            payload["is_discarded"] = block.is_discarded
    page_idx = payload.get("page_idx")
    if "human_page_number" not in payload and isinstance(page_idx, int):
        payload["human_page_number"] = page_idx + 1
    if "metadata" in payload:
        payload["metadata"] = sanitize_public_value(payload["metadata"])
    return payload


def public_document_blocks(
    blocks: list[Block],
    *,
    geometry_overrides: Mapping[str, Mapping[str, object]] | None = None,
    merge_into: Mapping[str, str] | None = None,
) -> list[dict]:
    """Return logical document Blocks with every page location preserved.

    MinerU can emit one logical paragraph or table as several physical Blocks
    that share the same parser identifier, especially at page boundaries.  The
    stored rows remain untouched for compatibility, while the document viewer
    receives one content item plus all of its page-specific bounding boxes.
    """

    geometry_overrides = geometry_overrides or {}
    merge_into = merge_into or {}
    merge_targets = set(merge_into.values())
    grouped: dict[tuple[str, str], list[Block]] = {}
    order: list[tuple[str, str]] = []
    for block in blocks:
        mineru_id = block.metadata.get("mineru_id")
        if block.id in merge_into:
            group_id = merge_into[block.id]
        elif block.id in merge_targets:
            group_id = block.id
        else:
            group_id = mineru_id if isinstance(mineru_id, str) and mineru_id else block.id
        key = (group_id, block.block_type.casefold())
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(block)

    payloads: list[dict] = []
    for key in order:
        parts = grouped[key]
        primary = parts[0]
        content = max(
            parts,
            key=lambda block: (
                len(block.html or "") + len(block.text or ""),
                bool(block.asset_id),
                -block.page_idx,
                -block.block_idx,
            ),
        )
        payload = public_block_payload(primary)
        payload["text"] = content.text
        payload["html"] = content.html
        payload["asset_id"] = content.asset_id

        bbox_by_page: dict[str, list[list[float]]] = {}
        page_size_by_page: dict[str, list[float]] = {}
        for part in parts:
            page_key = str(part.page_idx)
            if part.bbox:
                boxes = bbox_by_page.setdefault(page_key, [])
                if part.bbox not in boxes:
                    boxes.append(part.bbox)
            if part.page_size:
                page_size_by_page.setdefault(page_key, part.page_size)
        stored_bbox_by_page = primary.metadata.get("bbox_by_page")
        stored_page_sizes = primary.metadata.get("bbox_page_size_by_page")
        override = geometry_overrides.get(primary.id, {})
        for candidate in (stored_bbox_by_page, override.get("bbox_by_page")):
            if not isinstance(candidate, Mapping):
                continue
            for page_key, boxes in candidate.items():
                if not isinstance(page_key, str) or not isinstance(boxes, list):
                    continue
                target = bbox_by_page.setdefault(page_key, [])
                for box in boxes:
                    if isinstance(box, list) and box not in target:
                        target.append(box)
        for candidate in (
            stored_page_sizes,
            override.get("bbox_page_size_by_page"),
        ):
            if not isinstance(candidate, Mapping):
                continue
            for page_key, page_size in candidate.items():
                if isinstance(page_key, str) and isinstance(page_size, list):
                    page_size_by_page.setdefault(page_key, page_size)
        metadata = dict(payload.get("metadata") or {})
        metadata.pop("bbox_by_page", None)
        metadata.pop("bbox_page_size_by_page", None)
        payload["metadata"] = metadata
        payload["bbox_by_page"] = bbox_by_page
        payload["bbox_page_size_by_page"] = page_size_by_page
        if len(bbox_by_page) > 1:
            metadata["cross_page"] = True
        payloads.append(payload)
    return payloads


def public_page_metadata(blocks: list[Block]) -> list[dict]:
    """Summarize public page geometry and Block counts without payload drift."""

    pages: dict[int, dict] = {}
    for block in blocks:
        page = pages.setdefault(
            block.page_idx,
            {
                "page_idx": block.page_idx,
                "human_page_number": block.page_idx + 1,
                "page_size": None,
                "block_count": 0,
            },
        )
        page["block_count"] += 1
        if page["page_size"] is None and block.page_size:
            page["page_size"] = block.page_size
    return [pages[page_idx] for page_idx in sorted(pages)]


def public_evidence_payload(evidence: Mapping[str, object]) -> dict:
    """Serialize an Evidence payload through the stable public field whitelist."""

    payload = {
        field: evidence[field]
        for field in _PUBLIC_EVIDENCE_FIELDS
        if field in evidence
    }
    if "supporting_blocks" in payload:
        payload["supporting_blocks"] = [
            public_block_payload(block)
            for block in payload["supporting_blocks"]
            if isinstance(block, (Block, Mapping))
        ]
    retrieval = payload.get("retrieval")
    if isinstance(retrieval, Mapping):
        payload["retrieval"] = {
            key: sanitize_public_value(retrieval[key])
            for key in ("score", "support_level", "mode")
            if key in retrieval
        }
    return payload
