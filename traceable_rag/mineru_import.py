from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .image_assets import create_image_preview, inspect_static_image
from .models import Asset, Block, Document
from .store import EvidenceStore


logger = logging.getLogger(__name__)
NORMALIZATION_VERSION = "mineru-content.v4"
AUXILIARY_TYPES = {
    "header",
    "page_header",
    "footer",
    "page_footer",
    "page_number",
    "aside_text",
    "page_aside_text",
    # MinerU v1 emits these as sibling blocks even though the same text is
    # already attached to the parent content item in content_list.json.
    "image_caption",
    "image_footnote",
    "chart_caption",
    "chart_footnote",
    "table_caption",
    "table_footnote",
    "code_caption",
    "code_footnote",
    "algorithm_caption",
    "algorithm_footnote",
}
ASSET_TYPES = {"table", "image", "chart", "formula"}
VISUAL_TYPES = {"image", "chart"}
VISUAL_CAPTION_TYPES = {"image_caption", "chart_caption"}


@dataclass(frozen=True)
class ImportResult:
    document: Document
    block_count: int
    asset_count: int


@dataclass(frozen=True)
class ParsedMinerUDocument:
    document: Document
    blocks: list[Block]
    assets: list[Asset]


@dataclass(frozen=True)
class _ManagedImportPaths:
    document_dir: Path
    active_run: Path
    result_dir: Path
    source_file: Path
    preview_file: Path


def import_mineru_directory(
    input_dir: str | Path,
    store: EvidenceStore,
    *,
    knowledge_base_id: str,
    document_id: str,
    original_filename: str,
    display_name: str,
    source_file_path: str | Path,
    preview_file_path: str | Path,
    source_type: str = "mineru_import",
    managed_root: str | Path | None = None,
) -> ImportResult:
    managed: _ManagedImportPaths | None = None
    if managed_root is not None:
        managed = _prepare_managed_import(
            input_dir=Path(input_dir),
            source_file=Path(source_file_path),
            preview_file=Path(preview_file_path),
            managed_root=Path(managed_root),
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
        )
        input_dir = managed.result_dir
        source_file_path = managed.source_file
        preview_file_path = managed.preview_file
    try:
        parsed = parse_mineru_directory(
            input_dir,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            original_filename=original_filename,
            display_name=display_name,
            source_file_path=source_file_path,
            preview_file_path=preview_file_path,
            source_type=source_type,
        )
        store.replace_document_graph(
            knowledge_base_id,
            parsed.document,
            parsed.blocks,
            parsed.assets,
            [],
            [],
        )
    except Exception:
        if managed is not None:
            _remove_managed_tree(managed.active_run)
        raise
    if managed is not None:
        try:
            _cleanup_managed_import_runs(managed.document_dir, managed.active_run)
        except Exception:
            logger.warning(
                "Could not clean up previous managed import runs after the document "
                "graph was committed; retained run directories for later cleanup."
            )
    return ImportResult(
        document=parsed.document,
        block_count=len(parsed.blocks),
        asset_count=len(parsed.assets),
    )


def parse_mineru_directory(
    input_dir: str | Path,
    *,
    knowledge_base_id: str,
    document_id: str,
    original_filename: str,
    display_name: str,
    source_file_path: str | Path,
    preview_file_path: str | Path,
    source_type: str = "mineru_import",
) -> ParsedMinerUDocument:
    input_dir = Path(input_dir)
    content_items, content_format, source_page_count = _load_content_items(input_dir)
    block_list_path = input_dir / "block_list.json"
    if content_format == "v2":
        pdf_pages = _pages_from_content_items(content_items)
        if source_page_count is not None and len(pdf_pages) < source_page_count:
            pdf_pages.extend([] for _ in range(source_page_count - len(pdf_pages)))
    elif block_list_path.exists():
        block_payload = json.loads(block_list_path.read_text(encoding="utf-8"))
        pdf_pages = block_payload.get("pdfData")
        if not isinstance(pdf_pages, list):
            raise ValueError("MinerU block_list.json does not contain pdfData pages")
    else:
        pdf_pages = _pages_from_content_items(content_items)
        if not pdf_pages:
            raise FileNotFoundError(f"Missing MinerU block_list.json or usable *_content_list.json in: {input_dir}")
    _attach_adjacent_visual_captions(pdf_pages, content_items)
    content_by_page_type = _content_index(content_items)

    source_file_path = Path(source_file_path)
    preview_file_path = Path(preview_file_path)
    document = Document(
        id=document_id,
        knowledge_base_id=knowledge_base_id,
        original_filename=original_filename,
        display_name=display_name,
        file_hash=_hash_path(source_file_path),
        source_type=source_type,
        source_file_path=str(source_file_path),
        preview_file_path=str(preview_file_path),
        mineru_result_dir=str(input_dir),
        page_count=len(pdf_pages),
        processing_status="parsed",
        processing_error=None,
        parser_provider="mineru",
        parser_version=_load_parser_version(input_dir),
        normalization_version=NORMALIZATION_VERSION,
    )

    blocks: list[Block] = []
    assets: list[Asset] = []
    global_idx = 0
    seen_block_ids: set[str] = set()
    for page in pdf_pages:
        if not isinstance(page, list):
            continue
        for raw_block in page:
            raw_type = str(raw_block.get("type", "unknown")).casefold()
            if raw_type in AUXILIARY_TYPES or bool(raw_block.get("is_discarded")):
                continue
            block_type = _normalize_block_type(raw_type, raw_block.get("sub_type"))
            text = _clean_text(raw_block.get("text") or "")
            html = raw_block.get("html")
            matched_content = _pop_best_content(
                content_by_page_type,
                raw_block.get("page_idx", 0),
                block_type,
                raw_block.get("bbox"),
            )
            content = matched_content or raw_block
            visual_captions: list[str] = []
            if block_type in VISUAL_TYPES:
                visual_captions = _visual_caption_parts(
                    raw_block,
                    content,
                    block_type,
                )
                text = _clean_text("\n".join(visual_captions))
            else:
                text = _merge_content_text(text, _content_item_text(content))
            if block_type == "table":
                html = html or content.get("table_body") or content.get("html")

            mineru_block_id = str(raw_block.get("id") or uuid.uuid5(uuid.NAMESPACE_URL, f"{document_id}:{global_idx}"))
            block_id = f"{document_id}:{mineru_block_id}"
            if block_id in seen_block_ids:
                block_id = f"{document_id}:{mineru_block_id}:{raw_block.get('page_idx', 0)}:{global_idx}"
            seen_block_ids.add(block_id)
            asset = None
            if block_type in ASSET_TYPES:
                asset = _make_asset(input_dir, document.id, block_id, block_type, raw_block, matched_content, text, html)
                assets.append(asset)

            blocks.append(
                Block(
                    id=block_id,
                    document_id=document.id,
                    page_idx=int(raw_block.get("page_idx", 0)),
                    block_idx=global_idx,
                    block_type=block_type,
                    text=text or None,
                    html=html,
                    asset_id=asset.id if asset else None,
                    bbox=_num_list(raw_block.get("bbox")),
                    page_size=_num_list(raw_block.get("page_size")),
                    source_ref=raw_block.get("block_position"),
                    is_discarded=bool(raw_block.get("is_discarded", False)),
                    metadata={
                        "mineru_id": mineru_block_id,
                        "mineru_type": raw_block.get("type"),
                        "sub_type": raw_block.get("sub_type") or content.get("sub_type"),
                        "index": raw_block.get("index"),
                        "level": raw_block.get("level") or content.get("text_level"),
                        "caption": (
                            visual_captions
                            if block_type in VISUAL_TYPES
                            else _content_caption(content, block_type)
                        ),
                        "footnotes": _content_footnotes(content, block_type),
                        "bbox_by_page": content.get("bbox_by_page"),
                        "bbox_page_size_by_page": content.get(
                            "bbox_page_size_by_page"
                        ),
                    },
                )
            )
            global_idx += 1

    return ParsedMinerUDocument(document=document, blocks=blocks, assets=assets)


def _load_content_items(
    input_dir: Path,
) -> tuple[list[dict[str, Any]], str, int | None]:
    v2_candidates = sorted(input_dir.glob("*_content_list_v2.json"))
    v2_path = v2_candidates[0] if v2_candidates else input_dir / "content_list_v2.json"
    if v2_path.exists():
        data = json.loads(v2_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return _flatten_v2_content(data), "v2", len(data)
    candidates = sorted(input_dir.glob("*_content_list.json"))
    content_path = candidates[0] if candidates else input_dir / "content_list.json"
    if not content_path.exists():
        return [], "missing", None
    data = json.loads(content_path.read_text(encoding="utf-8"))
    return (data if isinstance(data, list) else []), "v1", None


def load_cross_page_block_merges(
    input_dir: str | Path | None,
    blocks: list[Block],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Recover cross-page geometry for documents normalized before this fix."""

    if not input_dir:
        return {}, {}
    try:
        content_items, format_version, _ = _load_content_items(Path(input_dir))
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return {}, {}
    if format_version != "v2":
        return {}, {}

    geometry: dict[str, dict[str, Any]] = {}
    merge_into: dict[str, str] = {}
    for item in content_items:
        bbox_by_page = item.get("bbox_by_page")
        page_size_by_page = item.get("bbox_page_size_by_page")
        if not isinstance(bbox_by_page, dict) or len(bbox_by_page) < 2:
            continue
        block_type = _normalize_block_type(
            str(item.get("type", "content")), item.get("sub_type")
        )
        primary = _match_geometry_block(
            blocks,
            page_idx=int(item.get("page_idx", 0)),
            block_type=block_type,
            bbox=_num_list(item.get("bbox")),
        )
        if primary is None:
            continue
        geometry[primary.id] = {
            "bbox_by_page": bbox_by_page,
            "bbox_page_size_by_page": (
                page_size_by_page if isinstance(page_size_by_page, dict) else {}
            ),
        }
        for continuation in item.get("continuation_locations") or []:
            if not isinstance(continuation, dict):
                continue
            continuation_block = _match_geometry_block(
                blocks,
                page_idx=int(continuation.get("page_idx", 0)),
                block_type=block_type,
                bbox=_num_list(continuation.get("bbox")),
                empty_only=True,
            )
            if continuation_block is not None:
                merge_into[continuation_block.id] = primary.id
    return geometry, merge_into


def _match_geometry_block(
    blocks: list[Block],
    *,
    page_idx: int,
    block_type: str,
    bbox: list[float] | None,
    empty_only: bool = False,
) -> Block | None:
    if not bbox:
        return None
    candidates = [
        block
        for block in blocks
        if block.page_idx == page_idx
        and block.block_type.casefold() == block_type.casefold()
        and block.bbox
        and (
            not empty_only
            or not (block.text or block.html or block.asset_id)
        )
    ]
    if not candidates:
        return None
    matched = min(candidates, key=lambda block: _bbox_distance(bbox, block.bbox))
    return matched if _bbox_distance(bbox, matched.bbox) <= 4 else None


def _flatten_v2_content(pages: list[Any]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    previous_page_tail: dict[str, Any] | None = None
    for page_idx, page in enumerate(pages):
        if not isinstance(page, list):
            continue
        page_content: list[dict[str, Any]] = []
        for item in page:
            if not isinstance(item, dict):
                continue
            normalized = _v2_to_content_item(item, page_idx)
            raw_type = str(normalized.get("type", "content")).casefold()
            if raw_type in AUXILIARY_TYPES:
                flattened.append(normalized)
                continue
            if _is_empty_cross_page_continuation(normalized):
                if (
                    not page_content
                    and previous_page_tail is not None
                    and _is_page_boundary_continuation(
                        previous_page_tail,
                        normalized,
                    )
                    and _normalize_block_type(raw_type, normalized.get("sub_type"))
                    == _normalize_block_type(
                        str(previous_page_tail.get("type", "content")),
                        previous_page_tail.get("sub_type"),
                    )
                ):
                    _append_cross_page_location(previous_page_tail, normalized)
                continue
            flattened.append(normalized)
            page_content.append(normalized)
        if page_content:
            previous_page_tail = page_content[-1]
    return flattened


def _is_empty_cross_page_continuation(item: dict[str, Any]) -> bool:
    block_type = _normalize_block_type(
        str(item.get("type", "content")), item.get("sub_type")
    )
    if block_type == "table":
        return _is_empty_table_content(item)
    if block_type == "paragraph":
        return not _clean_text(_content_item_text(item))
    return False


def _is_page_boundary_continuation(
    primary: dict[str, Any], continuation: dict[str, Any]
) -> bool:
    bbox_by_page = primary.get("bbox_by_page")
    if isinstance(bbox_by_page, dict) and bbox_by_page:
        previous_page = max(int(page) for page in bbox_by_page)
        previous_boxes = bbox_by_page.get(str(previous_page)) or []
        previous_bbox = _num_list(previous_boxes[-1]) if previous_boxes else None
    else:
        previous_page = int(primary.get("page_idx", 0))
        previous_bbox = _num_list(primary.get("bbox"))
    if int(continuation.get("page_idx", 0)) != previous_page + 1:
        return False

    continuation_bbox = _num_list(continuation.get("bbox"))
    primary_size = _num_list(primary.get("page_size")) or [1000.0, 1000.0]
    continuation_size = _num_list(continuation.get("page_size")) or [1000.0, 1000.0]
    if not previous_bbox or not continuation_bbox:
        return False
    return (
        previous_bbox[3] >= primary_size[1] * 0.8
        and continuation_bbox[1] <= continuation_size[1] * 0.25
    )


def _append_cross_page_location(
    primary: dict[str, Any], continuation: dict[str, Any]
) -> None:
    primary_page = str(int(primary.get("page_idx", 0)))
    continuation_page = str(int(continuation.get("page_idx", 0)))
    bbox_by_page = primary.setdefault("bbox_by_page", {})
    page_size_by_page = primary.setdefault("bbox_page_size_by_page", {})
    primary_bbox = _num_list(primary.get("bbox"))
    continuation_bbox = _num_list(continuation.get("bbox"))
    primary_page_size = _num_list(primary.get("page_size"))
    continuation_page_size = _num_list(continuation.get("page_size"))
    if primary_bbox:
        bbox_by_page.setdefault(primary_page, [primary_bbox])
    if continuation_bbox:
        bbox_by_page.setdefault(continuation_page, []).append(continuation_bbox)
    if primary_page_size:
        page_size_by_page.setdefault(primary_page, primary_page_size)
    if continuation_page_size:
        page_size_by_page.setdefault(continuation_page, continuation_page_size)
    primary.setdefault("continuation_locations", []).append(
        {
            "page_idx": int(continuation.get("page_idx", 0)),
            "bbox": continuation_bbox,
            "page_size": continuation_page_size,
        }
    )


def _is_empty_table_content(item: dict[str, Any]) -> bool:
    if str(item.get("type", "")).casefold() != "table":
        return False
    if _clean_text(str(item.get("table_body") or "")):
        return False
    if _text_parts(item.get("table_caption")) or _text_parts(
        item.get("table_footnote")
    ):
        return False
    image_path = str(item.get("img_path") or "").strip()
    return not image_path or image_path.endswith(("/", "\\"))


def _v2_to_content_item(item: dict[str, Any], page_idx: int) -> dict[str, Any]:
    raw_type = str(item.get("type", "content")).casefold()
    content = item.get("content") if isinstance(item.get("content"), dict) else {}
    result: dict[str, Any] = {
        "type": raw_type,
        "page_idx": page_idx,
        "bbox": item.get("bbox"),
        "page_size": item.get("page_size") or [1000, 1000],
        "sub_type": item.get("sub_type") or content.get("sub_type"),
    }
    for relation_key in (
        "id",
        "block_id",
        "parent_id",
        "parent_block_id",
        "parent",
        "children",
        "child_ids",
    ):
        if relation_key in item:
            result[relation_key] = item[relation_key]
        elif relation_key in content:
            result[relation_key] = content[relation_key]
    if raw_type == "title":
        result["text"] = _inline_text(content.get("title_content"))
        result["text_level"] = content.get("level")
    elif raw_type == "paragraph":
        result["text"] = _inline_text(content.get("paragraph_content"))
    elif raw_type in {"list", "index", "reference"}:
        items = content.get("list_items") or []
        result["list_items"] = [
            _inline_text(entry.get("item_content"))
            for entry in items
            if isinstance(entry, dict)
        ]
        result["text"] = "\n".join(part for part in result["list_items"] if part)
        result["sub_type"] = result.get("sub_type") or content.get("list_type")
    elif raw_type in {"equation", "equation_interline", "interline_equation"}:
        result["text"] = _inline_text(content.get("math_content"))
    elif raw_type in {"image", "chart"}:
        visual_type = _normalize_block_type(raw_type, result.get("sub_type"))
        result["img_path"] = _image_source_path(content)
        result[f"{visual_type}_caption"] = _unique_text(
            _text_parts(content.get(f"{visual_type}_caption"))
            + (
                _text_parts(content.get(f"{raw_type}_caption"))
                if raw_type != visual_type
                else []
            )
        )
        result[f"{visual_type}_footnote"] = _unique_text(
            _text_parts(content.get(f"{visual_type}_footnote"))
            + (
                _text_parts(content.get(f"{raw_type}_footnote"))
                if raw_type != visual_type
                else []
            )
        )
        # Deliberately ignore MinerU's optional image-analysis content.  The
        # original pixels are the visual source of truth; only explicit
        # captions participate in text retrieval.
        result["text"] = _clean_text(" ".join(result[f"{visual_type}_caption"]))
    elif raw_type == "table":
        result["img_path"] = _image_source_path(content)
        result["table_body"] = content.get("html") or content.get("table_body")
        result["table_caption"] = _text_parts(content.get("table_caption"))
        result["table_footnote"] = _text_parts(content.get("table_footnote"))
        result["text"] = _table_related_text(result)
    elif raw_type in {"code", "algorithm"}:
        prefix = "algorithm" if raw_type == "algorithm" else "code"
        body = (
            content.get(f"{prefix}_content")
            or content.get(f"{prefix}_body")
            or content.get("code_body")
            or ""
        )
        result["text"] = _inline_text(body) if isinstance(body, list) else str(body)
        result[f"{prefix}_caption"] = _text_parts(content.get(f"{prefix}_caption"))
        result[f"{prefix}_footnote"] = _text_parts(content.get(f"{prefix}_footnote"))
        result["text"] = _clean_text(
            " ".join(result[f"{prefix}_caption"] + [result["text"]] + result[f"{prefix}_footnote"])
        )
    elif raw_type in {"page_footnote", "footnote"}:
        result["text"] = _inline_text(
            content.get("footnote_content") or content.get("page_footnote_content")
        )
    else:
        content_key = f"{raw_type}_content"
        fallback = content.get("text") or content.get("content") or ""
        result["text"] = _inline_text(content.get(content_key)) or (
            _inline_text(fallback) if isinstance(fallback, list) else str(fallback)
        )
    return result


def _pages_from_content_items(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    pages: dict[int, list[dict[str, Any]]] = {}
    inferred_page_sizes = _infer_content_page_sizes(items)
    for idx, item in enumerate(items):
        page_idx = int(item.get("page_idx", 0))
        raw_type = item.get("type", "unknown")
        text = _content_item_text(item)
        raw = {
            **item,
            "id": str(
                item.get("id")
                or uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"content-list:{page_idx}:{idx}:{text[:40]}",
                )
            ),
            "bbox": item.get("bbox"),
            "type": raw_type,
            "index": idx,
            "text": text,
            "page_idx": page_idx,
            "page_size": item.get("page_size") or inferred_page_sizes.get(page_idx),
            "block_position": f"{page_idx}-{idx}",
        }
        if item.get("text_level"):
            raw["level"] = item["text_level"]
            raw["type"] = "title"
        pages.setdefault(page_idx, []).append(raw)
    if not pages:
        return []
    return [pages.get(page_idx, []) for page_idx in range(max(pages) + 1)]


def _infer_content_page_sizes(items: list[dict[str, Any]]) -> dict[int, list[float]]:
    boxes_by_page: dict[int, list[list[float]]] = {}
    explicit: dict[int, list[float]] = {}
    for item in items:
        page_idx = int(item.get("page_idx", 0))
        page_size = _num_list(item.get("page_size"))
        if page_size:
            explicit[page_idx] = page_size
            continue
        bbox = _num_list(item.get("bbox"))
        if bbox and len(bbox) == 4:
            boxes_by_page.setdefault(page_idx, []).append(bbox)

    inferred = dict(explicit)
    for page_idx, boxes in boxes_by_page.items():
        if page_idx in inferred:
            continue
        max_x = max(box[2] for box in boxes)
        max_y = max(box[3] for box in boxes)
        if max_x <= 1000 and max_y <= 1000 and (max_x > 700 or max_y > 900):
            inferred[page_idx] = [1000.0, 1000.0]
    return inferred


def _content_index(items: list[dict[str, Any]]) -> dict[tuple[int, str], list[dict[str, Any]]]:
    out: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for item in items:
        page_idx = int(item.get("page_idx", 0))
        kind = _normalize_block_type(item.get("type", "unknown"), item.get("sub_type"))
        out.setdefault((page_idx, kind), []).append(item)
    return out


def _pop_best_content(
    index: dict[tuple[int, str], list[dict[str, Any]]],
    page_idx: int,
    block_type: str,
    bbox: list[float] | None,
) -> dict[str, Any] | None:
    candidates = index.get((int(page_idx), block_type)) or []
    if not candidates:
        return None
    if not bbox:
        return candidates.pop(0)
    best_pos = min(range(len(candidates)), key=lambda i: _bbox_distance(bbox, candidates[i].get("bbox")))
    return candidates.pop(best_pos)


def _bbox_distance(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b or len(a) != 4 or len(b) != 4:
        return 1e12
    ac = ((a[0] + a[2]) / 2, (a[1] + a[3]) / 2)
    bc = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
    return abs(ac[0] - bc[0]) + abs(ac[1] - bc[1])


def _attach_adjacent_visual_captions(
    pdf_pages: list[Any], content_items: list[dict[str, Any]]
) -> None:
    """Attach detached, explicitly typed captions before auxiliary filtering."""

    for page in pdf_pages:
        if not isinstance(page, list):
            continue
        visuals = [
            index
            for index, block in enumerate(page)
            if isinstance(block, dict)
            and not bool(block.get("is_discarded"))
            and _normalize_block_type(
                str(block.get("type", "unknown")), block.get("sub_type")
            )
            in VISUAL_TYPES
        ]
        captions = [
            index
            for index, block in enumerate(page)
            if isinstance(block, dict)
            and not bool(block.get("is_discarded"))
            and str(block.get("type", "")).casefold() in VISUAL_CAPTION_TYPES
        ]
        if not visuals or not captions:
            continue
        visual_by_id = {
            identifier: index
            for index in visuals
            if (identifier := _block_identifier(page[index])) is not None
        }
        caption_by_id = {
            identifier: index
            for index in captions
            if (identifier := _block_identifier(page[index])) is not None
        }
        assigned: dict[int, list[tuple[int, str]]] = {}
        consumed: set[int] = set()

        for caption_index in captions:
            parent_id = _caption_parent_identifier(page[caption_index])
            visual_index = visual_by_id.get(parent_id) if parent_id else None
            if visual_index is None or not _caption_matches_visual(
                page[caption_index], page[visual_index]
            ):
                continue
            _assign_caption(page, assigned, consumed, visual_index, caption_index)

        for visual_index in visuals:
            for child_id in _child_identifiers(page[visual_index]):
                caption_index = caption_by_id.get(child_id)
                if (
                    caption_index is None
                    or caption_index in consumed
                    or not _caption_matches_visual(
                        page[caption_index], page[visual_index]
                    )
                ):
                    continue
                _assign_caption(page, assigned, consumed, visual_index, caption_index)

        _claim_content_parent_captions(
            page,
            visuals,
            captions,
            content_items,
            assigned,
            consumed,
        )

        for caption_index in captions:
            if caption_index in consumed:
                continue
            candidates = _adjacent_visual_candidates(page, caption_index)
            if not candidates:
                continue
            caption_bbox = _num_list(page[caption_index].get("bbox"))
            visual_index = min(
                candidates,
                key=lambda index: (
                    _bbox_distance(caption_bbox, _num_list(page[index].get("bbox"))),
                    abs(index - caption_index),
                    index,
                ),
            )
            _assign_caption(page, assigned, consumed, visual_index, caption_index)

        for visual_index, values in assigned.items():
            ordered = [text for _index, text in sorted(values)]
            page[visual_index]["_adjacent_visual_captions"] = _unique_text(ordered)


def _claim_content_parent_captions(
    page: list[Any],
    visuals: list[int],
    captions: list[int],
    content_items: list[dict[str, Any]],
    assigned: dict[int, list[tuple[int, str]]],
    consumed: set[int],
) -> None:
    if not page:
        return
    page_idx = int(next((item.get("page_idx", 0) for item in page if isinstance(item, dict)), 0))
    content_by_type: dict[str, list[dict[str, Any]]] = {}
    for item in content_items:
        if int(item.get("page_idx", 0)) != page_idx:
            continue
        block_type = _normalize_block_type(item.get("type", "unknown"), item.get("sub_type"))
        if block_type in VISUAL_TYPES:
            content_by_type.setdefault(block_type, []).append(item)

    available_by_type = {key: list(values) for key, values in content_by_type.items()}
    for visual_index in visuals:
        visual = page[visual_index]
        visual_type = _normalize_block_type(
            visual.get("type", "unknown"), visual.get("sub_type")
        )
        candidates = available_by_type.get(visual_type) or []
        if not candidates:
            continue
        match_index = min(
            range(len(candidates)),
            key=lambda index: _bbox_distance(
                _num_list(visual.get("bbox")),
                _num_list(candidates[index].get("bbox")),
            ),
        )
        content = candidates.pop(match_index)
        for parent_text in _content_caption(content, visual_type):
            matching_caption_indices = [
                caption_index
                for caption_index in captions
                if caption_index not in consumed
                and _caption_matches_visual(page[caption_index], visual)
                and _clean_text(str(page[caption_index].get("text") or ""))
                == _clean_text(parent_text)
            ]
            if not matching_caption_indices:
                continue
            caption_index = min(
                matching_caption_indices,
                key=lambda index: (
                    _bbox_distance(
                        _num_list(page[index].get("bbox")),
                        _num_list(visual.get("bbox")),
                    ),
                    abs(index - visual_index),
                ),
            )
            _assign_caption(page, assigned, consumed, visual_index, caption_index)


def _adjacent_visual_candidates(page: list[Any], caption_index: int) -> list[int]:
    candidates: list[int] = []
    for step in (-1, 1):
        index = caption_index + step
        while 0 <= index < len(page):
            block = page[index]
            if not isinstance(block, dict):
                break
            raw_type = str(block.get("type", "unknown")).casefold()
            if raw_type in VISUAL_CAPTION_TYPES:
                index += step
                continue
            if (
                _normalize_block_type(raw_type, block.get("sub_type"))
                in VISUAL_TYPES
                and _caption_matches_visual(page[caption_index], block)
            ):
                candidates.append(index)
            break
    return candidates


def _block_identifier(block: dict[str, Any]) -> str | None:
    for key in ("id", "block_id"):
        value = block.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _caption_parent_identifier(block: dict[str, Any]) -> str | None:
    for key in ("parent_id", "parent_block_id", "parent"):
        value = block.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            identifier = _block_identifier(value)
            if identifier:
                return identifier
    return None


def _child_identifiers(block: dict[str, Any]) -> list[str]:
    value = block.get("children") or block.get("child_ids") or []
    if not isinstance(value, list):
        return []
    identifiers: list[str] = []
    for child in value:
        if isinstance(child, str) and child:
            identifiers.append(child)
        elif isinstance(child, dict):
            identifier = _block_identifier(child)
            if identifier:
                identifiers.append(identifier)
    return identifiers


def _caption_matches_visual(
    caption: dict[str, Any], visual: dict[str, Any]
) -> bool:
    caption_type = str(caption.get("type", "")).casefold()
    raw_visual_type = str(visual.get("type", "unknown")).casefold()
    visual_type = _normalize_block_type(
        raw_visual_type, visual.get("sub_type")
    )
    if visual_type == "image":
        return caption_type == "image_caption"
    if visual_type == "chart":
        return caption_type == "chart_caption" or (
            raw_visual_type == "image" and caption_type == "image_caption"
        )
    return False


def _assign_caption(
    page: list[Any],
    assigned: dict[int, list[tuple[int, str]]],
    consumed: set[int],
    visual_index: int,
    caption_index: int,
) -> None:
    text = _clean_text(str(page[caption_index].get("text") or ""))
    if text:
        assigned.setdefault(visual_index, []).append((caption_index, text))
    consumed.add(caption_index)


def _visual_caption_parts(
    raw_block: dict[str, Any],
    content: dict[str, Any],
    block_type: str,
) -> list[str]:
    adjacent = raw_block.get("_adjacent_visual_captions")
    parts = list(adjacent) if isinstance(adjacent, list) else []
    parts.extend(_content_caption(content, block_type))
    return _unique_text(parts)


def _unique_text(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_text(str(value or ""))
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _make_asset(
    input_dir: Path,
    document_id: str,
    block_id: str,
    block_type: str,
    raw_block: dict[str, Any],
    content: dict[str, Any] | None,
    text: str,
    html: str | None,
) -> Asset:
    asset_path: Path | None = None
    if content and content.get("img_path"):
        asset_path = _safe_asset_path(input_dir, str(content["img_path"]))
    elif raw_block.get("img_path"):
        asset_path = _safe_asset_path(
            input_dir, str(Path("images") / str(raw_block["img_path"]))
        )
    asset_path_str = (
        asset_path.relative_to(input_dir.resolve()).as_posix()
        if asset_path
        else None
    )
    asset_type = "formula" if block_type == "formula" else block_type
    related_text = text or _html_to_text(html or "")
    file_hash = _hash_path(asset_path) if asset_path else None
    content_type = _content_type(asset_path_str, asset_type)
    metadata: dict[str, Any] = {
        "has_html": bool(html),
        "source_img_path": (content or raw_block).get("img_path"),
    }
    if asset_path and asset_type in VISUAL_TYPES and file_hash:
        image_info = inspect_static_image(asset_path)
        if image_info is None:
            content_type = None
            metadata["renderable"] = False
        else:
            content_type = image_info.content_type
            metadata.update(
                {
                    "renderable": True,
                    "width": image_info.width,
                    "height": image_info.height,
                }
            )
            preview = create_image_preview(
                input_dir,
                asset_path,
                file_hash=file_hash,
                info=image_info,
            )
            if preview:
                metadata["preview_file_path"] = preview.key
                metadata["preview_file_hash"] = preview.file_hash
                metadata["preview_content_type"] = preview.content_type
    asset_identity = ":".join(
        (document_id, block_id, asset_type, file_hash or "missing")
    )
    return Asset(
        id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{asset_identity}:asset")),
        document_id=document_id,
        block_id=block_id,
        asset_type=asset_type,
        file_path=asset_path_str,
        content_type=content_type,
        file_hash=file_hash,
        page_idx=int(raw_block.get("page_idx", 0)),
        bbox=_num_list(raw_block.get("bbox")),
        related_text=related_text or None,
        metadata=metadata,
    )


def _safe_asset_path(input_dir: Path, relative_path: str) -> Path | None:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        return None
    root = input_dir.resolve()
    candidate = input_dir.joinpath(*relative.parts)
    current = input_dir
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return None
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def _normalize_block_type(raw_type: str, sub_type: Any = None) -> str:
    raw_type = str(raw_type).casefold()
    normalized_sub_type = str(sub_type or "").casefold()
    if raw_type == "list" and normalized_sub_type in {
        "ref_text",
        "reference",
        "reference_list",
        "reference",
        "ref",
    }:
        return "reference"
    if raw_type == "list" and normalized_sub_type in {"index", "catalog", "toc"}:
        return "index"
    if raw_type == "image" and normalized_sub_type in {"chart", "plot", "figure_chart"}:
        return "chart"
    mapping = {
        "text": "paragraph",
        "paragraph": "paragraph",
        "title": "title",
        "list": "list",
        "index": "index",
        "reference": "reference",
        "ref_text": "reference",
        "table": "table",
        "table_body": "table",
        "image": "image",
        "chart": "chart",
        "equation": "formula",
        "equation_interline": "formula",
        "interline_equation": "formula",
        "code": "code",
        "algorithm": "algorithm",
        "page_footnote": "footnote",
        "footnote": "footnote",
        "header": "header",
        "page_header": "header",
        "footer": "footer",
        "page_footer": "footer",
        "page_number": "page_number",
        "aside_text": "aside",
        "page_aside_text": "aside",
    }
    return mapping.get(raw_type, "content")


def _content_item_text(content: dict[str, Any]) -> str:
    raw_type = str(content.get("type", "content")).casefold()
    if raw_type == "table":
        return _table_related_text(content)
    if raw_type in {"image", "chart"}:
        return _clean_text(" ".join(_content_caption(content, raw_type)))
    if raw_type in {"code", "algorithm"}:
        return _clean_text(
            " ".join(
                _content_caption(content, raw_type)
                + [str(content.get("text") or content.get("code_body") or "")]
                + _content_footnotes(content, raw_type)
            )
        )
    if raw_type in {"list", "index", "reference"}:
        return _clean_text("\n".join(str(value) for value in content.get("list_items") or []))
    return _clean_text(str(content.get("text") or ""))


def _merge_content_text(primary: str, related: str) -> str:
    """Keep parent body and attached annotations without duplicating either."""
    primary = _clean_text(primary)
    related = _clean_text(related)
    if not primary:
        return related
    if not related or related in primary:
        return primary
    if primary in related:
        return related
    return _clean_text(f"{primary}\n{related}")


def _content_caption(content: dict[str, Any], block_type: str) -> list[str]:
    prefix = "formula" if block_type == "formula" else block_type
    values = _text_parts(content.get(f"{prefix}_caption"))
    raw_type = str(content.get("type", "")).casefold()
    if block_type in VISUAL_TYPES and raw_type in VISUAL_TYPES and raw_type != block_type:
        values.extend(_text_parts(content.get(f"{raw_type}_caption")))
    if not values:
        values = _text_parts(content.get("caption"))
    return _unique_text(values)


def _content_footnotes(content: dict[str, Any], block_type: str) -> list[str]:
    prefix = "formula" if block_type == "formula" else block_type
    values = _text_parts(content.get(f"{prefix}_footnote"))
    raw_type = str(content.get("type", "")).casefold()
    if block_type in VISUAL_TYPES and raw_type in VISUAL_TYPES and raw_type != block_type:
        values.extend(_text_parts(content.get(f"{raw_type}_footnote")))
    if not values:
        values = _text_parts(content.get("footnotes"))
    return _unique_text(values)


def _text_parts(value: Any) -> list[str]:
    if not isinstance(value, list):
        return [str(value)] if value else []
    parts: list[str] = []
    for item in value:
        if isinstance(item, dict):
            text = item.get("content") or item.get("text")
            if text:
                text = str(text)
                span_type = str(item.get("type") or "").casefold()
                if span_type in {"equation_inline", "inline_equation", "inline_formula"}:
                    if not (
                        (text.startswith("$") and text.endswith("$"))
                        or (text.startswith(r"\(") and text.endswith(r"\)"))
                    ):
                        text = rf"\({text}\)"
                parts.append(text)
        elif item:
            parts.append(str(item))
    return parts


def _inline_text(value: Any) -> str:
    return _clean_text("".join(_text_parts(value)))


def _image_source_path(content: dict[str, Any]) -> str | None:
    source = content.get("image_source")
    if isinstance(source, dict) and source.get("path"):
        return str(source["path"])
    return None


def _table_related_text(content: dict[str, Any]) -> str:
    parts: list[str] = []
    parts.extend(_text_parts(content.get("table_caption")))
    parts.append(_html_to_text(content.get("table_body") or ""))
    parts.extend(_text_parts(content.get("table_footnote")))
    return _clean_text(" ".join(part for part in parts if part))


def _html_to_text(html: str) -> str:
    return _clean_text(re.sub(r"<[^>]+>", " ", html))


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _num_list(value: Any) -> list[float] | None:
    if not isinstance(value, list):
        return None
    return [float(v) for v in value]


def _load_parser_version(input_dir: Path) -> str | None:
    layout_path = input_dir / "layout.json"
    if layout_path.exists():
        try:
            data = json.loads(layout_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        version = data.get("_version_name") or data.get("_backend")
        if version:
            return str(version)
    processing_manifest = input_dir / "processing_manifest.json"
    if processing_manifest.exists():
        try:
            data = json.loads(processing_manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        mineru = data.get("mineru") if isinstance(data, dict) else None
        if isinstance(mineru, dict) and mineru.get("version"):
            return str(mineru["version"])
    return None


def _hash_path(path: Path | None) -> str:
    if not path:
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _content_type(path: str | None, asset_type: str) -> str | None:
    if asset_type == "table":
        return "text/html"
    if asset_type == "formula":
        return "text/latex"
    if not path:
        return None
    suffix = Path(path).suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(suffix)


def _prepare_managed_import(
    *,
    input_dir: Path,
    source_file: Path,
    preview_file: Path,
    managed_root: Path,
    knowledge_base_id: str,
    document_id: str,
) -> _ManagedImportPaths:
    if not input_dir.is_dir() or input_dir.is_symlink():
        raise ValueError("MinerU import directory must be a real directory")
    if not source_file.is_file() or source_file.is_symlink():
        raise ValueError("Managed import source file is unavailable")
    if not preview_file.is_file() or preview_file.is_symlink():
        raise ValueError("Managed import preview file is unavailable")
    _reject_tree_symlinks(input_dir)

    if managed_root.is_symlink():
        raise ValueError("Managed storage root cannot be a symlink")
    managed_root.mkdir(parents=True, exist_ok=True)
    root = managed_root.resolve(strict=True)
    knowledge_base_part = hashlib.sha256(
        knowledge_base_id.encode("utf-8")
    ).hexdigest()[:16]
    document_part = hashlib.sha256(document_id.encode("utf-8")).hexdigest()[:24]
    document_dir = root / knowledge_base_part / document_part
    _make_managed_directory(root, document_dir)

    source_hash = _hash_path(source_file)
    safe_suffix = source_file.suffix.lower()[:16]
    managed_source = document_dir / "source" / f"{source_hash[:24]}{safe_suffix}"
    _copy_managed_file(root, source_file, managed_source, expected_hash=source_hash)

    run_id = f"import-{uuid.uuid4().hex}"
    staging_run = document_dir / ".staging" / run_id
    active_run = document_dir / "runs" / run_id
    _make_managed_directory(root, staging_run)
    staged_result = staging_run / "mineru" / "result"
    staged_preview = staging_run / "preview" / "imported.pdf"
    try:
        shutil.copytree(input_dir, staged_result)
        _copy_managed_file(
            root,
            preview_file,
            staged_preview,
            expected_hash=_hash_path(preview_file),
        )
        _make_managed_directory(root, active_run.parent)
        staging_run.rename(active_run)
    except Exception:
        _remove_managed_tree(staging_run)
        raise
    return _ManagedImportPaths(
        document_dir=document_dir,
        active_run=active_run,
        result_dir=active_run / "mineru" / "result",
        source_file=managed_source,
        preview_file=active_run / "preview" / "imported.pdf",
    )


def _reject_tree_symlinks(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("MinerU import directory cannot contain symlinks")
        if not path.is_dir() and not path.is_file():
            raise ValueError("MinerU import directory contains an unsupported entry")


def _make_managed_directory(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError("Managed import path escapes storage root") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Managed import path contains a symlink")
        current.mkdir(exist_ok=True)


def _copy_managed_file(
    root: Path,
    source: Path,
    destination: Path,
    *,
    expected_hash: str,
) -> None:
    _make_managed_directory(root, destination.parent)
    if destination.exists():
        if destination.is_symlink() or not destination.is_file():
            raise ValueError("Managed import destination is invalid")
        if _hash_path(destination) != expected_hash:
            raise ValueError("Managed import destination hash does not match")
        return
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    try:
        shutil.copy2(source, temporary)
        if _hash_path(temporary) != expected_hash:
            raise ValueError("Managed import copy hash does not match")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _cleanup_managed_import_runs(document_dir: Path, active_run: Path) -> None:
    runs_root = document_dir / "runs"
    if runs_root.is_symlink() or not runs_root.is_dir():
        return
    for run in runs_root.iterdir():
        if run != active_run:
            _remove_managed_tree(run)
    staging_root = document_dir / ".staging"
    if staging_root.is_dir() and not staging_root.is_symlink() and not any(
        staging_root.iterdir()
    ):
        staging_root.rmdir()


def _remove_managed_tree(path: Path) -> None:
    if path.is_symlink():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)
