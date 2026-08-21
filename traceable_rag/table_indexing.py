from __future__ import annotations

import re
from html.parser import HTMLParser

from .models import Block, Chunk


TABLE_ROW_TARGET_TOKENS = 220


def table_index_variants(
    chunk: Chunk,
    blocks: list[Block],
    *,
    table_context_by_id: dict[str, str],
) -> list[tuple[str, str]]:
    """Return deterministic full-table and row-group index representations.

    The caller owns record identity and embedding.  Keeping this module focused on
    content representations makes the table path an optional, modality-specific
    extension to the normal Chunk -> IndexRecord flow.
    """

    tables = [block for block in blocks if block.block_type.casefold() == "table"]
    if not tables:
        return []

    full_context = _join_unique(table_context_by_id.get(table.id, "") for table in tables)
    full_text = _join_nonempty(full_context, chunk.chunk_text)
    variants: list[tuple[str, str]] = [("table-full", full_text)]
    for table in tables:
        context = table_context_by_id.get(table.id, "")
        for group_index, group_text in enumerate(table_row_groups(table)):
            indexing_text = _join_nonempty(context, group_text)
            if indexing_text and indexing_text != full_text:
                variants.append(
                    (f"table-row-group:{table.id}:{group_index}", indexing_text)
                )
    return variants


def table_contexts(blocks: list[Block]) -> dict[str, str]:
    """Collect same-page title and parser-provided captions for Table Blocks.

    This intentionally uses only document structure, never domain vocabulary or
    dataset-specific metadata.  A new page resets the remembered title so a table
    cannot inherit an unrelated heading from a previous page.
    """

    contexts: dict[str, str] = {}
    page_idx: int | None = None
    page_title: str | None = None
    for block in blocks:
        if block.page_idx != page_idx:
            page_idx = block.page_idx
            page_title = None
        if block.block_type.casefold() == "title":
            title = _clean(block.text or "")
            if title:
                page_title = title
            continue
        if block.block_type.casefold() != "table":
            continue
        captions = _caption_parts(block.metadata.get("caption"))
        contexts[block.id] = _join_unique([page_title or "", *captions])
    return contexts


def table_row_groups(block: Block, *, target_tokens: int = TABLE_ROW_TARGET_TOKENS) -> list[str]:
    """Split a table into approximately target-sized groups with its header.

    Every HTML-derived group repeats the first row as column labels.  If parser
    output lacks rows, retain indexability with generic text windows instead of
    dropping a table solely because its structure is incomplete.
    """

    rows = _html_rows(block.html or "")
    if len(rows) < 2:
        return _text_windows(_clean(block.text or _strip_html(block.html or "")), target_tokens)

    header = "Table columns: " + " | ".join(rows[0])
    groups: list[str] = []
    current: list[str] = []
    current_tokens = _estimate_tokens(header)
    for row in rows[1:]:
        line = " | ".join(row)
        if not line:
            continue
        line_tokens = _estimate_tokens(line)
        if current and current_tokens + line_tokens > target_tokens:
            groups.append("\n".join([header, *current]))
            current = []
            current_tokens = _estimate_tokens(header)
        current.append(line)
        current_tokens += line_tokens
    if current:
        groups.append("\n".join([header, *current]))
    return groups


class _RowsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag == "tr":
            self._row = []
        elif tag in {"th", "td"} and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"th", "td"} and self._cell is not None:
            self._row = self._row or []
            self._row.append(_clean(" ".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                self.rows.append(self._row)
            self._row = None


def _html_rows(html: str) -> list[list[str]]:
    parser = _RowsParser()
    parser.feed(html)
    parser.close()
    return parser.rows


def _caption_parts(value: object) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return [part for item in values if (part := _clean(str(item or "")))]


def _text_windows(text: str, target_tokens: int) -> list[str]:
    if not text:
        return []
    tokens = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]|[^\s]", text)
    windows: list[str] = []
    current: list[str] = []
    token_count = 0
    for token in tokens:
        if current and token_count >= target_tokens:
            windows.append(" ".join(current))
            current = []
            token_count = 0
        current.append(token)
        token_count += _estimate_tokens(token)
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


def _join_nonempty(*parts: str) -> str:
    return "\n".join(part for part in parts if part)


def _join_unique(parts: list[str] | object) -> str:
    return "\n".join(dict.fromkeys(part for part in parts if part))
