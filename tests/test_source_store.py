import re

from traceable_rag.models import Block, Chunk, Document, IndexRecord, KnowledgeBase
from traceable_rag.store import EvidenceStore


def _document() -> Document:
    return Document(
        id="doc-1",
        knowledge_base_id="kb-1",
        original_filename="source.pdf",
        display_name="Source",
        file_hash="hash-1",
        source_type="upload",
        source_file_path=None,
        preview_file_path=None,
        mineru_result_dir=None,
        page_count=2,
        processing_status="ready",
        processing_error=None,
        parser_provider="mineru",
    )


def _graph(*, record_id: str = "record-1", anchor_text: str = "anchor"):
    blocks = [
        Block("p0-b0", "doc-1", 0, 0, "text", text="first"),
        Block("p0-b2", "doc-1", 0, 2, "text", text="second"),
        Block("p1-b0", "doc-1", 1, 0, "text", text=anchor_text),
        Block("p1-b1", "doc-1", 1, 1, "text", text="after"),
        Block("discarded", "doc-1", 1, 2, "text", text="discarded", is_discarded=True),
    ]
    chunk = Chunk(
        id="chunk-1",
        document_id="doc-1",
        chunk_idx=0,
        strategy="block",
        block_ids=["p1-b0"],
        title_context="Heading",
        inferred_heading_path=["Heading"],
        chunk_text=anchor_text,
        indexing_text=anchor_text,
        page_range=[1],
        bbox_by_page={"1": [[1, 2, 3, 4]]},
        modality_mix=["text"],
        token_count=1,
        strategy_version="v1",
    )
    record = IndexRecord(
        id=record_id,
        evidence_id=f"evidence-{record_id}",
        document_id="doc-1",
        chunk_id=chunk.id,
        block_ids=chunk.block_ids,
        modality="text",
        indexing_text=anchor_text,
        vector=None,
        embedding_provider=None,
        embedding_model=None,
        embedding_dimension=None,
        index_version="v1",
    )
    return blocks, [chunk], [record]


def _store_with_graph(tmp_path) -> EvidenceStore:
    store = EvidenceStore(tmp_path / "source.db")
    store.create_knowledge_base(KnowledgeBase("kb-1", "KB"))
    blocks, chunks, records = _graph()
    store.replace_document_graph("kb-1", _document(), blocks, [], chunks, records)
    return store


def test_source_anchor_is_opaque_random_and_reused_for_active_record_block(tmp_path):
    store = _store_with_graph(tmp_path)

    first = store.get_or_create_active_source_anchor("record-1", "p1-b0")
    second = store.get_or_create_active_source_anchor("record-1", "p1-b0")

    assert re.fullmatch(r"src_[0-9a-f]{32}", first.id)
    assert first == second
    assert first.document_id == "doc-1"
    assert store.source_anchor_status(first.id) == "active"
    assert store.source_anchor_status("src_" + "0" * 32) == "unknown"


def test_source_context_reads_before_and_after_in_document_order(tmp_path):
    store = _store_with_graph(tmp_path)
    anchor = store.get_or_create_active_source_anchor("record-1", "p1-b0")

    context = store.list_source_context_blocks(anchor.id, before=5, after=5)

    assert context is not None
    selected, before, after = context
    assert selected.id == "p1-b0"
    assert [block.id for block in before] == ["p0-b0", "p0-b2"]
    assert [block.id for block in after] == ["p1-b1"]


def test_graph_replacement_invalidates_old_source_and_creates_new_current_source(tmp_path):
    store = _store_with_graph(tmp_path)
    old = store.get_or_create_active_source_anchor("record-1", "p1-b0")
    blocks, chunks, records = _graph(anchor_text="same coordinates but rebuilt")

    store.replace_document_graph("kb-1", _document(), blocks, [], chunks, records)
    new = store.get_or_create_active_source_anchor("record-1", "p1-b0")

    assert old.id != new.id
    assert store.source_anchor_status(old.id) == "gone"
    assert store.get_active_source_anchor(old.id) is None
    assert store.list_source_context_blocks(old.id) is None
    assert store.source_anchor_status(new.id) == "active"


def test_replacing_index_records_invalidates_old_source(tmp_path):
    store = _store_with_graph(tmp_path)
    old = store.get_or_create_active_source_anchor("record-1", "p1-b0")
    _, _, records = _graph(record_id="record-2")

    store.replace_index_records("kb-1", "doc-1", records)
    new = store.get_or_create_active_source_anchor("record-2", "p1-b0")

    assert store.source_anchor_status(old.id) == "gone"
    assert new.id != old.id
    assert store.source_anchor_status(new.id) == "active"


def test_document_and_knowledge_base_deletion_leave_source_tombstones(tmp_path):
    store = _store_with_graph(tmp_path)
    document_source = store.get_or_create_active_source_anchor("record-1", "p1-b0")

    assert store.delete_document("kb-1", "doc-1") is True
    assert store.source_anchor_status(document_source.id) == "gone"

    blocks, chunks, records = _graph(record_id="record-2")
    store.replace_document_graph("kb-1", _document(), blocks, [], chunks, records)
    kb_source = store.get_or_create_active_source_anchor("record-2", "p1-b0")
    assert store.delete_knowledge_base("kb-1") is True
    assert store.source_anchor_status(kb_source.id) == "gone"
