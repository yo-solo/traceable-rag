from __future__ import annotations

import json
import sqlite3
import unicodedata
import uuid
from pathlib import Path
from typing import Iterable, Iterator

from .models import (
    Asset,
    Block,
    Chunk,
    Document,
    IndexRecord,
    KnowledgeBase,
    ProcessingJob,
    SourceAnchor,
)


SCHEMA_VERSION = "3"
DEFAULT_KNOWLEDGE_BASE_ID = "default"
DEFAULT_KNOWLEDGE_BASE_NAME = "默认知识库"


class IncompatibleDatabaseError(RuntimeError):
    """Raised when a database does not match the current Traceable-RAG schema."""


class DocumentNotFoundError(LookupError):
    """Raised when a document is absent from the requested knowledge-base scope."""


class EvidenceStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys = on")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            tables = self._list_user_tables(conn)
            if tables:
                self._validate_schema(conn, tables)
                self._ensure_document_normalization_schema(conn)
                self._ensure_processing_jobs_schema(conn)
                self._ensure_source_anchor_schema(conn)
                return

        with self.connect() as conn:
            conn.execute("begin immediate")
            tables = self._list_user_tables(conn)
            if tables:
                self._validate_schema(conn, tables)
                self._ensure_document_normalization_schema(conn)
                self._ensure_processing_jobs_schema(conn)
                self._ensure_source_anchor_schema(conn)
            else:
                self._create_schema(conn)

    @staticmethod
    def _list_user_tables(conn: sqlite3.Connection) -> set[str]:
        return {
            row["name"]
            for row in conn.execute(
                "select name from sqlite_master where type = 'table' and name not like 'sqlite_%'"
            )
        }

    @staticmethod
    def _validate_schema(conn: sqlite3.Connection, tables: set[str]) -> None:
        if "schema_metadata" not in tables:
            raise IncompatibleDatabaseError(
                "Database is not a Traceable-RAG V3 database; use a fresh database path."
            )

        row = conn.execute(
            "select value from schema_metadata where key = 'schema_version'"
        ).fetchone()
        if row is None or row["value"] != SCHEMA_VERSION:
            found = row["value"] if row is not None else "missing"
            raise IncompatibleDatabaseError(
                f"Database schema version {found!r} is incompatible with Traceable-RAG V3."
            )

        required_tables = {
            "schema_metadata",
            "knowledge_bases",
            "documents",
            "blocks",
            "assets",
            "chunks",
            "index_records",
            "source_anchors",
        }
        missing = required_tables - tables
        if missing:
            raise IncompatibleDatabaseError(
                "Traceable-RAG V3 database is incomplete; missing tables: "
                + ", ".join(sorted(missing))
            )

        document_columns = {
            row["name"] for row in conn.execute("pragma table_info(documents)")
        }
        if "display_name_key" not in document_columns:
            raise IncompatibleDatabaseError(
                "Traceable-RAG V3 documents table is missing required column: "
                "display_name_key"
            )

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        schema_sql = """
            create table schema_metadata (
                key text primary key,
                value text not null
            );

            create table knowledge_bases (
                id text primary key,
                name text not null collate nocase unique,
                description text not null default '',
                created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );

            create table documents (
                id text primary key,
                knowledge_base_id text not null,
                original_filename text not null,
                display_name text not null,
                display_name_key text not null,
                file_hash text not null,
                source_type text not null,
                source_file_path text,
                preview_file_path text,
                mineru_result_dir text,
                page_count integer not null default 0,
                processing_status text not null,
                processing_error text,
                parser_provider text not null,
                parser_version text,
                normalization_version text,
                created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                foreign key(knowledge_base_id) references knowledge_bases(id) on delete cascade,
                unique(knowledge_base_id, file_hash),
                unique(knowledge_base_id, display_name_key)
            );

            create table blocks (
                id text primary key,
                document_id text not null,
                page_idx integer not null,
                block_idx integer not null,
                block_type text not null,
                text text,
                html text,
                asset_id text,
                bbox_json text,
                page_size_json text,
                source_ref text,
                is_discarded integer not null default 0,
                metadata_json text not null default '{}',
                foreign key(document_id) references documents(id) on delete cascade
            );

            create table assets (
                id text primary key,
                document_id text not null,
                block_id text not null,
                asset_type text not null,
                file_path text,
                content_type text,
                file_hash text,
                page_idx integer not null,
                bbox_json text,
                related_text text,
                metadata_json text not null default '{}',
                foreign key(document_id) references documents(id) on delete cascade,
                foreign key(block_id) references blocks(id) on delete cascade
            );

            create table chunks (
                id text primary key,
                document_id text not null,
                chunk_idx integer not null,
                strategy text not null,
                block_ids_json text not null,
                title_context text,
                inferred_heading_path_json text not null,
                chunk_text text not null,
                indexing_text text not null,
                page_range_json text not null,
                bbox_by_page_json text not null,
                modality_mix_json text not null,
                token_count integer not null,
                strategy_version text not null,
                foreign key(document_id) references documents(id) on delete cascade
            );

            create table index_records (
                id text primary key,
                evidence_id text not null unique,
                document_id text not null,
                chunk_id text not null,
                block_ids_json text not null,
                modality text not null,
                indexing_text text not null,
                vector_json text,
                embedding_provider text,
                embedding_model text,
                embedding_dimension integer,
                index_version text not null,
                created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                foreign key(document_id) references documents(id) on delete cascade,
                foreign key(chunk_id) references chunks(id) on delete cascade
            );

            create table processing_jobs (
                id text primary key,
                knowledge_base_id text not null,
                document_id text not null,
                operation text not null,
                status text not null,
                stage text not null,
                attempt integer not null,
                error_code text,
                error_message text,
                retryable integer,
                created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                started_at text,
                finished_at text,
                updated_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                foreign key(knowledge_base_id) references knowledge_bases(id) on delete cascade,
                foreign key(document_id) references documents(id) on delete cascade
            );

            -- Source anchor tombstones intentionally have no foreign keys: a
            -- deleted/rebuilt document must leave its old public Source URL
            -- identifiable as gone rather than silently resolving elsewhere.
            create table source_anchors (
                id text primary key,
                document_id text not null,
                record_id text not null,
                anchor_block_id text not null,
                active integer not null default 1 check(active in (0, 1)),
                invalidated_at text,
                created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );

            create index idx_documents_knowledge_base on documents(knowledge_base_id);
            create index idx_blocks_document on blocks(document_id, page_idx, block_idx);
            create index idx_assets_document on assets(document_id, page_idx);
            create index idx_chunks_document on chunks(document_id, chunk_idx);
            create index idx_index_records_document on index_records(document_id, chunk_id);
            create index idx_processing_jobs_queue
                on processing_jobs(status, created_at, id);
            create index idx_processing_jobs_scope
                on processing_jobs(knowledge_base_id, created_at desc, id desc);
            create unique index idx_processing_jobs_active_document
                on processing_jobs(document_id)
                where status in ('queued', 'running');
            create unique index idx_source_anchors_active_record_block
                on source_anchors(record_id, anchor_block_id)
                where active = 1;
            create index idx_source_anchors_active_document
                on source_anchors(document_id)
                where active = 1;
            """
        for statement in schema_sql.split(";"):
            if statement.strip():
                conn.execute(statement)
        conn.execute(
            "insert into schema_metadata (key, value) values ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        conn.execute(
            "insert into schema_metadata (key, value) values ('default_knowledge_base_initialized', '1')"
        )
        conn.execute(
            "insert into knowledge_bases (id, name, description) values (?, ?, '')",
            (DEFAULT_KNOWLEDGE_BASE_ID, DEFAULT_KNOWLEDGE_BASE_NAME),
        )

    @staticmethod
    def _ensure_document_normalization_schema(conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("pragma table_info(documents)")}
        if "normalization_version" not in columns:
            conn.execute("alter table documents add column normalization_version text")

    @staticmethod
    def _ensure_processing_jobs_schema(conn: sqlite3.Connection) -> None:
        statements = """
            create table if not exists processing_jobs (
                id text primary key,
                knowledge_base_id text not null,
                document_id text not null,
                operation text not null,
                status text not null,
                stage text not null,
                attempt integer not null,
                error_code text,
                error_message text,
                retryable integer,
                created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                started_at text,
                finished_at text,
                updated_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                foreign key(knowledge_base_id) references knowledge_bases(id) on delete cascade,
                foreign key(document_id) references documents(id) on delete cascade
            );
            create index if not exists idx_processing_jobs_queue
                on processing_jobs(status, created_at, id);
            create index if not exists idx_processing_jobs_scope
                on processing_jobs(knowledge_base_id, created_at desc, id desc);
            create unique index if not exists idx_processing_jobs_active_document
                on processing_jobs(document_id)
                where status in ('queued', 'running');
        """
        for statement in statements.split(";"):
            if statement.strip():
                conn.execute(statement)

    @staticmethod
    def _ensure_source_anchor_schema(conn: sqlite3.Connection) -> None:
        """Install opaque Source locator state without attaching it to old graphs."""

        statements = """
            create table if not exists source_anchors (
                id text primary key,
                document_id text not null,
                record_id text not null,
                anchor_block_id text not null,
                active integer not null default 1 check(active in (0, 1)),
                invalidated_at text,
                created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            create unique index if not exists idx_source_anchors_active_record_block
                on source_anchors(record_id, anchor_block_id)
                where active = 1;
            create index if not exists idx_source_anchors_active_document
                on source_anchors(document_id)
                where active = 1;
        """
        for statement in statements.split(";"):
            if statement.strip():
                conn.execute(statement)

    def enqueue_processing_job(
        self,
        job_id: str,
        knowledge_base_id: str,
        document_id: str,
        *,
        operation: str = "process",
    ) -> ProcessingJob:
        with self.connect() as conn:
            attempt = int(
                conn.execute(
                    "select count(*) + 1 from processing_jobs where document_id = ?",
                    (document_id,),
                ).fetchone()[0]
            )
            conn.execute(
                """
                insert into processing_jobs (
                    id, knowledge_base_id, document_id, operation,
                    status, stage, attempt
                ) values (?, ?, ?, ?, 'queued', 'queued', ?)
                """,
                (job_id, knowledge_base_id, document_id, operation, attempt),
            )
        job = self.get_processing_job(job_id)
        assert job is not None
        return job

    def get_processing_job(self, job_id: str) -> ProcessingJob | None:
        with self.connect() as conn:
            row = conn.execute(
                "select * from processing_jobs where id = ?", (job_id,)
            ).fetchone()
        return _processing_job_from_row(row) if row is not None else None

    def latest_processing_job(self, document_id: str) -> ProcessingJob | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                select * from processing_jobs where document_id = ?
                order by rowid desc limit 1
                """,
                (document_id,),
            ).fetchone()
        return _processing_job_from_row(row) if row is not None else None

    def list_processing_jobs(self, knowledge_base_id: str) -> list[ProcessingJob]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select * from processing_jobs where knowledge_base_id = ?
                order by rowid desc
                """,
                (knowledge_base_id,),
            ).fetchall()
        return [_processing_job_from_row(row) for row in rows]

    def processing_job_queue_position(self, job_id: str) -> int | None:
        job = self.get_processing_job(job_id)
        if job is None or job.status != "queued":
            return None
        with self.connect() as conn:
            row = conn.execute(
                "select rowid as queue_rowid from processing_jobs where id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            count = conn.execute(
                """
                select count(*) from processing_jobs
                where status = 'queued'
                  and rowid <= ?
                """,
                (row["queue_rowid"],),
            ).fetchone()[0]
        return int(count)

    def recover_processing_jobs(self) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                update processing_jobs
                set status = 'queued', stage = 'queued', started_at = null,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                where status = 'running'
                """
            )
        return cursor.rowcount

    def claim_next_processing_job(self) -> ProcessingJob | None:
        with self.connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute(
                """
                select id from processing_jobs where status = 'queued'
                order by rowid limit 1
                """
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                update processing_jobs
                set status = 'running', stage = 'converting',
                    started_at = coalesce(started_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                where id = ? and status = 'queued'
                """,
                (row["id"],),
            )
        return self.get_processing_job(row["id"])

    def update_processing_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        stage: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        retryable: bool | None = None,
        finished: bool = False,
    ) -> ProcessingJob:
        assignments = ["updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"]
        values: list[object] = []
        if status is not None:
            assignments.append("status = ?")
            values.append(status)
        if stage is not None:
            assignments.append("stage = ?")
            values.append(stage)
        if status == "failed" or error_code is not None:
            assignments.extend(
                ["error_code = ?", "error_message = ?", "retryable = ?"]
            )
            values.extend(
                [error_code, error_message, None if retryable is None else int(retryable)]
            )
        if finished:
            assignments.append("finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')")
        values.append(job_id)
        with self.connect() as conn:
            conn.execute(
                f"update processing_jobs set {', '.join(assignments)} where id = ?",
                values,
            )
        job = self.get_processing_job(job_id)
        if job is None:
            raise LookupError(job_id)
        return job

    def create_knowledge_base(self, knowledge_base: KnowledgeBase) -> None:
        with self.connect() as conn:
            conn.execute(
                "insert into knowledge_bases (id, name, description) values (?, ?, ?)",
                (knowledge_base.id, knowledge_base.name, knowledge_base.description),
            )

    def update_knowledge_base(self, knowledge_base: KnowledgeBase) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                update knowledge_bases
                set name = ?, description = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                where id = ?
                """,
                (knowledge_base.name, knowledge_base.description, knowledge_base.id),
            )
        return cursor.rowcount == 1

    def get_knowledge_base(self, knowledge_base_id: str) -> KnowledgeBase | None:
        with self.connect() as conn:
            row = conn.execute(
                "select * from knowledge_bases where id = ?", (knowledge_base_id,)
            ).fetchone()
        return _row_to_knowledge_base(row) if row else None

    def list_knowledge_bases(self) -> list[KnowledgeBase]:
        with self.connect() as conn:
            rows = conn.execute(
                "select * from knowledge_bases order by created_at, name collate nocase"
            ).fetchall()
        return [_row_to_knowledge_base(row) for row in rows]

    def list_knowledge_base_document_stats(self) -> dict[str, dict[str, int]]:
        return self._knowledge_base_document_stats()

    def get_knowledge_base_document_stats(
        self, knowledge_base_id: str
    ) -> dict[str, int]:
        return self._knowledge_base_document_stats(knowledge_base_id).get(
            knowledge_base_id,
            {
                "document_count": 0,
                "ready_document_count": 0,
                "processing_document_count": 0,
                "failed_document_count": 0,
            },
        )

    def _knowledge_base_document_stats(
        self, knowledge_base_id: str | None = None
    ) -> dict[str, dict[str, int]]:
        sql = """
            select
                knowledge_bases.id as knowledge_base_id,
                count(documents.id) as document_count,
                sum(case when documents.processing_status = 'ready' then 1 else 0 end)
                    as ready_document_count,
                sum(case when documents.processing_status in ('uploading', 'queued', 'parsing', 'indexing')
                    then 1 else 0 end) as processing_document_count,
                sum(case when documents.processing_status = 'failed' then 1 else 0 end)
                    as failed_document_count
            from knowledge_bases
            left join documents
                on documents.knowledge_base_id = knowledge_bases.id
        """
        args: tuple[object, ...] = ()
        if knowledge_base_id is not None:
            sql += " where knowledge_bases.id = ?"
            args = (knowledge_base_id,)
        sql += " group by knowledge_bases.id"
        with self.connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return {
            row["knowledge_base_id"]: {
                "document_count": row["document_count"],
                "ready_document_count": row["ready_document_count"],
                "processing_document_count": row["processing_document_count"],
                "failed_document_count": row["failed_document_count"],
            }
            for row in rows
        }

    def delete_knowledge_base(self, knowledge_base_id: str) -> bool:
        with self.connect() as conn:
            self._invalidate_knowledge_base_sources(conn, knowledge_base_id)
            cursor = conn.execute("delete from knowledge_bases where id = ?", (knowledge_base_id,))
        return cursor.rowcount == 1

    @staticmethod
    def _invalidate_document_sources(conn: sqlite3.Connection, document_id: str) -> None:
        conn.execute(
            """
            update source_anchors
            set active = 0,
                invalidated_at = coalesce(
                    invalidated_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                )
            where document_id = ? and active = 1
            """,
            (document_id,),
        )

    @classmethod
    def _invalidate_knowledge_base_sources(
        cls, conn: sqlite3.Connection, knowledge_base_id: str
    ) -> None:
        document_ids = [
            row["id"]
            for row in conn.execute(
                "select id from documents where knowledge_base_id = ?",
                (knowledge_base_id,),
            )
        ]
        for document_id in document_ids:
            cls._invalidate_document_sources(conn, document_id)

    @staticmethod
    def _create_source_anchors(
        conn: sqlite3.Connection, records: Iterable[IndexRecord]
    ) -> None:
        """Create one opaque active anchor per record and selected source block."""

        for record in records:
            if not record.block_ids:
                continue
            anchor_block_id = record.block_ids[0]
            existing = conn.execute(
                """
                select id from source_anchors
                where record_id = ? and anchor_block_id = ? and active = 1
                """,
                (record.id, anchor_block_id),
            ).fetchone()
            if existing is not None:
                continue
            # UUID4 is random, public-safe, and has no recoverable record or
            # block identity. Retrying on the astronomically unlikely collision
            # keeps the primary-key contract explicit.
            while True:
                source_id = f"src_{uuid.uuid4().hex}"
                try:
                    conn.execute(
                        """
                        insert into source_anchors (
                            id, document_id, record_id, anchor_block_id
                        ) values (?, ?, ?, ?)
                        """,
                        (source_id, record.document_id, record.id, anchor_block_id),
                    )
                    break
                except sqlite3.IntegrityError:
                    # The partial unique index can only race with another
                    # writer; re-check it before trying a new random ID.
                    existing = conn.execute(
                        """
                        select id from source_anchors
                        where record_id = ? and anchor_block_id = ? and active = 1
                        """,
                        (record.id, anchor_block_id),
                    ).fetchone()
                    if existing is not None:
                        break

    def get_source_anchor(self, source_id: str) -> SourceAnchor | None:
        with self.connect() as conn:
            row = conn.execute(
                "select * from source_anchors where id = ?", (source_id,)
            ).fetchone()
        return _row_to_source_anchor(row) if row else None

    def get_active_source_anchor(self, source_id: str) -> SourceAnchor | None:
        with self.connect() as conn:
            row = conn.execute(
                "select * from source_anchors where id = ? and active = 1",
                (source_id,),
            ).fetchone()
        return _row_to_source_anchor(row) if row else None

    def source_anchor_status(self, source_id: str) -> str:
        """Return the public-facing lifecycle state: active, gone, or unknown."""

        anchor = self.get_source_anchor(source_id)
        if anchor is None:
            return "unknown"
        return "active" if anchor.active else "gone"

    def get_or_create_active_source_anchor(
        self, record_id: str, anchor_block_id: str
    ) -> SourceAnchor:
        """Return the unique active opaque locator for a record/source block pair."""

        with self.connect() as conn:
            record = conn.execute(
                "select * from index_records where id = ?", (record_id,)
            ).fetchone()
            if record is None:
                raise LookupError(record_id)
            block_ids = _loads(record["block_ids_json"], [])
            if anchor_block_id not in block_ids:
                raise ValueError("Anchor block does not belong to the index record.")
            self._create_source_anchors(
                conn,
                [
                    IndexRecord(
                        id=record["id"],
                        evidence_id=record["evidence_id"],
                        document_id=record["document_id"],
                        chunk_id=record["chunk_id"],
                        block_ids=[anchor_block_id],
                        modality=record["modality"],
                        indexing_text=record["indexing_text"],
                        vector=_loads(record["vector_json"]),
                        embedding_provider=record["embedding_provider"],
                        embedding_model=record["embedding_model"],
                        embedding_dimension=record["embedding_dimension"],
                        index_version=record["index_version"],
                    )
                ],
            )
            row = conn.execute(
                """
                select * from source_anchors
                where record_id = ? and anchor_block_id = ? and active = 1
                """,
                (record_id, anchor_block_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("Active source anchor was not created.")
        return _row_to_source_anchor(row)

    def list_source_context_blocks(
        self, source_id: str, *, before: int = 2, after: int = 2
    ) -> tuple[Block, list[Block], list[Block]] | None:
        """Load the active anchor and its neighbours in forward reading order."""

        if before < 0 or after < 0:
            raise ValueError("Context bounds must be non-negative.")
        with self.connect() as conn:
            anchor = conn.execute(
                """
                select blocks.*
                from source_anchors
                join blocks on blocks.id = source_anchors.anchor_block_id
                where source_anchors.id = ?
                  and source_anchors.active = 1
                  and blocks.is_discarded = 0
                """,
                (source_id,),
            ).fetchone()
            if anchor is None:
                return None
            document_id = anchor["document_id"]
            page_idx = anchor["page_idx"]
            block_idx = anchor["block_idx"]
            block_id = anchor["id"]
            before_rows = conn.execute(
                """
                select * from blocks
                where document_id = ? and is_discarded = 0
                  and not (
                    block_type in ('image', 'chart')
                    and coalesce(trim(text), '') = ''
                  )
                  and (
                    page_idx < ?
                    or (page_idx = ? and block_idx < ?)
                    or (page_idx = ? and block_idx = ? and id < ?)
                  )
                order by page_idx desc, block_idx desc, id desc
                limit ?
                """,
                (document_id, page_idx, page_idx, block_idx, page_idx, block_idx, block_id, before),
            ).fetchall()
            after_rows = conn.execute(
                """
                select * from blocks
                where document_id = ? and is_discarded = 0
                  and not (
                    block_type in ('image', 'chart')
                    and coalesce(trim(text), '') = ''
                  )
                  and (
                    page_idx > ?
                    or (page_idx = ? and block_idx > ?)
                    or (page_idx = ? and block_idx = ? and id > ?)
                  )
                order by page_idx asc, block_idx asc, id asc
                limit ?
                """,
                (document_id, page_idx, page_idx, block_idx, page_idx, block_idx, block_id, after),
            ).fetchall()
        return (
            _row_to_block(anchor),
            [_row_to_block(row) for row in reversed(before_rows)],
            [_row_to_block(row) for row in after_rows],
        )

    def load_active_source_graph(
        self, source_id: str
    ) -> tuple[SourceAnchor, Document, Chunk, IndexRecord, Block] | None:
        """Load the current graph behind an active Source anchor atomically."""

        with self.connect() as conn:
            row = conn.execute(
                """
                select
                    source_anchors.id as source_anchor_id,
                    source_anchors.document_id as source_anchor_document_id,
                    source_anchors.record_id as source_anchor_record_id,
                    source_anchors.anchor_block_id as source_anchor_block_id,
                    source_anchors.active as source_anchor_active,
                    source_anchors.invalidated_at as source_anchor_invalidated_at,
                    source_anchors.created_at as source_anchor_created_at,
                    documents.id as scoped_document_id,
                    chunks.id as scoped_chunk_id,
                    index_records.id as scoped_record_id,
                    blocks.id as scoped_block_id
                from source_anchors
                join documents
                    on documents.id = source_anchors.document_id
                join index_records
                    on index_records.id = source_anchors.record_id
                   and index_records.document_id = documents.id
                join chunks
                    on chunks.id = index_records.chunk_id
                   and chunks.document_id = documents.id
                join blocks
                    on blocks.id = source_anchors.anchor_block_id
                   and blocks.document_id = documents.id
                where source_anchors.id = ?
                  and source_anchors.active = 1
                  and blocks.is_discarded = 0
                """,
                (source_id,),
            ).fetchone()
            if row is None:
                return None
            document_row = conn.execute(
                "select * from documents where id = ?", (row["scoped_document_id"],)
            ).fetchone()
            chunk_row = conn.execute(
                "select * from chunks where id = ?", (row["scoped_chunk_id"],)
            ).fetchone()
            record_row = conn.execute(
                "select * from index_records where id = ?", (row["scoped_record_id"],)
            ).fetchone()
            block_row = conn.execute(
                "select * from blocks where id = ?", (row["scoped_block_id"],)
            ).fetchone()
        if any(item is None for item in (document_row, chunk_row, record_row, block_row)):
            return None
        record = _row_to_index_record(record_row)
        if row["source_anchor_block_id"] not in record.block_ids:
            return None
        return (
            SourceAnchor(
                id=row["source_anchor_id"],
                document_id=row["source_anchor_document_id"],
                record_id=row["source_anchor_record_id"],
                anchor_block_id=row["source_anchor_block_id"],
                active=bool(row["source_anchor_active"]),
                invalidated_at=row["source_anchor_invalidated_at"],
                created_at=row["source_anchor_created_at"],
            ),
            _row_to_document(document_row),
            _row_to_chunk(chunk_row),
            record,
            _row_to_block(block_row),
        )

    def upsert_document(self, document: Document) -> None:
        with self.connect() as conn:
            self._upsert_document(conn, document)

    def replace_document_graph(
        self,
        knowledge_base_id: str,
        document: Document,
        blocks: Iterable[Block],
        assets: Iterable[Asset],
        chunks: Iterable[Chunk],
        records: Iterable[IndexRecord],
    ) -> None:
        blocks = list(blocks)
        assets = list(assets)
        chunks = list(chunks)
        records = list(records)
        if document.knowledge_base_id != knowledge_base_id:
            raise ValueError("Document belongs to another knowledge base.")
        for items in (blocks, assets, chunks, records):
            _validate_document_ids(document.id, items)
        block_ids = {block.id for block in blocks}
        if any(asset.block_id not in block_ids for asset in assets):
            raise ValueError("Asset references a block outside the replacement graph.")
        if any(set(chunk.block_ids) - block_ids for chunk in chunks):
            raise ValueError("Chunk references a block outside the replacement graph.")
        chunks_by_id = {chunk.id: chunk for chunk in chunks}
        for record in records:
            chunk = chunks_by_id.get(record.chunk_id)
            if chunk is None or record.block_ids != chunk.block_ids:
                raise ValueError("Index record does not match its replacement chunk.")

        with self.connect() as conn:
            conn.execute("begin immediate")
            self._upsert_document(conn, document)
            self._invalidate_document_sources(conn, document.id)
            conn.execute("delete from chunks where document_id = ?", (document.id,))
            conn.execute("delete from blocks where document_id = ?", (document.id,))
            conn.executemany(
                """
                insert into blocks (
                    id, document_id, page_idx, block_idx, block_type, text, html, asset_id,
                    bbox_json, page_size_json, source_ref, is_discarded, metadata_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        block.id,
                        block.document_id,
                        block.page_idx,
                        block.block_idx,
                        block.block_type,
                        block.text,
                        block.html,
                        block.asset_id,
                        _dumps(block.bbox),
                        _dumps(block.page_size),
                        block.source_ref,
                        1 if block.is_discarded else 0,
                        _dumps(block.metadata),
                    )
                    for block in blocks
                ],
            )
            conn.executemany(
                """
                insert into assets (
                    id, document_id, block_id, asset_type, file_path, content_type,
                    file_hash, page_idx, bbox_json, related_text, metadata_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        asset.id,
                        asset.document_id,
                        asset.block_id,
                        asset.asset_type,
                        asset.file_path,
                        asset.content_type,
                        asset.file_hash,
                        asset.page_idx,
                        _dumps(asset.bbox),
                        asset.related_text,
                        _dumps(asset.metadata),
                    )
                    for asset in assets
                ],
            )
            conn.executemany(
                """
                insert into chunks (
                    id, document_id, chunk_idx, strategy, block_ids_json, title_context,
                    inferred_heading_path_json, chunk_text, indexing_text, page_range_json,
                    bbox_by_page_json, modality_mix_json, token_count, strategy_version
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        chunk.id,
                        chunk.document_id,
                        chunk.chunk_idx,
                        chunk.strategy,
                        _dumps(chunk.block_ids),
                        chunk.title_context,
                        _dumps(chunk.inferred_heading_path),
                        chunk.chunk_text,
                        chunk.indexing_text,
                        _dumps(chunk.page_range),
                        _dumps(chunk.bbox_by_page),
                        _dumps(chunk.modality_mix),
                        chunk.token_count,
                        chunk.strategy_version,
                    )
                    for chunk in chunks
                ],
            )
            conn.executemany(
                """
                insert into index_records (
                    id, evidence_id, document_id, chunk_id, block_ids_json, modality,
                    indexing_text, vector_json, embedding_provider, embedding_model,
                    embedding_dimension, index_version
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        record.id,
                        record.evidence_id,
                        record.document_id,
                        record.chunk_id,
                        _dumps(record.block_ids),
                        record.modality,
                        record.indexing_text,
                        _dumps(record.vector),
                        record.embedding_provider,
                        record.embedding_model,
                        record.embedding_dimension,
                        record.index_version,
                    )
                    for record in records
                ],
            )
            self._create_source_anchors(conn, records)

    @staticmethod
    def _upsert_document(conn: sqlite3.Connection, document: Document) -> None:
        cursor = conn.execute(
            """
            insert into documents (
                id, knowledge_base_id, original_filename, display_name, display_name_key, file_hash,
                source_type, source_file_path, preview_file_path, mineru_result_dir,
                page_count, processing_status, processing_error, parser_provider,
                parser_version, normalization_version
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set
                original_filename = excluded.original_filename,
                display_name = excluded.display_name,
                display_name_key = excluded.display_name_key,
                file_hash = excluded.file_hash,
                source_type = excluded.source_type,
                source_file_path = excluded.source_file_path,
                preview_file_path = excluded.preview_file_path,
                mineru_result_dir = excluded.mineru_result_dir,
                page_count = excluded.page_count,
                processing_status = excluded.processing_status,
                processing_error = excluded.processing_error,
                parser_provider = excluded.parser_provider,
                parser_version = excluded.parser_version,
                normalization_version = excluded.normalization_version,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            where documents.knowledge_base_id = excluded.knowledge_base_id
            """,
            (
                document.id,
                document.knowledge_base_id,
                document.original_filename,
                document.display_name,
                normalize_document_display_name(document.display_name),
                document.file_hash,
                document.source_type,
                document.source_file_path,
                document.preview_file_path,
                document.mineru_result_dir,
                document.page_count,
                document.processing_status,
                document.processing_error,
                document.parser_provider,
                document.parser_version,
                document.normalization_version,
            ),
        )
        if cursor.rowcount != 1:
            raise DocumentNotFoundError(
                f"Document {document.id!r} was not found in knowledge base "
                f"{document.knowledge_base_id!r}."
            )

    def get_document(self, knowledge_base_id: str, document_id: str) -> Document | None:
        with self.connect() as conn:
            row = conn.execute(
                "select * from documents where knowledge_base_id = ? and id = ?",
                (knowledge_base_id, document_id),
            ).fetchone()
        return _row_to_document(row) if row else None

    def document_id_exists(self, document_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "select 1 from documents where id = ?", (document_id,)
            ).fetchone()
        return row is not None

    def list_documents(self, knowledge_base_id: str) -> list[Document]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select * from documents
                where knowledge_base_id = ?
                order by created_at, display_name collate nocase
                """,
                (knowledge_base_id,),
            ).fetchall()
        return [_row_to_document(row) for row in rows]

    def list_documents_needing_normalization(
        self, normalization_version: str
    ) -> list[Document]:
        target_operation = f"normalize_upgrade:{normalization_version}"
        with self.connect() as conn:
            rows = conn.execute(
                """
                select documents.* from documents
                where coalesce(documents.normalization_version, '') != ?
                  and documents.processing_status in ('parsed', 'ready')
                  and not exists (
                      select 1 from processing_jobs
                      where processing_jobs.document_id = documents.id
                        and processing_jobs.operation = ?
                        and (
                            processing_jobs.status in ('queued', 'running', 'succeeded')
                            or (
                                processing_jobs.status = 'failed'
                                and coalesce(processing_jobs.retryable, 0) = 0
                            )
                        )
                  )
                order by documents.created_at, documents.id
                """,
                (normalization_version, target_operation),
            ).fetchall()
        return [_row_to_document(row) for row in rows]

    def list_document_graph_stats(
        self,
        knowledge_base_id: str,
        document_id: str | None = None,
    ) -> dict[str, dict[str, int]]:
        document_filter = ""
        args: tuple[object, ...] = (knowledge_base_id,)
        if document_id is not None:
            document_filter = " and id = ?"
            args = (knowledge_base_id, document_id)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                with scoped_documents as (
                    select id from documents
                    where knowledge_base_id = ?{document_filter}
                ),
                block_counts as (
                    select blocks.document_id, count(*) as count
                    from blocks
                    join scoped_documents on scoped_documents.id = blocks.document_id
                    where blocks.is_discarded = 0
                    group by blocks.document_id
                ),
                asset_counts as (
                    select assets.document_id, count(*) as count
                    from assets
                    join scoped_documents on scoped_documents.id = assets.document_id
                    group by assets.document_id
                ),
                chunk_counts as (
                    select chunks.document_id, count(*) as count
                    from chunks
                    join scoped_documents on scoped_documents.id = chunks.document_id
                    group by chunks.document_id
                ),
                index_record_counts as (
                    select index_records.document_id, count(*) as count
                    from index_records
                    join scoped_documents on scoped_documents.id = index_records.document_id
                    group by index_records.document_id
                ),
                valid_embedding_records as (
                    select
                        index_records.document_id,
                        index_records.embedding_provider,
                        index_records.embedding_model,
                        index_records.embedding_dimension
                    from index_records
                    join scoped_documents on scoped_documents.id = index_records.document_id
                    where index_records.vector_json is not null
                      and json_valid(index_records.vector_json)
                      and json_type(index_records.vector_json) = 'array'
                      and index_records.embedding_dimension > 0
                      and json_array_length(index_records.vector_json)
                          = index_records.embedding_dimension
                      and coalesce(trim(index_records.embedding_provider), '') != ''
                      and coalesce(trim(index_records.embedding_model), '') != ''
                      and not exists (
                          select 1
                          from json_each(index_records.vector_json) as vector_value
                          where vector_value.type not in ('integer', 'real')
                      )
                      and not exists (
                          select 1
                          from json_each(index_records.vector_json) as vector_value
                          where abs(cast(vector_value.value as real))
                              > 1.7976931348623157e308
                      )
                      and exists (
                          select 1
                          from json_each(index_records.vector_json) as vector_value
                          where cast(vector_value.value as real) != 0.0
                      )
                ),
                embedding_record_counts as (
                    select document_id, count(*) as count
                    from valid_embedding_records
                    group by document_id
                ),
                embedding_configuration_counts as (
                    select document_id, count(*) as count
                    from (
                        select distinct
                            document_id,
                            embedding_provider,
                            embedding_model,
                            embedding_dimension
                        from valid_embedding_records
                    ) as embedding_configurations
                    group by document_id
                )
                select
                    scoped_documents.id as document_id,
                    coalesce(block_counts.count, 0) as blocks,
                    coalesce(asset_counts.count, 0) as assets,
                    coalesce(chunk_counts.count, 0) as chunks,
                    coalesce(index_record_counts.count, 0) as index_records,
                    coalesce(embedding_record_counts.count, 0) as embedding_records,
                    coalesce(embedding_configuration_counts.count, 0)
                        as embedding_configurations
                from scoped_documents
                left join block_counts
                    on block_counts.document_id = scoped_documents.id
                left join asset_counts
                    on asset_counts.document_id = scoped_documents.id
                left join chunk_counts
                    on chunk_counts.document_id = scoped_documents.id
                left join index_record_counts
                    on index_record_counts.document_id = scoped_documents.id
                left join embedding_record_counts
                    on embedding_record_counts.document_id = scoped_documents.id
                left join embedding_configuration_counts
                    on embedding_configuration_counts.document_id = scoped_documents.id
                """,
                args,
            ).fetchall()
        return {
            row["document_id"]: {
                "blocks": row["blocks"],
                "assets": row["assets"],
                "chunks": row["chunks"],
                "index_records": row["index_records"],
                "embedding_records": row["embedding_records"],
                "embedding_configurations": row["embedding_configurations"],
            }
            for row in rows
        }

    def delete_document(self, knowledge_base_id: str, document_id: str) -> bool:
        with self.connect() as conn:
            self._require_document(conn, knowledge_base_id, document_id)
            self._invalidate_document_sources(conn, document_id)
            cursor = conn.execute(
                "delete from documents where knowledge_base_id = ? and id = ?",
                (knowledge_base_id, document_id),
            )
        return cursor.rowcount == 1

    def update_document_display_name(
        self,
        knowledge_base_id: str,
        document_id: str,
        display_name: str,
    ) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                update documents
                set display_name = ?, display_name_key = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                where knowledge_base_id = ? and id = ?
                """,
                (
                    display_name,
                    normalize_document_display_name(display_name),
                    knowledge_base_id,
                    document_id,
                ),
            )
        return cursor.rowcount == 1

    def replace_blocks(
        self, knowledge_base_id: str, document_id: str, blocks: Iterable[Block]
    ) -> None:
        blocks = list(blocks)
        _validate_document_ids(document_id, blocks)
        with self.connect() as conn:
            self._require_document(conn, knowledge_base_id, document_id)
            self._invalidate_document_sources(conn, document_id)
            conn.execute("delete from chunks where document_id = ?", (document_id,))
            conn.execute("delete from blocks where document_id = ?", (document_id,))
            conn.executemany(
                """
                insert into blocks (
                    id, document_id, page_idx, block_idx, block_type, text, html, asset_id,
                    bbox_json, page_size_json, source_ref, is_discarded, metadata_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        block.id,
                        block.document_id,
                        block.page_idx,
                        block.block_idx,
                        block.block_type,
                        block.text,
                        block.html,
                        block.asset_id,
                        _dumps(block.bbox),
                        _dumps(block.page_size),
                        block.source_ref,
                        1 if block.is_discarded else 0,
                        _dumps(block.metadata),
                    )
                    for block in blocks
                ],
            )

    def list_blocks(
        self,
        knowledge_base_id: str,
        document_id: str,
        include_discarded: bool = False,
    ) -> list[Block]:
        sql = "select * from blocks where document_id = ?"
        if not include_discarded:
            sql += " and is_discarded = 0"
        sql += " order by page_idx, block_idx"
        with self.connect() as conn:
            self._require_document(conn, knowledge_base_id, document_id)
            rows = conn.execute(sql, (document_id,)).fetchall()
        return [_row_to_block(row) for row in rows]

    def replace_assets(
        self, knowledge_base_id: str, document_id: str, assets: Iterable[Asset]
    ) -> None:
        assets = list(assets)
        _validate_document_ids(document_id, assets)
        with self.connect() as conn:
            self._require_document(conn, knowledge_base_id, document_id)
            _validate_owned_references(
                conn,
                table="blocks",
                document_id=document_id,
                referenced_ids={asset.block_id for asset in assets},
                reference_name="block",
            )
            conn.execute("delete from assets where document_id = ?", (document_id,))
            conn.executemany(
                """
                insert into assets (
                    id, document_id, block_id, asset_type, file_path, content_type,
                    file_hash, page_idx, bbox_json, related_text, metadata_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        asset.id,
                        asset.document_id,
                        asset.block_id,
                        asset.asset_type,
                        asset.file_path,
                        asset.content_type,
                        asset.file_hash,
                        asset.page_idx,
                        _dumps(asset.bbox),
                        asset.related_text,
                        _dumps(asset.metadata),
                    )
                    for asset in assets
                ],
            )

    def list_assets(self, knowledge_base_id: str, document_id: str) -> list[Asset]:
        with self.connect() as conn:
            self._require_document(conn, knowledge_base_id, document_id)
            rows = conn.execute(
                "select * from assets where document_id = ? order by page_idx, block_id",
                (document_id,),
            ).fetchall()
        return [_row_to_asset(row) for row in rows]

    def get_asset(
        self,
        knowledge_base_id: str,
        document_id: str,
        asset_id: str,
    ) -> Asset | None:
        with self.connect() as conn:
            self._require_document(conn, knowledge_base_id, document_id)
            row = conn.execute(
                """
                select assets.* from assets
                join documents on documents.id = assets.document_id
                where assets.id = ?
                  and assets.document_id = ?
                  and documents.knowledge_base_id = ?
                """,
                (asset_id, document_id, knowledge_base_id),
            ).fetchone()
        return _row_to_asset(row) if row else None

    def replace_chunks(
        self, knowledge_base_id: str, document_id: str, chunks: Iterable[Chunk]
    ) -> None:
        chunks = list(chunks)
        _validate_document_ids(document_id, chunks)
        with self.connect() as conn:
            self._require_document(conn, knowledge_base_id, document_id)
            _validate_owned_references(
                conn,
                table="blocks",
                document_id=document_id,
                referenced_ids={block_id for chunk in chunks for block_id in chunk.block_ids},
                reference_name="block",
            )
            self._invalidate_document_sources(conn, document_id)
            conn.execute("delete from chunks where document_id = ?", (document_id,))
            conn.executemany(
                """
                insert into chunks (
                    id, document_id, chunk_idx, strategy, block_ids_json, title_context,
                    inferred_heading_path_json, chunk_text, indexing_text, page_range_json,
                    bbox_by_page_json, modality_mix_json, token_count, strategy_version
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        chunk.id,
                        chunk.document_id,
                        chunk.chunk_idx,
                        chunk.strategy,
                        _dumps(chunk.block_ids),
                        chunk.title_context,
                        _dumps(chunk.inferred_heading_path),
                        chunk.chunk_text,
                        chunk.indexing_text,
                        _dumps(chunk.page_range),
                        _dumps(chunk.bbox_by_page),
                        _dumps(chunk.modality_mix),
                        chunk.token_count,
                        chunk.strategy_version,
                    )
                    for chunk in chunks
                ],
            )

    def list_chunks(
        self, knowledge_base_id: str, document_id: str | None = None
    ) -> list[Chunk]:
        sql = """
            select chunks.* from chunks
            join documents on documents.id = chunks.document_id
            where documents.knowledge_base_id = ?
        """
        args: tuple[object, ...] = (knowledge_base_id,)
        with self.connect() as conn:
            if document_id is not None:
                self._require_document(conn, knowledge_base_id, document_id)
                sql += " and chunks.document_id = ?"
                args = (knowledge_base_id, document_id)
            sql += " order by chunks.document_id, chunks.chunk_idx"
            rows = conn.execute(sql, args).fetchall()
        return [_row_to_chunk(row) for row in rows]

    def replace_index_records(
        self, knowledge_base_id: str, document_id: str, records: Iterable[IndexRecord]
    ) -> None:
        records = list(records)
        _validate_document_ids(document_id, records)
        with self.connect() as conn:
            self._require_document(conn, knowledge_base_id, document_id)
            _validate_owned_references(
                conn,
                table="chunks",
                document_id=document_id,
                referenced_ids={record.chunk_id for record in records},
                reference_name="chunk",
            )
            _validate_owned_references(
                conn,
                table="blocks",
                document_id=document_id,
                referenced_ids={block_id for record in records for block_id in record.block_ids},
                reference_name="block",
            )
            _validate_index_record_block_ids(conn, document_id, records)
            self._invalidate_document_sources(conn, document_id)
            conn.execute("delete from index_records where document_id = ?", (document_id,))
            conn.executemany(
                """
                insert into index_records (
                    id, evidence_id, document_id, chunk_id, block_ids_json, modality,
                    indexing_text, vector_json, embedding_provider, embedding_model,
                    embedding_dimension, index_version
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        record.id,
                        record.evidence_id,
                        record.document_id,
                        record.chunk_id,
                        _dumps(record.block_ids),
                        record.modality,
                        record.indexing_text,
                        _dumps(record.vector),
                        record.embedding_provider,
                        record.embedding_model,
                        record.embedding_dimension,
                        record.index_version,
                    )
                    for record in records
                ],
            )
            self._create_source_anchors(conn, records)

    def list_index_records(
        self, knowledge_base_id: str, document_id: str | None = None
    ) -> list[IndexRecord]:
        sql = """
            select index_records.* from index_records
            join documents on documents.id = index_records.document_id
            where documents.knowledge_base_id = ?
        """
        args: tuple[object, ...] = (knowledge_base_id,)
        with self.connect() as conn:
            if document_id is not None:
                self._require_document(conn, knowledge_base_id, document_id)
                sql += " and index_records.document_id = ?"
                args = (knowledge_base_id, document_id)
            sql += " order by index_records.document_id, index_records.chunk_id"
            rows = conn.execute(sql, args).fetchall()
        return [_row_to_index_record(row) for row in rows]

    def load_retrieval_scope(
        self,
        knowledge_base_ids: Iterable[str],
        *,
        document_ids: Iterable[str] | None = None,
    ) -> tuple[list[Document], list[Chunk], list[IndexRecord], list[Block]]:
        """Load only rows owned by the requested knowledge bases.

        The scope is applied in SQL so callers never need to retrieve the global
        index and filter it in application memory.
        """

        scope = list(dict.fromkeys(knowledge_base_ids))
        if not scope:
            return [], [], [], []
        document_scope = (
            list(dict.fromkeys(document_ids)) if document_ids is not None else None
        )
        if document_scope == []:
            return [], [], [], []
        placeholders = ", ".join("?" for _ in scope)
        document_clause = ""
        args = list(scope)
        if document_scope is not None:
            document_placeholders = ", ".join("?" for _ in document_scope)
            document_clause = f" and documents.id in ({document_placeholders})"
            args.extend(document_scope)
        with self.connect() as conn:
            conn.execute("begin")
            document_rows = conn.execute(
                f"""
                select documents.* from documents
                where documents.knowledge_base_id in ({placeholders})
                  {document_clause}
                order by knowledge_base_id, created_at, id
                """,
                args,
            ).fetchall()
            chunk_rows = conn.execute(
                f"""
                select chunks.* from chunks
                join documents on documents.id = chunks.document_id
                where documents.knowledge_base_id in ({placeholders})
                  {document_clause}
                order by documents.knowledge_base_id, chunks.document_id, chunks.chunk_idx
                """,
                args,
            ).fetchall()
            record_rows = conn.execute(
                f"""
                select index_records.* from index_records
                join documents on documents.id = index_records.document_id
                where documents.knowledge_base_id in ({placeholders})
                  {document_clause}
                order by documents.knowledge_base_id, index_records.document_id,
                         index_records.chunk_id
                """,
                args,
            ).fetchall()
            block_rows = conn.execute(
                f"""
                select blocks.* from blocks
                join documents on documents.id = blocks.document_id
                where documents.knowledge_base_id in ({placeholders})
                  {document_clause}
                  and blocks.is_discarded = 0
                order by documents.knowledge_base_id, blocks.document_id,
                         blocks.page_idx, blocks.block_idx
                """,
                args,
            ).fetchall()
        return (
            [_row_to_document(row) for row in document_rows],
            [_row_to_chunk(row) for row in chunk_rows],
            [_row_to_index_record(row) for row in record_rows],
            [_row_to_block(row) for row in block_rows],
        )

    def load_evidence_scope(
        self, evidence_id: str
    ) -> tuple[Document, Chunk, IndexRecord, list[Block]] | None:
        """Load one evidence graph through its owning document in SQL."""

        with self.connect() as conn:
            conn.execute("begin")
            row = conn.execute(
                """
                select
                    documents.id as scoped_document_id,
                    chunks.id as scoped_chunk_id,
                    index_records.id as scoped_record_id
                from index_records
                join documents on documents.id = index_records.document_id
                join chunks on chunks.id = index_records.chunk_id
                    and chunks.document_id = documents.id
                where index_records.evidence_id = ?
                """,
                (evidence_id,),
            ).fetchone()
            if row is None:
                return None
            document_row = conn.execute(
                "select * from documents where id = ?",
                (row["scoped_document_id"],),
            ).fetchone()
            chunk_row = conn.execute(
                "select * from chunks where id = ? and document_id = ?",
                (row["scoped_chunk_id"], row["scoped_document_id"]),
            ).fetchone()
            record_row = conn.execute(
                "select * from index_records where id = ? and document_id = ?",
                (row["scoped_record_id"], row["scoped_document_id"]),
            ).fetchone()
            block_rows = conn.execute(
                """
                select * from blocks
                where document_id = ? and is_discarded = 0
                order by page_idx, block_idx
                """,
                (row["scoped_document_id"],),
            ).fetchall()
        return (
            _row_to_document(document_row),
            _row_to_chunk(chunk_row),
            _row_to_index_record(record_row),
            [_row_to_block(block_row) for block_row in block_rows],
        )

    def existing_knowledge_base_ids(self, knowledge_base_ids: Iterable[str]) -> set[str]:
        requested = sorted(set(knowledge_base_ids))
        if not requested:
            return set()
        payload = _dumps(requested)
        with self.connect() as conn:
            rows = conn.execute(
                """
                select knowledge_bases.id
                from knowledge_bases
                join json_each(?) as requested on knowledge_bases.id = requested.value
                """,
                (payload,),
            ).fetchall()
        return {row["id"] for row in rows}

    def existing_document_pairs(
        self, pairs: Iterable[tuple[str, str]]
    ) -> set[tuple[str, str]]:
        requested = sorted(set(pairs))
        if not requested:
            return set()
        payload = _dumps([[knowledge_base_id, document_id] for knowledge_base_id, document_id in requested])
        with self.connect() as conn:
            rows = conn.execute(
                """
                select distinct documents.knowledge_base_id, documents.id
                from documents
                join json_each(?) as requested
                  on documents.knowledge_base_id = json_extract(requested.value, '$[0]')
                 and documents.id = json_extract(requested.value, '$[1]')
                """,
                (payload,),
            ).fetchall()
        return {(row["knowledge_base_id"], row["id"]) for row in rows}

    @staticmethod
    def _require_document(
        conn: sqlite3.Connection, knowledge_base_id: str, document_id: str
    ) -> None:
        row = conn.execute(
            "select 1 from documents where knowledge_base_id = ? and id = ?",
            (knowledge_base_id, document_id),
        ).fetchone()
        if row is None:
            raise DocumentNotFoundError(
                f"Document {document_id!r} was not found in knowledge base {knowledge_base_id!r}."
            )


def _validate_document_ids(document_id: str, records: Iterable[object]) -> None:
    for record in records:
        if getattr(record, "document_id", None) != document_id:
            raise ValueError(f"Record {getattr(record, 'id', None)!r} belongs to another document.")


def normalize_document_display_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _validate_owned_references(
    conn: sqlite3.Connection,
    *,
    table: str,
    document_id: str,
    referenced_ids: set[str],
    reference_name: str,
) -> None:
    if not referenced_ids:
        return
    placeholders = ", ".join("?" for _ in referenced_ids)
    rows = conn.execute(
        f"select id from {table} where document_id = ? and id in ({placeholders})",
        (document_id, *sorted(referenced_ids)),
    ).fetchall()
    owned_ids = {row["id"] for row in rows}
    invalid_ids = referenced_ids - owned_ids
    if invalid_ids:
        raise ValueError(
            f"Invalid {reference_name} reference for document {document_id!r}: "
            + ", ".join(sorted(invalid_ids))
        )


def _validate_index_record_block_ids(
    conn: sqlite3.Connection, document_id: str, records: list[IndexRecord]
) -> None:
    if not records:
        return
    chunk_ids = sorted({record.chunk_id for record in records})
    placeholders = ", ".join("?" for _ in chunk_ids)
    rows = conn.execute(
        f"""
        select id, block_ids_json from chunks
        where document_id = ? and id in ({placeholders})
        """,
        (document_id, *chunk_ids),
    ).fetchall()
    block_ids_by_chunk = {
        row["id"]: _loads(row["block_ids_json"], [])
        for row in rows
    }
    for record in records:
        if record.block_ids != block_ids_by_chunk[record.chunk_id]:
            raise ValueError(
                f"Index record {record.id!r} block_ids must match referenced chunk "
                f"{record.chunk_id!r}."
            )


def _dumps(value: object) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _loads(value: str | None, default: object = None):
    if value is None:
        return default
    return json.loads(value)


def _row_to_knowledge_base(row: sqlite3.Row) -> KnowledgeBase:
    return KnowledgeBase(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_document(row: sqlite3.Row) -> Document:
    return Document(
        id=row["id"],
        knowledge_base_id=row["knowledge_base_id"],
        original_filename=row["original_filename"],
        display_name=row["display_name"],
        file_hash=row["file_hash"],
        source_type=row["source_type"],
        source_file_path=row["source_file_path"],
        preview_file_path=row["preview_file_path"],
        mineru_result_dir=row["mineru_result_dir"],
        page_count=row["page_count"],
        processing_status=row["processing_status"],
        processing_error=row["processing_error"],
        parser_provider=row["parser_provider"],
        parser_version=row["parser_version"],
        normalization_version=row["normalization_version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_block(row: sqlite3.Row) -> Block:
    return Block(
        id=row["id"],
        document_id=row["document_id"],
        page_idx=row["page_idx"],
        block_idx=row["block_idx"],
        block_type=row["block_type"],
        text=row["text"],
        html=row["html"],
        asset_id=row["asset_id"],
        bbox=_loads(row["bbox_json"]),
        page_size=_loads(row["page_size_json"]),
        source_ref=row["source_ref"],
        is_discarded=bool(row["is_discarded"]),
        metadata=_loads(row["metadata_json"], {}),
    )


def _row_to_asset(row: sqlite3.Row) -> Asset:
    return Asset(
        id=row["id"],
        document_id=row["document_id"],
        block_id=row["block_id"],
        asset_type=row["asset_type"],
        file_path=row["file_path"],
        content_type=row["content_type"],
        file_hash=row["file_hash"],
        page_idx=row["page_idx"],
        bbox=_loads(row["bbox_json"]),
        related_text=row["related_text"],
        metadata=_loads(row["metadata_json"], {}),
    )


def _row_to_chunk(row: sqlite3.Row) -> Chunk:
    return Chunk(
        id=row["id"],
        document_id=row["document_id"],
        chunk_idx=row["chunk_idx"],
        strategy=row["strategy"],
        block_ids=_loads(row["block_ids_json"], []),
        title_context=row["title_context"],
        inferred_heading_path=_loads(row["inferred_heading_path_json"], []),
        chunk_text=row["chunk_text"],
        indexing_text=row["indexing_text"],
        page_range=_loads(row["page_range_json"], []),
        bbox_by_page=_loads(row["bbox_by_page_json"], {}),
        modality_mix=_loads(row["modality_mix_json"], []),
        token_count=row["token_count"],
        strategy_version=row["strategy_version"],
    )


def _row_to_index_record(row: sqlite3.Row) -> IndexRecord:
    return IndexRecord(
        id=row["id"],
        evidence_id=row["evidence_id"],
        document_id=row["document_id"],
        chunk_id=row["chunk_id"],
        block_ids=_loads(row["block_ids_json"], []),
        modality=row["modality"],
        indexing_text=row["indexing_text"],
        vector=_loads(row["vector_json"]),
        embedding_provider=row["embedding_provider"],
        embedding_model=row["embedding_model"],
        embedding_dimension=row["embedding_dimension"],
        index_version=row["index_version"],
    )


def _row_to_source_anchor(row: sqlite3.Row) -> SourceAnchor:
    return SourceAnchor(
        id=row["id"],
        document_id=row["document_id"],
        record_id=row["record_id"],
        anchor_block_id=row["anchor_block_id"],
        active=bool(row["active"]),
        invalidated_at=row["invalidated_at"],
        created_at=row["created_at"],
    )


def _processing_job_from_row(row: sqlite3.Row) -> ProcessingJob:
    return ProcessingJob(
        id=row["id"],
        knowledge_base_id=row["knowledge_base_id"],
        document_id=row["document_id"],
        operation=row["operation"],
        status=row["status"],
        stage=row["stage"],
        attempt=row["attempt"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        retryable=(None if row["retryable"] is None else bool(row["retryable"])),
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        updated_at=row["updated_at"],
    )
