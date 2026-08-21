from traceable_rag.models import Block, Chunk, Document, IndexRecord, KnowledgeBase
from traceable_rag.source_service import (
    MAX_CONTEXT_ITEM_CHARS,
    MAX_CONTEXT_RESPONSE_TEXT_CHARS,
    MAX_SOURCE_TEXT_CHARS,
    SourceService,
)
from traceable_rag.store import EvidenceStore


def _build_store(tmp_path, *, source_text: str, neighbours: list[Block]):
    store = EvidenceStore(tmp_path / "source-service.db")
    store.create_knowledge_base(KnowledgeBase("kb-1", "Knowledge base"))
    document = Document(
        id="doc-1",
        knowledge_base_id="kb-1",
        original_filename="source.pdf",
        display_name="Source document",
        file_hash="hash",
        source_type="upload",
        source_file_path=None,
        preview_file_path=None,
        mineru_result_dir=None,
        page_count=1,
        processing_status="ready",
        processing_error=None,
        parser_provider="mineru",
    )
    source = Block(
        "source-block",
        "doc-1",
        0,
        10,
        "table",
        text=source_text,
        bbox=[1, 2, 3, 4],
        page_size=[100, 200],
    )
    blocks = [*neighbours, source]
    chunk = Chunk(
        id="chunk-1",
        document_id="doc-1",
        chunk_idx=0,
        strategy="block",
        block_ids=[source.id],
        title_context="Fallback heading",
        inferred_heading_path=["Section", "Subsection"],
        chunk_text=source_text,
        indexing_text=source_text,
        page_range=[0],
        bbox_by_page={"0": [source.bbox]},
        modality_mix=["table"],
        token_count=1,
        strategy_version="v1",
    )
    record = IndexRecord(
        id="record-1",
        evidence_id="evidence-1",
        document_id="doc-1",
        chunk_id=chunk.id,
        block_ids=[source.id],
        modality="table",
        indexing_text=source_text,
        vector=None,
        embedding_provider=None,
        embedding_model=None,
        embedding_dimension=None,
        index_version="v1",
    )
    store.replace_document_graph("kb-1", document, blocks, [], [chunk], [record])
    anchor = store.get_or_create_active_source_anchor(record.id, source.id)
    return store, anchor.id


def test_context_applies_item_and_global_text_budgets(tmp_path):
    neighbours = [
        Block(f"before-{index}", "doc-1", 0, index, "paragraph", text="b" * 3_000)
        for index in range(5)
    ]
    neighbours.extend(
        Block(
            f"after-{index}",
            "doc-1",
            0,
            11 + index,
            "paragraph",
            text="a" * 3_000,
        )
        for index in range(5)
    )
    store, source_id = _build_store(
        tmp_path, source_text="s" * 9_000, neighbours=neighbours
    )

    payload = SourceService(store).context(source_id, before=5, after=5)

    assert len(payload["source"]["text"]) == MAX_SOURCE_TEXT_CHARS
    assert all(
        len(item["text"]) <= MAX_CONTEXT_ITEM_CHARS
        for item in payload["before"] + payload["after"]
    )
    assert sum(
        len(item["text"])
        for item in [payload["source"], *payload["before"], *payload["after"]]
    ) <= MAX_CONTEXT_RESPONSE_TEXT_CHARS
    assert payload["truncated"] is True


def test_context_converts_html_only_blocks_to_plain_text(tmp_path):
    neighbour = Block(
        "html-before",
        "doc-1",
        0,
        0,
        "table",
        html="<table><tr><th>Metric</th><td>42</td></tr></table>",
    )
    store, source_id = _build_store(
        tmp_path, source_text="answer", neighbours=[neighbour]
    )

    payload = SourceService(store).context(source_id, before=2, after=2)

    assert payload["before"][0]["text"] == "Metric\n42"
    assert "<table" not in payload["before"][0]["text"]
    assert payload["source"]["title_path"] == ["Section", "Subsection"]
