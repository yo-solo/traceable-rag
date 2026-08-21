from __future__ import annotations

import hashlib
import hmac
import os
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from .models import Asset


VISUAL_ASSET_TYPES = frozenset({"image", "chart"})
ALLOWED_IMAGE_FORMATS = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}
ALLOWED_IMAGE_CONTENT_TYPES = frozenset(
    content_type for content_type, _suffix in ALLOWED_IMAGE_FORMATS.values()
)
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_BYTES = 100 * 1024 * 1024
PREVIEW_MAX_EDGE = 1280


@dataclass(frozen=True)
class StaticImageInfo:
    content_type: str
    suffix: str
    width: int
    height: int


@dataclass(frozen=True)
class ImagePreview:
    key: str
    file_hash: str
    content_type: str


@dataclass(frozen=True)
class ResolvedImageAsset:
    path: Path
    info: StaticImageInfo
    file_hash: str


def inspect_static_image(path: Path) -> StaticImageInfo | None:
    """Validate a bounded, static JPEG/PNG/WebP without trusting its suffix."""

    try:
        if path.stat().st_size > MAX_IMAGE_BYTES:
            return None
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image_format = str(image.format or "").upper()
                format_info = ALLOWED_IMAGE_FORMATS.get(image_format)
                if format_info is None or int(getattr(image, "n_frames", 1)) != 1:
                    return None
                width, height = image.size
                if (
                    width <= 0
                    or height <= 0
                    or width * height > MAX_IMAGE_PIXELS
                ):
                    return None
                image.verify()
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return None
    content_type, suffix = format_info
    return StaticImageInfo(
        content_type=content_type,
        suffix=suffix,
        width=width,
        height=height,
    )


def create_image_preview(
    input_dir: Path,
    source: Path,
    *,
    file_hash: str,
    info: StaticImageInfo,
) -> ImagePreview | None:
    """Create a derived preview inside the retained run without mutating raw assets."""

    preview_dir = _safe_preview_dir(input_dir)
    if preview_dir is None:
        return None
    destination = preview_dir / f"{file_hash}{info.suffix}"
    if destination.exists():
        existing = inspect_static_image(destination)
        key = _relative_key(input_dir, destination)
        if existing is None or existing.content_type != info.content_type or key is None:
            return None
        return ImagePreview(
            key=key,
            file_hash=sha256_path(destination),
            content_type=existing.content_type,
        )
    temporary = preview_dir / f".{destination.name}.{os.getpid()}.part"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as opened:
                if int(getattr(opened, "n_frames", 1)) != 1:
                    return None
                image = ImageOps.exif_transpose(opened)
                image.thumbnail((PREVIEW_MAX_EDGE, PREVIEW_MAX_EDGE))
                save_kwargs: dict[str, Any]
                if info.content_type == "image/jpeg":
                    if image.mode != "RGB":
                        image = image.convert("RGB")
                    save_kwargs = {"format": "JPEG", "quality": 85, "optimize": True}
                elif info.content_type == "image/png":
                    save_kwargs = {"format": "PNG", "optimize": True}
                else:
                    save_kwargs = {"format": "WEBP", "quality": 85, "method": 6}
                image.save(temporary, **save_kwargs)
        preview_info = inspect_static_image(temporary)
        if preview_info is None:
            return None
        temporary.replace(destination)
        key = _relative_key(input_dir, destination)
        if key is None:
            return None
        return ImagePreview(
            key=key,
            file_hash=sha256_path(destination),
            content_type=preview_info.content_type,
        )
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return None
    finally:
        temporary.unlink(missing_ok=True)


def visual_asset_payload(
    knowledge_base_id: str,
    asset: Asset,
    *,
    detailed: bool,
    include_block_id: bool = False,
) -> dict[str, Any] | None:
    if asset.asset_type not in VISUAL_ASSET_TYPES:
        return None
    available = bool(asset.file_path and asset.content_type in ALLOWED_IMAGE_CONTENT_TYPES)
    base_url = (
        f"/api/knowledge-bases/{knowledge_base_id}/documents/"
        f"{asset.document_id}/assets/{asset.id}"
    )
    preview_key = asset.metadata.get("preview_file_path")
    preview_hash = asset.metadata.get("preview_file_hash")
    preview_content_type = asset.metadata.get("preview_content_type")
    payload: dict[str, Any] = {
        "asset_id": asset.id,
        "modality": asset.asset_type,
        "asset_url": base_url if available else None,
        "preview_url": (
            f"{base_url}?variant=preview"
            if (
                available
                and isinstance(preview_key, str)
                and preview_key
                and isinstance(preview_hash, str)
                and preview_hash
                and preview_content_type in ALLOWED_IMAGE_CONTENT_TYPES
            )
            else None
        ),
    }
    if include_block_id:
        payload["block_id"] = asset.block_id
    if detailed:
        payload.update(
            {
                "content_type": asset.content_type,
                "file_hash": asset.file_hash,
                "width": _positive_int(asset.metadata.get("width")),
                "height": _positive_int(asset.metadata.get("height")),
                "caption": asset.related_text,
            }
        )
    return payload


def resolve_managed_image_asset(
    managed_root: Path,
    mineru_result_dir: str | None,
    key: str | None,
    *,
    expected_content_type: str | None,
    expected_file_hash: str | None,
) -> ResolvedImageAsset | None:
    if (
        not mineru_result_dir
        or not key
        or expected_content_type not in ALLOWED_IMAGE_CONTENT_TYPES
        or not isinstance(expected_file_hash, str)
        or len(expected_file_hash) != 64
    ):
        return None
    relative = Path(key)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        return None
    try:
        root = managed_root.resolve(strict=True)
        result_root_lexical = Path(mineru_result_dir)
        lexical_relative = result_root_lexical.relative_to(root)
        lexical_cursor = root
        for part in lexical_relative.parts:
            lexical_cursor = lexical_cursor / part
            if lexical_cursor.is_symlink():
                return None
        result_root = result_root_lexical.resolve(strict=True)
        result_root.relative_to(root)
    except (FileNotFoundError, ValueError, OSError):
        return None
    current = result_root_lexical
    if current.is_symlink():
        return None
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return None
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(result_root)
    except (FileNotFoundError, ValueError, OSError):
        return None
    if not resolved.is_file():
        return None
    info = inspect_static_image(resolved)
    if info is None or info.content_type != expected_content_type:
        return None
    try:
        actual_hash = sha256_path(resolved)
    except OSError:
        return None
    if not hmac.compare_digest(actual_hash, expected_file_hash.casefold()):
        return None
    return ResolvedImageAsset(path=resolved, info=info, file_hash=actual_hash)


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_preview_dir(input_dir: Path) -> Path | None:
    root = input_dir.resolve()
    current = input_dir
    for part in (".traceable-rag", "previews"):
        current = current / part
        if current.is_symlink():
            return None
        try:
            current.mkdir(exist_ok=True)
        except OSError:
            return None
        try:
            current.resolve().relative_to(root)
        except (ValueError, OSError):
            return None
    return current


def _relative_key(input_dir: Path, path: Path) -> str | None:
    try:
        return path.resolve().relative_to(input_dir.resolve()).as_posix()
    except (ValueError, OSError):
        return None


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
