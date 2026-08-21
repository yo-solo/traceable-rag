from __future__ import annotations

import re
import uuid
from html.parser import HTMLParser

from traceable_rag.embedding import EmbeddingProvider
from traceable_rag.indexing import embed_index_records
from traceable_rag.models import Block, Chunk, IndexRecord


TABLE_ROW_TARGET_TOKENS = 220


def build_table_aware_index_records(
    chunks: list[Chunk],
    blocks: list[Block],
    embedding_provider: EmbeddingProvider,
    *,
    index_version: str,
) -> list[IndexRecord]:
    blocks_by_id = {block.id: block for block in blocks}
    local_headings = _same_page_headings(blocks)
    records: list[IndexRecord] = []
    for chunk in chunks:
        chunk_blocks = [
            blocks_by_id[block_id]
            for block_id in chunk.block_ids
            if block_id in blocks_by_id
        ]
        table_blocks = [block for block in chunk_blocks if block.block_type == "table"]
        if not table_blocks:
            records.append(
                _record(
                    chunk,
                    chunk.indexing_text,
                    chunk.block_ids,
                    "base",
                    index_version,
                )
            )
            continue

        prefix = _table_prefix(table_blocks, local_headings)
        full_text = "\n".join(part for part in (prefix, chunk.chunk_text) if part)
        records.append(
            _record(
                chunk,
                full_text,
                chunk.block_ids,
                "table-full",
                index_version,
            )
        )
        for table in table_blocks:
            table_prefix = _table_prefix([table], local_headings)
            for group_index, group_text in enumerate(_table_row_groups(table)):
                indexing_text = "\n".join(
                    part for part in (table_prefix, group_text) if part
                )
                if not indexing_text or indexing_text == full_text:
                    continue
                records.append(
                    _record(
                        chunk,
                        indexing_text,
                        chunk.block_ids,
                        f"table-row-group:{table.id}:{group_index}",
                        index_version,
                    )
                )
    return embed_index_records(records, embedding_provider)


def _record(
    chunk: Chunk,
    indexing_text: str,
    block_ids: list[str],
    variant: str,
    index_version: str,
) -> IndexRecord:
    modality = (
        "mixed"
        if len(chunk.modality_mix) > 1
        else (chunk.modality_mix[0] if chunk.modality_mix else "text")
    )
    return IndexRecord(
        id=str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{chunk.id}:index:{index_version}:{variant}",
            )
        ),
        evidence_id=str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{chunk.id}:evidence:{variant}")
        ),
        document_id=chunk.document_id,
        chunk_id=chunk.id,
        block_ids=block_ids,
        modality=modality,
        indexing_text=indexing_text,
        vector=None,
        embedding_provider=None,
        embedding_model=None,
        embedding_dimension=None,
        index_version=index_version,
    )


def _same_page_headings(blocks: list[Block]) -> dict[str, str]:
    headings: dict[str, str] = {}
    page_heading: str | None = None
    page_idx: int | None = None
    for block in blocks:
        if block.page_idx != page_idx:
            page_idx = block.page_idx
            page_heading = None
        if block.block_type == "title" and (block.text or "").strip():
            page_heading = _clean(block.text or "")
        elif block.block_type == "table" and page_heading:
            headings[block.id] = page_heading
    return headings


def _table_prefix(
    table_blocks: list[Block], local_headings: dict[str, str]
) -> str:
    parts: list[str] = []
    for block in table_blocks:
        heading = local_headings.get(block.id)
        if heading:
            parts.append(heading)
        caption = block.metadata.get("caption")
        if isinstance(caption, list):
            parts.extend(_clean(str(item)) for item in caption if _clean(str(item)))
        elif caption and _clean(str(caption)):
            parts.append(_clean(str(caption)))
    return "\n".join(dict.fromkeys(parts))


def _table_row_groups(block: Block) -> list[str]:
    rows = _html_rows(block.html or "")
    if len(rows) < 2:
        text = _clean(block.text or _strip_html(block.html or ""))
        return _text_windows(text, TABLE_ROW_TARGET_TOKENS)

    header = rows[0]
    header_text = "Table columns: " + " | ".join(header)
    groups: list[str] = []
    current: list[str] = []
    current_tokens = _estimate_tokens(header_text)
    for row in rows[1:]:
        line = " | ".join(row)
        line_tokens = _estimate_tokens(line)
        if current and current_tokens + line_tokens > TABLE_ROW_TARGET_TOKENS:
            groups.append("\n".join([header_text, *current]))
            current = []
            current_tokens = _estimate_tokens(header_text)
        current.append(line)
        current_tokens += line_tokens
    if current:
        groups.append("\n".join([header_text, *current]))
    return groups


class _RowsParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs):
        tag = tag.casefold()
        if tag == "tr":
            self._row = []
        elif tag in {"th", "td"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str):
        tag = tag.casefold()
        if tag in {"th", "td"} and self._cell is not None:
            value = _clean(" ".join(self._cell))
            self._row = self._row or []
            self._row.append(value)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                self.rows.append(self._row)
            self._row = None


def _html_rows(html: str) -> list[list[str]]:
    parser = _RowsParser()
    parser.feed(html)
    return parser.rows


def _text_windows(text: str, target_tokens: int) -> list[str]:
    if not text:
        return []
    words = text.split()
    windows: list[str] = []
    current: list[str] = []
    tokens = 0
    for word in words:
        word_tokens = _estimate_tokens(word)
        if current and tokens + word_tokens > target_tokens:
            windows.append(" ".join(current))
            current = []
            tokens = 0
        current.append(word)
        tokens += word_tokens
    if current:
        windows.append(" ".join(current))
    return windows


def _estimate_tokens(text: str) -> int:
    ascii_words = re.findall(r"[A-Za-z0-9_]+", text)
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", text)
    other = max(0, len(text) - sum(len(word) for word in ascii_words) - len(cjk_chars))
    return len(ascii_words) + len(cjk_chars) + other // 4


def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
