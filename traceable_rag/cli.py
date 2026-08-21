from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

from .chunking import BlockWindowChunker
from .document_processing import DocumentProcessingService
from .embedding import provider_from_api_doc
from .indexing import build_index_records
from .mineru_client import (
    MinerUClient,
    find_mineru_result_dir,
    mineru_api_key_from_doc,
    mineru_api_key_from_environment,
)
from .mineru_import import import_mineru_directory
from .models import Document
from .runtime_config import (
    DEFAULT_DATABASE_PATH,
    DEFAULT_MANAGED_STORAGE_ROOT,
    RuntimeConfigError,
    load_runtime_config,
)
from .sdk import DocumentScopeError
from .service_validation import KnowledgeBaseScopeError
from .store import DEFAULT_KNOWLEDGE_BASE_ID, DocumentNotFoundError, EvidenceStore


DEFAULT_DB = DEFAULT_DATABASE_PATH
DEFAULT_MANAGED_STORAGE = DEFAULT_MANAGED_STORAGE_ROOT
DEFAULT_EXAMPLE_CORPUS = Path("examples/vidore/documents")
SUPPORTED_ACCEPTANCE_SUFFIXES = frozenset({".docx", ".pdf"})
DEMO_DOCUMENT_FILENAMES = (
    "openstax-introduction-to-computer-science-p357.pdf",
    "openstax-introduction-to-python-programming-p334-335.pdf",
    "eu-joint-employment-report-2025-p97.pdf",
    "eu-future-oriented-occupations-p19.pdf",
)
ACCEPTANCE_QUESTIONS = (
    {
        "id": "compiler_middle_end_optimization",
        "question": (
            "What are the three stages of compilation, and what does the "
            "middle end optimize?"
        ),
        "expected_support": "evidence_required",
    },
    {
        "id": "hierarchical_vs_multiple_inheritance",
        "question": (
            "How do hierarchical inheritance and multiple inheritance differ, "
            "and how are they implemented in the chapter reference table?"
        ),
        "expected_support": "multi_page_evidence",
    },
    {
        "id": "eu_income_inequality_after_peak",
        "question": (
            "What happened to EU income inequality after its 2014-15 peak?"
        ),
        "expected_support": "visual_evidence",
    },
    {
        "id": "gender_segregation_future_occupations",
        "question": (
            "What does Figure 3 show about gender segregation in "
            "future-oriented occupations?"
        ),
        "expected_support": "visual_evidence",
    },
    {
        "id": "unsupported_question",
        "question": (
            "Do these documents provide a quantum error-correction procedure "
            "for a lunar communications network?"
        ),
        "expected_support": "unsupported",
    },
)


class AcceptanceCorpusError(ValueError):
    """Raised when an acceptance run would reuse or overwrite prior state."""


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="traceable-rag")
    sub = parser.add_subparsers(dest="command", required=True)

    import_parser = sub.add_parser("import-mineru", help="Import a local MinerU result directory into evidence store")
    import_parser.add_argument("--input", required=True)
    import_parser.add_argument("--db")
    import_parser.add_argument("--managed-root")
    import_parser.add_argument("--source-type", default="mineru_import")
    import_parser.add_argument("--knowledge-base-id", default=DEFAULT_KNOWLEDGE_BASE_ID)
    import_parser.add_argument("--document-id", required=True)
    import_parser.add_argument("--original-filename", required=True)
    import_parser.add_argument("--display-name", required=True)
    import_parser.add_argument("--source-file", required=True)
    import_parser.add_argument("--preview-file", required=True)

    parse_parser = sub.add_parser("parse-mineru", help="Parse one local file with real MinerU service")
    parse_parser.add_argument("--file", required=True)
    parse_parser.add_argument("--out", required=True)
    parse_parser.add_argument("--db")
    parse_parser.add_argument("--api-doc")
    parse_parser.add_argument("--model-version", default="vlm")
    parse_parser.add_argument("--poll-interval", type=int, default=10)
    parse_parser.add_argument("--timeout-seconds", type=int, default=900)
    parse_parser.add_argument("--no-import", action="store_true")
    parse_parser.add_argument("--knowledge-base-id", default=DEFAULT_KNOWLEDGE_BASE_ID)
    parse_parser.add_argument("--document-id")

    seed_parser = sub.add_parser(
        "seed-demo",
        help="Process the four fixed public ViDoRe samples into the V3 store",
    )
    seed_parser.add_argument("--db")
    seed_parser.add_argument("--storage-root")
    seed_parser.add_argument(
        "--source-dir",
        default=str(DEFAULT_EXAMPLE_CORPUS),
        help="Public sample directory (default: examples/vidore/documents)",
    )
    seed_parser.add_argument("--api-doc")
    seed_parser.add_argument("--knowledge-base-id", default=DEFAULT_KNOWLEDGE_BASE_ID)

    acceptance_parser = sub.add_parser(
        "acceptance-corpus",
        help="Process every PDF or DOCX in a directory into a fresh auditable store",
    )
    acceptance_parser.add_argument(
        "--source-dir",
        default=str(DEFAULT_EXAMPLE_CORPUS),
        help="Corpus directory (default: examples/vidore/documents)",
    )
    acceptance_parser.add_argument("--db", required=True)
    acceptance_parser.add_argument("--storage-root", required=True)
    acceptance_parser.add_argument("--report", required=True)
    acceptance_parser.add_argument(
        "--expected-document-count",
        type=_positive_int,
    )
    acceptance_parser.add_argument(
        "--processing-attempts",
        type=_positive_int,
        default=1,
    )
    acceptance_parser.add_argument(
        "--retry-delay-seconds",
        type=_non_negative_int,
        default=0,
    )
    acceptance_parser.add_argument("--api-doc")
    acceptance_parser.add_argument(
        "--knowledge-base-id", default=DEFAULT_KNOWLEDGE_BASE_ID
    )

    index_parser = sub.add_parser("index", help="Build chunks and index records")
    index_parser.add_argument("--db")
    index_parser.add_argument(
        "--knowledge-base-id", default=DEFAULT_KNOWLEDGE_BASE_ID
    )
    index_parser.add_argument("--document-id")
    index_parser.add_argument("--api-doc")
    index_parser.add_argument("--embedding", action="store_true")
    index_parser.add_argument("--target-tokens", type=int, default=450)
    index_parser.add_argument("--max-tokens", type=int, default=700)

    args = parser.parse_args(argv)
    try:
        _apply_runtime_defaults(args)
        if args.command == "import-mineru":
            return _import_mineru(args)
        if args.command == "parse-mineru":
            return _parse_mineru(args)
        if args.command == "seed-demo":
            return _seed_demo(args)
        if args.command == "acceptance-corpus":
            return _acceptance_corpus(args)
        if args.command == "index":
            return _index(args)
        return 1
    except KnowledgeBaseScopeError:
        print("error: Knowledge base not found.", file=sys.stderr)
        return 2
    except (DocumentScopeError, DocumentNotFoundError):
        print(
            "error: Document not found in the selected knowledge base.",
            file=sys.stderr,
        )
        return 2
    except RuntimeConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except AcceptanceCorpusError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _apply_runtime_defaults(args: argparse.Namespace) -> None:
    needs_database = getattr(args, "db", None) is None
    needs_managed_storage = (
        args.command == "seed-demo" and getattr(args, "storage_root", None) is None
    ) or (
        args.command == "import-mineru"
        and getattr(args, "managed_root", None) is None
    )
    if not needs_database and not needs_managed_storage:
        return
    runtime = load_runtime_config()
    if needs_database:
        args.db = str(runtime.database_path)
    if needs_managed_storage:
        if args.command == "import-mineru":
            args.managed_root = str(runtime.managed_storage_root)
        else:
            args.storage_root = str(runtime.managed_storage_root)


def _import_mineru(args: argparse.Namespace) -> int:
    store = EvidenceStore(args.db)
    result = import_mineru_directory(
        find_mineru_result_dir(args.input),
        store=store,
        knowledge_base_id=args.knowledge_base_id,
        document_id=args.document_id,
        original_filename=args.original_filename,
        display_name=args.display_name,
        source_file_path=args.source_file,
        preview_file_path=args.preview_file,
        source_type=args.source_type,
        managed_root=args.managed_root,
    )
    _print_json(
        {
            "document_id": result.document.id,
            "original_filename": result.document.original_filename,
            "page_count": result.document.page_count,
            "block_count": result.block_count,
            "asset_count": result.asset_count,
        }
    )
    return 0


def _parse_mineru(args: argparse.Namespace) -> int:
    store = EvidenceStore(args.db)
    api_key = _mineru_api_key(args.api_doc)
    client = MinerUClient(api_key)
    source_file = Path(args.file)
    if not args.no_import:
        document_id = args.document_id or str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{args.knowledge_base_id}:{source_file.resolve()}",
            )
        )
        service = DocumentProcessingService(
            store,
            args.out,
            mineru_client=client,
        )
        uploaded = service.ensure_uploaded(
            args.knowledge_base_id,
            source_file,
            display_name=source_file.stem,
            document_id=document_id,
            source_type="mineru_api",
        )
        document = service.process_document(
            args.knowledge_base_id,
            uploaded.id,
            model_version=args.model_version,
            poll_interval=args.poll_interval,
            timeout_seconds=args.timeout_seconds,
        )
        _print_json(
            {
                "document_id": document.id,
                "knowledge_base_id": document.knowledge_base_id,
                "original_filename": document.original_filename,
                "source_file_path": document.source_file_path,
                "preview_file_path": document.preview_file_path,
                "mineru_result_dir": document.mineru_result_dir,
                "page_count": document.page_count,
                "processing_status": document.processing_status,
            }
        )
        return 0

    parsed = client.parse_local_file(
        source_file,
        args.out,
        model_version=args.model_version,
        poll_interval=args.poll_interval,
        timeout_seconds=args.timeout_seconds,
    )
    payload = {
        "batch_id": parsed.batch_id,
        "file_name": parsed.file_name,
        "state": parsed.state,
        "output_dir": str(parsed.output_dir),
        "zip_path": str(parsed.zip_path) if parsed.zip_path else None,
    }
    _print_json(payload)
    return 0 if parsed.state == "done" else 2


def seed_demo_documents(
    service: DocumentProcessingService,
    source_dir: str | Path,
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
) -> list[Document]:
    source_dir = Path(source_dir)
    seeded: list[Document] = []
    for filename in DEMO_DOCUMENT_FILENAMES:
        source_file = source_dir / filename
        if not source_file.is_file():
            raise FileNotFoundError(source_file)
        document_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"traceable-rag:demo:{knowledge_base_id}:{filename}")
        )
        uploaded = service.ensure_uploaded(
            knowledge_base_id,
            source_file,
            display_name=source_file.stem,
            document_id=document_id,
            source_type="demo_seed",
        )
        seeded.append(service.process_document(knowledge_base_id, uploaded.id))
    return seeded


def _seed_demo(args: argparse.Namespace) -> int:
    store = EvidenceStore(args.db)
    api_key = _mineru_api_key(args.api_doc)
    service = DocumentProcessingService(
        store,
        args.storage_root,
        mineru_client=MinerUClient(api_key),
    )
    documents = seed_demo_documents(service, args.source_dir, args.knowledge_base_id)
    _print_json(
        {
            "database_path": str(args.db),
            "managed_storage_root": str(args.storage_root),
            "source_directory": str(args.source_dir),
            "seeded": [
                _seeded_document_audit(store, document)
                for document in documents
            ]
        }
    )
    return 0


def _acceptance_corpus(args: argparse.Namespace) -> int:
    source_dir = Path(args.source_dir)
    database_path = Path(args.db)
    storage_root = Path(args.storage_root)
    report_path = Path(args.report)
    source_files = sorted(
        (
            path
            for path in source_dir.iterdir()
            if path.is_file()
            and path.suffix.casefold() in SUPPORTED_ACCEPTANCE_SUFFIXES
        ),
        key=lambda path: (path.name.casefold(), path.name),
    )
    _validate_fresh_acceptance_paths(
        database_path,
        storage_root,
        report_path,
        source_files,
    )

    api_key = _mineru_api_key(args.api_doc)
    store = EvidenceStore(database_path)
    service = DocumentProcessingService(
        store,
        storage_root,
        mineru_client=MinerUClient(api_key),
        embedding_api_doc_path=args.api_doc,
        runtime_root=Path.cwd(),
    )
    documents: list[dict] = []
    for source_file in source_files:
        document_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                (
                    "traceable-rag:acceptance:"
                    f"{args.knowledge_base_id}:{source_file.name}"
                ),
            )
        )
        try:
            uploaded = service.ensure_uploaded(
                args.knowledge_base_id,
                source_file,
                display_name=source_file.stem,
                document_id=document_id,
                source_type="acceptance_corpus",
            )
        except Exception:
            stored = store.get_document(args.knowledge_base_id, document_id)
            audit = _failed_acceptance_document(source_file, stored)
            audit["processing_attempts"] = 0
            documents.append(audit)
            continue

        for attempt in range(1, args.processing_attempts + 1):
            try:
                processed = service.process_document(
                    args.knowledge_base_id,
                    uploaded.id,
                )
                audit = _acceptance_document_audit(store, processed)
                audit["processing_attempts"] = attempt
                documents.append(audit)
                break
            except Exception:
                if attempt < args.processing_attempts:
                    if args.retry_delay_seconds:
                        time.sleep(args.retry_delay_seconds)
                    continue
                stored = store.get_document(args.knowledge_base_id, document_id)
                audit = _failed_acceptance_document(source_file, stored)
                audit["processing_attempts"] = attempt
                documents.append(audit)

    passed_count = sum(1 for document in documents if document["passed"])
    failed_count = len(documents) - passed_count
    corpus_failures = []
    if (
        args.expected_document_count is not None
        and len(documents) != args.expected_document_count
    ):
        corpus_failures.append(
            f"Expected {args.expected_document_count} PDF/DOCX files but found "
            f"{len(documents)}."
        )
    payload = {
        "status": (
            "passed"
            if documents and failed_count == 0 and not corpus_failures
            else "failed"
        ),
        "database_path": str(database_path),
        "managed_storage_root": str(storage_root),
        "source_directory": str(source_dir),
        "report_path": str(report_path),
        "document_count": len(documents),
        "expected_document_count": args.expected_document_count,
        "passed_document_count": passed_count,
        "failed_document_count": failed_count,
        "corpus_failures": corpus_failures,
        "documents": documents,
        "acceptance_questions": [dict(item) for item in ACCEPTANCE_QUESTIONS],
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _print_json(payload)
    return 0 if payload["status"] == "passed" else 2


def _validate_fresh_acceptance_paths(
    database_path: Path,
    storage_root: Path,
    report_path: Path,
    source_files: list[Path],
) -> None:
    report_target = report_path.resolve()
    if report_target == database_path.resolve():
        raise AcceptanceCorpusError(
            "acceptance report must not target the database."
        )
    if any(report_target == source_file.resolve() for source_file in source_files):
        raise AcceptanceCorpusError(
            "acceptance report must not target a source document."
        )
    if database_path.exists():
        raise AcceptanceCorpusError(
            "acceptance database must not already exist."
        )
    if storage_root.exists() and any(storage_root.iterdir()):
        raise AcceptanceCorpusError(
            "acceptance storage root must be empty."
        )


def _failed_acceptance_document(
    source_file: Path,
    document: Document | None,
) -> dict:
    return {
        "document_id": document.id if document is not None else None,
        "knowledge_base_id": (
            document.knowledge_base_id if document is not None else None
        ),
        "original_filename": source_file.name,
        "page_count": document.page_count if document is not None else 0,
        "processing_status": (
            document.processing_status if document is not None else "failed"
        ),
        "preview_pdf_path": (
            document.preview_file_path if document is not None else None
        ),
        "preview_pdf_exists": False,
        "preview_pdf_valid": False,
        "block_count": 0,
        "chunk_count": 0,
        "index_record_count": 0,
        "evidence_record_count": 0,
        "evidence_records_available": False,
        "sample_index_evidence_ids": [],
        "embedding_record_count": 0,
        "embedding_ready": False,
        "embedding_status": "not_indexed",
        "embedding_warning": None,
        "keyword_fallback": False,
        "passed": False,
        "failure": "Document processing failed.",
    }


def _acceptance_document_audit(
    store: EvidenceStore,
    document: Document,
) -> dict:
    audit = _seeded_document_audit(store, document)
    stats = store.list_document_graph_stats(
        document.knowledge_base_id,
        document.id,
    ).get(document.id, {})
    index_record_count = audit["index_record_count"]
    embedding_record_count = stats.get("embedding_records", 0)
    embedding_configuration_count = stats.get(
        "embedding_configurations", 0
    )
    embedding_ready = (
        index_record_count > 0
        and embedding_record_count == index_record_count
        and embedding_configuration_count == 1
    )
    embedding_status = (
        "ready"
        if embedding_ready
        else "fallback_keyword"
        if index_record_count > 0
        else "not_indexed"
    )
    audit.update(
        {
            "embedding_record_count": embedding_record_count,
            "embedding_configuration_count": embedding_configuration_count,
            "embedding_ready": embedding_ready,
            "embedding_status": embedding_status,
            "embedding_warning": (
                "Embedding 索引未完整就绪，当前文档仍可使用关键词检索。"
                if embedding_status == "fallback_keyword"
                else None
            ),
            "keyword_fallback": embedding_status == "fallback_keyword",
            "failure": None,
        }
    )
    audit["passed"] = bool(
        audit["processing_status"] == "ready"
        and audit["preview_pdf_valid"]
        and audit["block_count"] > 0
        and audit["chunk_count"] > 0
        and audit["index_record_count"] > 0
        and audit["evidence_records_available"]
        and embedding_status in {"ready", "fallback_keyword"}
    )
    return audit


def _seeded_document_audit(store: EvidenceStore, document: Document) -> dict:
    stats = store.list_document_graph_stats(
        document.knowledge_base_id,
        document.id,
    ).get(
        document.id,
        {
            "blocks": 0,
            "chunks": 0,
            "index_records": 0,
        },
    )
    records = store.list_index_records(document.knowledge_base_id, document.id)
    usable_records = [
        record
        for record in records
        if record.evidence_id and record.block_ids
    ]
    preview_path = (
        Path(document.preview_file_path)
        if document.preview_file_path
        else None
    )
    preview_exists = bool(preview_path and preview_path.is_file())
    preview_valid = False
    if (
        preview_exists
        and preview_path is not None
        and preview_path.suffix.lower() == ".pdf"
    ):
        with preview_path.open("rb") as preview_file:
            preview_valid = preview_file.read(5) == b"%PDF-"
    return {
        "document_id": document.id,
        "knowledge_base_id": document.knowledge_base_id,
        "original_filename": document.original_filename,
        "page_count": document.page_count,
        "processing_status": document.processing_status,
        "preview_pdf_path": str(preview_path) if preview_path is not None else None,
        "preview_pdf_exists": preview_exists,
        "preview_pdf_valid": preview_valid,
        "block_count": stats["blocks"],
        "chunk_count": stats["chunks"],
        "index_record_count": stats["index_records"],
        "evidence_record_count": len(records),
        "evidence_records_available": (
            bool(records) and len(usable_records) == len(records)
        ),
        "sample_index_evidence_ids": [
            record.evidence_id for record in usable_records[:3]
        ],
    }


def _mineru_api_key(api_doc: str | None) -> str:
    if api_doc:
        return mineru_api_key_from_doc(api_doc)
    api_key = mineru_api_key_from_environment()
    if not api_key:
        raise ValueError("MINERU_TOKEN or MINERU_API_KEY is required")
    return api_key


def _index(args: argparse.Namespace) -> int:
    store = EvidenceStore(args.db)
    if store.get_knowledge_base(args.knowledge_base_id) is None:
        raise KnowledgeBaseScopeError(args.knowledge_base_id)
    if (
        args.document_id
        and store.get_document(args.knowledge_base_id, args.document_id) is None
    ):
        raise DocumentScopeError(args.document_id)
    document_ids = (
        [args.document_id]
        if args.document_id
        else [doc.id for doc in store.list_documents(args.knowledge_base_id)]
    )
    provider = provider_from_api_doc(args.api_doc) if args.embedding else None
    totals = []
    for document_id in document_ids:
        blocks = store.list_blocks(args.knowledge_base_id, document_id)
        chunks = BlockWindowChunker(target_tokens=args.target_tokens, max_tokens=args.max_tokens).chunk(document_id, blocks)
        records = build_index_records(chunks, provider)
        store.replace_chunks(args.knowledge_base_id, document_id, chunks)
        store.replace_index_records(args.knowledge_base_id, document_id, records)
        totals.append(
            {
                "document_id": document_id,
                "chunk_count": len(chunks),
                "index_record_count": len(records),
                "embedding_provider": provider.provider_name if provider else None,
                "embedding_model": provider.model_name if provider else None,
                "embedding_dimension": records[0].embedding_dimension if records else None,
            }
        )
    _print_json({"indexed": totals})
    return 0


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
