from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

import traceable_rag
from traceable_rag.openapi import build_openapi_spec


ROOT = Path(__file__).resolve().parents[1]


def validate_versions(
    *,
    tag_version: str,
    project_version: str,
    package_version: str,
    openapi_version: str,
) -> str:
    versions = {
        "tag": tag_version,
        "pyproject": project_version,
        "package": package_version,
        "openapi": openapi_version,
    }
    if len(set(versions.values())) != 1:
        raise ValueError(f"release version mismatch: {versions}")
    return project_version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate all release version sources.")
    parser.add_argument("--tag-version", required=True)
    args = parser.parse_args(argv)

    with (ROOT / "pyproject.toml").open("rb") as source:
        project_version = tomllib.load(source)["project"]["version"]
    version = validate_versions(
        tag_version=args.tag_version,
        project_version=project_version,
        package_version=traceable_rag.__version__,
        openapi_version=build_openapi_spec()["info"]["version"],
    )
    print(f"release version verified: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
