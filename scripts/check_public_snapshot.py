from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlsplit


REQUIRED_FILES = {
    Path("LICENSE"),
    Path("TRADEMARKS.md"),
    Path("SECURITY.md"),
    Path("CONTRIBUTING.md"),
    Path("CODE_OF_CONDUCT.md"),
    Path("THIRD_PARTY_NOTICES.md"),
    Path("README.md"),
    Path("README.zh-CN.md"),
    Path("examples/vidore/manifest.json"),
}

FORBIDDEN_PREFIXES = (
    Path("example"),
    Path("docs/design"),
    Path("docs/superpowers"),
    Path("docs/evaluation/vidore-finance-en-mineru-pc-guide.md"),
    Path("storage"),
    Path(".artifacts"),
    Path(".runtime"),
)

FORBIDDEN_SUFFIXES = {
    ".db",
    ".doc",
    ".docx",
    ".sqlite",
    ".sqlite3",
}

FORBIDDEN_BASENAMES = {
    ".DS_Store",
    ".env",
    "重点外部 API.example.txt",
    "问题记录.md",
}

KNOWN_TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsonc",
    ".jsonl",
    ".md",
    ".mjs",
    ".ps1",
    ".py",
    ".sh",
    ".svg",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}

TEXT_SCAN_EXCLUSIONS = {
    Path("scripts/check_public_snapshot.py"),
}

# These exact values identify material that belongs only in the private repository.
# The checker is the sole text-scan exclusion, so declarations remain maintainable
# without weakening coverage for tests or other repository files.
FORBIDDEN_TEXT = (
    "example/示例文档",
    "example/mineru 解析结果示例",
    "traceable-rag-internal",
    "冀北",
    "求职项目叙事",
    "项目路线图与任务清单",
    "Rainbound交接",
    "处理负荷突增问题和行业数据拟合调度数据",
)

FORBIDDEN_TEXT_PATTERNS = (
    (
        "personal macOS home path",
        re.compile(r"(?<![A-Za-z0-9._-])/Users/[A-Za-z0-9._-]+(?:/|\b)"),
    ),
    (
        "personal Windows home path",
        re.compile(r"\b[A-Za-z]:\\Users\\[^\\\s\"']+", re.IGNORECASE),
    ),
    (
        "NAS hostname",
        re.compile(
            r"(?<![A-Za-z0-9._-])[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?-nas\b",
            re.IGNORECASE,
        ),
    ),
    (
        "NAS internal volume path",
        re.compile(r"(?<![A-Za-z0-9._-])/volume\d+/internal(?:/|\b)", re.IGNORECASE),
    ),
    (
        "private deployment subnet",
        re.compile(r"\b192\.168\.31\.\d{1,3}\b"),
    ),
)

README_FILES = (Path("README.md"), Path("README.zh-CN.md"))
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
HTML_LINK_RE = re.compile(r"\b(?:href|src)=[\"']([^\"']+)[\"']")


def _is_relative_to(path: Path, prefix: Path) -> bool:
    return path == prefix or prefix in path.parents


def _iter_public_files(root: Path) -> Iterable[Path]:
    git_files = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        check=False,
        capture_output=True,
    )
    if git_files.returncode == 0:
        for raw_path in sorted(git_files.stdout.split(b"\0")):
            if not raw_path:
                continue
            relative = Path(raw_path.decode("utf-8"))
            path = root / relative
            if path.is_symlink() or path.is_file():
                yield relative
        return

    ignored_parts = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in ignored_parts for part in relative.parts):
            continue
        if path.is_symlink() or path.is_file():
            yield relative


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_expected_utf8_text(relative: Path) -> bool:
    name = relative.name
    return (
        relative.suffix.casefold() in KNOWN_TEXT_SUFFIXES
        or not relative.suffix
        or name in {".dockerignore", ".gitignore", "Dockerfile"}
        or (name.startswith(".env") and name.endswith(".example"))
    )


def _validate_vidore_manifest(root: Path) -> list[str]:
    manifest_path = root / "examples/vidore/manifest.json"
    if not manifest_path.is_file():
        return []
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"invalid ViDoRe manifest: {exc}"]

    samples = payload.get("samples")
    if not isinstance(samples, list) or not samples:
        return ["ViDoRe manifest must declare at least one sample"]

    errors: list[str] = []
    declared: set[Path] = set()
    required_fields = {
        "id",
        "dataset_id",
        "dataset_revision",
        "source_document",
        "source_url",
        "source_pages",
        "license",
        "attribution",
        "file",
        "sha256",
    }
    for index, sample in enumerate(samples):
        label = f"ViDoRe sample {index + 1}"
        if not isinstance(sample, dict):
            errors.append(f"{label} is not an object")
            continue
        missing = sorted(required_fields - sample.keys())
        if missing:
            errors.append(f"{label} is missing fields: {', '.join(missing)}")
            continue
        relative = Path(str(sample["file"]))
        if relative.is_absolute() or ".." in relative.parts:
            errors.append(f"{label} file escapes the repository")
            continue
        if not _is_relative_to(relative, Path("examples/vidore/documents")):
            errors.append(f"{label} file must be under examples/vidore/documents")
            continue
        declared.add(relative)
        sample_path = root / relative
        if not sample_path.is_file():
            errors.append(f"{label} file is missing: {relative.as_posix()}")
            continue
        expected_hash = str(sample["sha256"]).casefold()
        if len(expected_hash) != 64 or _sha256(sample_path) != expected_hash:
            errors.append(f"{label} checksum does not match")
        if not str(sample["dataset_revision"]).strip():
            errors.append(f"{label} dataset revision is empty")
        if not str(sample["license"]).strip():
            errors.append(f"{label} license is empty")

    document_root = root / "examples/vidore/documents"
    actual = (
        {path.relative_to(root) for path in document_root.glob("*.pdf")}
        if document_root.is_dir()
        else set()
    )
    for undeclared in sorted(actual - declared):
        errors.append(f"undeclared ViDoRe PDF: {undeclared.as_posix()}")
    return errors


def _validate_readme_links(root: Path) -> list[str]:
    errors: list[str] = []
    for relative in README_FILES:
        readme = root / relative
        if not readme.is_file():
            continue
        text = readme.read_text(encoding="utf-8")
        targets = MARKDOWN_LINK_RE.findall(text) + HTML_LINK_RE.findall(text)
        for raw_target in targets:
            target = raw_target.strip().strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or target.startswith("#"):
                continue
            local_path = unquote(parsed.path)
            if not local_path:
                continue
            candidate = (readme.parent / local_path).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                errors.append(
                    f"README link escapes the public snapshot: {relative.as_posix()} -> {target}"
                )
                continue
            if not candidate.exists():
                errors.append(
                    f"README link target is missing: {relative.as_posix()} -> {target}"
                )
    return errors


def validate_public_snapshot(root: Path) -> list[str]:
    root = root.resolve()
    files = set(_iter_public_files(root))
    errors = [
        f"required public file is missing: {path.as_posix()}"
        for path in sorted(REQUIRED_FILES - files)
    ]

    for relative in sorted(files):
        path = root / relative
        if path.is_symlink():
            errors.append(f"symlinks are not allowed in the public snapshot: {relative.as_posix()}")
            continue
        if relative.name in FORBIDDEN_BASENAMES:
            errors.append(f"forbidden file: {relative.as_posix()}")
        if relative.suffix.casefold() in FORBIDDEN_SUFFIXES:
            errors.append(f"forbidden file type: {relative.as_posix()}")
        if any(_is_relative_to(relative, prefix) for prefix in FORBIDDEN_PREFIXES):
            errors.append(f"forbidden private path: {relative.as_posix()}")
        if relative in TEXT_SCAN_EXCLUSIONS:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            if not _is_expected_utf8_text(relative):
                continue
            errors.append(f"could not scan text file {relative.as_posix()}: {exc}")
            continue
        except OSError as exc:
            errors.append(f"could not scan text file {relative.as_posix()}: {exc}")
            continue
        for marker in FORBIDDEN_TEXT:
            if marker in text:
                errors.append(
                    f"private marker {marker!r} found in {relative.as_posix()}"
                )
        for label, pattern in FORBIDDEN_TEXT_PATTERNS:
            match = pattern.search(text)
            if match is not None:
                errors.append(
                    f"private marker ({label}) found in {relative.as_posix()}"
                )

    errors.extend(_validate_vidore_manifest(root))
    errors.extend(_validate_readme_links(root))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reject private material and unlicensed samples from a public snapshot."
    )
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    errors = validate_public_snapshot(Path(args.root))
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Public snapshot validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
