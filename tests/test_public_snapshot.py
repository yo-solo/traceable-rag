import hashlib
import json
from pathlib import Path

from scripts.check_public_snapshot import REQUIRED_FILES, validate_public_snapshot


def _write_minimum_snapshot(root: Path) -> Path:
    for relative in REQUIRED_FILES - {Path("examples/vidore/manifest.json")}:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("public\n", encoding="utf-8")

    sample = root / "examples/vidore/documents/sample.pdf"
    sample.parent.mkdir(parents=True)
    sample.write_bytes(b"%PDF-1.4\npublic sample\n")
    digest = hashlib.sha256(sample.read_bytes()).hexdigest()
    manifest = {
        "samples": [
            {
                "id": "sample",
                "dataset_id": "vidore/example",
                "dataset_revision": "revision",
                "source_document": "sample.pdf",
                "source_url": "https://example.com/sample.pdf",
                "source_pages": [1],
                "license": "Public Domain",
                "attribution": "Example source",
                "file": "examples/vidore/documents/sample.pdf",
                "sha256": digest,
            }
        ]
    }
    manifest_path = root / "examples/vidore/manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return sample


def test_public_snapshot_accepts_declared_hashed_samples(tmp_path):
    _write_minimum_snapshot(tmp_path)

    assert validate_public_snapshot(tmp_path) == []


def test_public_snapshot_rejects_private_paths_and_markers(tmp_path):
    _write_minimum_snapshot(tmp_path)
    private = tmp_path / "example/private.docx"
    private.parent.mkdir(parents=True)
    private.write_bytes(b"private")
    readme = tmp_path / "docs/note.md"
    readme.parent.mkdir()
    private_home = Path("/", "Users", "snapshot-sentinel", "private")
    readme.write_text(f"local path: {private_home}", encoding="utf-8")
    internal_guide = (
        tmp_path / "docs/evaluation/vidore-finance-en-mineru-pc-guide.md"
    )
    internal_guide.parent.mkdir(parents=True, exist_ok=True)
    nas_host = "-".join(("nebula-sentinel", "nas"))
    internal_volume = Path("/", "volume73", "internal", "transfer")
    internal_guide.write_text(
        f"Transfer artifacts through {nas_host} under {internal_volume}.",
        encoding="utf-8",
    )
    repository_note = tmp_path / "docs/repository.md"
    non_public_slug = "/".join(("yo-solo", "private-source-sentinel"))
    repository_note.write_text(
        f"Source mirror: https://github.com/{non_public_slug}\n",
        encoding="utf-8",
    )

    errors = validate_public_snapshot(tmp_path)

    assert any("forbidden private path" in error for error in errors)
    assert any("forbidden file type" in error for error in errors)
    assert any("private marker" in error for error in errors)
    assert any("vidore-finance-en-mineru-pc-guide.md" in error for error in errors)
    assert any("NAS hostname" in error for error in errors)
    assert any("NAS internal volume path" in error for error in errors)
    assert any("non-public GitHub repository reference" in error for error in errors)


def test_public_snapshot_scans_hidden_config_dockerfile_and_extensionless_scripts(
    tmp_path,
):
    _write_minimum_snapshot(tmp_path)
    private_home = Path("/", "Users", "snapshot-config-sentinel", "private")
    candidates = (
        Path(".gitignore"),
        Path(".dockerignore"),
        Path(".env.local.example"),
        Path("Dockerfile"),
        Path("deploy/public-snapshot-check"),
    )
    for relative in candidates:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"sentinel={private_home}\n", encoding="utf-8")

    errors = validate_public_snapshot(tmp_path)

    for relative in candidates:
        assert any(
            "personal macOS home path" in error and relative.as_posix() in error
            for error in errors
        )


def test_public_snapshot_rejects_tampered_or_undeclared_samples(tmp_path):
    sample = _write_minimum_snapshot(tmp_path)
    sample.write_bytes(b"tampered")
    (sample.parent / "undeclared.pdf").write_bytes(b"%PDF")

    errors = validate_public_snapshot(tmp_path)

    assert any("checksum does not match" in error for error in errors)
    assert any("undeclared ViDoRe PDF" in error for error in errors)


def test_public_snapshot_rejects_missing_or_escaping_readme_links(tmp_path):
    _write_minimum_snapshot(tmp_path)
    (tmp_path / "README.md").write_text(
        "[missing](docs/missing.md) [escape](../private.md)\n",
        encoding="utf-8",
    )

    errors = validate_public_snapshot(tmp_path)

    assert any("README link target is missing" in error for error in errors)
    assert any("README link escapes" in error for error in errors)
