import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from traceable_rag.models import (
    Asset,
    Block,
    Chunk,
    Document,
    IndexRecord,
    KnowledgeBase,
)
from traceable_rag.store import (
    DEFAULT_KNOWLEDGE_BASE_ID,
    DocumentNotFoundError,
    EvidenceStore,
    IncompatibleDatabaseError,
)
from traceable_rag.mineru_import import NORMALIZATION_VERSION
from traceable_rag.processing_jobs import normalization_upgrade_operation


class SqlTracingEvidenceStore(EvidenceStore):
    def __init__(self, db_path):
        self.sql_statements: list[str] = []
        super().__init__(db_path)

    def connect(self):
        connection = super().connect()
        connection.set_trace_callback(self.sql_statements.append)
        return connection


def _select_statements(store: SqlTracingEvidenceStore) -> list[str]:
    return [
        statement
        for statement in store.sql_statements
        if statement.lstrip().lower().startswith(("select", "with"))
    ]


def _knowledge_base(kb_id: str, name: str) -> KnowledgeBase:
    return KnowledgeBase(id=kb_id, name=name, description=f"{name} description")


def _document(kb_id: str, doc_id: str, file_hash: str = "hash-1") -> Document:
    return Document(
        id=doc_id,
        knowledge_base_id=kb_id,
        original_filename="source.docx",
        display_name="示例文档",
        file_hash=file_hash,
        source_type="upload",
        source_file_path=f"/managed/{doc_id}/source.docx",
        preview_file_path=f"/managed/{doc_id}/preview.pdf",
        mineru_result_dir=f"/managed/{doc_id}/mineru",
        page_count=2,
        processing_status="parsed",
        processing_error=None,
        parser_provider="mineru",
        parser_version="2.0",
    )


def _add_document_graph(store: EvidenceStore, kb_id: str, doc_id: str) -> None:
    store.upsert_document(
        replace(
            _document(kb_id, doc_id, f"hash:{kb_id}:{doc_id}"),
            display_name=f"示例文档-{doc_id}",
        )
    )
    block = Block(
        id=f"{doc_id}:block-1",
        document_id=doc_id,
        page_idx=0,
        block_idx=0,
        block_type="paragraph",
        text="用于不可变快照的来源段落",
    )
    asset = Asset(
        id=f"{doc_id}:asset-1",
        document_id=doc_id,
        block_id=block.id,
        asset_type="image",
        file_path="image.png",
        content_type="image/png",
        file_hash="asset-hash",
        page_idx=0,
        bbox=[1, 2, 3, 4],
    )
    chunk = Chunk(
        id=f"{doc_id}:chunk-1",
        document_id=doc_id,
        chunk_idx=0,
        strategy="block",
        block_ids=[block.id],
        title_context="继承结构",
        inferred_heading_path=["继承结构"],
        chunk_text=block.text or "",
        indexing_text=block.text or "",
        page_range=[0],
        bbox_by_page={"0": [[1, 2, 3, 4]]},
        modality_mix=["text"],
        token_count=12,
        strategy_version="v1",
    )
    record = IndexRecord(
        id=f"{doc_id}:record-1",
        evidence_id=f"evidence:{doc_id}:1",
        document_id=doc_id,
        chunk_id=chunk.id,
        block_ids=[block.id],
        modality="text",
        indexing_text=block.text or "",
        vector=None,
        embedding_provider=None,
        embedding_model=None,
        embedding_dimension=None,
        index_version="v1",
    )
    store.replace_blocks(kb_id, doc_id, [block])
    store.replace_assets(kb_id, doc_id, [asset])
    store.replace_chunks(kb_id, doc_id, [chunk])
    store.replace_index_records(kb_id, doc_id, [record])


def test_default_knowledge_base_is_created_only_for_first_initialization(tmp_path):
    db_path = tmp_path / "traceable_v2.db"

    store = EvidenceStore(db_path)

    assert [kb.id for kb in store.list_knowledge_bases()] == [DEFAULT_KNOWLEDGE_BASE_ID]
    assert store.get_knowledge_base(DEFAULT_KNOWLEDGE_BASE_ID).name == "默认知识库"

    assert store.delete_knowledge_base(DEFAULT_KNOWLEDGE_BASE_ID) is True
    assert store.list_knowledge_bases() == []

    reopened = EvidenceStore(db_path)
    assert reopened.list_knowledge_bases() == []


def test_knowledge_base_names_are_unique_case_insensitively(tmp_path):
    store = EvidenceStore(tmp_path / "traceable_v2.db")

    store.create_knowledge_base(_knowledge_base("kb-a", "Operations"))

    with pytest.raises(sqlite3.IntegrityError):
        store.create_knowledge_base(_knowledge_base("kb-b", "operations"))


def test_knowledge_base_document_stats_use_one_aggregate_query_for_all_bases(tmp_path):
    store = SqlTracingEvidenceStore(tmp_path / "traceable_v2.db")
    store.create_knowledge_base(_knowledge_base("kb-a", "A"))
    store.create_knowledge_base(_knowledge_base("kb-b", "B"))
    statuses = (
        "ready",
        "uploading",
        "parsing",
        "indexing",
        "failed",
        "uploaded",
        "parsed",
    )
    for index, status in enumerate(statuses):
        store.upsert_document(
            replace(
                _document("kb-a", f"doc-a-{index}", f"hash-a-{index}"),
                display_name=f"A document {index}",
                processing_status=status,
            )
        )
    store.upsert_document(
        replace(
            _document("kb-b", "doc-b", "hash-b"),
            display_name="B document",
            processing_status="ready",
        )
    )

    store.sql_statements.clear()
    stats = store.list_knowledge_base_document_stats()

    assert stats == {
        DEFAULT_KNOWLEDGE_BASE_ID: {
            "document_count": 0,
            "ready_document_count": 0,
            "processing_document_count": 0,
            "failed_document_count": 0,
        },
        "kb-a": {
            "document_count": 7,
            "ready_document_count": 1,
            "processing_document_count": 3,
            "failed_document_count": 1,
        },
        "kb-b": {
            "document_count": 1,
            "ready_document_count": 1,
            "processing_document_count": 0,
            "failed_document_count": 0,
        },
    }
    assert len(_select_statements(store)) == 1


def test_document_graph_stats_use_one_aggregate_query_for_all_documents(tmp_path):
    store = SqlTracingEvidenceStore(tmp_path / "traceable_v2.db")
    store.create_knowledge_base(_knowledge_base("kb-a", "A"))
    _add_document_graph(store, "kb-a", "doc-with-graph")
    store.upsert_document(
        replace(
            _document("kb-a", "doc-empty", "hash-empty"),
            display_name="Empty document",
        )
    )

    store.sql_statements.clear()
    stats = store.list_document_graph_stats("kb-a")

    assert stats == {
        "doc-with-graph": {
            "blocks": 1,
            "assets": 1,
            "chunks": 1,
            "index_records": 1,
            "embedding_records": 0,
            "embedding_configurations": 0,
        },
        "doc-empty": {
            "blocks": 0,
            "assets": 0,
            "chunks": 0,
            "index_records": 0,
            "embedding_records": 0,
            "embedding_configurations": 0,
        },
    }
    assert len(_select_statements(store)) == 1


def test_document_graph_stats_detect_incompatible_embedding_dimensions(tmp_path):
    store = SqlTracingEvidenceStore(tmp_path / "traceable_v2.db")
    store.create_knowledge_base(_knowledge_base("kb-a", "A"))
    _add_document_graph(store, "kb-a", "doc-with-graph")
    chunk = store.list_chunks("kb-a", "doc-with-graph")[0]
    record = store.list_index_records("kb-a", "doc-with-graph")[0]
    second_chunk = replace(chunk, id=f"{chunk.id}-second", chunk_idx=1)
    store.replace_chunks("kb-a", "doc-with-graph", [chunk, second_chunk])
    store.replace_index_records(
        "kb-a",
        "doc-with-graph",
        [
            replace(
                record,
                vector=[0.2, 0.8],
                embedding_provider="fake",
                embedding_model="fake-model",
                embedding_dimension=2,
            ),
            replace(
                record,
                id=f"{record.id}-second",
                evidence_id=f"{record.evidence_id}-second",
                chunk_id=second_chunk.id,
                vector=[0.1, 0.2, 0.7],
                embedding_provider="fake",
                embedding_model="fake-model",
                embedding_dimension=3,
            ),
        ],
    )

    store.sql_statements.clear()
    stats = store.list_document_graph_stats("kb-a")

    assert stats["doc-with-graph"]["embedding_records"] == 2
    assert stats["doc-with-graph"]["embedding_configurations"] == 2
    assert len(_select_statements(store)) == 1


def test_document_display_names_are_unique_by_nfkc_casefold_key(tmp_path):
    store = EvidenceStore(tmp_path / "traceable_v2.db")
    store.create_knowledge_base(_knowledge_base("kb-a", "A"))
    first = replace(
        _document("kb-a", "doc-a", "hash-a"),
        display_name="ＡＢＣ 方案",
    )
    equivalent = replace(
        _document("kb-a", "doc-b", "hash-b"),
        display_name="abc 方案",
    )

    store.upsert_document(first)

    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_document(equivalent)


def test_scoped_document_rename_updates_only_name_and_cannot_revive_deleted_row(tmp_path):
    store = EvidenceStore(tmp_path / "traceable_v2.db")
    store.create_knowledge_base(_knowledge_base("kb-a", "A"))
    original = _document("kb-a", "doc-a", "hash-a")
    store.upsert_document(original)

    assert store.update_document_display_name("kb-a", "doc-a", "新示例文档") is True

    renamed = store.get_document("kb-a", "doc-a")
    assert renamed is not None
    assert renamed.display_name == "新示例文档"
    assert renamed.file_hash == original.file_hash
    assert renamed.preview_file_path == original.preview_file_path
    assert renamed.processing_status == original.processing_status

    assert store.delete_document("kb-a", "doc-a") is True
    assert store.update_document_display_name("kb-a", "doc-a", "不应复活") is False
    assert store.get_document("kb-a", "doc-a") is None


def test_document_hash_is_unique_within_a_knowledge_base_but_reusable_across_bases(tmp_path):
    store = EvidenceStore(tmp_path / "traceable_v2.db")
    store.create_knowledge_base(_knowledge_base("kb-a", "A"))
    store.create_knowledge_base(_knowledge_base("kb-b", "B"))

    store.upsert_document(_document("kb-a", "doc-a", "same-hash"))

    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_document(_document("kb-a", "doc-b", "same-hash"))

    store.upsert_document(_document("kb-b", "doc-c", "same-hash"))
    assert store.get_document("kb-b", "doc-c") is not None


def test_document_deletion_cascades_to_all_derived_records(tmp_path):
    store = EvidenceStore(tmp_path / "traceable_v2.db")
    store.create_knowledge_base(_knowledge_base("kb-a", "A"))
    _add_document_graph(store, "kb-a", "doc-a")

    assert store.delete_document("kb-a", "doc-a") is True

    with store.connect() as conn:
        for table in ("documents", "blocks", "assets", "chunks", "index_records"):
            assert conn.execute(f"select count(*) from {table}").fetchone()[0] == 0


def test_atomic_document_graph_replacement_rolls_back_document_and_graph_on_conflict(tmp_path):
    store = EvidenceStore(tmp_path / "traceable_v2.db")
    store.create_knowledge_base(_knowledge_base("kb-a", "A"))
    _add_document_graph(store, "kb-a", "doc-a")
    _add_document_graph(store, "kb-a", "doc-b")
    old_document = store.get_document("kb-a", "doc-a")
    old_graph = (
        store.list_blocks("kb-a", "doc-a"),
        store.list_assets("kb-a", "doc-a"),
        store.list_chunks("kb-a", "doc-a"),
        store.list_index_records("kb-a", "doc-a"),
    )
    new_block = replace(old_graph[0][0], id="doc-a:new-block", text="new")
    new_asset = replace(
        old_graph[1][0], id="doc-a:new-asset", block_id=new_block.id
    )
    new_chunk = replace(
        old_graph[2][0], id="doc-a:new-chunk", block_ids=[new_block.id]
    )
    conflicting_record = replace(
        old_graph[3][0],
        id="doc-a:new-record",
        evidence_id="evidence:doc-b:1",
        chunk_id=new_chunk.id,
        block_ids=[new_block.id],
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.replace_document_graph(
            "kb-a",
            replace(old_document, preview_file_path="/new/preview.pdf", processing_status="ready"),
            [new_block],
            [new_asset],
            [new_chunk],
            [conflicting_record],
        )

    assert store.get_document("kb-a", "doc-a") == old_document
    assert store.list_blocks("kb-a", "doc-a") == old_graph[0]
    assert store.list_assets("kb-a", "doc-a") == old_graph[1]
    assert store.list_chunks("kb-a", "doc-a") == old_graph[2]
    assert store.list_index_records("kb-a", "doc-a") == old_graph[3]


def _removed_test_foreign_keys_are_enabled_and_knowledge_base_deletion_preserves_trace(tmp_path):
    store = EvidenceStore(tmp_path / "traceable_v2.db")
    store.create_knowledge_base(_knowledge_base("kb-a", "A"))
    _add_document_graph(store, "kb-a", "doc-a")
    trace = QueryTrace(
        id="trace-1",
        query="层次继承如何工作？",
        answer="多个子类可以继承同一个父类。",
        source_type="manual_query",
        support_level="strong",
        knowledge_base_ids=["kb-a"],
        cited_evidence_ids=["evidence:doc-a:1"],
        read_events=[],
        knowledge_base_snapshots=[{"id": "kb-a", "name": "A"}],
        citation_snapshots=[
            {
                "evidence_id": "evidence:doc-a:1",
                "document_id": "doc-a",
                "original_filename": "source.docx",
                "human_page_number": 1,
                "block_ids": ["doc-a:block-1"],
                "block_text": "用于不可变快照的来源段落",
                "title_context": "继承结构",
            }
        ],
        created_at="2026-07-21T02:00:00.000000Z",
    )
    store.create_query_trace(trace)

    with store.connect() as conn:
        assert conn.execute("pragma foreign_keys").fetchone()[0] == 1

    assert store.delete_knowledge_base("kb-a") is True

    with store.connect() as conn:
        for table in ("documents", "blocks", "assets", "chunks", "index_records"):
            assert conn.execute(f"select count(*) from {table}").fetchone()[0] == 0

    loaded_trace = store.get_query_trace("trace-1")
    assert loaded_trace == replace(trace, snapshot_status="degraded")

    with pytest.raises(sqlite3.IntegrityError):
        store.create_query_trace(trace)


def test_legacy_database_is_rejected_instead_of_mutated(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("create table documents (id text primary key, filename text not null)")

    with pytest.raises(IncompatibleDatabaseError, match="V3"):
        EvidenceStore(db_path)

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute("select name from sqlite_master where type = 'table'")
        }
    assert tables == {"documents"}


def test_v3_schema_keeps_source_anchors_and_has_no_query_trace_table(tmp_path):
    store = EvidenceStore(tmp_path / "traceable_v3.db")

    with store.connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "select name from sqlite_master where type = 'table' and name not like 'sqlite_%'"
            )
        }
        version = conn.execute(
            "select value from schema_metadata where key = 'schema_version'"
        ).fetchone()["value"]

    assert version == "3"
    assert "source_anchors" in tables
    assert "query_traces" not in tables


def test_v2_database_is_rejected_without_migration(tmp_path):
    db_path = tmp_path / "incomplete-v2.db"
    required_tables = (
        "knowledge_bases",
        "blocks",
        "assets",
        "chunks",
        "index_records",
        "query_traces",
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute("create table schema_metadata (key text primary key, value text not null)")
        conn.execute(
            "insert into schema_metadata (key, value) values ('schema_version', '2')"
        )
        conn.execute(
            "create table documents (id text primary key, display_name text not null)"
        )
        for table in required_tables:
            conn.execute(f"create table {table} (id text primary key)")

    with pytest.raises(IncompatibleDatabaseError, match="schema version '2'"):
        EvidenceStore(db_path)


def test_existing_v2_database_adds_and_persists_normalization_version(tmp_path):
    db_path = tmp_path / "traceable_v2.db"
    store = EvidenceStore(db_path)
    store.create_knowledge_base(_knowledge_base("kb-a", "A"))
    store.upsert_document(_document("kb-a", "doc-a", "hash-a"))
    with store.connect() as conn:
        conn.execute("alter table documents drop column normalization_version")

    reopened = EvidenceStore(db_path)
    with reopened.connect() as conn:
        columns = {row["name"] for row in conn.execute("pragma table_info(documents)")}
    assert "normalization_version" in columns
    assert reopened.get_document("kb-a", "doc-a").normalization_version is None

    reopened.upsert_document(
        replace(
            reopened.get_document("kb-a", "doc-a"),
            normalization_version=NORMALIZATION_VERSION,
        )
    )
    assert (
        reopened.get_document("kb-a", "doc-a").normalization_version
        == NORMALIZATION_VERSION
    )


def test_normalization_upgrade_candidates_are_deduplicated_per_target_version(tmp_path):
    store = EvidenceStore(tmp_path / "traceable_v2.db")
    store.create_knowledge_base(_knowledge_base("kb-a", "A"))
    candidates = {
        "outdated": _document("kb-a", "outdated", "hash-outdated"),
        "current": replace(
            _document("kb-a", "current", "hash-current"),
            normalization_version=NORMALIZATION_VERSION,
        ),
        "missing-result": replace(
            _document("kb-a", "missing-result", "hash-missing"),
            mineru_result_dir=None,
        ),
        "failed-once": _document("kb-a", "failed-once", "hash-failed"),
        "legacy-upgraded": replace(
            _document("kb-a", "legacy-upgraded", "hash-legacy"),
            normalization_version="mineru-content.v2",
        ),
        "previous-version-upgraded": replace(
            _document("kb-a", "previous-version-upgraded", "hash-v3"),
            normalization_version="mineru-content.v3",
        ),
    }
    for document in candidates.values():
        store.upsert_document(replace(document, display_name=document.id))
    job = store.enqueue_processing_job(
        "upgrade-failed",
        "kb-a",
        "failed-once",
        operation=normalization_upgrade_operation(NORMALIZATION_VERSION),
    )
    store.update_processing_job(
        job.id,
        status="failed",
        stage="normalizing",
        error_code="normalization_failed",
        error_message="failed",
        retryable=False,
        finished=True,
    )
    legacy_job = store.enqueue_processing_job(
        "upgrade-legacy", "kb-a", "legacy-upgraded", operation="normalize_upgrade"
    )
    store.update_processing_job(
        legacy_job.id,
        status="succeeded",
        stage="complete",
        finished=True,
    )
    previous_version_job = store.enqueue_processing_job(
        "upgrade-v3",
        "kb-a",
        "previous-version-upgraded",
        operation=normalization_upgrade_operation("mineru-content.v3"),
    )
    store.update_processing_job(
        previous_version_job.id,
        status="succeeded",
        stage="complete",
        finished=True,
    )

    assert {
        document.id
        for document in store.list_documents_needing_normalization(
            NORMALIZATION_VERSION
        )
    } == {
        "outdated",
        "missing-result",
        "legacy-upgraded",
        "previous-version-upgraded",
    }


def test_document_operations_validate_knowledge_base_and_document_together(tmp_path):
    store = EvidenceStore(tmp_path / "traceable_v2.db")
    store.create_knowledge_base(_knowledge_base("kb-a", "A"))
    store.create_knowledge_base(_knowledge_base("kb-b", "B"))
    _add_document_graph(store, "kb-a", "doc-a")

    assert store.get_document("kb-b", "doc-a") is None
    assert store.list_documents("kb-b") == []

    scoped_operations = (
        lambda: store.upsert_document(_document("kb-b", "doc-a", "moved-hash")),
        lambda: store.list_blocks("kb-b", "doc-a"),
        lambda: store.replace_blocks("kb-b", "doc-a", []),
        lambda: store.list_assets("kb-b", "doc-a"),
        lambda: store.replace_assets("kb-b", "doc-a", []),
        lambda: store.list_chunks("kb-b", "doc-a"),
        lambda: store.replace_chunks("kb-b", "doc-a", []),
        lambda: store.list_index_records("kb-b", "doc-a"),
        lambda: store.replace_index_records("kb-b", "doc-a", []),
        lambda: store.delete_document("kb-b", "doc-a"),
    )
    for operation in scoped_operations:
        with pytest.raises(DocumentNotFoundError):
            operation()

    assert [block.id for block in store.list_blocks("kb-a", "doc-a")] == ["doc-a:block-1"]
    assert store.list_index_records("kb-a", "doc-a")[0].evidence_id == "evidence:doc-a:1"


@pytest.mark.parametrize(
    ("foreign_kb_id", "foreign_doc_id"),
    (("kb-a", "doc-other"), ("kb-b", "doc-foreign")),
)
def test_asset_cannot_reference_a_block_owned_by_another_document(
    tmp_path, foreign_kb_id, foreign_doc_id
):
    store = EvidenceStore(tmp_path / "traceable_v2.db")
    store.create_knowledge_base(_knowledge_base("kb-a", "A"))
    store.create_knowledge_base(_knowledge_base("kb-b", "B"))
    _add_document_graph(store, "kb-a", "doc-a")
    _add_document_graph(store, foreign_kb_id, foreign_doc_id)
    invalid_asset = Asset(
        id="invalid-asset",
        document_id="doc-a",
        block_id=f"{foreign_doc_id}:block-1",
        asset_type="image",
        file_path="foreign.png",
        content_type="image/png",
        file_hash="foreign-asset-hash",
        page_idx=0,
        bbox=[1, 2, 3, 4],
    )

    with pytest.raises(ValueError, match="block"):
        store.replace_assets("kb-a", "doc-a", [invalid_asset])

    assert [asset.id for asset in store.list_assets("kb-a", "doc-a")] == ["doc-a:asset-1"]


@pytest.mark.parametrize(
    ("foreign_kb_id", "foreign_doc_id"),
    (("kb-a", "doc-other"), ("kb-b", "doc-foreign")),
)
def test_index_record_cannot_reference_a_chunk_owned_by_another_document(
    tmp_path, foreign_kb_id, foreign_doc_id
):
    store = EvidenceStore(tmp_path / "traceable_v2.db")
    store.create_knowledge_base(_knowledge_base("kb-a", "A"))
    store.create_knowledge_base(_knowledge_base("kb-b", "B"))
    _add_document_graph(store, "kb-a", "doc-a")
    _add_document_graph(store, foreign_kb_id, foreign_doc_id)
    invalid_record = IndexRecord(
        id="invalid-record",
        evidence_id="invalid-evidence",
        document_id="doc-a",
        chunk_id=f"{foreign_doc_id}:chunk-1",
        block_ids=["doc-a:block-1"],
        modality="text",
        indexing_text="invalid",
        vector=None,
        embedding_provider=None,
        embedding_model=None,
        embedding_dimension=None,
        index_version="v1",
    )

    with pytest.raises(ValueError, match="chunk"):
        store.replace_index_records("kb-a", "doc-a", [invalid_record])

    assert [record.id for record in store.list_index_records("kb-a", "doc-a")] == [
        "doc-a:record-1"
    ]


@pytest.mark.parametrize(
    ("foreign_kb_id", "foreign_doc_id"),
    (("kb-a", "doc-other"), ("kb-b", "doc-foreign")),
)
def test_chunk_cannot_reference_a_block_owned_by_another_document(
    tmp_path, foreign_kb_id, foreign_doc_id
):
    store = EvidenceStore(tmp_path / "traceable_v2.db")
    store.create_knowledge_base(_knowledge_base("kb-a", "A"))
    store.create_knowledge_base(_knowledge_base("kb-b", "B"))
    _add_document_graph(store, "kb-a", "doc-a")
    _add_document_graph(store, foreign_kb_id, foreign_doc_id)
    invalid_chunk = Chunk(
        id="invalid-chunk",
        document_id="doc-a",
        chunk_idx=1,
        strategy="block",
        block_ids=[f"{foreign_doc_id}:block-1"],
        title_context=None,
        inferred_heading_path=[],
        chunk_text="invalid",
        indexing_text="invalid",
        page_range=[0],
        bbox_by_page={},
        modality_mix=["text"],
        token_count=1,
        strategy_version="v1",
    )

    with pytest.raises(ValueError, match="block"):
        store.replace_chunks("kb-a", "doc-a", [invalid_chunk])

    assert [chunk.id for chunk in store.list_chunks("kb-a", "doc-a")] == ["doc-a:chunk-1"]


@pytest.mark.parametrize(
    ("foreign_kb_id", "foreign_doc_id"),
    (("kb-a", "doc-other"), ("kb-b", "doc-foreign")),
)
def test_index_record_cannot_reference_blocks_owned_by_another_document(
    tmp_path, foreign_kb_id, foreign_doc_id
):
    store = EvidenceStore(tmp_path / "traceable_v2.db")
    store.create_knowledge_base(_knowledge_base("kb-a", "A"))
    store.create_knowledge_base(_knowledge_base("kb-b", "B"))
    _add_document_graph(store, "kb-a", "doc-a")
    _add_document_graph(store, foreign_kb_id, foreign_doc_id)
    invalid_record = IndexRecord(
        id="invalid-record",
        evidence_id="invalid-evidence",
        document_id="doc-a",
        chunk_id="doc-a:chunk-1",
        block_ids=[f"{foreign_doc_id}:block-1"],
        modality="text",
        indexing_text="invalid",
        vector=None,
        embedding_provider=None,
        embedding_model=None,
        embedding_dimension=None,
        index_version="v1",
    )

    with pytest.raises(ValueError, match="block"):
        store.replace_index_records("kb-a", "doc-a", [invalid_record])

    assert [record.id for record in store.list_index_records("kb-a", "doc-a")] == [
        "doc-a:record-1"
    ]


def test_index_record_block_ids_must_match_its_referenced_chunk(tmp_path):
    store = EvidenceStore(tmp_path / "traceable_v2.db")
    store.create_knowledge_base(_knowledge_base("kb-a", "A"))
    _add_document_graph(store, "kb-a", "doc-a")
    invalid_record = IndexRecord(
        id="invalid-record",
        evidence_id="invalid-evidence",
        document_id="doc-a",
        chunk_id="doc-a:chunk-1",
        block_ids=[],
        modality="text",
        indexing_text="invalid",
        vector=None,
        embedding_provider=None,
        embedding_model=None,
        embedding_dimension=None,
        index_version="v1",
    )

    with pytest.raises(ValueError, match="match"):
        store.replace_index_records("kb-a", "doc-a", [invalid_record])

    assert [record.id for record in store.list_index_records("kb-a", "doc-a")] == [
        "doc-a:record-1"
    ]


class _CoordinatedConnection:
    def __init__(self, connection, read_barrier):
        self._connection = connection
        self._read_barrier = read_barrier

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, *args):
        return self._connection.__exit__(*args)

    def execute(self, sql, parameters=()):
        cursor = self._connection.execute(sql, parameters)
        if sql.strip().lower().startswith("select knowledge_base_id from documents"):
            self._read_barrier.wait(timeout=5)
        return cursor


def test_cross_knowledge_base_upsert_conflict_is_atomic_under_concurrency(tmp_path):
    db_path = tmp_path / "traceable_v2.db"
    bootstrap = EvidenceStore(db_path)
    bootstrap.create_knowledge_base(_knowledge_base("kb-a", "A"))
    bootstrap.create_knowledge_base(_knowledge_base("kb-b", "B"))
    stores = [EvidenceStore(db_path), EvidenceStore(db_path)]
    read_barrier = threading.Barrier(2)
    start_barrier = threading.Barrier(2)
    for store in stores:
        original_connect = store.connect
        store.connect = lambda connect=original_connect: _CoordinatedConnection(
            connect(), read_barrier
        )

    documents = [
        _document("kb-a", "shared-doc", "hash-a"),
        _document("kb-b", "shared-doc", "hash-b"),
    ]

    def write(index):
        start_barrier.wait(timeout=5)
        try:
            stores[index].upsert_document(documents[index])
        except DocumentNotFoundError:
            return "scope-conflict"
        return "success"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(write, (0, 1)))

    assert sorted(results) == ["scope-conflict", "success"]
    loaded = bootstrap.get_document("kb-a", "shared-doc") or bootstrap.get_document(
        "kb-b", "shared-doc"
    )
    assert loaded is not None
    assert (loaded.knowledge_base_id, loaded.file_hash) in {
        ("kb-a", "hash-a"),
        ("kb-b", "hash-b"),
    }


def test_replacing_blocks_invalidates_all_derived_document_data(tmp_path):
    store = EvidenceStore(tmp_path / "traceable_v2.db")
    store.create_knowledge_base(_knowledge_base("kb-a", "A"))
    _add_document_graph(store, "kb-a", "doc-a")
    replacement = Block(
        id="doc-a:new-block",
        document_id="doc-a",
        page_idx=0,
        block_idx=0,
        block_type="paragraph",
        text="重新解析后的段落",
    )

    store.replace_blocks("kb-a", "doc-a", [replacement])

    assert [block.id for block in store.list_blocks("kb-a", "doc-a")] == [replacement.id]
    assert store.list_assets("kb-a", "doc-a") == []
    assert store.list_chunks("kb-a", "doc-a") == []
    assert store.list_index_records("kb-a", "doc-a") == []


def test_failed_block_replacement_rolls_back_blocks_and_all_derived_data(tmp_path):
    store = EvidenceStore(tmp_path / "traceable_v2.db")
    store.create_knowledge_base(_knowledge_base("kb-a", "A"))
    _add_document_graph(store, "kb-a", "doc-a")
    duplicate = Block(
        id="duplicate-block",
        document_id="doc-a",
        page_idx=0,
        block_idx=0,
        block_type="paragraph",
        text="duplicate",
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.replace_blocks("kb-a", "doc-a", [duplicate, duplicate])

    assert [block.id for block in store.list_blocks("kb-a", "doc-a")] == ["doc-a:block-1"]
    assert [asset.id for asset in store.list_assets("kb-a", "doc-a")] == ["doc-a:asset-1"]
    assert [chunk.id for chunk in store.list_chunks("kb-a", "doc-a")] == ["doc-a:chunk-1"]
    assert [record.id for record in store.list_index_records("kb-a", "doc-a")] == [
        "doc-a:record-1"
    ]


def test_concurrent_first_initialization_creates_one_complete_v2_database(tmp_path):
    db_path = tmp_path / "traceable_v2.db"
    start_barrier = threading.Barrier(12)

    def initialize(_):
        start_barrier.wait(timeout=5)
        return EvidenceStore(db_path).list_knowledge_bases()

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(initialize, range(12)))

    assert all([kb.id for kb in knowledge_bases] == [DEFAULT_KNOWLEDGE_BASE_ID] for knowledge_bases in results)
    with EvidenceStore(db_path).connect() as conn:
        assert conn.execute(
            "select count(*) from schema_metadata where key = 'schema_version'"
        ).fetchone()[0] == 1
        assert conn.execute("select count(*) from knowledge_bases").fetchone()[0] == 1


def test_existing_database_validation_does_not_compete_for_a_writer_lock(tmp_path):
    db_path = tmp_path / "traceable_v2.db"
    EvidenceStore(db_path)

    class LowTimeoutEvidenceStore(EvidenceStore):
        def connect(self):
            conn = sqlite3.connect(self.db_path, timeout=0.05)
            conn.row_factory = sqlite3.Row
            conn.execute("pragma foreign_keys = on")
            return conn

    writer = sqlite3.connect(db_path)
    try:
        writer.execute("begin immediate")
        writer.execute(
            "update knowledge_bases set description = ? where id = ?",
            ("uncommitted", DEFAULT_KNOWLEDGE_BASE_ID),
        )

        reopened = LowTimeoutEvidenceStore(db_path)

        assert reopened.get_knowledge_base(DEFAULT_KNOWLEDGE_BASE_ID) is not None
    finally:
        writer.rollback()
        writer.close()


def _removed_trace(trace_id: str, created_at: str):
    return QueryTrace(
        id=trace_id,
        query="query",
        answer="answer",
        source_type="manual_query",
        support_level="unsupported",
        knowledge_base_ids=[],
        cited_evidence_ids=[],
        read_events=[],
        knowledge_base_snapshots=[],
        citation_snapshots=[],
        created_at=created_at,
    )


def _removed_test_external_trace_request_key_is_nullable_but_unique_when_present(tmp_path):
    store = EvidenceStore(tmp_path / "traceable_v2.db")
    first = replace(_trace("external-1", "2026-07-21T01:00:00Z"), request_key="key-1")
    duplicate = replace(
        _trace("external-2", "2026-07-21T01:00:01Z"), request_key="key-1"
    )

    store.create_query_trace(first)
    store.create_query_trace(_trace("manual-1", "2026-07-21T01:00:02Z"))
    store.create_query_trace(_trace("manual-2", "2026-07-21T01:00:03Z"))

    with pytest.raises(sqlite3.IntegrityError):
        store.create_query_trace(duplicate)
    loaded = store.get_query_trace_by_request_key("key-1")
    assert loaded is not None
    assert loaded.id == first.id
    assert loaded.request_key == "key-1"


def _removed_test_idempotent_insert_returns_the_database_winner(tmp_path):
    store = EvidenceStore(tmp_path / "traceable_v2.db")
    candidate = replace(
        _trace("external-db-winner", "2026-07-21T10:30:00+08:00"),
        request_key="db-winner-key",
    )

    winner, created = store.create_query_trace_idempotent(candidate)

    assert created is True
    assert winner.created_at == "2026-07-21T02:30:00.000000Z"
    assert winner == store.get_query_trace(candidate.id)


def _removed_test_existing_v2_store_requires_request_key_unique_index(tmp_path):
    db_path = tmp_path / "traceable_v2.db"
    store = EvidenceStore(db_path)
    with store.connect() as connection:
        connection.execute("drop index idx_query_traces_request_key")

    with pytest.raises(IncompatibleDatabaseError, match="request_key"):
        EvidenceStore(db_path)


def _removed_test_schema_timestamps_and_trace_times_are_iso_utc_z(tmp_path):
    store = EvidenceStore(tmp_path / "traceable_v2.db")
    store.create_query_trace(_trace("offset-trace", "2026-07-21T10:30:00+08:00"))

    knowledge_base = store.get_knowledge_base(DEFAULT_KNOWLEDGE_BASE_ID)
    trace = store.get_query_trace("offset-trace")

    assert knowledge_base is not None
    assert "T" in knowledge_base.created_at and knowledge_base.created_at.endswith("Z")
    assert "T" in knowledge_base.updated_at and knowledge_base.updated_at.endswith("Z")
    assert trace is not None
    assert trace.created_at == "2026-07-21T02:30:00.000000Z"


def _removed_test_trace_rejects_naive_time_and_lists_by_normalized_real_time(tmp_path):
    store = EvidenceStore(tmp_path / "traceable_v2.db")

    with pytest.raises(ValueError, match="timezone"):
        store.create_query_trace(_trace("naive", "2026-07-21T10:00:00"))

    store.create_query_trace(_trace("later", "2026-07-21T09:00:00+08:00"))
    store.create_query_trace(_trace("earlier", "2026-07-21T01:30:00+01:00"))

    assert [trace.id for trace in store.list_query_traces()] == ["later", "earlier"]


def _removed_test_prune_query_traces_before_deletes_only_rows_older_than_cutoff(tmp_path):
    store = EvidenceStore(tmp_path / "traceable_v2.db")
    store.create_query_trace(_trace("old", "2026-04-01T00:00:00Z"))
    store.create_query_trace(_trace("boundary", "2026-05-01T00:00:00Z"))
    store.create_query_trace(_trace("recent", "2026-06-01T00:00:00Z"))

    deleted = store.prune_query_traces_before("2026-05-01T00:00:00Z")

    assert deleted == 1
    assert [trace.id for trace in store.list_query_traces()] == [
        "recent",
        "boundary",
    ]


def _removed_test_trace_insert_rolls_back_when_retention_prune_fails(
    tmp_path, monkeypatch
):
    store = EvidenceStore(tmp_path / "traceable_v2.db")

    def fail_prune(_connection, _cutoff):
        raise sqlite3.OperationalError("simulated prune failure")

    monkeypatch.setattr(
        EvidenceStore,
        "_prune_query_traces_before",
        staticmethod(fail_prune),
    )

    with pytest.raises(sqlite3.OperationalError, match="prune failure"):
        store.create_query_trace(
            _trace("rolled-back", "2026-07-21T02:00:00Z"),
            prune_before="2026-06-21T02:00:00Z",
        )

    assert store.get_query_trace("rolled-back") is None


def _removed_test_idempotent_replay_prunes_other_expired_traces_before_returning_winner(
    tmp_path,
):
    store = EvidenceStore(tmp_path / "traceable_v2.db")
    winner = replace(
        _trace("winner", "2026-06-01T00:00:00Z"),
        request_key="replay-key",
    )
    candidate = replace(
        _trace("candidate", "2026-06-02T00:00:00Z"),
        request_key="replay-key",
    )
    store.create_query_trace(winner)
    store.create_query_trace(_trace("expired-other", "2026-04-01T00:00:00Z"))

    loaded, created = store.create_query_trace_idempotent(
        candidate,
        prune_before="2026-05-01T00:00:00Z",
    )

    assert created is False
    assert loaded.id == winner.id
    assert [trace.id for trace in store.list_query_traces()] == [winner.id]


def _removed_test_idempotent_replay_replaces_expired_winner_with_new_candidate(tmp_path):
    store = EvidenceStore(tmp_path / "traceable_v2.db")
    expired_winner = replace(
        _trace("expired-winner", "2026-04-01T00:00:00Z"),
        request_key="replay-key",
    )
    candidate = replace(
        _trace("replacement", "2026-06-01T00:00:00Z"),
        request_key="replay-key",
    )
    store.create_query_trace(expired_winner)

    loaded, created = store.create_query_trace_idempotent(
        candidate,
        prune_before="2026-05-01T00:00:00Z",
    )

    assert created is True
    assert loaded.id == candidate.id
    assert store.get_query_trace(expired_winner.id) is None
    assert [trace.id for trace in store.list_query_traces()] == [candidate.id]


def _removed_test_idempotent_replay_rolls_back_prune_when_prune_fails(
    tmp_path, monkeypatch
):
    store = EvidenceStore(tmp_path / "traceable_v2.db")
    winner = replace(
        _trace("winner", "2026-06-01T00:00:00Z"),
        request_key="replay-key",
    )
    candidate = replace(
        _trace("candidate", "2026-06-02T00:00:00Z"),
        request_key="replay-key",
    )
    store.create_query_trace(winner)
    store.create_query_trace(_trace("expired-other", "2026-04-01T00:00:00Z"))

    def fail_prune(_connection, _cutoff):
        raise sqlite3.OperationalError("simulated prune failure")

    monkeypatch.setattr(
        EvidenceStore,
        "_prune_query_traces_before",
        staticmethod(fail_prune),
    )

    with pytest.raises(sqlite3.OperationalError, match="prune failure"):
        store.create_query_trace_idempotent(
            candidate,
            prune_before="2026-05-01T00:00:00Z",
        )

    assert {trace.id for trace in store.list_query_traces()} == {
        winner.id,
        "expired-other",
    }
