from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import tomllib

import pytest

from scripts import release_api_smoke, verify_release_image_identity


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _workflow_step(workflow: str, name: str) -> str:
    marker = f"      - name: {name}\n"
    start = workflow.index(marker)
    end = workflow.find("\n      - ", start + len(marker))
    return workflow[start:] if end == -1 else workflow[start:end]


def _normalize_release_asset(name: str) -> str:
    return (
        name.strip()
        .strip('"')
        .replace("$version", "{version}")
        .replace("${{ github.ref_name }}", "{version}")
    )


def test_release_version_is_consistent_across_package_and_openapi() -> None:
    from traceable_rag import __version__
    from traceable_rag.web_app import app

    project = tomllib.loads(_read("pyproject.toml"))["project"]
    web_package = json.loads(_read("web/package.json"))
    web_lock = json.loads(_read("web/package-lock.json"))
    evaluation = tomllib.loads(_read("evaluation/pyproject.toml"))["project"]
    skill = _read("integrations/codex-skill/traceable-rag/SKILL.md")
    assert project["version"] == "0.5.0"
    assert __version__ == project["version"]
    assert app.openapi()["info"]["version"] == project["version"]
    assert web_package["version"] == project["version"]
    assert web_lock["version"] == project["version"]
    assert web_lock["packages"][""]["version"] == project["version"]
    assert f"traceable-rag=={project['version']}" in evaluation["dependencies"]
    assert f"Skill version: `{project['version']}`" in skill


def test_docker_and_compose_keep_the_runtime_health_contract() -> None:
    dockerfile = _read("Dockerfile")
    compose = _read("compose.yaml")
    local_deploy = _read("scripts/deploy_local.sh")
    for marker in (
        "FROM node:22",
        "FROM python:3.12",
        "npm ci",
        "npm run build",
        "USER traceable-rag",
        "HEALTHCHECK",
        "http://127.0.0.1:8000/readyz",
    ):
        assert marker in dockerfile
    assert '"127.0.0.1:${TRACEABLE_RAG_PORT:-8000}:8000"' in compose
    assert "no-new-privileges:true" in compose
    assert "http://127.0.0.1:8000/readyz" in compose
    assert "TRACEABLE_RAG_PROVIDER_STORE_PATH" in compose
    assert "TRACEABLE_RAG_MASTER_KEY_FILE" in compose
    assert "TRACEABLE_RAG_ADMIN_TOKEN_FILE" in compose
    assert "TRACEABLE_RAG_TRUSTED_HOSTS" in compose
    assert "OPENAI_EMBEDDING_QUERY_PROMPT" in compose
    assert "TRACEABLE_RAG_RERANKER_API_KEY" in compose
    assert "TRACEABLE_RAG_LLM_API_KEY" in compose
    assert "--env-file .env" in local_deploy
    assert "--env-file .env.local" in local_deploy
    assert "dev-compose-user.yaml" in local_deploy
    assert "master.key" in local_deploy
    assert "admin-token" in local_deploy
    assert "platform: linux/arm64" not in _read("compose.local.yaml")
    assert "Dockerfile" not in local_deploy


def test_ci_and_release_workflows_use_current_retrieval_surfaces() -> None:
    ci = _read(".github/workflows/ci.yml")
    release = _read(".github/workflows/release.yml")
    for marker in (
        "pytest",
        "npm test",
        "npm run build",
        "test_openapi.py",
        "test_codex_skill_template.py",
        "/api/health",
        "/retrieval",
    ):
        assert marker in ci
    assert "scripts/release_api_smoke.py" in release
    assert "tests/test_deployment_experience.py" in release
    assert "tests/test_release_acceptance_seed.py" in release
    assert "sh -n install.sh deploy/traceable-rag" in release
    assert "fetch-depth: 0" in release
    assert "+refs/heads/main:refs/remotes/origin/main" in release
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main' in release
    assert "--platform linux/arm64" in release
    assert "http://127.0.0.1:18002/readyz" in release
    assert "libreoffice --version" in release
    assert "platforms: amd64,arm64" in release
    assert "softprops/action-gh-release@v2" in release
    assert "traceable-rag-deployment-${{ github.ref_name }}.tar.gz" in release
    assert "SHA256SUMS" in release
    assert release.index("Authenticate to GHCR only after acceptance passes") > release.index("Block critical arm64 candidate vulnerabilities")
    assert "TRACEABLE_RAG_DATABASE_PATH=/data/traceable_v3.db" in _read("Dockerfile")
    assert "--db /data/traceable_v3.db" in release
    assert "--db /data/traceable_v2.db" not in release
    assert "Retrieval and Source acceptance" in release
    assert "scripts/seed_release_acceptance.py" in release
    assert "$GITHUB_WORKSPACE/examples/vidore:/input:ro" in release
    assert "--manifest /input/manifest.json" in release
    assert "--expected-document-count 4" in release
    assert "/retrieval" in release
    assert '"http://127.0.0.1:18001/sources/$SOURCE_ID"' in release
    assert "/query" not in ci
    assert "/query" not in release
    assert "/traces" not in release


def test_public_release_is_provider_free_and_staged_as_a_draft() -> None:
    release = _read(".github/workflows/release.yml")
    acceptance = _workflow_step(
        release,
        "Build deterministic public ViDoRe acceptance corpus inside the candidate image",
    )
    runtime = _workflow_step(release, "Start the accepted candidate image")
    publish = _workflow_step(
        release, "Publish deployment bundle as GitHub Release assets"
    )

    secret_references = set(
        re.findall(r"\bsecrets\.[A-Za-z_][A-Za-z0-9_]*\b", release)
    )
    assert secret_references == {"secrets.GITHUB_TOKEN"}
    assert acceptance.count('--user "$(id -u):$(id -g)"') == 1
    assert acceptance.count("--network none") == 1
    assert runtime.count('--user "$(id -u):$(id -g)"') == 1
    assert "seed_release_acceptance.py" in acceptance
    assert "draft: true" in publish


def test_release_images_declare_the_public_source_before_the_first_push() -> None:
    dockerfile = _read("Dockerfile")
    release = _read(".github/workflows/release.yml")
    amd64_build = _workflow_step(
        release, "Build the candidate image without publishing it"
    )
    arm64_build = _workflow_step(
        release, "Build and run the arm64 candidate under QEMU"
    )

    assert "ARG TRACEABLE_RAG_SOURCE_URL=https://github.com/yo-solo/traceable-rag" in dockerfile
    assert 'org.opencontainers.image.source="$TRACEABLE_RAG_SOURCE_URL"' in dockerfile
    for build in (amd64_build, arm64_build):
        assert '--build-arg "TRACEABLE_RAG_SOURCE_URL=https://github.com/$GITHUB_REPOSITORY"' in build


def test_public_release_uses_an_isolated_ghcr_package() -> None:
    release = _read(".github/workflows/release.yml")
    expected = "ghcr.io/yo-solo/traceable-rag-public"

    assert (
        'image="ghcr.io/${GITHUB_REPOSITORY_OWNER,,}/traceable-rag-public"'
        in release
    )
    assert expected in _read(".github/workflows/ci.yml")
    assert expected in _read("install.sh")
    assert expected in _read("install.ps1")


def test_windows_ci_keeps_successful_installs_in_private_user_directories() -> None:
    ci = _read(".github/workflows/ci.yml")
    windows_job = ci.split("  windows-deployment:", 1)[1].split("\n  security:", 1)[0]
    packaged_install = _workflow_step(
        ci, "Run the packaged installer through an iex-equivalent path"
    )
    dry_run = _workflow_step(ci, "Dry-run the idempotent Windows installer without Docker")
    unmarked_directory = _workflow_step(
        ci, "Refuse to claim an existing unmarked Windows directory"
    )

    assert windows_job.isascii()
    assert "$env:LOCALAPPDATA" in packaged_install
    assert "$env:RUNNER_TEMP" not in packaged_install
    assert "0x53EF, 0x9A8C, 0x8BC1, 0x90E8, 0x7F72, 0x002D, 0x8DEF, 0x5F84" in packaged_install
    assert "$env:LOCALAPPDATA" in dry_run
    assert "$env:RUNNER_TEMP" not in dry_run
    assert "$env:RUNNER_TEMP" in unmarked_directory


def test_release_workflow_preserves_exact_accepted_image_identity() -> None:
    release = _read(".github/workflows/release.yml")

    amd64_build = _workflow_step(
        release, "Build the candidate image without publishing it"
    )
    arm64_build = _workflow_step(
        release, "Build and run the arm64 candidate under QEMU"
    )
    document_acceptance = _workflow_step(
        release,
        "Build deterministic public ViDoRe acceptance corpus inside the candidate image",
    )
    amd64_runtime = _workflow_step(release, "Start the accepted candidate image")
    amd64_sbom = _workflow_step(release, "Generate amd64 candidate SBOM")
    amd64_trivy = _workflow_step(
        release, "Block critical amd64 candidate vulnerabilities"
    )
    arm64_sbom = _workflow_step(release, "Generate arm64 candidate SBOM")
    arm64_trivy = _workflow_step(
        release, "Block critical arm64 candidate vulnerabilities"
    )
    push_platforms = _workflow_step(
        release, "Push the exact accepted platform images"
    )
    assemble_index = _workflow_step(
        release, "Assemble one accepted index from the platform digests"
    )
    detect_version = _workflow_step(
        release,
        "Detect an existing immutable version tag without masking registry errors",
    )
    publish_version = _workflow_step(
        release, "Publish or reuse the immutable version tag"
    )
    verify_version = _workflow_step(
        release, "Verify the published immutable version tag"
    )
    publish_aliases = _workflow_step(
        release, "Publish mutable aliases only after release assets succeed"
    )
    verify_aliases = _workflow_step(
        release, "Verify every mutable alias references the accepted index"
    )

    assert release.count("docker buildx build") == 2
    assert "docker/build-push-action" not in release
    assert "push: true" not in release
    assert release.index("docker buildx build") < release.index(
        "Authenticate to GHCR only after acceptance passes"
    )
    assert "docker buildx build" not in release[
        release.index("Authenticate to GHCR only after acceptance passes") :
    ]
    assert "--platform linux/amd64" in amd64_build
    assert "--tag traceable-rag:candidate" in amd64_build
    assert "--platform linux/arm64" in arm64_build
    assert "--tag traceable-rag:candidate-arm64" in arm64_build
    assert 'echo "image_id=$image_id" >> "$GITHUB_OUTPUT"' in amd64_build
    assert 'echo "image_id=$image_id" >> "$GITHUB_OUTPUT"' in arm64_build

    for accepted_amd64_step in (
        document_acceptance,
        amd64_runtime,
        amd64_sbom,
        amd64_trivy,
    ):
        assert "traceable-rag:candidate" in accepted_amd64_step
        assert "traceable-rag:candidate-arm64" not in accepted_amd64_step
    for accepted_arm64_step in (arm64_build, arm64_sbom, arm64_trivy):
        assert "traceable-rag:candidate-arm64" in accepted_arm64_step
    assert "http://127.0.0.1:18002/readyz" in arm64_build

    assert "${{ steps.build_amd64.outputs.image_id }}" in push_platforms
    assert "${{ steps.build_arm64.outputs.image_id }}" in push_platforms
    assert push_platforms.count("docker push") == 2
    assert "docker buildx build" not in push_platforms
    source_inspection = (
        "docker image inspect --format "
        "'{{ index .Config.Labels \"org.opencontainers.image.source\" }}'"
    )
    assert 'expected_source="https://github.com/yo-solo/traceable-rag"' in push_platforms
    assert push_platforms.count(source_inspection) == 2
    assert push_platforms.rindex(source_inspection) < push_platforms.index(
        "docker push"
    )
    assert push_platforms.count(
        "python scripts/verify_release_image_identity.py platform"
    ) == 2
    assert "--expected-platform linux/amd64" in push_platforms
    assert "--expected-platform linux/arm64" in push_platforms
    assert "GITHUB_RUN_ID" in release
    assert "GITHUB_RUN_ATTEMPT" in release
    assert "manifest unknown|MANIFEST_UNKNOWN|: not found" in detect_version
    assert "Could not safely determine whether" in detect_version
    assert 'echo "exists=false" >> "$GITHUB_OUTPUT"' in detect_version

    assert assemble_index.count("docker buildx imagetools create") == 1
    assert '"$IMAGE@$AMD64_DIGEST"' in assemble_index
    assert '"$IMAGE@$ARM64_DIGEST"' in assemble_index
    assert "--annotation" not in assemble_index
    assert "--expected-source-commit" not in assemble_index
    assert publish_version.count("docker buildx imagetools create") == 1
    assert publish_version.count("--tag") == 1
    assert "--prefer-index=false" in publish_version
    assert '"$IMAGE@$INDEX_DIGEST"' in publish_version
    assert '--expected-index-digest "$INDEX_DIGEST"' in publish_version
    assert "--expected-source-commit" not in publish_version

    assert '--expected-platform "linux/amd64=$AMD64_DIGEST"' in verify_version
    assert '--expected-platform "linux/arm64=$ARM64_DIGEST"' in verify_version
    assert '--expected-index-digest "$INDEX_DIGEST"' in verify_version
    assert "--expected-source-commit" not in verify_version
    assert publish_aliases.count("docker buildx imagetools create") == 1
    assert publish_aliases.count("--tag") == 3
    assert '"$IMAGE@$INDEX_DIGEST"' in publish_aliases
    assert "verify_release_image_identity.py descriptor" in verify_aliases

    ordered_steps = (
        "Build the candidate image without publishing it",
        "Build deterministic public ViDoRe acceptance corpus inside the candidate image",
        "Generate amd64 candidate SBOM",
        "Block critical amd64 candidate vulnerabilities",
        "Build and run the arm64 candidate under QEMU",
        "Generate arm64 candidate SBOM",
        "Block critical arm64 candidate vulnerabilities",
        "Authenticate to GHCR only after acceptance passes",
        "Detect an existing immutable version tag without masking registry errors",
        "Push the exact accepted platform images",
        "Assemble one accepted index from the platform digests",
        "Write the accepted release manifest and package installers",
        "Publish or reuse the immutable version tag",
        "Verify the published immutable version tag",
        "Publish deployment bundle as GitHub Release assets",
        "Publish mutable aliases only after release assets succeed",
        "Verify every mutable alias references the accepted index",
    )
    positions = [release.index(f"- name: {name}") for name in ordered_steps]
    assert positions == sorted(positions)


def test_release_manifest_bundle_and_asset_order_share_one_contract() -> None:
    release = _read(".github/workflows/release.yml")
    package = _workflow_step(
        release, "Write the accepted release manifest and package installers"
    )
    publish = _workflow_step(
        release, "Publish deployment bundle as GitHub Release assets"
    )

    for field in (
        '"schema_version": 1',
        '"version": os.environ["version"]',
        '"image": os.environ["IMAGE"]',
        '"index_digest": os.environ["index_digest"]',
        '"linux/amd64": os.environ["AMD64_DIGEST"]',
        '"linux/arm64": os.environ["ARM64_DIGEST"]',
        '"source_commit": os.environ["GITHUB_SHA"]',
    ):
        assert field in package
    assert 'index_digest="$INDEX_DIGEST"' in package
    assert "cp .artifacts/release/release-manifest.json \"$stage/deploy/release-manifest.json\"" in package

    posix_installer = _read("install.sh")
    powershell_installer = _read("install.ps1")
    assert "UNRENDERED_VERSION=$(printf '@%s@' TRACEABLE_RAG_VERSION)" in posix_installer
    assert '$UnrenderedVersion = "@" + "TRACEABLE_RAG_VERSION" + "@"' in powershell_installer
    assert 'sed "s/@TRACEABLE_RAG_VERSION@/$version/g" install.sh' in package
    assert 'sed "s/@TRACEABLE_RAG_VERSION@/$version/g" install.ps1' in package

    checksum_body = package.split("sha256sum \\\n", 1)[1].split(
        "> SHA256SUMS", 1
    )[0]
    checksum_assets = [
        _normalize_release_asset(line.strip().removesuffix("\\").strip())
        for line in checksum_body.splitlines()
        if line.strip()
    ]
    release_assets = [
        _normalize_release_asset(Path(line.strip()).name)
        for line in publish.split("files: |", 1)[1].splitlines()
        if line.strip().startswith(".artifacts/release/")
    ]
    assert release_assets[-1] == "SHA256SUMS"
    assert checksum_assets == release_assets[:-1]


def test_release_identity_validator_binds_local_config_to_remote_platform() -> None:
    digest = lambda character: "sha256:" + character * 64
    manifest_media_type = "application/vnd.oci.image.manifest.v1+json"
    local_image = [
        {
            "Id": digest("a"),
            "Os": "linux",
            "Architecture": "amd64",
        }
    ]
    manifest = {
        "schemaVersion": 2,
        "mediaType": manifest_media_type,
        "config": {"digest": digest("a")},
        "layers": [{"digest": digest("c")}],
    }
    manifest_bytes = json.dumps(
        manifest, separators=(",", ":"), sort_keys=True
    ).encode()
    descriptor = {
        "digest": "sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
        "mediaType": manifest_media_type,
        "size": len(manifest_bytes),
    }

    assert verify_release_image_identity.verify_platform_image(
        local_image=local_image,
        descriptor=descriptor,
        manifest_bytes=manifest_bytes,
        expected_image_id=digest("a"),
        expected_platform="linux/amd64",
    ) == descriptor["digest"]

    changed_local = copy.deepcopy(local_image)
    changed_local[0]["Id"] = digest("d")
    with pytest.raises(ValueError, match="changed after acceptance"):
        verify_release_image_identity.verify_platform_image(
            local_image=changed_local,
            descriptor=descriptor,
            manifest_bytes=manifest_bytes,
            expected_image_id=digest("a"),
            expected_platform="linux/amd64",
        )

    changed_remote = copy.deepcopy(manifest)
    changed_remote["config"]["digest"] = digest("d")
    changed_remote_bytes = json.dumps(
        changed_remote, separators=(",", ":"), sort_keys=True
    ).encode()
    changed_remote_descriptor = {
        "digest": "sha256:" + hashlib.sha256(changed_remote_bytes).hexdigest(),
        "mediaType": manifest_media_type,
        "size": len(changed_remote_bytes),
    }
    with pytest.raises(ValueError, match="not the accepted local image"):
        verify_release_image_identity.verify_platform_image(
            local_image=local_image,
            descriptor=changed_remote_descriptor,
            manifest_bytes=changed_remote_bytes,
            expected_image_id=digest("a"),
            expected_platform="linux/amd64",
        )

    tampered_bytes = manifest_bytes.replace(b'"layers"', b'"layerz"', 1)
    assert len(tampered_bytes) == len(manifest_bytes)
    with pytest.raises(ValueError, match="raw digest differs"):
        verify_release_image_identity.verify_platform_image(
            local_image=local_image,
            descriptor=descriptor,
            manifest_bytes=tampered_bytes,
            expected_image_id=digest("a"),
            expected_platform="linux/amd64",
        )

    wrong_size_descriptor = {**descriptor, "size": len(manifest_bytes) + 1}
    with pytest.raises(ValueError, match="raw size differs"):
        verify_release_image_identity.verify_platform_image(
            local_image=local_image,
            descriptor=wrong_size_descriptor,
            manifest_bytes=manifest_bytes,
            expected_image_id=digest("a"),
            expected_platform="linux/amd64",
        )


def test_release_identity_validator_requires_exact_index_membership_and_commit() -> None:
    digest = lambda character: "sha256:" + character * 64
    manifest_media_type = "application/vnd.oci.image.manifest.v1+json"
    index_media_type = "application/vnd.oci.image.index.v1+json"
    source_commit = "e" * 40
    index = {
        "schemaVersion": 2,
        "mediaType": index_media_type,
        "annotations": {"org.opencontainers.image.revision": source_commit},
        "manifests": [
            {
                "mediaType": manifest_media_type,
                "digest": digest("a"),
                "platform": {"os": "linux", "architecture": "amd64"},
            },
            {
                "mediaType": manifest_media_type,
                "digest": digest("b"),
                "platform": {
                    "os": "linux",
                    "architecture": "arm64",
                    "variant": "v8",
                },
            },
        ],
    }
    index_bytes = json.dumps(index, separators=(",", ":"), sort_keys=True).encode()
    descriptor = {
        "digest": "sha256:" + hashlib.sha256(index_bytes).hexdigest(),
        "mediaType": index_media_type,
        "size": len(index_bytes),
    }
    expected_platforms = {
        "linux/amd64": digest("a"),
        "linux/arm64": digest("b"),
    }

    assert verify_release_image_identity.verify_image_index(
        descriptor=descriptor,
        index_bytes=index_bytes,
        expected_platforms=expected_platforms,
        expected_index_digest=descriptor["digest"],
        expected_source_commit=source_commit,
    ) == descriptor["digest"]

    index_with_attestation = copy.deepcopy(index)
    index_with_attestation["manifests"].append(
        {
            "mediaType": manifest_media_type,
            "digest": digest("c"),
            "platform": {"os": "unknown", "architecture": "unknown"},
        }
    )
    attestation_bytes = json.dumps(
        index_with_attestation, separators=(",", ":"), sort_keys=True
    ).encode()
    attestation_descriptor = {
        "digest": "sha256:" + hashlib.sha256(attestation_bytes).hexdigest(),
        "mediaType": index_media_type,
        "size": len(attestation_bytes),
    }
    with pytest.raises(ValueError, match="published platform digests differ"):
        verify_release_image_identity.verify_image_index(
            descriptor=attestation_descriptor,
            index_bytes=attestation_bytes,
            expected_platforms=expected_platforms,
            expected_source_commit=source_commit,
        )

    wrong_commit_index = copy.deepcopy(index)
    wrong_commit_index["annotations"]["org.opencontainers.image.revision"] = "f" * 40
    wrong_commit_bytes = json.dumps(
        wrong_commit_index, separators=(",", ":"), sort_keys=True
    ).encode()
    wrong_commit_descriptor = {
        "digest": "sha256:" + hashlib.sha256(wrong_commit_bytes).hexdigest(),
        "mediaType": index_media_type,
        "size": len(wrong_commit_bytes),
    }
    with pytest.raises(ValueError, match="does not match GITHUB_SHA"):
        verify_release_image_identity.verify_image_index(
            descriptor=wrong_commit_descriptor,
            index_bytes=wrong_commit_bytes,
            expected_platforms=expected_platforms,
            expected_source_commit=source_commit,
        )

    tampered_bytes = index_bytes.replace(b'"manifests"', b'"manifestz"', 1)
    assert len(tampered_bytes) == len(index_bytes)
    with pytest.raises(ValueError, match="raw digest differs"):
        verify_release_image_identity.verify_image_index(
            descriptor=descriptor,
            index_bytes=tampered_bytes,
            expected_platforms=expected_platforms,
            expected_source_commit=source_commit,
        )


def test_release_smoke_uses_only_retrieval_and_source_contracts() -> None:
    smoke = _read("scripts/release_api_smoke.py")
    assert "/api/retrieval" in smoke
    assert "/api/sources/{source_id}/context" in smoke
    assert "/api/evidence" not in smoke
    assert "/api/query" not in smoke
    assert "/api/traces" not in smoke


def test_release_document_probes_are_independent_and_deterministic() -> None:
    document_probes = [
        probe for probe in release_api_smoke.PROBES if probe["expected_document_names"]
    ]

    assert [probe["expected_document_names"] for probe in document_probes] == [
        ("openstax-introduction-to-computer-science-p357",),
        ("openstax-introduction-to-python-programming-p334-335",),
        ("eu-joint-employment-report-2025-p97",),
        ("eu-future-oriented-occupations-p19",),
    ]
    assert all(probe["mode"] == "keyword" for probe in document_probes)
    assert all(probe["minimum_sources"] == 1 for probe in document_probes)


def test_retrieval_probe_checks_source_context_and_clickable_page(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    def fake_request_json(url: str, payload=None):
        calls.append((url, payload))
        if url.endswith("/api/retrieval"):
            return {
                "mode_requested": "auto",
                "pipeline_used": "dense_reranker",
                "degraded": False,
                "warnings": [],
                "results": [
                    {
                        "source_id": "src_0123456789abcdef0123456789abcdef",
                        "source_url": "/sources/src_0123456789abcdef0123456789abcdef",
                        "text": "The middle end optimizes execution time and memory usage.",
                        "document_id": "doc-a",
                        "document_name": "openstax-introduction-to-computer-science-p357",
                        "human_page_number": 3,
                        "bbox": [1, 2, 3, 4],
                        "score": 0.9,
                    }
                ],
            }
        if "/api/sources/" in url:
            return {
                "source": {"source_id": "src_0123456789abcdef0123456789abcdef"},
                "before": [],
                "after": [],
                "truncated": False,
            }
        raise AssertionError(url)

    monkeypatch.setattr(release_api_smoke, "_request_json", fake_request_json)
    monkeypatch.setattr(
        release_api_smoke,
        "_request_text",
        lambda url, **_kwargs: calls.append((url, "html")) or "ok",
    )

    result = release_api_smoke._run_retrieval_probe(
        "http://127.0.0.1:8000",
        "default",
        query="What does the middle end of compilation optimize?",
        minimum_sources=1,
        expected_document_names=(
            "openstax-introduction-to-computer-science-p357",
        ),
    )

    assert result["pipeline_used"] == "dense_reranker"
    assert result["mode_requested"] == "auto"
    assert result["checked_source_ids"] == [
        "src_0123456789abcdef0123456789abcdef"
    ]
    retrieval_payload = next(
        payload for url, payload in calls if url.endswith("/api/retrieval")
    )
    assert retrieval_payload["mode"] == "auto"
    assert retrieval_payload["top_k"] == 50
    assert any("/context?" in url for url, _payload in calls)
    assert any("/sources/src_" in url and payload == "html" for url, payload in calls)


def test_keyword_retrieval_probe_preserves_deterministic_pipeline(monkeypatch) -> None:
    captured: dict = {}

    def fake_request_json(url: str, payload=None):
        captured.update(payload or {})
        return {
            "mode_requested": "keyword",
            "pipeline_used": "keyword",
            "degraded": False,
            "warnings": [],
            "results": [],
        }

    monkeypatch.setattr(release_api_smoke, "_request_json", fake_request_json)

    result = release_api_smoke._run_retrieval_probe(
        "http://127.0.0.1:8000",
        "default",
        query="不存在的唯一词",
        mode="keyword",
        minimum_sources=0,
        expected_document_names=(),
    )

    assert captured["mode"] == "keyword"
    assert captured["top_k"] == 50
    assert result["mode_requested"] == "keyword"
    assert result["pipeline_used"] == "keyword"


def test_visual_retrieval_probe_validates_reference_and_detailed_context(monkeypatch) -> None:
    asset_url = "/api/knowledge-bases/default/documents/doc-image/assets/asset-image"

    def fake_request_json(url: str, payload=None):
        if url.endswith("/api/retrieval"):
            return {
                "mode_requested": "keyword",
                "pipeline_used": "keyword",
                "degraded": False,
                "warnings": [],
                "results": [
                    {
                        "source_id": "src_0123456789abcdef0123456789abcdef",
                        "source_url": "/sources/src_0123456789abcdef0123456789abcdef",
                        "text": "图 4-2 项目总体技术路线图",
                        "document_id": "doc-image",
                        "document_name": "视觉报告",
                        "human_page_number": 8,
                        "bbox": [1, 2, 3, 4],
                        "score": 0.9,
                        "asset": {
                            "asset_id": "asset-image",
                            "modality": "image",
                            "asset_url": asset_url,
                            "preview_url": f"{asset_url}?variant=preview",
                        },
                    }
                ],
            }
        if "/api/sources/" in url:
            return {
                "source": {
                    "source_id": "src_0123456789abcdef0123456789abcdef",
                    "asset": {
                        "asset_id": "asset-image",
                        "modality": "image",
                        "asset_url": asset_url,
                        "preview_url": f"{asset_url}?variant=preview",
                        "content_type": "image/png",
                        "file_hash": "abc123",
                        "width": 640,
                        "height": 320,
                        "caption": "图 4-2 项目总体技术路线图",
                    },
                },
                "before": [],
                "after": [],
                "truncated": False,
            }
        raise AssertionError(url)

    monkeypatch.setattr(release_api_smoke, "_request_json", fake_request_json)
    monkeypatch.setattr(release_api_smoke, "_request_text", lambda *_args, **_kwargs: "ok")
    monkeypatch.setattr(
        release_api_smoke,
        "_request_bytes",
        lambda *_args, **_kwargs: ("image/png", b"image"),
    )

    result = release_api_smoke._run_retrieval_probe(
        "http://127.0.0.1:8000",
        "default",
        query="项目总体技术路线图",
        mode="keyword",
        minimum_sources=1,
        expected_document_names=("视觉报告",),
        require_visual=True,
    )

    assert result["checked_source_ids"] == [
        "src_0123456789abcdef0123456789abcdef"
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        {"asset": "invalid"},
        {"asset": {"asset_id": "asset-image"}},
        {"asset": {"asset_id": "asset-image", "modality": "audio"}},
    ],
)
def test_release_probe_rejects_malformed_visual_source(mutation) -> None:
    result = {
        "source_id": "src_0123456789abcdef0123456789abcdef",
        "source_url": "/sources/src_0123456789abcdef0123456789abcdef",
        "text": "图题",
        "document_id": "doc-image",
        "document_name": "视觉报告",
        "human_page_number": 1,
        "bbox": None,
        "score": 0.1,
        "asset": {
            "asset_id": "asset-image",
            "modality": "image",
            "asset_url": "/api/knowledge-bases/default/documents/doc-image/assets/asset-image",
            "preview_url": None,
        },
        **mutation,
    }

    with pytest.raises(AssertionError):
        release_api_smoke._validate_source_result(result)


@pytest.mark.parametrize(
    "mutation",
    [
        {"pipeline_used": "unknown"},
        {"warnings": "unsafe"},
        {"results": []},
    ],
)
def test_retrieval_probe_rejects_invalid_contract(monkeypatch, mutation) -> None:
    response = {
        "mode_requested": "auto",
        "pipeline_used": "keyword",
        "degraded": True,
        "warnings": [],
        "results": [
            {
                "source_id": "src_0123456789abcdef0123456789abcdef",
                "source_url": "/sources/src_0123456789abcdef0123456789abcdef",
                "text": "source",
                "document_id": "doc-a",
                "document_name": "doc.pdf",
                "human_page_number": 1,
                "bbox": None,
                "score": 0.1,
            }
        ],
        **mutation,
    }
    monkeypatch.setattr(release_api_smoke, "_request_json", lambda *_args, **_kwargs: response)
    with pytest.raises(AssertionError):
        release_api_smoke._run_retrieval_probe(
            "http://127.0.0.1:8000",
            "default",
            query="q",
            minimum_sources=1,
            expected_document_names=(),
        )
