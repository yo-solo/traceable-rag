from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "resume-release.yml"


def _read() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _step(workflow: str, name: str) -> str:
    marker = f"      - name: {name}\n"
    start = workflow.index(marker)
    end = workflow.find("\n      - ", start + len(marker))
    return workflow[start:] if end == -1 else workflow[start:end]


def test_resume_release_accepts_only_tag_and_source_run_inputs() -> None:
    workflow = _read()

    assert "workflow_dispatch:" in workflow
    assert "release_tag:" in workflow
    assert "source_run_id:" in workflow
    assert "digest:" not in workflow.split("permissions:", 1)[0]
    assert "actions: read" in workflow
    assert "contents: write" in workflow
    assert "packages: write" in workflow
    assert "secrets." not in workflow
    assert workflow.count("github.token") >= 4
    assert "41dc92e" not in workflow
    assert "32450547235" not in workflow


def test_resume_release_proves_tag_run_and_artifact_before_download() -> None:
    workflow = _read()
    source = _step(workflow, "Resolve and verify the release source commit")
    provenance = _step(workflow, "Verify the failed source run and its unique artifact")
    download = workflow.index("Download only the proven source-run artifact")

    assert 'git rev-parse "refs/tags/$RELEASE_TAG^{commit}"' in source
    assert "git merge-base --is-ancestor" in source
    assert "git push" not in workflow
    assert "git tag" not in workflow
    for required in (
        "getWorkflowRun",
        "listWorkflowRunArtifacts",
        'workflow_id: ".github/workflows/release.yml"',
        'run.event !== "push"',
        'run.conclusion !== "failure"',
        "run.head_branch !== process.env.RELEASE_TAG",
        "run.head_sha !== process.env.SOURCE_COMMIT",
        "run.repository?.full_name",
        "run.head_repository?.full_name",
    ):
        assert required in provenance
    assert "artifact-ids: ${{ steps.provenance.outputs.artifact_id }}" in workflow
    assert 'core.setOutput("artifact_digest", matches[0].digest)' in workflow
    assert workflow.index("Verify the failed source run") < download


def test_resume_release_reverifies_artifact_and_registry_identity() -> None:
    workflow = _read()
    artifact = _step(workflow, "Re-verify the accepted artifact without registry access")
    registry = _step(workflow, "Match registry objects to the accepted artifact bytes")
    login = workflow.index("Authenticate to GHCR with the ephemeral workflow token")

    assert 'acceptance.get("ok") is not True' in artifact
    assert 'smoke.get("ok") is not True' in artifact
    assert 'get("status") != "passed"' in artifact
    assert artifact.count("verify_release_image_identity.py platform") == 2
    assert artifact.count("verify_release_image_identity.py index") == 1
    assert workflow.index("Re-verify the accepted artifact") < login
    assert registry.count("verify_release_image_identity.py platform") == 1
    assert registry.count("verify_release_image_identity.py index") == 1
    # The platform comparison is inside a two-platform loop; the second cmp is
    # the accepted index, so the workflow performs three byte comparisons.
    assert registry.count("cmp ") == 2
    assert '"$IMAGE@$INDEX_DIGEST"' in registry
    assert "docker buildx build" not in workflow


def test_resume_release_keeps_version_immutable_and_aliases_last() -> None:
    workflow = _read()
    detect = workflow.index("Detect the immutable version image")
    package = workflow.index("Build the eight release assets")
    immutable = _step(workflow, "Publish or reuse the immutable version image")
    draft = _step(
        workflow,
        "Create or update the draft GitHub Release with exactly eight assets",
    )
    aliases = _step(
        workflow,
        "Publish mutable aliases only after the draft Release succeeds",
    )
    verify_aliases = _step(workflow, "Verify every recovered mutable alias")

    assert detect < package < workflow.index("Publish or reuse the immutable version image")
    assert immutable.count("docker buildx imagetools create") == 1
    assert '"$IMAGE@$INDEX_DIGEST"' in immutable
    assert "--expected-index-digest" in immutable
    assert "gh release create" in draft
    assert "--verify-tag" in draft
    assert "--draft" in draft
    assert "--clobber" not in draft
    assert "releases/tags/" not in draft
    assert "gh release upload" not in draft
    assert "--paginate --slurp" in draft
    assert "releases?per_page=100" in draft
    assert "Expected exactly one Release matching tag" in draft
    assert "releases/$release_id" in draft
    assert "uploads.github.com/repos/$GITHUB_REPOSITORY/releases/$release_id/assets" in draft
    assert "Existing draft contains unexpected asset" in draft
    assert "exactly eight expected assets" in draft
    assert aliases.count("docker buildx imagetools create") == 1
    assert aliases.count("--tag") == 3
    assert workflow.index("draft GitHub Release") < workflow.index(
        "Publish mutable aliases only"
    )
    assert "verify_release_image_identity.py descriptor" in verify_aliases
    assert "--expected-digest \"$INDEX_DIGEST\"" in verify_aliases


def test_resume_release_builds_reproducible_archives_from_the_source_epoch(
    tmp_path: Path,
) -> None:
    workflow = _read()
    source = _step(workflow, "Resolve and verify the release source commit")
    package = _step(workflow, "Build the eight release assets from verified digests")

    assert 'git show -s --format=%ct "$source_commit"' in source
    assert 'echo "source_date_epoch=$source_date_epoch" >> "$GITHUB_OUTPUT"' in source
    for marker in (
        "SOURCE_DATE_EPOCH: ${{ steps.source.outputs.source_date_epoch }}",
        'export TZ=UTC LC_ALL=C',
        'touch -h -d "@$SOURCE_DATE_EPOCH"',
        "--sort=name",
        '--mtime="@$SOURCE_DATE_EPOCH"',
        "--owner=0",
        "--group=0",
        "--numeric-owner",
        "--format=gnu",
        "gzip -n",
        "find deploy -print",
        "LC_ALL=C sort",
        "zip -X -q",
    ):
        assert marker in package

    tar_version = subprocess.run(
        ["tar", "--version"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout
    if "GNU tar" not in tar_version:
        pytest.skip("the executable archive check requires GNU tar, as used by ubuntu-latest")
    if shutil.which("zip") is None or shutil.which("gzip") is None:
        pytest.skip("the executable archive check requires zip and gzip")

    source_epoch = "1700000000"
    hashes: list[tuple[str, str]] = []
    for attempt, initial_epoch in (("first", 1600000000), ("second", 1800000000)):
        stage = tmp_path / attempt / "stage"
        output = tmp_path / attempt / "output"
        deploy = stage / "deploy"
        deploy.mkdir(parents=True)
        output.mkdir()
        for name in ("compose.yaml", "traceable-rag", "traceable-rag.ps1"):
            shutil.copyfile(ROOT / "deploy" / name, deploy / name)
        shutil.copyfile(ROOT / "install.sh", stage / "install.sh")
        shutil.copyfile(ROOT / "install.ps1", stage / "install.ps1")
        (deploy / "release-manifest.json").write_text(
            '{"schema_version":1,"version":"v0.5.0"}\n',
            encoding="utf-8",
        )
        for path in (stage, *stage.rglob("*")):
            os.utime(path, (initial_epoch, initial_epoch), follow_symlinks=False)

        env = os.environ.copy()
        env.update(
            {
                "STAGE": str(stage),
                "OUTPUT": str(output),
                "SOURCE_DATE_EPOCH": source_epoch,
            }
        )
        subprocess.run(
            [
                "bash",
                "-o",
                "pipefail",
                "-c",
                r'''
set -euo pipefail
find "$STAGE/deploy" -type d -exec chmod 0755 {} +
find "$STAGE/deploy" -type f -exec chmod 0644 {} +
chmod 0755 "$STAGE/install.sh" "$STAGE/deploy/traceable-rag"
chmod 0644 "$STAGE/install.ps1"
export TZ=UTC LC_ALL=C
find "$STAGE" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +
tar \
  --sort=name \
  --mtime="@$SOURCE_DATE_EPOCH" \
  --owner=0 \
  --group=0 \
  --numeric-owner \
  --format=gnu \
  -C "$STAGE" \
  -cf - deploy \
  | gzip -n > "$OUTPUT/deployment.tar.gz"
(
  cd "$STAGE"
  find deploy -print \
    | LC_ALL=C sort \
    | zip -X -q "$OUTPUT/deployment.zip" -@
)
''',
            ],
            check=True,
            env=env,
        )
        hashes.append(
            tuple(
                hashlib.sha256((output / name).read_bytes()).hexdigest()
                for name in ("deployment.tar.gz", "deployment.zip")
            )
        )

    assert hashes[0] == hashes[1]
