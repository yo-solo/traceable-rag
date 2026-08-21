import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from traceable_rag.cli import main
from traceable_rag.models import KnowledgeBase
from traceable_rag.store import EvidenceStore
from tests.test_sdk import make_store


def test_cli_removes_the_builtin_agent_investigate_command(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    assert exit_info.value.code == 0
    assert "agent-investigate" not in capsys.readouterr().out


def test_cli_help_does_not_load_invalid_dotenv(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_bytes(b"SECRET=\xff\n")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    assert exit_info.value.code == 0
    assert "usage: traceable-rag" in capsys.readouterr().out


def test_cli_reports_non_utf8_dotenv_without_exposing_its_contents(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / ".env").write_bytes(b"MINERU_TOKEN=do-not-leak-\xff\n")
    monkeypatch.chdir(tmp_path)

    exit_code = main(["seed-demo"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert "must be UTF-8" in captured.err
    assert ".env.example" in captured.err
    assert "do-not-leak" not in captured.err


def test_seed_demo_uses_runtime_path_defaults_from_dotenv(
    tmp_path, monkeypatch, capsys
):
    import traceable_rag.cli as cli

    (tmp_path / ".env").write_text(
        "\n".join(
            (
                "MINERU_TOKEN=test-token",
                "TRACEABLE_RAG_DATABASE_PATH=/runtime/data/traceable.db",
                "TRACEABLE_RAG_MANAGED_STORAGE_ROOT=/runtime/data/managed",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli,
        "EvidenceStore",
        lambda path: captured.setdefault("database_path", path) or object(),
    )
    monkeypatch.setattr(cli, "MinerUClient", lambda _token: object())

    def processing_service(store, managed_root, **_kwargs):
        captured["managed_root"] = managed_root
        return object()

    monkeypatch.setattr(cli, "DocumentProcessingService", processing_service)

    def seed_demo_documents(_service, source_dir, _knowledge_base_id):
        captured["source_dir"] = source_dir
        return []

    monkeypatch.setattr(cli, "seed_demo_documents", seed_demo_documents)

    assert cli.main(["seed-demo"]) == 0
    assert captured["database_path"] == "/runtime/data/traceable.db"
    assert captured["managed_root"] == "/runtime/data/managed"
    assert captured["source_dir"] == "examples/vidore/documents"
    assert "test-token" not in capsys.readouterr().out


def test_acceptance_corpus_defaults_to_public_vidore_directory(
    tmp_path, monkeypatch
):
    import traceable_rag.cli as cli

    captured: dict[str, object] = {}

    def run_acceptance(args):
        captured["source_dir"] = args.source_dir
        return 0

    monkeypatch.setattr(cli, "_acceptance_corpus", run_acceptance)

    assert cli.main(
        [
            "acceptance-corpus",
            "--db",
            str(tmp_path / "traceable.db"),
            "--storage-root",
            str(tmp_path / "managed"),
            "--report",
            str(tmp_path / "acceptance.json"),
        ]
    ) == 0
    assert captured["source_dir"] == "examples/vidore/documents"


def test_import_mineru_uses_runtime_managed_storage_default(
    tmp_path, monkeypatch, capsys
):
    import traceable_rag.cli as cli

    (tmp_path / ".env").write_text(
        "\n".join(
            (
                "TRACEABLE_RAG_DATABASE_PATH=/runtime/data/traceable.db",
                "TRACEABLE_RAG_MANAGED_STORAGE_ROOT=/runtime/data/managed",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    captured: dict[str, object] = {}
    fake_store = object()

    def make_store(path):
        captured["database_path"] = path
        return fake_store

    monkeypatch.setattr(cli, "EvidenceStore", make_store)
    monkeypatch.setattr(
        cli,
        "find_mineru_result_dir",
        lambda path: Path(path) / "resolved-result",
    )

    def fake_import(input_dir, store, **kwargs):
        captured["input_dir"] = input_dir
        captured["store"] = store
        captured.update(kwargs)
        return SimpleNamespace(
            document=SimpleNamespace(
                id="doc-import",
                original_filename="source.pdf",
                page_count=1,
            ),
            block_count=2,
            asset_count=1,
        )

    monkeypatch.setattr(cli, "import_mineru_directory", fake_import)

    assert cli.main(
        [
            "import-mineru",
            "--input",
            str(tmp_path / "mineru"),
            "--document-id",
            "doc-import",
            "--original-filename",
            "source.pdf",
            "--display-name",
            "导入文档",
            "--source-file",
            str(tmp_path / "source.pdf"),
            "--preview-file",
            str(tmp_path / "preview.pdf"),
        ]
    ) == 0

    assert captured["database_path"] == "/runtime/data/traceable.db"
    assert captured["managed_root"] == "/runtime/data/managed"
    assert captured["store"] is fake_store
    assert captured["input_dir"] == tmp_path / "mineru" / "resolved-result"
    assert json.loads(capsys.readouterr().out)["asset_count"] == 1


def test_parse_mineru_cli_default_output_omits_signed_download_url(
    tmp_path, monkeypatch, capsys
):
    import traceable_rag.cli as cli
    from traceable_rag.mineru_client import MinerUParseResult

    signed_url = "https://signed.example/private-secret"
    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf")
    monkeypatch.setenv("MINERU_TOKEN", "test-token")

    class FakeClient:
        def parse_local_file(self, file_path, output_dir, **_kwargs):
            return MinerUParseResult(
                batch_id="safe-batch",
                file_name=Path(file_path).name,
                state="done",
                output_dir=Path(output_dir),
                zip_path=None,
                full_zip_url=signed_url,
                raw_result={"signed_url": signed_url},
            )

    monkeypatch.setattr(cli, "MinerUClient", lambda _token: FakeClient())

    exit_code = cli.main(
        [
            "parse-mineru",
            "--file",
            str(source),
            "--out",
            str(tmp_path / "out"),
            "--db",
            str(tmp_path / "db.sqlite"),
            "--no-import",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert signed_url not in output
    assert "full_zip_url" not in output


def test_parse_mineru_cli_sanitizes_signed_result_download_failure(
    tmp_path, monkeypatch, capsys
):
    import traceable_rag.cli as cli
    import traceable_rag.mineru_client as mineru_module

    signed_url = "https://signed.example/result-secret"
    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf")
    monkeypatch.setenv("MINERU_TOKEN", "test-token")
    client = mineru_module.MinerUClient("test-token")
    monkeypatch.setattr(
        client,
        "create_upload_url",
        lambda **_kwargs: ("safe-batch", "https://signed.example/upload-secret"),
    )
    monkeypatch.setattr(client, "upload_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        client,
        "poll_batch_result",
        lambda *_args, **_kwargs: {
            "data": {
                "extract_result": [
                    {
                        "file_name": source.name,
                        "state": "done",
                        "full_zip_url": signed_url,
                    }
                ]
            }
        },
    )
    monkeypatch.setattr(
        mineru_module,
        "download_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(f"download failed for {signed_url}")
        ),
    )
    monkeypatch.setattr(cli, "MinerUClient", lambda _token: client)

    with pytest.raises(RuntimeError) as error:
        cli.main(
            [
                "parse-mineru",
                "--file",
                str(source),
                "--out",
                str(tmp_path / "out"),
                "--db",
                str(tmp_path / "db.sqlite"),
                "--no-import",
            ]
        )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert str(error.value) == "MinerU result download failed."
    assert signed_url not in str(error.value)
    assert signed_url not in repr(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_seed_demo_reports_auditable_preview_graph_and_evidence_stats(
    tmp_path, monkeypatch, capsys
):
    import traceable_rag.cli as cli

    preview = tmp_path / "managed" / "preview.pdf"
    preview.parent.mkdir()
    preview.write_bytes(b"%PDF-1.7\n")
    document = SimpleNamespace(
        id="doc-acceptance",
        knowledge_base_id="default",
        original_filename="验收文档.docx",
        page_count=3,
        preview_file_path=str(preview),
        processing_status="ready",
    )

    class FakeStore:
        def list_document_graph_stats(self, knowledge_base_id, document_id):
            assert (knowledge_base_id, document_id) == ("default", "doc-acceptance")
            return {
                document_id: {
                    "blocks": 7,
                    "assets": 1,
                    "chunks": 3,
                    "index_records": 3,
                }
            }

        def list_index_records(self, knowledge_base_id, document_id):
            assert (knowledge_base_id, document_id) == ("default", "doc-acceptance")
            return [
                SimpleNamespace(evidence_id="index-evidence-1", block_ids=["block-1"]),
                SimpleNamespace(evidence_id="index-evidence-2", block_ids=["block-2"]),
                SimpleNamespace(evidence_id="index-evidence-3", block_ids=["block-3"]),
            ]

    store = FakeStore()
    monkeypatch.setenv("MINERU_TOKEN", "test-token")
    monkeypatch.setattr(cli, "EvidenceStore", lambda _path: store)
    monkeypatch.setattr(cli, "MinerUClient", lambda _key: object())
    monkeypatch.setattr(
        cli, "DocumentProcessingService", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(
        cli, "seed_demo_documents", lambda *_args, **_kwargs: [document]
    )

    exit_code = cli.main(
        [
            "seed-demo",
            "--db",
            str(tmp_path / "traceable_v2.db"),
            "--storage-root",
            str(tmp_path / "managed"),
            "--source-dir",
            str(tmp_path / "source"),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["database_path"] == str(tmp_path / "traceable_v2.db")
    assert payload["managed_storage_root"] == str(tmp_path / "managed")
    assert payload["source_directory"] == str(tmp_path / "source")
    assert payload["seeded"] == [
        {
            "document_id": "doc-acceptance",
            "knowledge_base_id": "default",
            "original_filename": "验收文档.docx",
            "page_count": 3,
            "processing_status": "ready",
            "preview_pdf_path": str(preview),
            "preview_pdf_exists": True,
            "preview_pdf_valid": True,
            "block_count": 7,
            "chunk_count": 3,
            "index_record_count": 3,
            "evidence_record_count": 3,
            "evidence_records_available": True,
            "sample_index_evidence_ids": [
                "index-evidence-1",
                "index-evidence-2",
                "index-evidence-3",
            ],
        }
    ]


def test_acceptance_corpus_processes_every_pdf_and_docx_in_order_and_writes_report(
    tmp_path, monkeypatch, capsys
):
    import traceable_rag.cli as cli

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    for filename in ("02-layout.pdf", "01-reference.DOCX", "ignore.txt"):
        (source_dir / filename).write_bytes(b"source")
    database_path = tmp_path / "run" / "traceable_v2.db"
    storage_root = tmp_path / "run" / "managed"
    report_path = tmp_path / "run" / "acceptance.json"
    processed: list[str] = []

    class FakeStore:
        def list_document_graph_stats(self, knowledge_base_id, document_id):
            assert knowledge_base_id == "default"
            return {
                document_id: {
                    "blocks": 8,
                    "assets": 0,
                    "chunks": 4,
                    "index_records": 4,
                    "embedding_records": (
                        4 if document_id.endswith("01-reference.DOCX") else 0
                    ),
                    "embedding_configurations": (
                        1 if document_id.endswith("01-reference.DOCX") else 0
                    ),
                }
            }

        def list_index_records(self, knowledge_base_id, document_id):
            assert knowledge_base_id == "default"
            embedded = document_id.endswith("01-reference.DOCX")
            return [
                SimpleNamespace(
                    evidence_id=f"evidence-{index}",
                    block_ids=[f"block-{index}"],
                    vector=[1.0, 0.0] if embedded else None,
                    embedding_provider="provider" if embedded else None,
                    embedding_model="model" if embedded else None,
                    embedding_dimension=2 if embedded else None,
                )
                for index in range(4)
            ]

    class FakeService:
        def ensure_uploaded(
            self,
            knowledge_base_id,
            source_file,
            *,
            display_name,
            document_id,
            source_type,
        ):
            assert knowledge_base_id == "default"
            assert source_type == "acceptance_corpus"
            preview = storage_root / f"{source_file.name}.pdf"
            preview.parent.mkdir(parents=True, exist_ok=True)
            preview.write_bytes(b"%PDF-1.7\n")
            return SimpleNamespace(
                id=f"{document_id}:{source_file.name}",
                knowledge_base_id=knowledge_base_id,
                original_filename=source_file.name,
                page_count=2,
                preview_file_path=str(preview),
                processing_status="uploaded",
            )

        def process_document(self, knowledge_base_id, document_id):
            processed.append(document_id.rsplit(":", 1)[-1])
            filename = document_id.rsplit(":", 1)[-1]
            preview = storage_root / f"{filename}.pdf"
            return SimpleNamespace(
                id=document_id,
                knowledge_base_id=knowledge_base_id,
                original_filename=filename,
                page_count=2,
                preview_file_path=str(preview),
                processing_status="ready",
            )

    store = FakeStore()
    monkeypatch.setattr(cli, "_mineru_api_key", lambda _path: "test-token")
    monkeypatch.setattr(cli, "EvidenceStore", lambda _path: store)
    monkeypatch.setattr(cli, "MinerUClient", lambda _token: object())
    monkeypatch.setattr(
        cli,
        "DocumentProcessingService",
        lambda *_args, **_kwargs: FakeService(),
    )

    exit_code = cli.main(
        [
            "acceptance-corpus",
            "--source-dir",
            str(source_dir),
            "--db",
            str(database_path),
            "--storage-root",
            str(storage_root),
            "--report",
            str(report_path),
            "--expected-document-count",
            "2",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert processed == ["01-reference.DOCX", "02-layout.pdf"]
    assert json.loads(report_path.read_text(encoding="utf-8")) == payload
    assert payload["status"] == "passed"
    assert payload["document_count"] == 2
    assert payload["expected_document_count"] == 2
    assert payload["corpus_failures"] == []
    assert payload["passed_document_count"] == 2
    assert payload["failed_document_count"] == 0
    assert [item["original_filename"] for item in payload["documents"]] == [
        "01-reference.DOCX",
        "02-layout.pdf",
    ]
    assert payload["documents"][0]["embedding_status"] == "ready"
    assert payload["documents"][0]["embedding_ready"] is True
    assert payload["documents"][1]["embedding_status"] == "fallback_keyword"
    assert payload["documents"][1]["keyword_fallback"] is True
    assert payload["documents"][1]["passed"] is True
    assert [item["id"] for item in payload["acceptance_questions"]] == [
        "compiler_middle_end_optimization",
        "hierarchical_vs_multiple_inheritance",
        "eu_income_inequality_after_peak",
        "gender_segregation_future_occupations",
        "unsupported_question",
    ]


def test_acceptance_corpus_fails_after_processing_when_expected_count_is_missing(
    tmp_path, monkeypatch, capsys
):
    import traceable_rag.cli as cli

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "only-one.pdf").write_bytes(b"source")
    processed: list[str] = []

    class FakeService:
        def ensure_uploaded(self, knowledge_base_id, source_file, **_kwargs):
            return SimpleNamespace(
                id=source_file.name,
                knowledge_base_id=knowledge_base_id,
                original_filename=source_file.name,
            )

        def process_document(self, knowledge_base_id, document_id):
            processed.append(document_id)
            return SimpleNamespace(
                id=document_id,
                knowledge_base_id=knowledge_base_id,
                original_filename=document_id,
            )

    monkeypatch.setattr(cli, "_mineru_api_key", lambda _path: "test-token")
    monkeypatch.setattr(cli, "EvidenceStore", lambda _path: object())
    monkeypatch.setattr(cli, "MinerUClient", lambda _token: object())
    monkeypatch.setattr(
        cli,
        "DocumentProcessingService",
        lambda *_args, **_kwargs: FakeService(),
    )
    monkeypatch.setattr(
        cli,
        "_acceptance_document_audit",
        lambda _store, document: {
            "document_id": document.id,
            "original_filename": document.original_filename,
            "passed": True,
        },
    )

    exit_code = cli.main(
        [
            "acceptance-corpus",
            "--source-dir",
            str(source_dir),
            "--db",
            str(tmp_path / "traceable.db"),
            "--storage-root",
            str(tmp_path / "managed"),
            "--report",
            str(tmp_path / "report.json"),
            "--expected-document-count",
            "2",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert processed == ["only-one.pdf"]
    assert payload["document_count"] == 1
    assert payload["expected_document_count"] == 2
    assert payload["passed_document_count"] == 1
    assert payload["failed_document_count"] == 0
    assert payload["status"] == "failed"
    assert payload["corpus_failures"] == [
        "Expected 2 PDF/DOCX files but found 1."
    ]


def test_acceptance_corpus_continues_after_failure_and_returns_nonzero(
    tmp_path, monkeypatch, capsys
):
    import traceable_rag.cli as cli

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    for filename in ("01失败.docx", "02继续.docx"):
        (source_dir / filename).write_bytes(b"source")
    report_path = tmp_path / "acceptance.json"
    attempted: list[str] = []

    class FakeStore:
        def list_document_graph_stats(self, _knowledge_base_id, document_id):
            return {
                document_id: {
                    "blocks": 1,
                    "assets": 0,
                    "chunks": 1,
                    "index_records": 1,
                    "embedding_records": 0,
                    "embedding_configurations": 0,
                }
            }

        def list_index_records(self, _knowledge_base_id, _document_id):
            return [
                SimpleNamespace(
                    evidence_id="evidence",
                    block_ids=["block"],
                    vector=None,
                    embedding_provider=None,
                    embedding_model=None,
                    embedding_dimension=None,
                )
            ]

        def get_document(self, _knowledge_base_id, _document_id):
            return None

    class FakeService:
        def ensure_uploaded(self, knowledge_base_id, source_file, **_kwargs):
            return SimpleNamespace(
                id=source_file.name,
                knowledge_base_id=knowledge_base_id,
                original_filename=source_file.name,
            )

        def process_document(self, knowledge_base_id, document_id):
            attempted.append(document_id)
            if document_id.startswith("01"):
                raise RuntimeError("private upstream detail")
            preview = tmp_path / f"{document_id}.pdf"
            preview.write_bytes(b"%PDF-1.7\n")
            return SimpleNamespace(
                id=document_id,
                knowledge_base_id=knowledge_base_id,
                original_filename=document_id,
                page_count=1,
                preview_file_path=str(preview),
                processing_status="ready",
            )

    monkeypatch.setattr(cli, "_mineru_api_key", lambda _path: "test-token")
    monkeypatch.setattr(cli, "EvidenceStore", lambda _path: FakeStore())
    monkeypatch.setattr(cli, "MinerUClient", lambda _token: object())
    monkeypatch.setattr(
        cli,
        "DocumentProcessingService",
        lambda *_args, **_kwargs: FakeService(),
    )

    exit_code = cli.main(
        [
            "acceptance-corpus",
            "--source-dir",
            str(source_dir),
            "--db",
            str(tmp_path / "traceable.db"),
            "--storage-root",
            str(tmp_path / "managed"),
            "--report",
            str(report_path),
        ]
    )

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert exit_code == 2
    assert attempted == ["01失败.docx", "02继续.docx"]
    assert payload["status"] == "failed"
    assert payload["failed_document_count"] == 1
    assert payload["documents"][0]["failure"] == "Document processing failed."
    assert payload["documents"][1]["passed"] is True
    assert "private upstream detail" not in output
    assert json.loads(report_path.read_text(encoding="utf-8")) == payload


def test_acceptance_corpus_retries_failed_processing_and_reports_attempts(
    tmp_path, monkeypatch, capsys
):
    import traceable_rag.cli as cli

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "瞬时失败.docx").write_bytes(b"source")
    attempts: list[str] = []

    class FakeService:
        def ensure_uploaded(self, knowledge_base_id, source_file, **_kwargs):
            return SimpleNamespace(
                id="document-id",
                knowledge_base_id=knowledge_base_id,
                original_filename=source_file.name,
            )

        def process_document(self, knowledge_base_id, document_id):
            attempts.append(document_id)
            if len(attempts) == 1:
                raise RuntimeError("temporary upstream failure")
            return SimpleNamespace(
                id=document_id,
                knowledge_base_id=knowledge_base_id,
                original_filename="瞬时失败.docx",
            )

    monkeypatch.setattr(cli, "_mineru_api_key", lambda _path: "test-token")
    monkeypatch.setattr(cli, "EvidenceStore", lambda _path: object())
    monkeypatch.setattr(cli, "MinerUClient", lambda _token: object())
    monkeypatch.setattr(
        cli,
        "DocumentProcessingService",
        lambda *_args, **_kwargs: FakeService(),
    )
    monkeypatch.setattr(
        cli,
        "_acceptance_document_audit",
        lambda _store, document: {
            "document_id": document.id,
            "original_filename": document.original_filename,
            "passed": True,
        },
    )

    exit_code = cli.main(
        [
            "acceptance-corpus",
            "--source-dir",
            str(source_dir),
            "--db",
            str(tmp_path / "traceable.db"),
            "--storage-root",
            str(tmp_path / "managed"),
            "--report",
            str(tmp_path / "report.json"),
            "--processing-attempts",
            "3",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert attempts == ["document-id", "document-id"]
    assert payload["documents"][0]["passed"] is True
    assert payload["documents"][0]["processing_attempts"] == 2


def test_acceptance_corpus_reports_exhausted_processing_retries(
    tmp_path, monkeypatch, capsys
):
    import traceable_rag.cli as cli

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_file = source_dir / "持续失败.docx"
    source_file.write_bytes(b"source")
    attempts: list[str] = []
    delays: list[int] = []
    stored_document = SimpleNamespace(
        id="document-id",
        knowledge_base_id="default",
        page_count=1,
        processing_status="failed",
        preview_file_path=None,
    )

    class FakeStore:
        def get_document(self, _knowledge_base_id, _document_id):
            return stored_document

    class FakeService:
        def ensure_uploaded(self, knowledge_base_id, source_file, **_kwargs):
            return SimpleNamespace(
                id=stored_document.id,
                knowledge_base_id=knowledge_base_id,
                original_filename=source_file.name,
            )

        def process_document(self, _knowledge_base_id, document_id):
            attempts.append(document_id)
            raise RuntimeError("temporary upstream failure")

    monkeypatch.setattr(cli, "_mineru_api_key", lambda _path: "test-token")
    monkeypatch.setattr(cli, "EvidenceStore", lambda _path: FakeStore())
    monkeypatch.setattr(cli, "MinerUClient", lambda _token: object())
    monkeypatch.setattr(
        cli,
        "DocumentProcessingService",
        lambda *_args, **_kwargs: FakeService(),
    )
    monkeypatch.setattr(cli.time, "sleep", delays.append)

    exit_code = cli.main(
        [
            "acceptance-corpus",
            "--source-dir",
            str(source_dir),
            "--db",
            str(tmp_path / "traceable.db"),
            "--storage-root",
            str(tmp_path / "managed"),
            "--report",
            str(tmp_path / "report.json"),
            "--processing-attempts",
            "3",
            "--retry-delay-seconds",
            "2",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert attempts == ["document-id", "document-id", "document-id"]
    assert delays == [2, 2]
    assert payload["documents"][0]["passed"] is False
    assert payload["documents"][0]["processing_attempts"] == 3
    assert payload["documents"][0]["failure"] == "Document processing failed."


def test_acceptance_corpus_rejects_reused_database_before_processing(
    tmp_path, monkeypatch, capsys
):
    database_path = tmp_path / "traceable.db"
    database_path.write_bytes(b"existing")
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "文档.docx").write_bytes(b"source")
    monkeypatch.setenv("MINERU_TOKEN", "test-token")

    exit_code = main(
        [
            "acceptance-corpus",
            "--source-dir",
            str(source_dir),
            "--db",
            str(database_path),
            "--storage-root",
            str(tmp_path / "managed"),
            "--report",
            str(tmp_path / "report.json"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err == "error: acceptance database must not already exist.\n"


def test_acceptance_corpus_rejects_report_aliasing_database_without_creating_it(
    tmp_path, monkeypatch, capsys
):
    import traceable_rag.cli as cli

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "文档.docx").write_bytes(b"source")
    database_path = tmp_path / "traceable.db"
    report_alias = tmp_path / "report.json"
    report_alias.symlink_to(database_path)
    storage_root = tmp_path / "managed"
    monkeypatch.setattr(
        cli,
        "_mineru_api_key",
        lambda _path: pytest.fail("provider config must not be loaded"),
    )

    exit_code = cli.main(
        [
            "acceptance-corpus",
            "--source-dir",
            str(source_dir),
            "--db",
            str(database_path),
            "--storage-root",
            str(storage_root),
            "--report",
            str(report_alias),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err == "error: acceptance report must not target the database.\n"
    assert not database_path.exists()
    assert report_alias.is_symlink()
    assert not storage_root.exists()


def test_acceptance_corpus_rejects_report_aliasing_source_docx_without_modifying_it(
    tmp_path, monkeypatch, capsys
):
    import traceable_rag.cli as cli

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_file = source_dir / "业务文档.docx"
    original_content = b"business-source"
    source_file.write_bytes(original_content)
    report_alias = tmp_path / "acceptance.json"
    report_alias.symlink_to(source_file)
    database_path = tmp_path / "traceable.db"
    storage_root = tmp_path / "managed"
    monkeypatch.setattr(
        cli,
        "_mineru_api_key",
        lambda _path: pytest.fail("provider config must not be loaded"),
    )

    exit_code = cli.main(
        [
            "acceptance-corpus",
            "--source-dir",
            str(source_dir),
            "--db",
            str(database_path),
            "--storage-root",
            str(storage_root),
            "--report",
            str(report_alias),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err == "error: acceptance report must not target a source document.\n"
    assert source_file.read_bytes() == original_content
    assert report_alias.is_symlink()
    assert not database_path.exists()
    assert not storage_root.exists()


def test_cli_index_rejects_missing_knowledge_base_instead_of_false_success(
    tmp_path, capsys
):
    exit_code = main(
        [
            "index",
            "--db",
            str(tmp_path / "traceable.db"),
            "--knowledge-base-id",
            "missing-kb",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err == "error: Knowledge base not found.\n"


@pytest.mark.parametrize("document_id", ["missing-doc", "doc-b"])
def test_cli_index_rejects_missing_or_cross_scope_document_without_traceback(
    document_id, tmp_path, capsys
):
    make_store(tmp_path)

    exit_code = main(
        [
            "index",
            "--db",
            str(tmp_path / "traceable.db"),
            "--knowledge-base-id",
            "kb-a",
            "--document-id",
            document_id,
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err == "error: Document not found in the selected knowledge base.\n"
    assert "Traceback" not in captured.err
