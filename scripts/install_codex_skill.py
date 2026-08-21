from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "integrations" / "codex-skill" / "traceable-rag"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
SKILL_VERSION_PATTERN = re.compile(r"^Skill version: `([^`]+)`$", re.MULTILINE)


def default_target() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return codex_home / "skills" / "traceable-rag"


def normalize_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must be an absolute http(s) URL")
    return normalized


def read_runtime(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() in {"base_url", "knowledge_base_id"}:
            result[key.strip()] = value.strip()
    return result


def read_skill_version(path: Path) -> str:
    match = SKILL_VERSION_PATTERN.search(path.read_text(encoding="utf-8"))
    if match is None:
        raise RuntimeError(f"Skill version is missing from {path}")
    return match.group(1)


def write_runtime(
    path: Path, *, base_url: str, knowledge_base_id: str | None
) -> None:
    lines = [
        "# Runtime configuration",
        "",
        f"base_url: {base_url}",
    ]
    if knowledge_base_id:
        lines.append(f"knowledge_base_id: {knowledge_base_id.strip()}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def install_skill(
    target: Path,
    *,
    base_url: str | None = None,
    knowledge_base_id: str | None = None,
) -> dict[str, str | None]:
    existing = read_runtime(target / "references" / "runtime.md")
    resolved_base_url = normalize_base_url(
        base_url or existing.get("base_url") or DEFAULT_BASE_URL
    )
    resolved_knowledge_base_id = (
        knowledge_base_id.strip()
        if knowledge_base_id and knowledge_base_id.strip()
        else existing.get("knowledge_base_id")
    )

    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(TEMPLATE / "SKILL.md", target / "SKILL.md")
    for directory in ("agents", "references"):
        destination = target / directory
        destination.mkdir(parents=True, exist_ok=True)
        for source in (TEMPLATE / directory).iterdir():
            if source.is_file() and source.name != "runtime.md":
                shutil.copy2(source, destination / source.name)
    write_runtime(
        target / "references" / "runtime.md",
        base_url=resolved_base_url,
        knowledge_base_id=resolved_knowledge_base_id,
    )
    return {
        "skill_version": read_skill_version(target / "SKILL.md"),
        "base_url": resolved_base_url,
        "knowledge_base_id": resolved_knowledge_base_id,
    }


def check_installation(target: Path, *, base_url: str) -> dict[str, object]:
    required = (
        target / "SKILL.md",
        target / "agents" / "openai.yaml",
        target / "references" / "http-contract.md",
        target / "references" / "runtime.md",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError("Skill installation is incomplete: " + ", ".join(missing))
    request = urllib.request.Request(
        f"{base_url}/api/health",
        headers={"Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if response.status != 200 or payload.get("ok") is not True:
        raise RuntimeError("Traceable RAG health check did not return ok=true")
    return {
        "installed": True,
        "health": True,
        "base_url": base_url,
        "skill_version": read_skill_version(target / "SKILL.md"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install or update the Traceable RAG Codex Skill."
    )
    parser.add_argument("--target", type=Path, default=default_target())
    parser.add_argument("--base-url")
    parser.add_argument("--knowledge-base-id")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify installed files and GET /api/health after installation.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = install_skill(
        args.target.expanduser(),
        base_url=args.base_url,
        knowledge_base_id=args.knowledge_base_id,
    )
    result: dict[str, object] = {
        "installed": True,
        "target": str(args.target.expanduser()),
        **config,
    }
    if args.check:
        result.update(
            check_installation(
                args.target.expanduser(), base_url=str(config["base_url"])
            )
        )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
