from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any


_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MANIFEST_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
}
_INDEX_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
}


def _load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _load_bytes(path: str | Path) -> bytes:
    return Path(path).read_bytes()


def _require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ValueError(f"{label} must be a sha256 digest")
    return value


def _require_descriptor(
    payload: object,
    *,
    label: str,
    media_types: set[str],
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError(f"{label} descriptor must be an object")
    _require_digest(payload.get("digest"), f"{label} descriptor digest")
    media_type = payload.get("mediaType")
    if media_type not in media_types:
        raise ValueError(f"{label} descriptor has unexpected media type: {media_type!r}")
    size = payload.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ValueError(f"{label} descriptor size must be a positive integer")
    return payload


def _verify_raw_descriptor(
    raw: bytes,
    descriptor: dict[str, Any],
    *,
    label: str,
) -> None:
    expected_size = descriptor["size"]
    if len(raw) != expected_size:
        raise ValueError(
            f"{label} raw size differs from its registry descriptor: "
            f"expected {expected_size}, got {len(raw)}"
        )
    expected_digest = descriptor["digest"]
    actual_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if actual_digest != expected_digest:
        raise ValueError(f"{label} raw digest differs from its registry descriptor")


def verify_platform_image(
    *,
    local_image: object,
    descriptor: object,
    manifest_bytes: bytes,
    expected_image_id: str,
    expected_platform: str,
) -> str:
    expected_image_id = _require_digest(expected_image_id, "expected local image ID")
    if (
        not isinstance(local_image, list)
        or len(local_image) != 1
        or not isinstance(local_image[0], dict)
    ):
        raise ValueError("docker image inspect output must contain exactly one image")
    local = local_image[0]
    local_image_id = _require_digest(local.get("Id"), "local image ID")
    if local_image_id != expected_image_id:
        raise ValueError("the local candidate image changed after acceptance")
    local_platform = f"{local.get('Os', '')}/{local.get('Architecture', '')}"
    if local_platform != expected_platform:
        raise ValueError(
            f"local candidate platform is {local_platform!r}, expected {expected_platform!r}"
        )

    remote_descriptor = _require_descriptor(
        descriptor,
        label="platform image",
        media_types=_MANIFEST_MEDIA_TYPES,
    )
    _verify_raw_descriptor(
        manifest_bytes,
        remote_descriptor,
        label="platform manifest",
    )
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 2:
        raise ValueError("platform manifest must use schemaVersion 2")
    if manifest.get("mediaType") != remote_descriptor.get("mediaType"):
        raise ValueError("platform manifest media type does not match its descriptor")
    config = manifest.get("config")
    if not isinstance(config, dict):
        raise ValueError("platform manifest config descriptor is missing")
    remote_config_digest = _require_digest(
        config.get("digest"), "remote image config digest"
    )
    if remote_config_digest != expected_image_id:
        raise ValueError("the pushed platform image is not the accepted local image")
    layers = manifest.get("layers")
    if not isinstance(layers, list) or not layers:
        raise ValueError("platform manifest must contain image layers")
    for position, layer in enumerate(layers):
        if not isinstance(layer, dict):
            raise ValueError(f"platform layer {position} descriptor must be an object")
        _require_digest(layer.get("digest"), f"platform layer {position} digest")
    return _require_digest(
        remote_descriptor.get("digest"), "platform image descriptor digest"
    )


def _parse_expected_platforms(values: list[str]) -> dict[str, str]:
    expected: dict[str, str] = {}
    for value in values:
        platform, separator, digest = value.partition("=")
        if not separator or not platform:
            raise ValueError("expected platform must use PLATFORM=sha256:DIGEST")
        if platform in expected:
            raise ValueError(f"duplicate expected platform: {platform}")
        expected[platform] = _require_digest(
            digest, f"expected digest for platform {platform}"
        )
    if not expected:
        raise ValueError("at least one expected platform is required")
    return expected


def verify_image_index(
    *,
    descriptor: object,
    index_bytes: bytes,
    expected_platforms: dict[str, str],
    expected_index_digest: str | None = None,
    expected_source_commit: str | None = None,
) -> str:
    index_descriptor = _require_descriptor(
        descriptor,
        label="image index",
        media_types=_INDEX_MEDIA_TYPES,
    )
    _verify_raw_descriptor(index_bytes, index_descriptor, label="image index")
    index = json.loads(index_bytes)
    index_digest = _require_digest(
        index_descriptor.get("digest"), "image index descriptor digest"
    )
    if expected_index_digest is not None:
        expected_index_digest = _require_digest(
            expected_index_digest, "expected image index digest"
        )
        if index_digest != expected_index_digest:
            raise ValueError("published tag does not reference the accepted image index")

    if not isinstance(index, dict) or index.get("schemaVersion") != 2:
        raise ValueError("image index must use schemaVersion 2")
    if index.get("mediaType") != index_descriptor.get("mediaType"):
        raise ValueError("image index media type does not match its descriptor")
    if expected_source_commit is not None:
        if not re.fullmatch(r"[0-9a-f]{40}", expected_source_commit):
            raise ValueError("expected source commit must be a full Git commit ID")
        annotations = index.get("annotations")
        if not isinstance(annotations, dict):
            raise ValueError("image index source annotations are missing")
        if annotations.get("org.opencontainers.image.revision") != expected_source_commit:
            raise ValueError("image index source commit does not match GITHUB_SHA")
    manifests = index.get("manifests")
    if not isinstance(manifests, list):
        raise ValueError("image index manifests must be a list")

    published: dict[str, str] = {}
    for position, item in enumerate(manifests):
        if not isinstance(item, dict):
            raise ValueError(f"image index manifest {position} must be an object")
        platform = item.get("platform")
        if not isinstance(platform, dict):
            raise ValueError(f"image index manifest {position} has no platform")
        platform_name = f"{platform.get('os', '')}/{platform.get('architecture', '')}"
        if platform_name in published:
            raise ValueError(f"image index repeats platform {platform_name}")
        if item.get("mediaType") not in _MANIFEST_MEDIA_TYPES:
            raise ValueError(
                f"image index platform {platform_name} has an unexpected media type"
            )
        published[platform_name] = _require_digest(
            item.get("digest"), f"published digest for platform {platform_name}"
        )

    if published != expected_platforms:
        raise ValueError(
            f"published platform digests differ: expected {expected_platforms!r}, "
            f"got {published!r}"
        )
    return index_digest


def verify_descriptor_digest(*, descriptor: object, expected_digest: str) -> str:
    expected_digest = _require_digest(expected_digest, "expected descriptor digest")
    payload = _require_descriptor(
        descriptor,
        label="published tag",
        media_types=_INDEX_MEDIA_TYPES,
    )
    actual_digest = _require_digest(
        payload.get("digest"), "published tag descriptor digest"
    )
    if actual_digest != expected_digest:
        raise ValueError("published tag does not reference the accepted image index")
    return actual_digest


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify that published OCI objects retain accepted local image identity."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    platform = subparsers.add_parser("platform")
    platform.add_argument("--local-image-json", required=True)
    platform.add_argument("--descriptor-json", required=True)
    platform.add_argument("--manifest-json", required=True)
    platform.add_argument("--expected-image-id", required=True)
    platform.add_argument("--expected-platform", required=True)

    index = subparsers.add_parser("index")
    index.add_argument("--descriptor-json", required=True)
    index.add_argument("--index-json", required=True)
    index.add_argument("--expected-platform", action="append", default=[])
    index.add_argument("--expected-index-digest")
    index.add_argument("--expected-source-commit")

    descriptor = subparsers.add_parser("descriptor")
    descriptor.add_argument("--descriptor-json", required=True)
    descriptor.add_argument("--expected-digest", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "platform":
            digest = verify_platform_image(
                local_image=_load_json(args.local_image_json),
                descriptor=_load_json(args.descriptor_json),
                manifest_bytes=_load_bytes(args.manifest_json),
                expected_image_id=args.expected_image_id,
                expected_platform=args.expected_platform,
            )
        elif args.command == "index":
            digest = verify_image_index(
                descriptor=_load_json(args.descriptor_json),
                index_bytes=_load_bytes(args.index_json),
                expected_platforms=_parse_expected_platforms(
                    args.expected_platform
                ),
                expected_index_digest=args.expected_index_digest,
                expected_source_commit=args.expected_source_commit,
            )
        else:
            digest = verify_descriptor_digest(
                descriptor=_load_json(args.descriptor_json),
                expected_digest=args.expected_digest,
            )
    except (OSError, json.JSONDecodeError, ValueError) as error:
        parser = _build_parser()
        parser.error(str(error))
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
