from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def _install_env(tmp_path: Path) -> dict[str, str]:
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "TRACEABLE_RAG_CONFIG_DIR": str(tmp_path / "config"),
        "TRACEABLE_RAG_DATA_DIR": str(tmp_path / "data"),
        "TRACEABLE_RAG_STATE_DIR": str(tmp_path / "state"),
        "TRACEABLE_RAG_BACKUP_DIR": str(tmp_path / "backups"),
        "TRACEABLE_RAG_BIN_DIR": str(tmp_path / "bin"),
        "TRACEABLE_RAG_NO_START": "1",
    }
    env.pop("TRACEABLE_RAG_PORT", None)
    env.pop("TRACEABLE_RAG_VERSION", None)
    env.pop("TRACEABLE_RAG_RELEASE_BASE_URL", None)
    return env


def _parse_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        result[key] = value
    return result


def _write_fake_runtime(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    log = tmp_path / "docker.log"
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/bin/sh
set -eu
: "${FAKE_DOCKER_LOG:?}"
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"

if [ "${1:-}" = compose ] && [ "${2:-}" = version ]; then exit 0; fi
if [ "${1:-}" = info ]; then
  if [ "${2:-}" = --format ]; then printf '%s\n' linux; fi
  exit 0
fi
if [ "${1:-}" = pull ]; then exit 0; fi
if [ "${1:-}" = image ] && [ "${2:-}" = inspect ]; then
  printf '%s\n' 'ghcr.io/yo-solo/traceable-rag-public@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
  exit 0
fi
if [ "${1:-}" = compose ]; then
  shift
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --project-name|--env-file|--file) shift 2 ;;
      *) break ;;
    esac
  done
  case "${1:-}" in
    config) exit 0 ;;
    up) [ "${FAKE_DOCKER_UP_FAIL:-0}" != 1 ] || exit 46; exit 0 ;;
  esac
fi
exit 0
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    curl = fake_bin / "curl"
    curl.write_text("#!/bin/sh\nprintf '200'\n", encoding="utf-8")
    curl.chmod(0o755)
    return fake_bin, log


def test_posix_same_version_rerun_migrates_port_and_recreates_service(
    tmp_path: Path,
) -> None:
    env = _install_env(tmp_path)
    subprocess.run(["sh", str(ROOT / "install.sh")], cwd=ROOT, env=env, check=True)

    config = Path(env["TRACEABLE_RAG_CONFIG_DIR"])
    data = Path(env["TRACEABLE_RAG_DATA_DIR"])
    env_file = config / "deployment.env"
    master_key = (config / "secrets/master.key").read_bytes()
    admin_token = (config / "secrets/admin-token").read_bytes()
    installation_id = _parse_env(env_file)["TRACEABLE_RAG_INSTALLATION_ID"]
    sentinel = data / "preserved-during-port-change.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    fake_bin, docker_log = _write_fake_runtime(tmp_path)

    migrate_env = {
        **env,
        "TRACEABLE_RAG_PORT": "8123",
        "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
        "FAKE_DOCKER_LOG": str(docker_log),
    }
    migrate_env.pop("TRACEABLE_RAG_NO_START")
    migrated = subprocess.run(
        ["sh", str(ROOT / "install.sh")],
        cwd=ROOT,
        env=migrate_env,
        check=True,
        capture_output=True,
        text=True,
    )

    deployment = _parse_env(env_file)
    assert deployment["TRACEABLE_RAG_PORT"] == "8123"
    assert deployment["TRACEABLE_RAG_INSTALLATION_ID"] == installation_id
    assert (config / "secrets/master.key").read_bytes() == master_key
    assert (config / "secrets/admin-token").read_bytes() == admin_token
    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert not list(config.glob("deployment.env.port.*"))
    assert admin_token.decode("ascii") not in migrated.stdout + migrated.stderr
    calls = docker_log.read_text(encoding="utf-8").splitlines()
    assert any(" up --detach --no-build --remove-orphans traceable-rag" in f" {call}" for call in calls)

    no_override_env = {**env, "TRACEABLE_RAG_NO_START": "1"}
    subprocess.run(
        ["sh", str(ROOT / "install.sh")],
        cwd=ROOT,
        env=no_override_env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert _parse_env(env_file)["TRACEABLE_RAG_PORT"] == "8123"


def test_posix_port_change_waits_for_marker_validation(tmp_path: Path) -> None:
    env = _install_env(tmp_path)
    subprocess.run(["sh", str(ROOT / "install.sh")], cwd=ROOT, env=env, check=True)
    env_file = Path(env["TRACEABLE_RAG_CONFIG_DIR"]) / "deployment.env"
    original = env_file.read_bytes()
    state_marker = Path(env["TRACEABLE_RAG_STATE_DIR"]) / ".traceable-rag-installation"
    state_marker.write_text("f" * 32, encoding="utf-8")

    changed = subprocess.run(
        ["sh", str(ROOT / "install.sh")],
        cwd=ROOT,
        env={**env, "TRACEABLE_RAG_PORT": "8124"},
        capture_output=True,
        text=True,
    )

    assert changed.returncode != 0
    assert "installation marker mismatch" in changed.stderr
    assert env_file.read_bytes() == original
    assert not list(env_file.parent.glob("deployment.env.port.*"))


def test_posix_start_failure_prints_secret_free_copyable_port_retry(
    tmp_path: Path,
) -> None:
    env = _install_env(tmp_path)
    subprocess.run(["sh", str(ROOT / "install.sh")], cwd=ROOT, env=env, check=True)
    config = Path(env["TRACEABLE_RAG_CONFIG_DIR"])
    admin_token = (config / "secrets/admin-token").read_text(encoding="ascii")
    fake_bin, docker_log = _write_fake_runtime(tmp_path)
    failing_env = {
        **env,
        "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
        "FAKE_DOCKER_LOG": str(docker_log),
        "FAKE_DOCKER_UP_FAIL": "1",
    }
    failing_env.pop("TRACEABLE_RAG_NO_START")

    failed = subprocess.run(
        ["sh", str(ROOT / "install.sh")],
        cwd=ROOT,
        env=failing_env,
        capture_output=True,
        text=True,
    )

    output = failed.stdout + failed.stderr
    assert failed.returncode != 0
    assert "TRACEABLE_RAG_PORT=8001 sh" in output
    assert str(ROOT / "install.sh") in output
    assert "keeps the existing data, master key, admin token, and installation ID" in output
    assert admin_token not in output


def test_windows_installer_has_equivalent_atomic_port_recovery_contract() -> None:
    installer = (ROOT / "install.ps1").read_text(encoding="utf-8")

    assert "TRACEABLE_RAG_PORT differs from the installed layout" not in installer
    assert "$PortChangeRequested = $true" in installer
    assert 'Set-EnvSettingAtomic $EnvFile "TRACEABLE_RAG_PORT" "$Port"' in installer
    assert "[IO.File]::Replace($TemporaryPath, $Path, $null)" in installer
    assert "if ($Indexes.Count -ne 1)" in installer
    assert "Write-PortRetryHint" in installer
    assert '$env:TRACEABLE_RAG_PORT = "{0}"' in installer
    assert "powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File" in installer
    assert installer.index("Assert-AdminToken $AdminTokenFile") < installer.index(
        'Set-EnvSettingAtomic $EnvFile "TRACEABLE_RAG_PORT" "$Port"'
    )
