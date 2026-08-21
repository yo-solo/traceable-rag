from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]
PROJECT_VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
RELEASE_VERSION = f"v{PROJECT_VERSION}"


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _install_env(tmp_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "TRACEABLE_RAG_CONFIG_DIR": str(tmp_path / "config"),
        "TRACEABLE_RAG_DATA_DIR": str(tmp_path / "data"),
        "TRACEABLE_RAG_STATE_DIR": str(tmp_path / "state"),
        "TRACEABLE_RAG_BACKUP_DIR": str(tmp_path / "backups"),
        "TRACEABLE_RAG_BIN_DIR": str(tmp_path / "bin"),
        "TRACEABLE_RAG_NO_START": "1",
    }


def _parse_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        result[key] = value
    return result


def _write_fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    log = tmp_path / "fake-docker.log"
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/bin/sh
set -eu

: "${FAKE_DOCKER_LOG:?}"
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"

if [ "${1:-}" = compose ] && [ "${2:-}" = version ]; then
  exit 0
fi

if [ "${1:-}" = info ]; then
  if [ "${2:-}" = --format ]; then printf '%s\n' linux; fi
  exit 0
fi

if [ "${1:-}" = ps ]; then
  [ "${FAKE_DOCKER_PS_FAIL:-0}" != 1 ] || exit 42
  container_ids=${FAKE_DOCKER_CONTAINER_IDS:-${FAKE_DOCKER_CONTAINER_ID:-}}
  if [ -n "$container_ids" ]; then printf '%s\n' "$container_ids"; fi
  exit 0
fi

if [ "${1:-}" = inspect ]; then
  [ "${FAKE_DOCKER_INSPECT_FAIL:-0}" != 1 ] || exit 44
  if [ -n "${FAKE_DOCKER_STATE_FILE:-}" ] && [ -f "$FAKE_DOCKER_STATE_FILE" ]; then
    sed -n '1p' "$FAKE_DOCKER_STATE_FILE"
  else
    printf '%s\n' "${FAKE_DOCKER_STATE:-running}"
  fi
  exit 0
fi

if [ "${1:-}" = stop ]; then
  [ "${FAKE_DOCKER_DIRECT_STOP_FAIL:-0}" != 1 ] || exit 45
  if [ -n "${FAKE_DOCKER_STATE_FILE:-}" ]; then
    printf '%s\n' "${FAKE_DOCKER_STATE_AFTER_DIRECT_STOP:-exited}" > "$FAKE_DOCKER_STATE_FILE"
  fi
  exit 0
fi

if [ "${1:-}" = run ]; then
  exit 0
fi

if [ "${1:-}" = compose ]; then
  shift
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --project-name|--env-file|--file)
        shift
        [ "$#" -gt 0 ] || exit 90
        shift
        ;;
      *) break ;;
    esac
  done
  case "${1:-}" in
    ps)
      [ "${FAKE_DOCKER_PS_FAIL:-0}" != 1 ] || exit 42
      if [ -n "${FAKE_DOCKER_CONTAINER_ID:-}" ]; then
        printf '%s\n' "$FAKE_DOCKER_CONTAINER_ID"
      fi
      ;;
    stop)
      [ "${FAKE_DOCKER_STOP_FAIL:-0}" != 1 ] || exit 43
      ;;
    up)
      [ "${FAKE_DOCKER_UP_FAIL:-0}" != 1 ] || exit 46
      ;;
  esac
  exit 0
fi

exit 0
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    curl = fake_bin / "curl"
    curl.write_text(
        """#!/bin/sh
set -eu

configuration=
case " $* " in
  *" --config - "*) configuration=$(cat) ;;
esac
if [ -n "${FAKE_CURL_ACCEPTED_TOKEN_FILE:-}" ] && [ -n "$configuration" ]; then
  expected=$(sed -n '1p' "$FAKE_CURL_ACCEPTED_TOKEN_FILE")
  case "$configuration" in
    *"X-Traceable-RAG-Admin-Token: $expected"*) printf '%s' 422 ;;
    *) printf '%s' 403 ;;
  esac
else
  printf '%s' "${FAKE_CURL_HTTP_CODE:-200}"
fi
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    return fake_bin, log


def test_end_user_compose_pulls_a_pinned_image_and_mounts_secrets() -> None:
    compose = _read("deploy/compose.yaml")

    assert "build:" not in compose
    assert "TRACEABLE_RAG_IMAGE:?" in compose
    assert "@TRACEABLE_RAG_CONTAINER_UID@:@TRACEABLE_RAG_CONTAINER_GID@" in compose
    assert 'host_ip: "${TRACEABLE_RAG_BIND_ADDRESS:-127.0.0.1}"' in compose
    assert "TRACEABLE_RAG_PROVIDER_STORE_PATH: /data/providers.db" in compose
    assert "TRACEABLE_RAG_TRUSTED_HOSTS: 127.0.0.1,localhost,::1" in compose
    assert "TRACEABLE_RAG_MASTER_KEY_FILE: /run/secrets/traceable-rag-master-key" in compose
    assert "TRACEABLE_RAG_ADMIN_TOKEN_FILE: /run/secrets/traceable-rag-admin-token" in compose
    assert compose.count("read_only: true") == 2
    assert "http://127.0.0.1:8000/readyz" in compose


def test_posix_installer_is_idempotent_and_never_writes_provider_keys(
    tmp_path: Path,
) -> None:
    env = _install_env(tmp_path)
    first = subprocess.run(
        ["sh", str(ROOT / "install.sh")],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    config = Path(env["TRACEABLE_RAG_CONFIG_DIR"])
    key = config / "secrets/master.key"
    token = config / "secrets/admin-token"
    env_file = config / "deployment.env"
    initial_key = key.read_bytes()
    initial_token = token.read_bytes()

    second = subprocess.run(
        ["sh", str(ROOT / "install.sh")],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert len(initial_key) == 32
    assert len(initial_token) == 64
    assert key.read_bytes() == initial_key
    assert token.read_bytes() == initial_token
    assert initial_token.decode("ascii").isalnum()
    assert initial_token.decode("ascii") == initial_token.decode("ascii").lower()
    assert initial_token.decode("ascii") not in first.stdout + first.stderr
    assert initial_token.decode("ascii") not in second.stdout + second.stderr

    deployment = _parse_env(env_file)
    assert deployment["TRACEABLE_RAG_RELEASE_VERSION"] == RELEASE_VERSION
    assert deployment["TRACEABLE_RAG_READY_TIMEOUT_SECONDS"] == "900"
    assert deployment["TRACEABLE_RAG_IMAGE"].endswith(f":{RELEASE_VERSION}")
    assert deployment["TRACEABLE_RAG_CONTAINER_UID"] == str(os.getuid())
    assert len(deployment["TRACEABLE_RAG_INSTALLATION_ID"]) == 32
    assert not any(
        name in deployment
        for name in (
            "MINERU_TOKEN",
            "OPENAI_EMBEDDING_API_KEY",
            "TRACEABLE_RAG_RERANKER_API_KEY",
            "TRACEABLE_RAG_LLM_API_KEY",
        )
    )
    assert f'user: "{os.getuid()}:{os.getgid()}"' in (config / "compose.yaml").read_text()
    for owned in (
        config,
        Path(env["TRACEABLE_RAG_DATA_DIR"]),
        Path(env["TRACEABLE_RAG_STATE_DIR"]),
        Path(env["TRACEABLE_RAG_BACKUP_DIR"]),
    ):
        assert (owned / ".traceable-rag-installation").read_text() == deployment[
            "TRACEABLE_RAG_INSTALLATION_ID"
        ]


def test_posix_installer_refuses_nonempty_unmarked_or_remote_paths(
    tmp_path: Path,
) -> None:
    env = _install_env(tmp_path)
    config = Path(env["TRACEABLE_RAG_CONFIG_DIR"])
    config.mkdir(parents=True)
    (config / "unrelated.txt").write_text("keep", encoding="utf-8")

    refused = subprocess.run(
        ["sh", str(ROOT / "install.sh")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert refused.returncode != 0
    assert "refusing to claim" in refused.stderr
    assert (config / "unrelated.txt").read_text() == "keep"

    remote_env = _install_env(tmp_path / "remote")
    remote_env["TRACEABLE_RAG_BIND_ADDRESS"] = "0.0.0.0"
    remote = subprocess.run(
        ["sh", str(ROOT / "install.sh")],
        cwd=ROOT,
        env=remote_env,
        capture_output=True,
        text=True,
    )
    assert remote.returncode != 0
    assert "loopback" in remote.stderr

    ambiguous_env = _install_env(tmp_path / "ambiguous")
    ambiguous_env["TRACEABLE_RAG_DATA_DIR"] = str(tmp_path / "bad#data")
    ambiguous = subprocess.run(
        ["sh", str(ROOT / "install.sh")],
        cwd=ROOT,
        env=ambiguous_env,
        capture_output=True,
        text=True,
    )
    assert ambiguous.returncode != 0
    assert "ambiguous" in ambiguous.stderr


def test_posix_installer_preserves_unmanaged_command_entries_and_reports_path(
    tmp_path: Path,
) -> None:
    file_env = _install_env(tmp_path / "file")
    file_bin = Path(file_env["TRACEABLE_RAG_BIN_DIR"])
    file_bin.mkdir(parents=True)
    command = file_bin / "traceable-rag"
    command.write_text("user command\n", encoding="utf-8")
    refused = subprocess.run(
        ["sh", str(ROOT / "install.sh")], cwd=ROOT, env=file_env, capture_output=True, text=True
    )
    assert refused.returncode != 0
    assert "choose another TRACEABLE_RAG_BIN_DIR" in refused.stderr
    assert command.read_text(encoding="utf-8") == "user command\n"
    assert not Path(file_env["TRACEABLE_RAG_CONFIG_DIR"]).exists()

    link_env = _install_env(tmp_path / "link")
    link_bin = Path(link_env["TRACEABLE_RAG_BIN_DIR"])
    link_bin.mkdir(parents=True)
    unrelated = tmp_path / "unrelated-manager"
    unrelated.write_text("unrelated\n", encoding="utf-8")
    (link_bin / "traceable-rag").symlink_to(unrelated)
    refused_link = subprocess.run(
        ["sh", str(ROOT / "install.sh")], cwd=ROOT, env=link_env, capture_output=True, text=True
    )
    assert refused_link.returncode != 0
    assert (link_bin / "traceable-rag").resolve() == unrelated

    path_env = _install_env(tmp_path / "path")
    path_env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    installed = subprocess.run(
        ["sh", str(ROOT / "install.sh")], cwd=ROOT, env=path_env, check=True, capture_output=True, text=True
    )
    assert "is not currently on PATH" in installed.stdout
    assert "~/.zshrc (zsh) or ~/.bashrc (bash)" in installed.stdout
    assert str(Path(path_env["TRACEABLE_RAG_BIN_DIR"]) / "traceable-rag") in installed.stdout


def test_installer_normalizes_localhost_and_rejects_cross_version_reruns(
    tmp_path: Path,
) -> None:
    env = _install_env(tmp_path)
    env["TRACEABLE_RAG_BIND_ADDRESS"] = "localhost"
    subprocess.run(["sh", str(ROOT / "install.sh")], cwd=ROOT, env=env, check=True)
    deployment = _parse_env(Path(env["TRACEABLE_RAG_CONFIG_DIR"]) / "deployment.env")
    assert deployment["TRACEABLE_RAG_BIND_ADDRESS"] == "127.0.0.1"

    different = {**env, "TRACEABLE_RAG_VERSION": "v9.9.9"}
    refused = subprocess.run(
        ["sh", str(ROOT / "install.sh")],
        cwd=ROOT,
        env=different,
        capture_output=True,
        text=True,
    )
    assert refused.returncode != 0
    assert "traceable-rag upgrade v9.9.9" in refused.stderr

    drift_path = tmp_path / "drift-data"
    drifted = {**env, "TRACEABLE_RAG_DATA_DIR": str(drift_path)}
    refused_drift = subprocess.run(
        ["sh", str(ROOT / "install.sh")],
        cwd=ROOT,
        env=drifted,
        capture_output=True,
        text=True,
    )
    assert refused_drift.returncode != 0
    assert "differs from the installed layout" in refused_drift.stderr
    assert not drift_path.exists()


def test_installer_recovers_one_consistent_partial_marker_id(tmp_path: Path) -> None:
    env = _install_env(tmp_path)
    config = Path(env["TRACEABLE_RAG_CONFIG_DIR"])
    config.mkdir(parents=True)
    installation_id = "a" * 32
    (config / ".traceable-rag-installation").write_text(
        installation_id, encoding="utf-8"
    )

    subprocess.run(["sh", str(ROOT / "install.sh")], cwd=ROOT, env=env, check=True)
    deployment = _parse_env(config / "deployment.env")
    assert deployment["TRACEABLE_RAG_INSTALLATION_ID"] == installation_id
    for owned in (
        config,
        Path(env["TRACEABLE_RAG_DATA_DIR"]),
        Path(env["TRACEABLE_RAG_STATE_DIR"]),
        Path(env["TRACEABLE_RAG_BACKUP_DIR"]),
    ):
        assert (owned / ".traceable-rag-installation").read_text() == installation_id


def test_piped_installer_never_uses_a_deploy_directory_from_cwd(tmp_path: Path) -> None:
    release = tmp_path / "release"
    stage = tmp_path / "stage"
    bundled_deploy = stage / "deploy"
    release.mkdir()
    shutil.copytree(ROOT / "deploy", bundled_deploy)
    marker = "# verified-release-bundle-marker\n"
    with (bundled_deploy / "traceable-rag").open("a", encoding="utf-8") as handle:
        handle.write(marker)
    manifest = release / "release-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": RELEASE_VERSION,
                "image": "ghcr.io/yo-solo/traceable-rag-public",
                "index_digest": "sha256:" + "b" * 64,
                "platforms": {
                    "linux/amd64": "sha256:" + "c" * 64,
                    "linux/arm64": "sha256:" + "d" * 64,
                },
                "source_commit": "e" * 40,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    shutil.copy2(manifest, bundled_deploy / "release-manifest.json")
    asset = release / f"traceable-rag-deployment-{RELEASE_VERSION}.tar.gz"
    with tarfile.open(asset, "w:gz") as archive:
        archive.add(bundled_deploy, arcname="deploy")
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    manifest_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    (release / "SHA256SUMS").write_text(
        f"{digest}  {asset.name}\n{manifest_digest}  release-manifest.json\n",
        encoding="utf-8",
    )

    env = _install_env(tmp_path / "installed")
    env["TRACEABLE_RAG_RELEASE_BASE_URL"] = release.as_uri()
    rendered = _read("install.sh").replace("@TRACEABLE_RAG_VERSION@", RELEASE_VERSION)
    assert "@TRACEABLE_RAG_VERSION@" not in rendered
    result = subprocess.run(
        ["sh"],
        cwd=ROOT,
        env=env,
        input=rendered,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    installed_manager = (
        Path(env["TRACEABLE_RAG_CONFIG_DIR"]) / "traceable-rag"
    ).read_text(encoding="utf-8")
    assert marker.strip() in installed_manager
    installed = _parse_env(Path(env["TRACEABLE_RAG_CONFIG_DIR"]) / "deployment.env")
    assert installed["TRACEABLE_RAG_IMAGE"] == (
        "ghcr.io/yo-solo/traceable-rag-public@sha256:" + "b" * 64
    )
    assert installed["TRACEABLE_RAG_RELEASE_SOURCE_COMMIT"] == "e" * 40
    assert (Path(env["TRACEABLE_RAG_CONFIG_DIR"]) / "release-manifest.json").read_bytes() == manifest.read_bytes()


def test_posix_manager_help_and_offline_doctor_json(tmp_path: Path) -> None:
    env = _install_env(tmp_path)
    subprocess.run(["sh", str(ROOT / "install.sh")], cwd=ROOT, env=env, check=True)
    manager = Path(env["TRACEABLE_RAG_CONFIG_DIR"]) / "traceable-rag"
    subprocess.run(["sh", "-n", str(ROOT / "install.sh")], check=True)
    subprocess.run(["sh", "-n", str(ROOT / "deploy/traceable-rag")], check=True)
    help_result = subprocess.run(
        [str(manager), "help"], env=env, check=True, capture_output=True, text=True
    )
    for command in (
        "start",
        "stop",
        "status",
        "doctor",
        "logs",
        "backup",
        "upgrade",
        "rollback",
        "uninstall",
        "agent install-codex",
        "admin rotate-token",
    ):
        assert command in help_result.stdout

    doctor = subprocess.run(
        [str(manager), "doctor", "--json"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert doctor.returncode != 0
    payload = json.loads(doctor.stdout)
    assert payload["installation"]["loopback"] is True
    assert payload["version"]["configured"] == RELEASE_VERSION
    assert payload["provider_tests"] == {"requested_roles": [], "results": None}
    assert payload["skill"]["command"].startswith("traceable-rag agent install-codex")


def test_posix_backup_fails_closed_when_container_state_query_fails(
    tmp_path: Path,
) -> None:
    env = _install_env(tmp_path / "installation")
    subprocess.run(["sh", str(ROOT / "install.sh")], cwd=ROOT, env=env, check=True)
    manager = Path(env["TRACEABLE_RAG_CONFIG_DIR"]) / "traceable-rag"
    data = Path(env["TRACEABLE_RAG_DATA_DIR"])
    state = Path(env["TRACEABLE_RAG_STATE_DIR"])
    sentinel = data / "live-writer-sentinel"
    sentinel.write_text("must survive\n", encoding="utf-8")
    fake_bin, log = _write_fake_docker(tmp_path)
    run_env = {
        **env,
        "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_DOCKER_PS_FAIL": "1",
    }

    result = subprocess.run(
        [str(manager), "backup", "audit"],
        env=run_env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "writer state is unknown" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "must survive\n"
    pending = state / "pending-operation"
    assert not pending.exists()
    docker_calls = log.read_text(encoding="utf-8").splitlines()
    assert not any(call.startswith("run ") for call in docker_calls)
    assert not any(" stop " in f" {call} " for call in docker_calls)


def test_posix_recover_preserves_data_and_journal_when_writer_stop_fails(
    tmp_path: Path,
) -> None:
    env = _install_env(tmp_path / "installation")
    subprocess.run(["sh", str(ROOT / "install.sh")], cwd=ROOT, env=env, check=True)
    config = Path(env["TRACEABLE_RAG_CONFIG_DIR"])
    data = Path(env["TRACEABLE_RAG_DATA_DIR"])
    state = Path(env["TRACEABLE_RAG_STATE_DIR"])
    backups = Path(env["TRACEABLE_RAG_BACKUP_DIR"])
    manager = config / "traceable-rag"
    live_sentinel = data / "live-writer-sentinel"
    live_sentinel.write_text("live data\n", encoding="utf-8")

    snapshot = backups / "interrupted-upgrade-snapshot"
    snapshot_data = snapshot / "data"
    snapshot_data.mkdir(parents=True)
    (snapshot_data / "old-snapshot-sentinel").write_text(
        "old snapshot\n", encoding="utf-8"
    )
    shutil.copy2(data / ".traceable-rag-installation", snapshot_data)
    deployment = _parse_env(config / "deployment.env")
    (snapshot / "manifest").write_text(
        f"image={deployment['TRACEABLE_RAG_IMAGE']}\n", encoding="utf-8"
    )
    (snapshot / "checksums.sha256").write_text("placeholder\n", encoding="utf-8")

    record = state / "upgrades/interrupted"
    previous_control = record / "control/previous"
    previous_control.mkdir(parents=True)
    for name in ("traceable-rag", "compose.yaml", "deployment.env"):
        shutil.copy2(config / name, previous_control / name)
    release_manifest = config / "release-manifest.json"
    if release_manifest.exists():
        shutil.copy2(release_manifest, previous_control / release_manifest.name)
    (record / "pre-upgrade-snapshot").write_text(
        f"{snapshot}\n", encoding="utf-8"
    )
    pending = state / "pending-operation"
    pending.write_text(
        "\n".join(
            (
                "operation=upgrade",
                "pid=12345",
                f"record={record}",
                "was_running=0",
                "started_at=2026-08-10T00:00:00Z",
                "",
            )
        ),
        encoding="utf-8",
    )

    fake_bin, log = _write_fake_docker(tmp_path)
    run_env = {
        **env,
        "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_DOCKER_CONTAINER_ID": "a" * 64,
        "FAKE_DOCKER_STATE": "running",
        "FAKE_DOCKER_STOP_FAIL": "1",
        "FAKE_DOCKER_DIRECT_STOP_FAIL": "1",
    }

    result = subprocess.run(
        [str(manager), "recover"],
        env=run_env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "writer could not be confirmed stopped" in result.stderr
    assert live_sentinel.read_text(encoding="utf-8") == "live data\n"
    assert not (data / "old-snapshot-sentinel").exists()
    assert pending.is_file()
    docker_calls = log.read_text(encoding="utf-8").splitlines()
    assert any(" stop " in f" {call} " for call in docker_calls)
    assert not any(call.startswith("run ") for call in docker_calls)


def test_posix_backup_falls_back_to_unique_labeled_container_when_compose_stop_fails(
    tmp_path: Path,
) -> None:
    env = _install_env(tmp_path / "installation")
    subprocess.run(["sh", str(ROOT / "install.sh")], cwd=ROOT, env=env, check=True)
    manager = Path(env["TRACEABLE_RAG_CONFIG_DIR"]) / "traceable-rag"
    state = Path(env["TRACEABLE_RAG_STATE_DIR"])
    state_file = tmp_path / "container-state"
    state_file.write_text("running\n", encoding="utf-8")
    fake_bin, log = _write_fake_docker(tmp_path)
    run_env = {
        **env,
        "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_DOCKER_CONTAINER_ID": "a" * 64,
        "FAKE_DOCKER_STATE_FILE": str(state_file),
        "FAKE_DOCKER_STOP_FAIL": "1",
    }

    result = subprocess.run(
        [str(manager), "backup", "compose-damaged"],
        env=run_env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Backup created:" in result.stdout
    assert state_file.read_text(encoding="utf-8") == "exited\n"
    assert not (state / "pending-operation").exists()
    docker_calls = log.read_text(encoding="utf-8").splitlines()
    assert any(call.startswith("ps --all --quiet --filter") for call in docker_calls)
    assert any(" stop " in f" {call} " for call in docker_calls)
    assert any(call.startswith("stop --time 45 ") for call in docker_calls)
    assert any(call.startswith("run ") for call in docker_calls)


def test_posix_backup_distinguishes_absent_and_ambiguous_labeled_containers(
    tmp_path: Path,
) -> None:
    absent_env = _install_env(tmp_path / "absent-installation")
    subprocess.run(
        ["sh", str(ROOT / "install.sh")], cwd=ROOT, env=absent_env, check=True
    )
    absent_manager = (
        Path(absent_env["TRACEABLE_RAG_CONFIG_DIR"]) / "traceable-rag"
    )
    fake_bin, absent_log = _write_fake_docker(tmp_path / "absent-fake")
    absent_run_env = {
        **absent_env,
        "PATH": f"{fake_bin}{os.pathsep}{absent_env.get('PATH', '')}",
        "FAKE_DOCKER_LOG": str(absent_log),
    }
    absent = subprocess.run(
        [str(absent_manager), "backup", "absent"],
        env=absent_run_env,
        capture_output=True,
        text=True,
    )
    assert absent.returncode == 0, absent.stderr
    assert not (
        Path(absent_env["TRACEABLE_RAG_STATE_DIR"]) / "pending-operation"
    ).exists()

    ambiguous_env = _install_env(tmp_path / "ambiguous-installation")
    subprocess.run(
        ["sh", str(ROOT / "install.sh")], cwd=ROOT, env=ambiguous_env, check=True
    )
    ambiguous_manager = (
        Path(ambiguous_env["TRACEABLE_RAG_CONFIG_DIR"]) / "traceable-rag"
    )
    ambiguous_bin, ambiguous_log = _write_fake_docker(tmp_path / "ambiguous-fake")
    ambiguous_run_env = {
        **ambiguous_env,
        "PATH": f"{ambiguous_bin}{os.pathsep}{ambiguous_env.get('PATH', '')}",
        "FAKE_DOCKER_LOG": str(ambiguous_log),
        "FAKE_DOCKER_CONTAINER_IDS": f"{'a' * 64}\n{'b' * 64}",
    }
    ambiguous = subprocess.run(
        [str(ambiguous_manager), "backup", "ambiguous"],
        env=ambiguous_run_env,
        capture_output=True,
        text=True,
    )
    assert ambiguous.returncode != 0
    assert "writer state is ambiguous" in ambiguous.stderr
    assert not (
        Path(ambiguous_env["TRACEABLE_RAG_STATE_DIR"]) / "pending-operation"
    ).exists()
    assert not any(
        call.startswith("run ")
        for call in ambiguous_log.read_text(encoding="utf-8").splitlines()
    )


@pytest.mark.parametrize("unsafe_kind", ["displaced", "orphan-stage"])
def test_posix_recover_preserves_unmanaged_interrupted_restore_paths(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    env = _install_env(tmp_path / "installation")
    subprocess.run(["sh", str(ROOT / "install.sh")], cwd=ROOT, env=env, check=True)
    config = Path(env["TRACEABLE_RAG_CONFIG_DIR"])
    data = Path(env["TRACEABLE_RAG_DATA_DIR"])
    state = Path(env["TRACEABLE_RAG_STATE_DIR"])
    backups = Path(env["TRACEABLE_RAG_BACKUP_DIR"])
    manager = config / "traceable-rag"
    installation_marker = (data / ".traceable-rag-installation").read_bytes()

    snapshot = backups / "interrupted-restore-snapshot"
    snapshot_data = snapshot / "data"
    snapshot_data.mkdir(parents=True)
    (snapshot_data / ".traceable-rag-installation").write_bytes(
        installation_marker
    )
    (snapshot_data / "snapshot-sentinel").write_text(
        "snapshot\n", encoding="utf-8"
    )
    deployment = _parse_env(config / "deployment.env")
    (snapshot / "manifest").write_text(
        f"image={deployment['TRACEABLE_RAG_IMAGE']}\n", encoding="utf-8"
    )
    (snapshot / "checksums.sha256").write_text("placeholder\n", encoding="utf-8")

    record = state / "upgrades/interrupted"
    previous_control = record / "control/previous"
    previous_control.mkdir(parents=True)
    for name in ("traceable-rag", "compose.yaml", "deployment.env"):
        shutil.copy2(config / name, previous_control / name)
    release_manifest = config / "release-manifest.json"
    if release_manifest.exists():
        shutil.copy2(release_manifest, previous_control / release_manifest.name)
    (record / "pre-upgrade-snapshot").write_text(
        f"{snapshot}\n", encoding="utf-8"
    )
    pending = state / "pending-operation"
    pending.write_text(
        f"operation=upgrade\npid=12345\nrecord={record}\nwas_running=0\n",
        encoding="utf-8",
    )

    displaced = data.parent / f".{data.name}.displaced.interrupted"
    data.rename(displaced)
    if unsafe_kind == "displaced":
        (displaced / ".traceable-rag-installation").write_text(
            "f" * 32, encoding="utf-8"
        )
        unsafe_path = displaced
    else:
        unsafe_path = data.parent / f".{data.name}.restore.unmanaged"
        unsafe_path.mkdir()
        (unsafe_path / "keep-me").write_text("unmanaged\n", encoding="utf-8")

    fake_bin, log = _write_fake_docker(tmp_path)
    run_env = {
        **env,
        "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
        "FAKE_DOCKER_LOG": str(log),
    }
    result = subprocess.run(
        [str(manager), "recover"],
        env=run_env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "unsafe or unmanaged interrupted restore" in result.stderr
    assert not data.exists()
    assert displaced.is_dir()
    assert unsafe_path.exists()
    assert pending.is_file()


def test_posix_admin_token_rotation_refuses_unknown_state_and_rolls_back_rejected_token(
    tmp_path: Path,
) -> None:
    unknown_env = _install_env(tmp_path / "unknown-installation")
    subprocess.run(
        ["sh", str(ROOT / "install.sh")], cwd=ROOT, env=unknown_env, check=True
    )
    unknown_config = Path(unknown_env["TRACEABLE_RAG_CONFIG_DIR"])
    unknown_manager = unknown_config / "traceable-rag"
    unknown_token = unknown_config / "secrets/admin-token"
    original_unknown = unknown_token.read_bytes()
    unknown_bin, unknown_log = _write_fake_docker(tmp_path / "unknown-fake")
    unknown_run_env = {
        **unknown_env,
        "PATH": f"{unknown_bin}{os.pathsep}{unknown_env.get('PATH', '')}",
        "FAKE_DOCKER_LOG": str(unknown_log),
        "FAKE_DOCKER_PS_FAIL": "1",
    }
    unknown = subprocess.run(
        [str(unknown_manager), "admin", "rotate-token"],
        env=unknown_run_env,
        capture_output=True,
        text=True,
    )
    assert unknown.returncode != 0
    assert "writer state is unknown" in unknown.stderr
    assert unknown_token.read_bytes() == original_unknown

    rollback_env = _install_env(tmp_path / "rollback-installation")
    subprocess.run(
        ["sh", str(ROOT / "install.sh")], cwd=ROOT, env=rollback_env, check=True
    )
    rollback_config = Path(rollback_env["TRACEABLE_RAG_CONFIG_DIR"])
    rollback_manager = rollback_config / "traceable-rag"
    rollback_token = rollback_config / "secrets/admin-token"
    original_rollback = rollback_token.read_bytes()
    accepted_token = tmp_path / "accepted-token"
    accepted_token.write_bytes(original_rollback)
    rollback_bin, rollback_log = _write_fake_docker(tmp_path / "rollback-fake")
    rollback_run_env = {
        **rollback_env,
        "PATH": f"{rollback_bin}{os.pathsep}{rollback_env.get('PATH', '')}",
        "FAKE_DOCKER_LOG": str(rollback_log),
        "FAKE_DOCKER_CONTAINER_ID": "c" * 64,
        "FAKE_DOCKER_STATE": "running",
        "FAKE_CURL_ACCEPTED_TOKEN_FILE": str(accepted_token),
    }
    rollback = subprocess.run(
        [str(rollback_manager), "admin", "rotate-token"],
        env=rollback_run_env,
        capture_output=True,
        text=True,
    )
    assert rollback.returncode != 0
    assert "previous token and service were restored" in rollback.stderr
    assert rollback_token.read_bytes() == original_rollback
    rollback_calls = rollback_log.read_text(encoding="utf-8").splitlines()
    assert sum(" up " in f" {call} " for call in rollback_calls) == 2


def test_snapshot_and_upgrade_contracts_keep_keys_separate_and_pin_digests() -> None:
    manager = _read("deploy/traceable-rag")
    snapshot_section = manager.split("create_snapshot_stopped()", 1)[1].split(
        "verify_snapshot()", 1
    )[0]
    restore_section = manager.split("restore_snapshot_stopped()", 1)[1].split(
        "prepare_upgrade_record()", 1
    )[0]

    assert "config/deployment.env" in snapshot_section
    assert "config/master.key" not in snapshot_section
    assert "config/admin-token" not in snapshot_section
    assert '("traceable_v3.db", "providers.db")' in manager
    assert "PRAGMA integrity_check" in manager
    assert "checksums.sha256" in manager
    assert "RepoDigest" in manager
    assert "pull_and_resolve_digest" in manager
    assert "--no-build" in manager
    assert 'is_managed_restore_directory "$stage"' in restore_section
    assert restore_section.index('is_managed_restore_directory "$stage"') < restore_section.index(
        'mv "$TRACEABLE_RAG_DATA_DIR" "$displaced"'
    )
    assert "com.docker.compose.project=$TRACEABLE_RAG_PROJECT_NAME" in manager
    assert "com.docker.compose.service=traceable-rag" in manager
    assert manager.index('> "$TRACEABLE_RAG_STATE_DIR/last-upgrade"') > manager.index(
        "wait_ready"
    )


def test_windows_scripts_target_powershell_51_security_boundaries() -> None:
    installer = _read("install.ps1")
    manager = _read("deploy/traceable-rag.ps1")

    assert "System.Text.UTF8Encoding($false)" in installer
    assert "System.Text.UTF8Encoding($false)" in manager
    assert "ExecutionPolicy Bypass" not in installer + manager
    assert "ReparsePoint" in installer
    assert "Set-SecretAcl" in installer
    assert '"S-1-5-18"' in installer
    assert "refusing to claim a non-empty unmarked directory" in installer
    assert "TRACEABLE_RAG_RELEASE_VERSION" in installer + manager
    assert "TRACEABLE_RAG_RELEASE_SOURCE_COMMIT" in installer + manager
    assert "Get-NormalizedRootedPath" in installer + manager
    assert "must be an absolute path" in installer + manager
    assert "release-manifest.json" in installer + manager
    assert "Download-ReleaseBundle" in manager
    assert "Activate-ControlPlane" in manager
    assert "Traceable-RAG installation:" in installer + manager
    assert "another deployment-changing command holds the installation lock" in manager
    assert "Pull-AndResolveDigest" in manager
    assert "/api/provider-profiles/$Role/test" in manager
    assert "/setup#token=" in manager
    assert "$MyInvocation.MyCommand.Path" in installer
    assert "$PSScriptRoot" not in installer
    assert "reparse point" in manager
    assert "knowledge base ID may contain only" in manager


def test_windows_lifecycle_fails_closed_before_snapshot_or_restore_data_changes() -> None:
    manager = _read("deploy/traceable-rag.ps1")
    container_id = manager.split("function Get-ContainerId", 1)[1].split(
        "function Get-ContainerState", 1
    )[0]
    container_state = manager.split("function Get-ContainerState", 1)[1].split(
        "function Test-WriterStopped", 1
    )[0]
    stop_writer = manager.split("function Stop-WriterAndConfirm", 1)[1].split(
        "function Wait-Ready", 1
    )[0]
    begin_pending = manager.split("function Begin-PendingOperation", 1)[1].split(
        "function Clear-PendingOperation", 1
    )[0]
    snapshot = manager.split("function New-Snapshot", 1)[1].split(
        "function Restart-IfNeeded", 1
    )[0]
    restore = manager.split("function Restore-Snapshot", 1)[1].split(
        "function Normalize-Version", 1
    )[0]
    recover = manager.split("function Recover-Command", 1)[1].split(
        "function Invoke-SafeJsonGet", 1
    )[0]

    assert 'label=com.docker.compose.project=$ProjectName' in container_id
    assert 'label=com.docker.compose.service=traceable-rag' in container_id
    assert "could not query Docker labels" in container_id
    assert "return \"\"" not in container_id.split("if ($LASTEXITCODE -ne 0)", 1)[1].split("}", 1)[0]
    assert "could not inspect the Traceable RAG container" in container_state
    assert begin_pending.index("Get-ContainerState") < begin_pending.index(
        "Write-PendingOperation $Operation $Record $WasRunning"
    )
    assert '$State -eq "removing"' in begin_pending
    assert "docker compose --project-name $ProjectName" in stop_writer
    assert 'docker stop --time 45 $Id' in stop_writer
    assert stop_writer.index("docker compose --project-name") < stop_writer.index("docker stop")
    assert stop_writer.index("docker stop") < stop_writer.index("Assert-WriterStopped")
    assert snapshot.index("Assert-WriterStopped") < snapshot.index("Get-ChildItem -LiteralPath $DataDir")
    assert restore.index("Assert-WriterStopped") < restore.index("Move-Item -LiteralPath $DataDir")
    assert recover.index("Stop-WriterAndConfirm") < recover.index("switch ($Pending.operation)")
    assert "-AllowFailure" not in recover.split("switch ($Pending.operation)", 1)[0]
    assert recover.index('@("config", "--quiet")') < recover.index(
        "Assert-InstallationMarkers -RequireActiveData"
    )
    assert recover.index("Assert-InstallationMarkers -RequireActiveData") < recover.index(
        "Clear-PendingOperation"
    )


def test_windows_pending_restore_can_reach_recovery_through_owned_displaced_data() -> None:
    manager = _read("deploy/traceable-rag.ps1")
    displacement = manager.split(
        "function Get-InterruptedRestoreDisplacement", 1
    )[1].split("function Assert-InstallationMarkers", 1)[0]
    marker_validation = manager.split("function Assert-InstallationMarkers", 1)[1].split(
        "if ($ImageRepository", 1
    )[0]
    restore = manager.split("function Restore-Snapshot", 1)[1].split(
        "function Normalize-Version", 1
    )[0]

    assert '$Pending.operation -notin @("upgrade", "rollback")' in displacement
    assert "[Regex]::Escape($Base)" in displacement
    assert "Candidates.Count -ne 1" in displacement
    assert "ReparsePoint" in displacement
    assert "Assert-InstallationMarker $Candidate.FullName" in displacement
    assert "$HasPendingOperation -and -not $RequireActiveData" in marker_validation
    assert "Get-InterruptedRestoreDisplacement | Out-Null" in marker_validation
    assert manager.index("$HasPendingOperation = Test-Path") < manager.index(
        "Assert-InstallationMarkers\n"
    )
    assert "Move-Item -LiteralPath $DisplacedCandidate.FullName -Destination $DataDir" in restore
    assert "Assert-InstallationMarker $InterruptedStage" in restore
    assert restore.index("Move-Item -LiteralPath $DisplacedCandidate.FullName") < restore.index(
        "Remove-Item -LiteralPath $InterruptedStage"
    )
    assert restore.index("Assert-InstallationMarker $Stage") < restore.index(
        "Move-Item -LiteralPath $DataDir -Destination $Displaced"
    )


def test_windows_admin_token_rotation_recreates_active_bind_mount_and_verifies_auth() -> None:
    manager = _read("deploy/traceable-rag.ps1")
    rotate = manager.split("function Rotate-AdminToken", 1)[1].split(
        "function Test-ManagedCommandEntry", 1
    )[0]
    recreate = manager.split("function Recreate-And-VerifyAdminToken", 1)[1].split(
        "function Rotate-AdminToken", 1
    )[0]

    assert rotate.index("$InitialState = Get-ContainerState") < rotate.index(
        "Install-AdminTokenFile $NewToken"
    )
    assert '$InitialState -eq "removing"' in rotate
    assert '@("running", "restarting", "paused")' in rotate
    assert rotate.index("Stop-WriterAndConfirm") < rotate.index(
        "Install-AdminTokenFile $NewToken"
    )
    assert 'Recreate-And-VerifyAdminToken $NewToken' in rotate
    assert 'Install-AdminTokenFile $PreviousToken' in rotate
    assert 'Recreate-And-VerifyAdminToken $PreviousToken' in rotate
    assert rotate.index("Recreate-And-VerifyAdminToken $NewToken") < rotate.index(
        'Write-Output "Admin token rotated.'
    )
    assert '"--force-recreate"' in recreate
    assert "Test-AdminTokenAccepted $Token" in recreate
    assert '/api/provider-profiles/mineru' in manager
    assert '-Method Put' in manager
    assert 'StatusCode -eq 422' in manager


def test_release_workflow_publishes_verified_multiarch_bundle_after_acceptance() -> None:
    release = _read(".github/workflows/release.yml")

    assert "fetch-depth: 0" in release
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main' in release
    assert "docker/setup-qemu-action@v3" in release
    assert "--platform linux/arm64" in release
    assert "http://127.0.0.1:18002/readyz" in release
    assert "libreoffice --version" in release
    assert "Push the exact accepted platform images" in release
    assert "verify_release_image_identity.py platform" in release
    assert "verify_release_image_identity.py index" in release
    assert "Assemble one accepted index from the platform digests" in release
    assert "traceable-rag-deployment-$version.tar.gz" in release
    assert "traceable-rag-deployment-$version.zip" in release
    assert "SHA256SUMS" in release
    assert release.index("Authenticate to GHCR only after acceptance passes") > release.index(
        "Block critical arm64 candidate vulnerabilities"
    )
    assert release.index('docker push "$IMAGE:$amd64_tag"') > release.index(
        "Authenticate to GHCR only after acceptance passes"
    )


def test_provider_templates_are_independent_and_empty_by_default() -> None:
    example = _parse_env(ROOT / ".env.example")
    local = _parse_env(ROOT / ".env.local.example")

    provider_keys = [
        key
        for key in example
        if any(role in key for role in ("MINERU", "EMBEDDING", "RERANKER", "LLM"))
    ]
    assert provider_keys
    assert all(example[key] == "" for key in provider_keys)
    assert "TRACEABLE_RAG_RERANKER_API_KEY" in example
    assert "TRACEABLE_RAG_LLM_API_KEY" in example
    assert "TRACEABLE_RAG_DEV_SECRET_DIR" in local
