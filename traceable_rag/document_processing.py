from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import uuid
import zipfile
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterator, Protocol

from PIL import Image, ImageOps

from .chunking import BlockWindowChunker
from .embedding import EmbeddingProvider, provider_from_api_doc
from .image_assets import inspect_static_image
from .indexing import build_index_records, embed_index_records
from .mineru_client import (
    MinerUClient,
    MinerUParseResult,
    find_mineru_result_dir,
    mineru_api_key_from_environment,
)
from .mineru_import import (
    _cleanup_managed_import_runs,
    _prepare_managed_import,
    _remove_managed_tree,
    parse_mineru_directory,
)
from .models import Block, Chunk, Document, IndexRecord, KnowledgeBase
from .store import DocumentNotFoundError, EvidenceStore

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on platforms without fcntl
    fcntl = None


_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
logger = logging.getLogger(__name__)

_DOCX_FONT_SUBSTITUTIONS = {
    "方正仿宋_GBK": "Noto Serif CJK SC",
    "方正小标宋_GBK": "Noto Serif CJK SC",
    "方正楷体_GBK": "Noto Serif CJK SC",
    "方正黑体_GBK": "Noto Sans CJK SC",
    "仿宋_GB2312": "Noto Serif CJK SC",
    "楷体_GB2312": "Noto Serif CJK SC",
}
_DOCX_RUN_FONT_ATTRIBUTE = re.compile(
    rb'(?P<attribute>w:(?:ascii|hAnsi|eastAsia|cs))="(?P<family>[^"]+)"'
)
_MINERU_NATIVE_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})
_MINERU_NATIVE_IMAGE_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}

_WINDOWS_GENERIC_READ = 0x80000000
_WINDOWS_GENERIC_WRITE = 0x40000000
_WINDOWS_FILE_SHARE_READ = 0x00000001
_WINDOWS_FILE_SHARE_WRITE = 0x00000002
_WINDOWS_FILE_SHARE_DELETE = 0x00000004
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_WINDOWS_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class DocumentBusyError(RuntimeError):
    """Raised when another process or thread is mutating the same document."""


class DocumentAlreadyExistsError(RuntimeError):
    """Raised when upload creation targets an existing document identity."""


class DocumentSourceMismatchError(RuntimeError):
    """Raised when retry input does not match an existing document's source identity."""


class KnowledgeBaseDeleteConfirmationError(RuntimeError):
    """Raised when a non-empty knowledge base lacks exact-name confirmation."""


class KnowledgeBaseRecoveryBlockedError(RuntimeError):
    """Raised when pending filesystem recovery prevents safe KB mutation."""


class KnowledgeBaseNotFoundError(LookupError):
    """Raised when a knowledge base disappears before a locked mutation."""


class PageLimitExceededError(ValueError):
    def __init__(self, page_count: int, page_limit: int):
        self.page_count = page_count
        self.page_limit = page_limit
        super().__init__(f"PDF has {page_count} pages; limit is {page_limit}")


class NormalizationSourceUnavailableError(ValueError):
    """Raised when an old document has no retained parse result to rebuild."""


class MinerUConfigurationError(RuntimeError):
    """Raised when new document processing has no configured MinerU client."""


class DocumentConverter(Protocol):
    def convert(self, source_file: Path, output_pdf: Path) -> Path: ...


class DocumentIndexer(Protocol):
    def build(
        self, document_id: str, blocks: list[Block]
    ) -> tuple[list[Chunk], list[IndexRecord]]: ...


class MinerUParser(Protocol):
    def parse_local_file(
        self, file_path: Path, output_dir: Path, **kwargs: object
    ) -> MinerUParseResult: ...


class LibreOfficeConverter:
    """Create the stable PDF preview consumed by page checks and the viewer."""

    def __init__(self, executable: str | None = None, timeout_seconds: int = 180):
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    def convert(self, source_file: Path, output_pdf: Path) -> Path:
        source_file = Path(source_file)
        output_pdf = Path(output_pdf)
        output_pdf.parent.mkdir(parents=True, exist_ok=True)
        if source_file.suffix.lower() == ".pdf":
            shutil.copy2(source_file, output_pdf)
            return output_pdf
        if _is_mineru_native_image(source_file):
            _convert_image_to_pdf(source_file, output_pdf)
            return output_pdf

        executable = self.executable or _find_libreoffice_executable()
        profile_dir = output_pdf.parent / f".libreoffice-profile-{uuid.uuid4().hex}"
        profile_dir.mkdir(parents=True)
        conversion_source = source_file
        try:
            conversion_source = _docx_with_stable_cjk_fonts(
                source_file, output_pdf.parent
            )
            result = subprocess.run(
                [
                    executable,
                    f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(output_pdf.parent),
                    str(conversion_source),
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
            generated = output_pdf.parent / f"{conversion_source.stem}.pdf"
            if result.returncode != 0 or not generated.exists():
                detail = (result.stderr or result.stdout).strip()
                raise RuntimeError(
                    f"LibreOffice conversion failed: {detail or 'PDF was not created'}"
                )
            if generated != output_pdf:
                generated.replace(output_pdf)
            return output_pdf
        finally:
            if conversion_source != source_file:
                conversion_source.unlink(missing_ok=True)
            shutil.rmtree(profile_dir, ignore_errors=True)


def _is_mineru_native_image(source_file: Path) -> bool:
    return source_file.suffix.lower() in _MINERU_NATIVE_IMAGE_SUFFIXES


def _convert_image_to_pdf(source_file: Path, output_pdf: Path) -> None:
    expected_content_type = _MINERU_NATIVE_IMAGE_CONTENT_TYPES[
        source_file.suffix.lower()
    ]
    image_info = inspect_static_image(source_file)
    if image_info is None or image_info.content_type != expected_content_type:
        raise ValueError("Image is not a valid static JPG, JPEG, or PNG file")

    with Image.open(source_file) as opened:
        image = ImageOps.exif_transpose(opened)
        if image.mode in {"RGBA", "LA"} or (
            image.mode == "P" and "transparency" in image.info
        ):
            rgba = image.convert("RGBA")
            rgb = Image.new("RGB", rgba.size, "white")
            rgb.paste(rgba, mask=rgba.getchannel("A"))
        else:
            rgb = image.convert("RGB")
        rgb.save(output_pdf, format="PDF", resolution=150.0)


def _docx_with_stable_cjk_fonts(source_file: Path, work_dir: Path) -> Path:
    """Create a conversion-only DOCX with deterministic, OCR-friendly CJK fonts.

    Some business DOCX files embed uncommon proprietary Chinese fonts. LibreOffice
    can faithfully embed those glyphs in the PDF, but vision-based parsers may then
    split or reorder otherwise correct text. The original upload remains untouched;
    only run-font references in the temporary conversion input are normalized to the
    Noto CJK families installed by the runtime image.
    """

    if source_file.suffix.lower() != ".docx":
        return source_file

    candidate = work_dir / f".libreoffice-source-{uuid.uuid4().hex}.docx"
    replacements: set[tuple[str, str]] = set()

    def replace_font(match: re.Match[bytes]) -> bytes:
        family = match.group("family").decode("utf-8")
        replacement = _DOCX_FONT_SUBSTITUTIONS.get(family)
        if replacement is None:
            return match.group(0)
        replacements.add((family, replacement))
        return (
            match.group("attribute")
            + b'="'
            + replacement.encode("utf-8")
            + b'"'
        )

    try:
        with zipfile.ZipFile(source_file, "r") as source_archive:
            with zipfile.ZipFile(candidate, "w", allowZip64=True) as target_archive:
                for info in source_archive.infolist():
                    payload = source_archive.read(info)
                    if (
                        info.filename.startswith("word/")
                        and info.filename.endswith(".xml")
                        and info.filename != "word/fontTable.xml"
                    ):
                        payload = _DOCX_RUN_FONT_ATTRIBUTE.sub(replace_font, payload)
                    target_archive.writestr(info, payload)
    except zipfile.BadZipFile:
        candidate.unlink(missing_ok=True)
        return source_file
    except Exception:
        candidate.unlink(missing_ok=True)
        raise

    if not replacements:
        candidate.unlink(missing_ok=True)
        return source_file

    logger.info(
        "Normalized DOCX fonts for stable PDF conversion: %s",
        ", ".join(f"{source} -> {target}" for source, target in sorted(replacements)),
    )
    return candidate


class LocalDocumentIndexer:
    def __init__(self, *, target_tokens: int = 450, max_tokens: int = 700):
        self.chunker = BlockWindowChunker(
            target_tokens=target_tokens, max_tokens=max_tokens
        )

    def build(
        self, document_id: str, blocks: list[Block]
    ) -> tuple[list[Chunk], list[IndexRecord]]:
        chunks = self.chunker.chunk(document_id, blocks)
        return chunks, build_index_records(chunks, blocks=blocks)


class DocumentProcessingService:
    """Own the managed upload -> MinerU -> normalized evidence -> index lifecycle."""

    def __init__(
        self,
        store: EvidenceStore,
        managed_root: str | Path,
        *,
        converter: DocumentConverter | None = None,
        mineru_client: MinerUParser | None = None,
        indexer: DocumentIndexer | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        embedding_api_doc_path: str | Path | None = None,
        runtime_root: str | Path | None = None,
        platform_name: str | None = None,
        windows_kernel32: object | None = None,
        windows_get_last_error: Callable[[], int] | None = None,
    ):
        self.store = store
        self.managed_root = Path(managed_root).resolve()
        self.managed_root.mkdir(parents=True, exist_ok=True)
        self.db_identity = self.store.db_path.resolve()
        self.lock_root = self.db_identity.parent / ".traceable-rag-locks"
        if self.lock_root.is_symlink():
            raise ValueError("Shared document lock directory cannot be a symlink")
        self.lock_root.mkdir(parents=True, exist_ok=True)
        self.recovery_root = self.managed_root / ".recovery"
        self.recovery_manifests_root = self.recovery_root / "manifests"
        self.recovery_tombstones_root = self.recovery_root / "tombstones"
        self._blocked_knowledge_base_ids: set[str] = set()
        self._make_managed_directory(self.recovery_manifests_root)
        self._make_managed_directory(self.recovery_tombstones_root)
        self.converter = converter or LibreOfficeConverter()
        self.mineru_client = mineru_client
        self.indexer = indexer or LocalDocumentIndexer()
        self.embedding_provider = embedding_provider
        self.embedding_api_doc_path = (
            Path(embedding_api_doc_path)
            if embedding_api_doc_path is not None
            else None
        )
        self.runtime_root = (
            Path(runtime_root)
            if runtime_root is not None
            else self.managed_root.parent
        )
        self.platform_name = os.name if platform_name is None else platform_name
        self.windows_kernel32 = windows_kernel32
        self.windows_get_last_error = windows_get_last_error
        self._recover_pending_knowledge_base_deletions()

    def mineru_configured(self) -> bool:
        if self.mineru_client is not None:
            configured = getattr(self.mineru_client, "configured", None)
            if callable(configured):
                try:
                    return bool(configured())
                except Exception:
                    return False
            return True
        return bool(
            mineru_api_key_from_environment(runtime_root=self.runtime_root)
        )

    def register_upload(
        self,
        knowledge_base_id: str,
        source_file: str | Path,
        *,
        display_name: str | None = None,
        document_id: str | None = None,
        source_type: str = "upload",
    ) -> Document:
        source_file = Path(source_file)
        if not source_file.is_file():
            raise FileNotFoundError(source_file)
        document_id = document_id or str(uuid.uuid4())
        with self._knowledge_base_lock(knowledge_base_id):
            self._ensure_knowledge_base_recovery_clear(knowledge_base_id)
            with self._document_lock(knowledge_base_id, document_id):
                if self.store.document_id_exists(document_id):
                    raise DocumentAlreadyExistsError(
                        f"Document {document_id!r} already exists in knowledge base "
                        f"{knowledge_base_id!r}; call process_document to retry it."
                    )
                return self._register_upload_locked(
                    knowledge_base_id,
                    source_file,
                    display_name=display_name,
                    document_id=document_id,
                    source_type=source_type,
                )

    def register_upload_bytes(
        self,
        knowledge_base_id: str,
        filename: str,
        content: bytes,
        *,
        display_name: str | None = None,
        document_id: str | None = None,
        source_type: str = "upload",
    ) -> Document:
        safe_filename = _safe_source_filename(filename)
        document_id = document_id or str(uuid.uuid4())
        incoming_root = self.managed_root / ".incoming"
        staging_dir = incoming_root / uuid.uuid4().hex
        staged_file = staging_dir / safe_filename
        with self._knowledge_base_lock(knowledge_base_id):
            self._ensure_knowledge_base_recovery_clear(knowledge_base_id)
            with self._document_lock(knowledge_base_id, document_id):
                staged = False
                try:
                    self._make_managed_directory(staging_dir)
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    descriptor = os.open(staged_file, flags, 0o600)
                    staged = True
                    try:
                        view = memoryview(content)
                        while view:
                            written = os.write(descriptor, view)
                            if written == 0:
                                raise OSError("Unable to write staged upload")
                            view = view[written:]
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    if self.store.document_id_exists(document_id):
                        raise DocumentAlreadyExistsError(
                            f"Document {document_id!r} already exists."
                        )
                    return self._register_upload_locked(
                        knowledge_base_id,
                        staged_file,
                        display_name=display_name,
                        document_id=document_id,
                        source_type=source_type,
                    )
                finally:
                    if staged:
                        self._safe_unlink_file(staged_file)
                    try:
                        self._remove_empty_directory(staging_dir)
                        self._remove_empty_directory(incoming_root)
                    except (FileNotFoundError, ValueError):
                        pass

    def ensure_uploaded(
        self,
        knowledge_base_id: str,
        source_file: str | Path,
        *,
        display_name: str | None = None,
        document_id: str,
        source_type: str = "upload",
    ) -> Document:
        source_file = Path(source_file)
        if not source_file.is_file():
            raise FileNotFoundError(source_file)
        source_hash = _hash_file(source_file)
        with self._knowledge_base_lock(knowledge_base_id):
            self._ensure_knowledge_base_recovery_clear(knowledge_base_id)
            with self._document_lock(knowledge_base_id, document_id):
                existing = self.store.get_document(knowledge_base_id, document_id)
                if existing is not None:
                    if existing.original_filename != source_file.name:
                        raise DocumentSourceMismatchError(
                            f"Document {document_id!r} source filename does not match "
                            f"{source_file.name!r}."
                        )
                    if existing.file_hash != source_hash:
                        raise DocumentSourceMismatchError(
                            f"Document {document_id!r} source hash does not match the existing upload."
                        )
                    return existing
                if self.store.document_id_exists(document_id):
                    raise DocumentAlreadyExistsError(
                        f"Document {document_id!r} already exists in another knowledge base."
                    )
                return self._register_upload_locked(
                    knowledge_base_id,
                    source_file,
                    display_name=display_name,
                    document_id=document_id,
                    source_type=source_type,
                )

    def _register_upload_locked(
        self,
        knowledge_base_id: str,
        source_file: Path,
        *,
        display_name: str | None,
        document_id: str,
        source_type: str,
    ) -> Document:
        document_dir = self._document_dir(knowledge_base_id, document_id)
        file_hash = _hash_file(source_file)
        safe_suffix = source_file.suffix.lower()[:16]
        managed_source = (
            document_dir
            / "source"
            / f"{file_hash[:24]}-{uuid.uuid4().hex}{safe_suffix}"
        )
        self._make_managed_directory(managed_source.parent)
        shutil.copy2(source_file, managed_source)
        document = Document(
            id=document_id,
            knowledge_base_id=knowledge_base_id,
            original_filename=source_file.name,
            display_name=display_name or source_file.stem,
            file_hash=file_hash,
            source_type=source_type,
            source_file_path=str(managed_source),
            preview_file_path=None,
            mineru_result_dir=None,
            page_count=0,
            processing_status="uploaded",
            processing_error=None,
            parser_provider="mineru",
            parser_version=None,
        )
        try:
            self.store.upsert_document(document)
        except Exception as exc:
            self._safe_unlink_file(managed_source)
            self._remove_empty_directory(managed_source.parent)
            self._remove_empty_directory(document_dir)
            if self.store.document_id_exists(document_id):
                raise DocumentAlreadyExistsError(
                    f"Document {document_id!r} already exists; choose a new identity."
                ) from exc
            raise
        return document

    def process_document(
        self,
        knowledge_base_id: str,
        document_id: str,
        *,
        model_version: str = "vlm",
        language: str = "ch",
        poll_interval: int = 10,
        timeout_seconds: int = 900,
        max_pages: int = 200,
        progress: Callable[[str], None] | None = None,
    ) -> Document:
        # Processing may spend minutes waiting for MinerU. Holding the
        # knowledge-base lock for that whole period would prevent unrelated
        # documents from being uploaded into the same queue. The document lock
        # is sufficient here: knowledge-base deletion also acquires every
        # document lock before moving or deleting managed storage.
        with self._document_lock(knowledge_base_id, document_id):
            self._ensure_knowledge_base_recovery_clear(knowledge_base_id)
            return self._process_document_locked(
                knowledge_base_id,
                document_id,
                model_version=model_version,
                language=language,
                poll_interval=poll_interval,
                timeout_seconds=timeout_seconds,
                max_pages=max_pages,
                progress=progress,
            )

    def ensure_knowledge_base_ready(self, knowledge_base_id: str) -> None:
        """Reject mutations while managed-storage recovery is incomplete."""
        self._ensure_knowledge_base_recovery_clear(knowledge_base_id)

    def rebuild_document_content(
        self,
        knowledge_base_id: str,
        document_id: str,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> Document:
        """Rebuild normalized content and indexes from an existing MinerU result."""
        with self._document_lock(knowledge_base_id, document_id):
            document = self._require_document(knowledge_base_id, document_id)
            if not document.mineru_result_dir:
                raise NormalizationSourceUnavailableError(
                    "Document has no reusable parsed result"
                )
            result_dir = Path(document.mineru_result_dir)
            if not result_dir.is_dir():
                raise NormalizationSourceUnavailableError(
                    "Reusable parsed result is unavailable"
                )
            if not document.source_file_path or not document.preview_file_path:
                raise ValueError("Document source or preview is unavailable")
            if progress:
                progress("persist")
            try:
                managed = _prepare_managed_import(
                    input_dir=result_dir,
                    source_file=Path(document.source_file_path),
                    preview_file=Path(document.preview_file_path),
                    managed_root=self.managed_root,
                    knowledge_base_id=knowledge_base_id,
                    document_id=document_id,
                )
            except (FileNotFoundError, ValueError) as exc:
                raise NormalizationSourceUnavailableError(
                    "Reusable parsed result could not be promoted safely"
                ) from exc
            try:
                if progress:
                    progress("normalizing")
                parsed_document = parse_mineru_directory(
                    managed.result_dir,
                    knowledge_base_id=knowledge_base_id,
                    document_id=document_id,
                    original_filename=document.original_filename,
                    display_name=document.display_name,
                    source_file_path=managed.source_file,
                    preview_file_path=managed.preview_file,
                    source_type=document.source_type,
                )
                if progress:
                    progress("indexing")
                chunks, records = self.indexer.build(
                    document_id, parsed_document.blocks
                )
                try:
                    embedding_provider = self.embedding_provider or provider_from_api_doc(
                        self.embedding_api_doc_path,
                        runtime_root=self.runtime_root,
                    )
                    if records:
                        records = embed_index_records(records, embedding_provider)
                except Exception:
                    logger.warning(
                        "Embedding 索引生成失败，文档内容升级已安全回退到关键词索引。"
                    )
                ready_document = replace(
                    parsed_document.document,
                    processing_status="ready",
                    processing_error=None,
                )
                if progress:
                    progress("persist")
                self.store.replace_document_graph(
                    knowledge_base_id,
                    ready_document,
                    parsed_document.blocks,
                    parsed_document.assets,
                    chunks,
                    records,
                )
            except Exception:
                _remove_managed_tree(managed.active_run)
                raise
            try:
                _cleanup_managed_import_runs(
                    managed.document_dir, managed.active_run
                )
            except Exception:
                logger.warning(
                    "Could not clean up previous managed document runs after the "
                    "normalization graph was committed; retained run directories "
                    "for later cleanup."
                )
            return self._require_document(knowledge_base_id, document_id)

    def _process_document_locked(
        self,
        knowledge_base_id: str,
        document_id: str,
        *,
        model_version: str,
        language: str,
        poll_interval: int,
        timeout_seconds: int,
        max_pages: int,
        progress: Callable[[str], None] | None,
    ) -> Document:
        document = self._require_document(knowledge_base_id, document_id)
        if not document.source_file_path:
            raise ValueError("Document has no managed source file")
        source_file = Path(document.source_file_path)
        stage = "convert"
        current = self._save_status(document, "parsing")
        document_dir = self._document_dir(knowledge_base_id, document_id)
        run_id = uuid.uuid4().hex
        staging_run = document_dir / ".staging" / run_id
        active_run = document_dir / "runs" / run_id
        promoted = False
        try:
            if progress:
                progress("converting")
            converted_pdf = staging_run / "preview" / "converted.pdf"
            self._make_managed_directory(converted_pdf.parent)
            self.converter.convert(source_file, converted_pdf)

            if progress:
                progress("page_check")
            try:
                from pypdf import PdfReader

                page_count = len(PdfReader(str(converted_pdf)).pages)
            except Exception as exc:
                raise ValueError("Converted PDF page count could not be read") from exc
            # Publish a preflight preview for a document's first processing
            # attempt so oversized files remain inspectable.  A retry of an
            # already-published document must keep its previous preview until
            # the new graph is committed successfully.
            if not document.preview_file_path:
                preview_file = document_dir / "preview.pdf"
                preview_temp = document_dir / f".preview-{run_id}.pdf"
                shutil.copy2(converted_pdf, preview_temp)
                os.replace(preview_temp, preview_file)
                current = replace(
                    current,
                    preview_file_path=str(preview_file),
                    page_count=page_count,
                )
                self.store.upsert_document(current)
            if page_count > max_pages:
                raise PageLimitExceededError(page_count, max_pages)

            stage = "mineru"
            if progress:
                progress("mineru_submit")
            result_root = staging_run / "mineru"
            client = self.mineru_client or _mineru_client_from_environment(
                runtime_root=self.runtime_root
            )
            mineru_input = (
                source_file if _is_mineru_native_image(source_file) else converted_pdf
            )
            parsed = client.parse_local_file(
                mineru_input,
                result_root,
                model_version=model_version,
                language=language,
                poll_interval=poll_interval,
                timeout_seconds=timeout_seconds,
                progress=progress,
            )
            if parsed.state != "done":
                raise RuntimeError(f"MinerU batch ended in state {parsed.state!r}")
            staged_result_dir = find_mineru_result_dir(parsed.output_dir)
            if progress:
                progress("normalizing")
            staged_preview_file = _find_mineru_preview(staged_result_dir) or converted_pdf
            result_relative_path = staged_result_dir.relative_to(staging_run)
            preview_relative_path = staged_preview_file.relative_to(staging_run)
            self._make_managed_directory(active_run.parent)
            staging_run.replace(active_run)
            promoted = True
            result_dir = active_run / result_relative_path
            preview_file = active_run / preview_relative_path
            parsed_document = parse_mineru_directory(
                result_dir,
                knowledge_base_id=knowledge_base_id,
                document_id=document_id,
                original_filename=document.original_filename,
                display_name=document.display_name,
                source_file_path=source_file,
                preview_file_path=preview_file,
                source_type=document.source_type,
            )
            current = self._save_status(current, "parsed")
            current = self._save_status(current, "indexing")
            stage = "index"
            if progress:
                progress("indexing")
            chunks, records = self.indexer.build(document_id, parsed_document.blocks)
            try:
                embedding_provider = self.embedding_provider or provider_from_api_doc(
                    self.embedding_api_doc_path,
                    runtime_root=self.runtime_root,
                )
                if records:
                    records = embed_index_records(records, embedding_provider)
            except Exception:
                logger.warning(
                    "Embedding 索引生成失败，文档已安全回退到关键词索引。"
                )
            ready_document = replace(
                parsed_document.document,
                processing_status="ready",
                processing_error=None,
            )
            stage = "persist"
            if progress:
                progress("persist")
            self.store.replace_document_graph(
                knowledge_base_id,
                ready_document,
                parsed_document.blocks,
                parsed_document.assets,
                chunks,
                records,
            )
        except Exception as exc:
            self._safe_remove_tree(staging_run)
            if promoted:
                self._safe_remove_tree(active_run)
            self._remove_empty_directory(staging_run.parent)
            self._save_status(
                current,
                "failed",
                processing_error=f"{stage}: operation failed",
            )
            raise
        ready = self._require_document(knowledge_base_id, document_id)
        preflight_preview = document_dir / "preview.pdf"
        if ready.preview_file_path and Path(ready.preview_file_path) != preflight_preview:
            self._safe_unlink_file(preflight_preview)
        try:
            self._cleanup_previous_runs(document_dir, active_run)
        except Exception:
            logger.warning(
                "Could not clean up previous managed document runs after the new "
                "document graph was committed; retained run directories for later cleanup."
            )
        return ready

    def delete_document(self, knowledge_base_id: str, document_id: str) -> bool:
        with self._knowledge_base_lock(knowledge_base_id):
            self._ensure_knowledge_base_recovery_clear(knowledge_base_id)
            with self._document_lock(knowledge_base_id, document_id):
                self._require_document(knowledge_base_id, document_id)
                document_dir = self._document_dir(knowledge_base_id, document_id)
                trash_entry: Path | None = None
                if document_dir.is_symlink() or document_dir.exists():
                    trash_root = self.managed_root / ".trash"
                    self._make_managed_directory(trash_root)
                    trash_entry = trash_root / uuid.uuid4().hex
                    self._reject_symlink_ancestors(document_dir.parent)
                    document_dir.rename(trash_entry)
                try:
                    deleted = self.store.delete_document(knowledge_base_id, document_id)
                except Exception:
                    if trash_entry is not None:
                        self._make_managed_directory(document_dir.parent)
                        trash_entry.rename(document_dir)
                        self._remove_empty_directory(trash_entry.parent)
                    raise
                if not deleted:
                    if trash_entry is not None:
                        self._make_managed_directory(document_dir.parent)
                        trash_entry.rename(document_dir)
                        self._remove_empty_directory(trash_entry.parent)
                    return False
                if trash_entry is not None:
                    try:
                        self._safe_remove_tree(trash_entry)
                        self._remove_empty_directory(trash_entry.parent)
                    except OSError:
                        pass
                return True

    def rename_document(
        self, knowledge_base_id: str, document_id: str, display_name: str
    ) -> Document:
        with self._knowledge_base_lock(knowledge_base_id):
            self._ensure_knowledge_base_recovery_clear(knowledge_base_id)
            with self._document_lock(knowledge_base_id, document_id):
                self._require_document(knowledge_base_id, document_id)
                if not self.store.update_document_display_name(
                    knowledge_base_id, document_id, display_name
                ):
                    raise DocumentNotFoundError(
                        f"Document {document_id!r} was not found in knowledge base "
                        f"{knowledge_base_id!r}."
                    )
                return self._require_document(knowledge_base_id, document_id)

    def update_knowledge_base(
        self,
        knowledge_base_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> KnowledgeBase:
        with self._knowledge_base_lock(knowledge_base_id):
            self._ensure_knowledge_base_recovery_clear(knowledge_base_id)
            existing = self.store.get_knowledge_base(knowledge_base_id)
            if existing is None:
                raise KnowledgeBaseNotFoundError(knowledge_base_id)
            updated = replace(
                existing,
                name=name if name is not None else existing.name,
                description=(
                    description if description is not None else existing.description
                ),
            )
            if not self.store.update_knowledge_base(updated):
                raise KnowledgeBaseNotFoundError(knowledge_base_id)
            saved = self.store.get_knowledge_base(knowledge_base_id)
            if saved is None:
                raise KnowledgeBaseNotFoundError(knowledge_base_id)
            return saved

    def delete_knowledge_base(
        self, knowledge_base_id: str, *, confirm_name: str | None = None
    ) -> bool:
        with self._knowledge_base_lock(knowledge_base_id):
            self._ensure_knowledge_base_recovery_clear(knowledge_base_id)
            knowledge_base = self.store.get_knowledge_base(knowledge_base_id)
            if knowledge_base is None:
                return False
            documents = sorted(
                self.store.list_documents(knowledge_base_id), key=lambda item: item.id
            )
            if documents and confirm_name != knowledge_base.name:
                raise KnowledgeBaseDeleteConfirmationError(knowledge_base_id)
            return self._delete_knowledge_base_locked(knowledge_base_id, documents)

    def _delete_knowledge_base_locked(
        self, knowledge_base_id: str, documents: list[Document]
    ) -> bool:
        """Delete a knowledge base through a recoverable filesystem journal."""

        with ExitStack() as locks:
            for document in documents:
                locks.enter_context(
                    self._document_lock(knowledge_base_id, document.id)
                )
            manifest_path: Path | None = None
            try:
                manifest_path = self._prepare_knowledge_base_delete(knowledge_base_id)
                manifest = self._load_delete_manifest(manifest_path)
                tombstone_root = self._manifest_tombstone_root(manifest)
                cleanup_probe = tombstone_root / ".cleanup-probe"
                self._make_managed_directory(cleanup_probe)
                self._fsync_directory(tombstone_root)
                self._safe_remove_tree(cleanup_probe)
                self._fsync_directory(tombstone_root)

                if not self._commit_knowledge_base_delete(knowledge_base_id):
                    self._recover_delete_manifest(manifest_path)
                    return False
                self._finalize_knowledge_base_delete(
                    manifest_path, tombstone_root
                )
                return True
            except Exception:
                if (
                    manifest_path is not None
                    and manifest_path.exists()
                    and self.store.get_knowledge_base(knowledge_base_id) is not None
                ):
                    try:
                        self._recover_delete_manifest(manifest_path)
                    except Exception:
                        self._blocked_knowledge_base_ids.add(knowledge_base_id)
                if manifest_path is not None and manifest_path.exists():
                    self._blocked_knowledge_base_ids.add(knowledge_base_id)
                raise

    def _prepare_knowledge_base_delete(self, knowledge_base_id: str) -> Path:
        operation_id = uuid.uuid4().hex
        tombstone_root = self.recovery_tombstones_root / operation_id
        active_root = self._knowledge_base_dir(knowledge_base_id)
        payload = tombstone_root / "payload"
        had_payload = active_root.exists() or active_root.is_symlink()
        manifest_path = self.recovery_manifests_root / f"{operation_id}.json"
        manifest = {
            "version": 1,
            "operation_id": operation_id,
            "knowledge_base_id": knowledge_base_id,
            "active": str(active_root.relative_to(self.managed_root)),
            "payload": str(payload.relative_to(self.managed_root)),
            "had_payload": had_payload,
        }
        try:
            self._write_delete_manifest(manifest_path, manifest)
        except Exception:
            self._blocked_knowledge_base_ids.add(knowledge_base_id)
            raise
        try:
            if had_payload:
                self._make_managed_directory(payload.parent)
                self._fsync_directory(self.recovery_tombstones_root)
                self._reject_symlink_ancestors(active_root.parent)
                active_root.rename(payload)
                self._fsync_directory(self.managed_root)
                self._fsync_directory(tombstone_root)
        except Exception:
            try:
                self._recover_delete_manifest(manifest_path)
            except Exception:
                self._blocked_knowledge_base_ids.add(knowledge_base_id)
            raise
        return manifest_path

    def _write_delete_manifest(self, manifest_path: Path, manifest: dict) -> None:
        self._make_managed_directory(manifest_path.parent)
        temporary = manifest_path.with_name(f".{manifest_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump(manifest, output, ensure_ascii=False, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, manifest_path)
            self._fsync_directory(manifest_path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_delete_manifest(self, manifest_path: Path) -> dict:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("version") != 1:
            raise ValueError("Unsupported knowledge-base recovery manifest")
        operation_id = manifest.get("operation_id")
        knowledge_base_id = manifest.get("knowledge_base_id")
        active_value = manifest.get("active")
        payload_value = manifest.get("payload")
        had_payload = manifest.get("had_payload")
        if (
            not isinstance(operation_id, str)
            or manifest_path.stem != operation_id
            or not isinstance(knowledge_base_id, str)
            or not isinstance(had_payload, bool)
        ):
            raise ValueError("Invalid knowledge-base recovery manifest")
        tombstone_root = self.recovery_tombstones_root / operation_id
        active = self._managed_path_from_manifest(active_value)
        payload = self._managed_path_from_manifest(payload_value)
        if active.parent != self.managed_root or active.name.startswith("."):
            raise ValueError("Recovery active root is invalid")
        if payload != tombstone_root / "payload":
            raise ValueError("Recovery payload is outside its operation")
        return manifest

    def _managed_path_from_manifest(self, value: object) -> Path:
        if not isinstance(value, str):
            raise ValueError("Recovery manifest path must be a string")
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Recovery manifest path must be managed and relative")
        return self._lexical_managed_child(self.managed_root / relative)

    def _manifest_tombstone_root(self, manifest: dict) -> Path:
        return self.recovery_tombstones_root / manifest["operation_id"]

    def _commit_knowledge_base_delete(self, knowledge_base_id: str) -> bool:
        return self.store.delete_knowledge_base(knowledge_base_id)

    def _finalize_knowledge_base_delete(
        self, manifest_path: Path, tombstone_root: Path
    ) -> None:
        self._safe_remove_tree(tombstone_root)
        self._fsync_directory(self.recovery_tombstones_root)
        manifest_path.unlink()
        self._fsync_directory(self.recovery_manifests_root)

    def _recover_delete_manifest(self, manifest_path: Path) -> None:
        manifest = self._load_delete_manifest(manifest_path)
        tombstone_root = self._manifest_tombstone_root(manifest)
        active = self._managed_path_from_manifest(manifest["active"])
        payload = self._managed_path_from_manifest(manifest["payload"])
        active_exists = active.exists() or active.is_symlink()
        payload_exists = payload.exists() or payload.is_symlink()
        if active_exists and payload_exists:
            raise RuntimeError("Recovery active root and payload both exist")
        if manifest["had_payload"]:
            if not active_exists and not payload_exists:
                raise RuntimeError("Recovery payload is missing")
            if payload_exists:
                payload.rename(active)
                self._fsync_directory(tombstone_root)
                self._fsync_directory(self.managed_root)
        elif active_exists or payload_exists:
            raise RuntimeError("Empty knowledge-base recovery has unexpected payload")
        self._safe_remove_tree(tombstone_root)
        self._fsync_directory(self.recovery_tombstones_root)
        manifest_path.unlink()
        self._fsync_directory(self.recovery_manifests_root)

    def _recover_pending_knowledge_base_deletions(self) -> None:
        for manifest_path in sorted(self.recovery_manifests_root.glob("*.json")):
            knowledge_base_id: str | None = None
            try:
                manifest = self._load_delete_manifest(manifest_path)
                knowledge_base_id = manifest["knowledge_base_id"]
                with self._knowledge_base_lock(knowledge_base_id):
                    if self.store.get_knowledge_base(knowledge_base_id) is not None:
                        self._recover_delete_manifest(manifest_path)
                    else:
                        self._finalize_knowledge_base_delete(
                            manifest_path,
                            self._manifest_tombstone_root(manifest),
                        )
            except Exception as exc:
                if knowledge_base_id is None:
                    raise KnowledgeBaseRecoveryBlockedError(
                        "A recovery manifest could not be validated safely."
                    ) from exc
                self._blocked_knowledge_base_ids.add(knowledge_base_id)

    def _ensure_knowledge_base_recovery_clear(
        self, knowledge_base_id: str
    ) -> None:
        if knowledge_base_id in self._blocked_knowledge_base_ids:
            raise KnowledgeBaseRecoveryBlockedError(
                f"Knowledge base {knowledge_base_id!r} is blocked by pending recovery."
            )

    def _save_status(
        self,
        document: Document,
        status: str,
        *,
        processing_error: str | None = None,
    ) -> Document:
        updated = replace(
            document,
            processing_status=status,
            processing_error=processing_error,
        )
        self.store.upsert_document(updated)
        return self._require_document(updated.knowledge_base_id, updated.id)

    def _require_document(self, knowledge_base_id: str, document_id: str) -> Document:
        document = self.store.get_document(knowledge_base_id, document_id)
        if document is None:
            raise DocumentNotFoundError(
                f"Document {document_id!r} was not found in knowledge base {knowledge_base_id!r}."
            )
        return document

    def _document_dir(self, knowledge_base_id: str, document_id: str) -> Path:
        doc_part = hashlib.sha256(document_id.encode("utf-8")).hexdigest()[:24]
        return self._knowledge_base_dir(knowledge_base_id) / doc_part

    def _knowledge_base_dir(self, knowledge_base_id: str) -> Path:
        kb_part = hashlib.sha256(knowledge_base_id.encode("utf-8")).hexdigest()[:16]
        return self.managed_root / kb_part

    def _safe_remove_tree(self, path: Path) -> None:
        child = self._lexical_managed_child(path)
        if child.is_symlink():
            child.unlink()
            return
        self._reject_symlink_ancestors(child.parent)
        if child.exists():
            shutil.rmtree(child)

    def _safe_unlink_file(self, path: Path) -> None:
        if not str(path):
            return
        child = self._lexical_managed_child(path)
        self._reject_symlink_ancestors(child.parent)
        if child.is_symlink() or child.is_file():
            child.unlink()

    def _cleanup_previous_runs(self, document_dir: Path, active_run: Path) -> None:
        runs_root = document_dir / "runs"
        if runs_root.is_symlink():
            raise ValueError("Refusing to traverse a symlinked runs directory")
        if runs_root.exists():
            for run_dir in runs_root.iterdir():
                if run_dir.is_dir() and run_dir != active_run:
                    self._safe_remove_tree(run_dir)
        for legacy_dir in (document_dir / "mineru", document_dir / "preview"):
            self._safe_remove_tree(legacy_dir)
        self._remove_empty_directory(document_dir / ".staging")

    def _remove_empty_directory(self, path: Path) -> None:
        child = self._lexical_managed_child(path)
        self._reject_symlink_ancestors(child.parent)
        if child.is_symlink():
            child.unlink()
        elif child.exists() and not any(child.iterdir()):
            child.rmdir()

    def _make_managed_directory(self, path: Path) -> None:
        child = self._lexical_managed_child(path)
        relative = child.relative_to(self.managed_root)
        current = self.managed_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError(f"Refusing to traverse managed symlink: {current}")
            current.mkdir(exist_ok=True)

    def _fsync_directory(self, path: Path) -> None:
        directory = Path(os.path.abspath(os.path.normpath(path)))
        if directory != self.managed_root:
            directory = self._lexical_managed_child(directory)
            self._reject_symlink_ancestors(directory)
        elif directory.is_symlink():
            raise ValueError("Managed storage root cannot be a symlink")
        if self.platform_name == "nt":
            _fsync_windows_directory(
                directory,
                kernel32=self.windows_kernel32,
                get_last_error=self.windows_get_last_error,
            )
            return
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        try:
            descriptor = os.open(directory, flags)
        except OSError as exc:
            raise OSError(
                f"Directory fsync is unavailable for managed path: {directory}"
            ) from exc
        try:
            os.fsync(descriptor)
        except OSError as exc:
            raise OSError(
                f"Directory fsync failed for managed path: {directory}"
            ) from exc
        finally:
            os.close(descriptor)

    def _lexical_managed_child(self, path: Path) -> Path:
        child = Path(os.path.abspath(os.path.normpath(path)))
        if os.path.commonpath((str(self.managed_root), str(child))) != str(
            self.managed_root
        ) or child == self.managed_root:
            raise ValueError(f"Refusing path outside managed storage: {path}")
        return child

    def _reject_symlink_ancestors(self, path: Path) -> None:
        if Path(os.path.abspath(os.path.normpath(path))) == self.managed_root:
            return
        child = self._lexical_managed_child(path)
        relative = child.relative_to(self.managed_root)
        current = self.managed_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError(f"Refusing to traverse managed symlink: {current}")

    @contextmanager
    def _knowledge_base_lock(self, knowledge_base_id: str) -> Iterator[None]:
        lock_key = hashlib.sha256(
            f"{self.db_identity}\0knowledge-base\0{knowledge_base_id}".encode("utf-8")
        ).hexdigest()
        with self._exclusive_lock(lock_key, f"Knowledge base {knowledge_base_id!r} is busy"):
            yield

    @contextmanager
    def _document_lock(
        self, knowledge_base_id: str, document_id: str
    ) -> Iterator[None]:
        lock_key = hashlib.sha256(
            f"{self.db_identity}\0document\0{knowledge_base_id}\0{document_id}".encode(
                "utf-8"
            )
        ).hexdigest()
        with self._exclusive_lock(lock_key, f"Document {document_id!r} is busy"):
            yield

    @contextmanager
    def _exclusive_lock(self, lock_key: str, busy_message: str) -> Iterator[None]:
        if self.lock_root.is_symlink():
            raise ValueError("Shared document lock directory cannot be a symlink")
        with _THREAD_LOCKS_GUARD:
            thread_lock = _THREAD_LOCKS.setdefault(lock_key, threading.Lock())
        if not thread_lock.acquire(blocking=False):
            raise DocumentBusyError(busy_message)
        lock_file = self.lock_root / f"{lock_key}.lock"
        descriptor: int | None = None
        fallback_claim: Path | None = None
        try:
            descriptor = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
            if fcntl is not None:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise DocumentBusyError(busy_message) from exc
            else:  # pragma: no cover - fcntl is available on the supported Unix runtime
                claim_path = lock_file.with_suffix(".claim")
                try:
                    claim_descriptor = os.open(
                        claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                    )
                except FileExistsError as exc:
                    raise DocumentBusyError(busy_message) from exc
                else:
                    os.close(claim_descriptor)
                    fallback_claim = claim_path
            yield
        finally:
            if descriptor is not None:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            if fallback_claim is not None:
                fallback_claim.unlink(missing_ok=True)
            thread_lock.release()


def _fsync_windows_directory(
    path: Path,
    *,
    kernel32: object | None = None,
    get_last_error: Callable[[], int] | None = None,
) -> None:
    if kernel32 is None:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.FlushFileBuffers.argtypes = (wintypes.HANDLE,)
        kernel32.FlushFileBuffers.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
    if get_last_error is None:
        get_last_error = ctypes.get_last_error

    handle = kernel32.CreateFileW(
        str(path),
        _WINDOWS_GENERIC_READ | _WINDOWS_GENERIC_WRITE,
        (
            _WINDOWS_FILE_SHARE_READ
            | _WINDOWS_FILE_SHARE_WRITE
            | _WINDOWS_FILE_SHARE_DELETE
        ),
        None,
        _WINDOWS_OPEN_EXISTING,
        _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    handle_value = handle.value if hasattr(handle, "value") else handle
    if handle_value in (None, _WINDOWS_INVALID_HANDLE_VALUE):
        raise _windows_directory_oserror("CreateFileW", get_last_error())

    flush_error: OSError | None = None
    if not kernel32.FlushFileBuffers(handle_value):
        flush_error = _windows_directory_oserror(
            "FlushFileBuffers", get_last_error()
        )
    close_succeeded = kernel32.CloseHandle(handle_value)
    if flush_error is not None:
        raise flush_error
    if not close_succeeded:
        raise _windows_directory_oserror("CloseHandle", get_last_error())


def _windows_directory_oserror(operation: str, error_code: int) -> OSError:
    return OSError(
        error_code,
        f"Windows directory durability operation {operation} failed "
        f"with error {error_code}",
    )


def _mineru_client_from_environment(
    *, runtime_root: str | Path | None = None
) -> MinerUClient:
    api_key = mineru_api_key_from_environment(runtime_root=runtime_root)
    if not api_key:
        raise MinerUConfigurationError(
            "MINERU_TOKEN or MINERU_API_KEY is required for MinerU processing"
        )
    return MinerUClient(api_key)


def _find_libreoffice_executable() -> str:
    for name in ("libreoffice", "soffice"):
        executable = shutil.which(name)
        if executable:
            return executable
    macos_executable = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
    if macos_executable.is_file():
        return str(macos_executable)
    raise RuntimeError("LibreOffice executable was not found")


def _find_mineru_preview(result_dir: Path) -> Path | None:
    candidates = sorted(result_dir.rglob("*_origin.pdf"))
    return candidates[0] if candidates else None


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_source_filename(filename: str) -> str:
    normalized = filename.replace("\\", "/")
    safe_name = Path(normalized).name.strip()
    if not safe_name or safe_name in {".", ".."} or len(safe_name) > 255:
        raise ValueError("Uploaded filename is invalid")
    return safe_name
