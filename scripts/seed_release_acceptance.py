from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import sys
import tempfile
import unicodedata
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pypdf import PdfReader

from traceable_rag.document_processing import LocalDocumentIndexer
from traceable_rag.mineru_import import import_mineru_directory
from traceable_rag.retrieval import Retriever
from traceable_rag.store import DEFAULT_KNOWLEDGE_BASE_ID, EvidenceStore


MODE = "deterministic_public_fixture"
SOURCE_TYPE = MODE
PARSER_VERSION = "pypdf-public-fixture-v1"
DOCUMENT_ID_PREFIX = "https://github.com/yo-solo/traceable-rag/examples/vidore"
SUPPORTED_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class VerifiedSample:
    sample_id: str
    source_path: Path
    source_name: str
    display_name: str
    source_sha256: str
    document_id: str


@dataclass(frozen=True)
class ExtractedFixture:
    result_dir: Path
    page_count: int
    text_sha256: str
    extracted_text_characters: int
    image_hashes: tuple[str, ...]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Seed the public ViDoRe release corpus without calling MinerU, "
            "Embedding, or any other external Provider."
        )
    )
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--storage-root", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--expected-document-count", type=int, default=4)
    args = parser.parse_args(argv)

    report_path = Path(args.report)
    report = _report_skeleton(args.expected_document_count)
    try:
        report = seed_release_acceptance(
            source_dir=Path(args.source_dir),
            manifest_path=Path(args.manifest),
            db_path=Path(args.db),
            storage_root=Path(args.storage_root),
            expected_document_count=args.expected_document_count,
        )
    except Exception as exc:
        report["ok"] = False
        report["error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
        }

    _write_report(report_path, report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("ok") is True else 1


def seed_release_acceptance(
    *,
    source_dir: Path,
    manifest_path: Path,
    db_path: Path,
    storage_root: Path,
    expected_document_count: int,
) -> dict[str, Any]:
    if expected_document_count <= 0:
        raise ValueError("expected document count must be positive")
    _require_fresh_targets(db_path, storage_root)

    source_dir = _real_directory(source_dir, "source directory")
    manifest_path = _real_file(manifest_path, "manifest")
    manifest = _load_manifest(manifest_path)

    # Complete the public-byte trust check before opening a PDF or creating the
    # database/storage targets. A single mismatch aborts the whole seed.
    verified_samples = _verify_manifest_sources(
        source_dir,
        manifest,
        expected_document_count=expected_document_count,
    )

    db_path.parent.mkdir(parents=True, exist_ok=True)
    storage_root.parent.mkdir(parents=True, exist_ok=True)
    store = EvidenceStore(db_path)
    indexer = LocalDocumentIndexer()
    document_reports: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="traceable-rag-public-fixture-") as temp:
        temporary_root = Path(temp)
        for sample in verified_samples:
            extracted = _build_mineru_v2_fixture(
                sample,
                temporary_root / sample.document_id,
            )
            imported = import_mineru_directory(
                extracted.result_dir,
                store=store,
                knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
                document_id=sample.document_id,
                original_filename=sample.source_name,
                display_name=sample.display_name,
                source_file_path=sample.source_path,
                preview_file_path=sample.source_path,
                source_type=SOURCE_TYPE,
                managed_root=storage_root,
            )
            blocks = store.list_blocks(
                DEFAULT_KNOWLEDGE_BASE_ID,
                sample.document_id,
            )
            assets = store.list_assets(
                DEFAULT_KNOWLEDGE_BASE_ID,
                sample.document_id,
            )
            chunks, records = indexer.build(sample.document_id, blocks)
            if not chunks or not records:
                raise ValueError(
                    f"keyword index is empty for public sample {sample.sample_id!r}"
                )
            if any(
                record.vector is not None
                or record.embedding_provider is not None
                or record.embedding_model is not None
                for record in records
            ):
                raise ValueError("public fixture unexpectedly contains an embedding index")

            visual_assets = [
                asset for asset in assets if asset.asset_type in {"image", "chart"}
            ]
            if len(visual_assets) != len(extracted.image_hashes):
                raise ValueError(
                    f"embedded image import count changed for {sample.sample_id!r}"
                )
            if any(
                not asset.file_path
                or asset.content_type not in {"image/jpeg", "image/png", "image/webp"}
                for asset in visual_assets
            ):
                raise ValueError(
                    f"an extracted visual is not renderable for {sample.sample_id!r}"
                )

            ready_document = replace(
                imported.document,
                processing_status="ready",
                processing_error=None,
            )
            store.replace_document_graph(
                DEFAULT_KNOWLEDGE_BASE_ID,
                ready_document,
                blocks,
                assets,
                chunks,
                records,
            )
            _verify_keyword_search(sample, blocks, chunks, records)
            _verify_managed_document(storage_root, ready_document, assets)

            document_reports.append(
                {
                    "sample_id": sample.sample_id,
                    "document_id": sample.document_id,
                    "display_name": sample.display_name,
                    "source_file": sample.source_name,
                    "source_sha256": sample.source_sha256,
                    "page_count": extracted.page_count,
                    "text_sha256": extracted.text_sha256,
                    "extracted_text_characters": extracted.extracted_text_characters,
                    "extracted_image_count": len(extracted.image_hashes),
                    "extracted_image_sha256": list(extracted.image_hashes),
                    "block_count": len(blocks),
                    "asset_count": len(assets),
                    "chunk_count": len(chunks),
                    "keyword_index_record_count": len(records),
                    "processing_status": "ready",
                    "managed_storage_verified": True,
                }
            )

    documents = store.list_documents(DEFAULT_KNOWLEDGE_BASE_ID)
    if len(documents) != expected_document_count:
        raise ValueError(
            "seeded document count does not match the acceptance contract"
        )
    if any(document.processing_status != "ready" for document in documents):
        raise ValueError("one or more public fixture documents are not ready")

    report = _report_skeleton(expected_document_count)
    report.update(
        {
            "ok": True,
            "manifest_sha256": _sha256_path(manifest_path),
            "manifest_schema_version": manifest.get("schema_version"),
            "knowledge_base_id": DEFAULT_KNOWLEDGE_BASE_ID,
            "document_count": len(documents),
            "keyword_index": {
                "status": "passed",
                "embedding_record_count": 0,
            },
            "documents": document_reports,
        }
    )
    return report


def _report_skeleton(expected_document_count: int) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "mode": MODE,
        "ok": False,
        "expected_document_count": expected_document_count,
        "network_access": "not_used",
        "external_provider_e2e": {
            "mineru": {
                "status": "untested",
                "fixture": PARSER_VERSION,
            },
            "embedding": {
                "status": "untested",
                "fixture": "keyword_only",
            },
        },
    }


def _require_fresh_targets(db_path: Path, storage_root: Path) -> None:
    if db_path.is_symlink() or db_path.exists():
        raise ValueError("database path must not exist before release seeding")
    if storage_root.is_symlink():
        raise ValueError("managed storage root cannot be a symlink")
    if storage_root.exists():
        if not storage_root.is_dir():
            raise ValueError("managed storage root must be a directory")
        if any(storage_root.iterdir()):
            raise ValueError("managed storage root must be empty before release seeding")


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("public fixture manifest is not valid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("samples"), list):
        raise ValueError("public fixture manifest does not contain a samples list")
    return payload


def _verify_manifest_sources(
    source_dir: Path,
    manifest: dict[str, Any],
    *,
    expected_document_count: int,
) -> list[VerifiedSample]:
    samples = manifest["samples"]
    if len(samples) != expected_document_count:
        raise ValueError(
            "manifest sample count does not match --expected-document-count"
        )

    verified: list[VerifiedSample] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    seen_document_ids: set[str] = set()
    for index, raw_sample in enumerate(samples):
        if not isinstance(raw_sample, dict):
            raise ValueError(f"manifest sample {index} is not an object")
        sample_id = _required_string(raw_sample, "id", index)
        manifest_file = _required_string(raw_sample, "file", index)
        expected_sha256 = _required_string(raw_sample, "sha256", index).casefold()
        if SHA256_PATTERN.fullmatch(expected_sha256) is None:
            raise ValueError(f"manifest sample {sample_id!r} has an invalid SHA256")

        source_name = Path(manifest_file).name
        if not source_name or Path(source_name).suffix.casefold() != ".pdf":
            raise ValueError(f"manifest sample {sample_id!r} is not a PDF")
        source_path = source_dir / source_name
        source_path = _real_file(source_path, f"source PDF {source_name!r}")
        try:
            source_path.relative_to(source_dir)
        except ValueError as exc:
            raise ValueError("manifest source escaped --source-dir") from exc

        actual_sha256 = _sha256_path(source_path)
        if not hmac.compare_digest(actual_sha256, expected_sha256):
            raise ValueError(f"SHA256 mismatch for public source {source_name!r}")

        display_name = Path(source_name).stem
        document_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{DOCUMENT_ID_PREFIX}/{sample_id}@sha256:{expected_sha256}",
            )
        )
        if sample_id in seen_ids or source_name in seen_names:
            raise ValueError("public fixture manifest contains duplicate samples")
        if document_id in seen_document_ids:
            raise ValueError("public fixture document identity collision")
        seen_ids.add(sample_id)
        seen_names.add(source_name)
        seen_document_ids.add(document_id)
        verified.append(
            VerifiedSample(
                sample_id=sample_id,
                source_path=source_path,
                source_name=source_name,
                display_name=display_name,
                source_sha256=expected_sha256,
                document_id=document_id,
            )
        )
    return verified


def _build_mineru_v2_fixture(
    sample: VerifiedSample,
    output_root: Path,
) -> ExtractedFixture:
    result_dir = output_root / "mineru-v2"
    images_dir = result_dir / "images"
    result_dir.mkdir(parents=True)

    reader = PdfReader(str(sample.source_path))
    if reader.is_encrypted:
        raise ValueError(f"encrypted public source is unsupported: {sample.source_name}")
    pages: list[list[dict[str, Any]]] = []
    page_texts: list[str] = []
    image_hashes: list[str] = []
    for page_idx, page in enumerate(reader.pages):
        page_text = _normalize_extracted_text(page.extract_text() or "")
        page_texts.append(page_text)
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        if width <= 0 or height <= 0:
            raise ValueError(f"invalid PDF page geometry in {sample.source_name}")
        page_size = [width, height]
        full_page_bbox = [0.0, 0.0, width, height]
        items: list[dict[str, Any]] = []
        if page_text:
            items.append(
                {
                    "id": f"page-{page_idx + 1:04d}-text",
                    "type": "paragraph",
                    "bbox": full_page_bbox,
                    "page_size": page_size,
                    "content": {
                        "paragraph_content": [
                            {"type": "text", "content": page_text}
                        ]
                    },
                }
            )

        for image_idx, image in enumerate(page.images):
            image_bytes = image.data
            if not image_bytes:
                raise ValueError(f"pypdf returned an empty image in {sample.source_name}")
            suffix = _supported_image_suffix(image)
            image_sha256 = hashlib.sha256(image_bytes).hexdigest()
            image_hashes.append(image_sha256)
            images_dir.mkdir(exist_ok=True)
            image_name = (
                f"page-{page_idx + 1:04d}-image-{image_idx + 1:03d}-"
                f"{image_sha256[:16]}{suffix}"
            )
            image_path = images_dir / image_name
            image_path.write_bytes(image_bytes)
            if _sha256_path(image_path) != image_sha256:
                raise ValueError("extracted image write verification failed")

            # pypdf exposes the original pixels but not a reliable caption-to-XObject
            # relationship. The real text extracted from the same page is retained as
            # visual context so the visual Source remains deterministically searchable.
            visual_context = page_text or (
                f"{sample.display_name}, PDF page {page_idx + 1} visual"
            )
            items.append(
                {
                    "id": (
                        f"page-{page_idx + 1:04d}-image-"
                        f"{image_idx + 1:03d}-{image_sha256[:16]}"
                    ),
                    "type": "image",
                    "bbox": full_page_bbox,
                    "page_size": page_size,
                    "content": {
                        "image_source": {"path": f"images/{image_name}"},
                        "image_caption": [
                            {"type": "text", "content": visual_context}
                        ],
                        "image_footnote": [],
                    },
                }
            )
        pages.append(items)

    joined_text = "\n\f\n".join(page_texts)
    if not joined_text.strip():
        raise ValueError(f"pypdf extracted no text from {sample.source_name}")
    (result_dir / "content_list_v2.json").write_text(
        json.dumps(pages, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (result_dir / "processing_manifest.json").write_text(
        json.dumps(
            {
                "mode": MODE,
                "mineru": {"version": PARSER_VERSION},
                "source_sha256": sample.source_sha256,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return ExtractedFixture(
        result_dir=result_dir,
        page_count=len(reader.pages),
        text_sha256=hashlib.sha256(joined_text.encode("utf-8")).hexdigest(),
        extracted_text_characters=sum(len(text) for text in page_texts),
        image_hashes=tuple(image_hashes),
    )


def _verify_keyword_search(
    sample: VerifiedSample,
    blocks: list[Any],
    chunks: list[Any],
    records: list[Any],
) -> None:
    searchable_text = " ".join(record.indexing_text for record in records)
    terms = re.findall(r"[A-Za-z0-9_]{4,}", searchable_text)
    query = " ".join(terms[:3])
    if not query or not Retriever(chunks, records, blocks).search(query, top_k=1):
        raise ValueError(
            f"keyword retrieval verification failed for {sample.sample_id!r}"
        )


def _verify_managed_document(
    storage_root: Path,
    document: Any,
    assets: list[Any],
) -> None:
    root = storage_root.resolve(strict=True)
    required_paths = (
        document.source_file_path,
        document.preview_file_path,
        document.mineru_result_dir,
    )
    for value in required_paths:
        if not value:
            raise ValueError("managed document path is missing")
        path = Path(value).resolve(strict=True)
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("managed document path escaped storage root") from exc
    result_dir = Path(document.mineru_result_dir).resolve(strict=True)
    if not (result_dir / "content_list_v2.json").is_file():
        raise ValueError("managed MinerU v2 result was not retained")
    for asset in assets:
        if not asset.file_path:
            continue
        asset_path = (result_dir / asset.file_path).resolve(strict=True)
        try:
            asset_path.relative_to(result_dir)
        except ValueError as exc:
            raise ValueError("managed image path escaped MinerU result") from exc
        if asset.file_hash != _sha256_path(asset_path):
            raise ValueError("managed image hash does not match the imported graph")


def _supported_image_suffix(image: Any) -> str:
    suffix = Path(str(getattr(image, "name", ""))).suffix.casefold()
    if suffix in SUPPORTED_IMAGE_SUFFIXES:
        return ".jpg" if suffix == ".jpeg" else suffix
    image_format = str(getattr(getattr(image, "image", None), "format", "")).upper()
    suffix = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(
        image_format
    )
    if suffix is None:
        raise ValueError("public fixture contains an unsupported embedded image format")
    return suffix


def _normalize_extracted_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).replace("\x00", " ")
    return re.sub(r"\s+", " ", value).strip()


def _required_string(sample: dict[str, Any], key: str, index: int) -> str:
    value = sample.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"manifest sample {index} is missing {key!r}")
    return value.strip()


def _real_directory(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{label} must be a real directory")
    return path.resolve(strict=True)


def _real_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a real file")
    return path.resolve(strict=True)


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
