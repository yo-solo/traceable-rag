from __future__ import annotations

import json
import math
import tarfile
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

from traceable_rag.mineru_import import parse_mineru_directory

from .manifest import DatasetDocument


@dataclass(frozen=True)
class MinerUValidation:
    doc_id: str
    result_dir: Path
    page_count: int
    block_count: int
    parser_version: str | None


def safe_extract_tar_gz(archive: str | Path, destination: str | Path) -> Path:
    archive = Path(archive)
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if target != destination and destination not in target.parents:
                raise ValueError(f"Unsafe archive path: {member.name}")
            if member.issym() or member.islnk() or member.isdev():
                raise ValueError(f"Archive links and device files are not allowed: {member.name}")
        try:
            bundle.extractall(destination, filter="data")
        except TypeError:  # Python 3.10/3.11 do not expose extraction filters.
            bundle.extractall(destination)
    return destination


def extract_and_validate_all(
    documents: tuple[DatasetDocument, ...],
    archives_root: str | Path,
    mineru_root: str | Path,
) -> list[MinerUValidation]:
    archives_root = Path(archives_root)
    mineru_root = Path(mineru_root)
    if archives_root.is_file():
        safe_extract_tar_gz(archives_root, mineru_root)
        nested = mineru_root / "mineru_output"
        if nested.is_dir():
            mineru_root = nested
    validations: list[MinerUValidation] = []
    for document in documents:
        result_dir = mineru_root / document.doc_id
        if not result_dir.exists():
            archive = archives_root / f"{document.doc_id}.tar.gz"
            if not archive.is_file():
                raise FileNotFoundError(archive)
            safe_extract_tar_gz(archive, result_dir)
        validations.append(validate_mineru_document(document, result_dir))
    return validations


def validate_mineru_document(document: DatasetDocument, result_dir: str | Path) -> MinerUValidation:
    result_dir = Path(result_dir)
    if not result_dir.is_dir():
        raise FileNotFoundError(result_dir)
    processing_manifest = result_dir / "processing_manifest.json"
    if not processing_manifest.is_file():
        raise FileNotFoundError(processing_manifest)
    processing = json.loads(processing_manifest.read_text(encoding="utf-8"))
    if str(processing.get("status", "")).casefold() not in {
        "done",
        "success",
        "completed",
    }:
        raise ValueError(f"MinerU processing did not complete for {document.doc_id}")
    actual_pdf_pages = len(PdfReader(str(document.pdf_path)).pages)
    if actual_pdf_pages != document.page_count:
        raise ValueError(f"PDF page count mismatch for {document.doc_id}")
    parsed = parse_mineru_directory(
        result_dir,
        knowledge_base_id="vidore-validation",
        document_id=f"vidore-validation:{document.doc_id}",
        original_filename=document.pdf_path.name,
        display_name=document.doc_id,
        source_file_path=document.pdf_path,
        preview_file_path=document.pdf_path,
        source_type="vidore_validation",
    )
    if parsed.document.page_count != document.page_count:
        raise ValueError(f"MinerU page count mismatch for {document.doc_id}")
    if not parsed.blocks:
        raise ValueError(f"MinerU produced no indexable blocks for {document.doc_id}")
    for block in parsed.blocks:
        if not 0 <= block.page_idx < document.page_count:
            raise ValueError(f"MinerU page index is out of range for {document.doc_id}")
        if block.bbox is not None and (
            len(block.bbox) != 4 or not all(math.isfinite(value) for value in block.bbox)
        ):
            raise ValueError(f"MinerU bbox is invalid for {document.doc_id}")
        if block.page_size is not None and (
            len(block.page_size) != 2
            or not all(math.isfinite(value) and value > 0 for value in block.page_size)
        ):
            raise ValueError(f"MinerU page size is invalid for {document.doc_id}")
    for asset in parsed.assets:
        source_path = asset.metadata.get("source_img_path")
        if source_path and not asset.file_path:
            raise ValueError(f"MinerU asset is missing or unsafe for {document.doc_id}: {source_path}")
    return MinerUValidation(
        doc_id=document.doc_id,
        result_dir=result_dir,
        page_count=parsed.document.page_count,
        block_count=len(parsed.blocks),
        parser_version=parsed.document.parser_version,
    )
