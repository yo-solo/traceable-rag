from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from traceable_rag.store import DEFAULT_KNOWLEDGE_BASE_ID, EvidenceStore
from traceable_rag.web_app import create_app


ROOT = Path(__file__).parents[1]
SOURCE_DIR = ROOT / "examples" / "vidore" / "documents"
MANIFEST = ROOT / "examples" / "vidore" / "manifest.json"
SEED_SCRIPT = ROOT / "scripts" / "seed_release_acceptance.py"
SMOKE_SCRIPT = ROOT / "scripts" / "release_api_smoke.py"


def test_release_acceptance_seed_is_deterministic_managed_and_smoke_ready(tmp_path):
    first = _run_seed(tmp_path / "first")
    second = _run_seed(tmp_path / "second")

    first_report = json.loads(first["report"].read_text(encoding="utf-8"))
    second_report = json.loads(second["report"].read_text(encoding="utf-8"))
    assert first_report["ok"] is True
    assert first_report["mode"] == "deterministic_public_fixture"
    assert first_report["document_count"] == 4
    assert first_report["external_provider_e2e"] == {
        "mineru": {
            "fixture": "pypdf-public-fixture-v1",
            "status": "untested",
        },
        "embedding": {"fixture": "keyword_only", "status": "untested"},
    }
    assert first_report["keyword_index"] == {
        "embedding_record_count": 0,
        "status": "passed",
    }
    assert _stable_documents(first_report) == _stable_documents(second_report)

    documents_by_name = {
        item["display_name"]: item for item in first_report["documents"]
    }
    assert set(documents_by_name) == {
        "openstax-introduction-to-computer-science-p357",
        "openstax-introduction-to-python-programming-p334-335",
        "eu-joint-employment-report-2025-p97",
        "eu-future-oriented-occupations-p19",
    }
    assert all(item["processing_status"] == "ready" for item in documents_by_name.values())
    assert all(item["keyword_index_record_count"] > 0 for item in documents_by_name.values())
    assert documents_by_name[
        "openstax-introduction-to-computer-science-p357"
    ]["extracted_image_count"] >= 1

    store = EvidenceStore(first["db"])
    documents = store.list_documents(DEFAULT_KNOWLEDGE_BASE_ID)
    assert len(documents) == 4
    assert {document.display_name for document in documents} == set(documents_by_name)
    assert all(document.processing_status == "ready" for document in documents)
    assert all(document.source_type == "deterministic_public_fixture" for document in documents)
    assert all(Path(document.source_file_path or "").is_relative_to(first["storage"]) for document in documents)
    assert all(Path(document.preview_file_path or "").is_relative_to(first["storage"]) for document in documents)
    assert all(Path(document.mineru_result_dir or "").is_relative_to(first["storage"]) for document in documents)
    assert all(
        record.vector is None
        for record in store.list_index_records(DEFAULT_KNOWLEDGE_BASE_ID)
    )
    for document in documents:
        result_dir = Path(document.mineru_result_dir or "")
        assert (result_dir / "content_list_v2.json").is_file()
        for asset in store.list_assets(DEFAULT_KNOWLEDGE_BASE_ID, document.id):
            if asset.file_path:
                assert (result_dir / asset.file_path).is_file()

    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("public fixture", encoding="utf-8")
    app = create_app(
        db_path=first["db"],
        managed_root=first["storage"],
        static_dir=static_dir,
        settings_path=tmp_path / "settings.json",
        provider_store_path=tmp_path / "providers.db",
        provider_master_key_file=tmp_path / "master.key",
        start_processing_worker=False,
    )
    with TestClient(app) as client:
        _run_release_api_smoke_against_client(client)


def test_release_acceptance_seed_rejects_hash_mismatch_before_writing_targets(tmp_path):
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["samples"][0]["sha256"] = "0" * 64
    bad_manifest = tmp_path / "manifest.json"
    bad_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    db_path = tmp_path / "traceable.db"
    storage_root = tmp_path / "managed"
    report_path = tmp_path / "report.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(SEED_SCRIPT),
            "--source-dir",
            str(SOURCE_DIR),
            "--manifest",
            str(bad_manifest),
            "--db",
            str(db_path),
            "--storage-root",
            str(storage_root),
            "--report",
            str(report_path),
            "--expected-document-count",
            "4",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert report["mode"] == "deterministic_public_fixture"
    assert "SHA256 mismatch" in report["error"]["message"]
    assert not db_path.exists()
    assert not storage_root.exists()


def _run_seed(root: Path) -> dict[str, Path]:
    root.mkdir()
    db_path = root / "traceable.db"
    storage_root = root / "managed"
    report_path = root / "acceptance.json"
    sentinel = "must-not-be-read-or-reported"
    environment = os.environ.copy()
    environment.update(
        {
            "MINERU_TOKEN": sentinel,
            "OPENAI_EMBEDDING_BASE_URL": "http://127.0.0.1:9",
            "OPENAI_EMBEDDING_API_KEY": sentinel,
            "OPENAI_EMBEDDING_MODEL": sentinel,
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(SEED_SCRIPT),
            "--source-dir",
            str(SOURCE_DIR),
            "--manifest",
            str(MANIFEST),
            "--db",
            str(db_path),
            "--storage-root",
            str(storage_root),
            "--report",
            str(report_path),
            "--expected-document-count",
            "4",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert sentinel not in completed.stdout
    assert sentinel not in completed.stderr
    assert sentinel not in report_path.read_text(encoding="utf-8")
    return {"db": db_path, "storage": storage_root, "report": report_path}


def _stable_documents(report: dict) -> list[dict]:
    stable_keys = {
        "sample_id",
        "document_id",
        "display_name",
        "source_file",
        "source_sha256",
        "page_count",
        "text_sha256",
        "extracted_text_characters",
        "extracted_image_count",
        "extracted_image_sha256",
        "block_count",
        "asset_count",
        "chunk_count",
        "keyword_index_record_count",
        "processing_status",
        "managed_storage_verified",
    }
    return [
        {key: item[key] for key in stable_keys}
        for item in report["documents"]
    ]


def _run_release_api_smoke_against_client(client: TestClient) -> None:
    spec = importlib.util.spec_from_file_location("release_api_smoke", SMOKE_SCRIPT)
    assert spec is not None and spec.loader is not None
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)

    def request_json(url: str, payload: dict | None = None) -> dict:
        target = urlsplit(url)
        response = client.request(
            "POST" if payload is not None else "GET",
            target.path + (f"?{target.query}" if target.query else ""),
            json=payload,
        )
        assert response.status_code == 200, response.text
        return response.json()

    def request_text(url: str, *, accept: str = "text/plain") -> str:
        target = urlsplit(url)
        response = client.get(
            target.path + (f"?{target.query}" if target.query else ""),
            headers={"Accept": accept},
        )
        assert response.status_code == 200, response.text
        return response.text

    def request_bytes(url: str) -> tuple[str, bytes]:
        target = urlsplit(url)
        response = client.get(
            target.path + (f"?{target.query}" if target.query else ""),
            headers={"Accept": "image/*"},
        )
        assert response.status_code == 200, response.text
        return response.headers["content-type"].split(";", 1)[0], response.content

    smoke._request_json = request_json
    smoke._request_text = request_text
    smoke._request_bytes = request_bytes

    for probe in smoke.PROBES:
        result = smoke._run_retrieval_probe(
            "http://testserver",
            DEFAULT_KNOWLEDGE_BASE_ID,
            **probe,
        )
        assert result["pipeline_used"] == "keyword"
