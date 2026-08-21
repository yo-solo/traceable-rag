from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import BenchmarkConfig


@dataclass(frozen=True)
class DatasetDocument:
    doc_id: str
    pdf_path: Path
    sha256: str
    page_count: int


@dataclass(frozen=True)
class DatasetManifest:
    root: Path
    dataset_id: str
    dataset_revision: str
    documents: tuple[DatasetDocument, ...]
    corpus_paths: tuple[Path, ...]
    queries_paths: tuple[Path, ...]
    qrels_paths: tuple[Path, ...]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: str | Path, config: BenchmarkConfig, *, verify_hashes: bool = True) -> DatasetManifest:
    manifest_path = Path(path).resolve()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("dataset_id") != config.dataset_id:
        raise ValueError("Dataset manifest id does not match benchmark config")
    if payload.get("dataset_revision") != config.dataset_revision:
        raise ValueError("Dataset manifest revision does not match benchmark config")
    root = manifest_path.parent
    documents: list[DatasetDocument] = []
    seen: set[str] = set()
    for item in payload.get("documents", []):
        doc_id = str(item.get("doc_id") or "").strip()
        if not doc_id or doc_id in seen:
            raise ValueError(f"Invalid or duplicate document id: {doc_id!r}")
        seen.add(doc_id)
        pdf_path = _inside(root, item.get("pdf"), "PDF")
        expected_hash = str(item.get("sha256") or "").lower()
        page_count = int(item.get("page_count") or 0)
        if len(expected_hash) != 64 or page_count <= 0:
            raise ValueError(f"Invalid metadata for document {doc_id}")
        if not pdf_path.is_file():
            raise FileNotFoundError(pdf_path)
        if verify_hashes and sha256_file(pdf_path) != expected_hash:
            raise ValueError(f"PDF checksum mismatch for document {doc_id}")
        documents.append(DatasetDocument(doc_id, pdf_path, expected_hash, page_count))
    if len(documents) != config.expected_documents:
        raise ValueError(f"Expected {config.expected_documents} documents, found {len(documents)}")
    if sum(document.page_count for document in documents) != config.expected_pages:
        raise ValueError("Manifest page total does not match benchmark config")
    return DatasetManifest(
        root=root,
        dataset_id=payload["dataset_id"],
        dataset_revision=payload["dataset_revision"],
        documents=tuple(documents),
        corpus_paths=_data_files(root, payload.get("corpus_files", payload.get("corpus")), "corpus"),
        queries_paths=_data_files(root, payload.get("queries_files", payload.get("queries")), "queries"),
        qrels_paths=_data_files(root, payload.get("qrels_files", payload.get("qrels")), "qrels"),
    )


def read_rows(paths: str | Path | Iterable[str | Path], *, columns: list[str] | None = None) -> list[dict[str, Any]]:
    selected = [Path(paths)] if isinstance(paths, (str, Path)) else [Path(path) for path in paths]
    rows: list[dict[str, Any]] = []
    for path in selected:
        suffix = path.suffix.casefold()
        if suffix == ".jsonl":
            rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        elif suffix == ".parquet":
            try:
                import pyarrow.parquet as parquet
            except ImportError as exc:
                raise RuntimeError("Reading ViDoRe Parquet snapshots requires the eval optional dependencies") from exc
            rows.extend(parquet.read_table(path, columns=columns).to_pylist())
        else:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                raise ValueError(f"Expected a JSON array in {path}")
            rows.extend(payload)
    return rows


def filter_english_queries(rows: Iterable[dict[str, Any]], expected_count: int) -> list[dict[str, Any]]:
    selected = [
        row
        for row in rows
        if str(row.get("language", "")).strip().casefold() in {"english", "en"}
    ]
    query_ids = [str(row.get("query_id")) for row in selected]
    if len(query_ids) != len(set(query_ids)):
        raise ValueError("Query ids are not unique")
    if len(selected) != expected_count:
        raise ValueError(f"Expected {expected_count} English queries, found {len(selected)}")
    return selected


def _data_files(root: Path, value: object, label: str) -> tuple[Path, ...]:
    values = value if isinstance(value, list) else [value]
    files: list[Path] = []
    for item in values:
        path = _inside(root, item, label)
        if path.is_dir():
            files.extend(sorted(path.glob("*.parquet")))
        elif any(character in str(item) for character in "*?["):
            pattern = str(Path(str(item)))
            files.extend(sorted(root.glob(pattern)))
        elif path.is_file():
            files.append(path)
        else:
            raise FileNotFoundError(path)
    unique = tuple(dict.fromkeys(path.resolve() for path in files))
    if not unique:
        raise FileNotFoundError(f"No {label} data files found")
    return unique


def _inside(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Manifest {label} path is missing")
    candidate = (root / value).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"Manifest {label} path escapes its dataset root")
    return candidate
