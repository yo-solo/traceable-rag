from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import stat
import subprocess
import time
import urllib.request
from urllib.error import HTTPError
from urllib.parse import urlsplit
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .runtime_config import load_runtime_config


MINERU_BASE_URL = "https://mineru.net"
_CURL_STATUS_MARKER = "__TRACEABLE_HTTP_STATUS__="
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(token|api[-_ ]?key|authorization|signature|credential)\b"
    r"\s*[:=]\s*([^\s,;]+)"
)
_HTTP_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_POSIX_PATH = re.compile(r"(?<![\w.-])/(?:[^\s/]+/)+[^\s,;:]+")
_WINDOWS_PATH = re.compile(r"(?i)\b[A-Z]:\\(?:[^\s\\]+\\)+[^\s,;:]+")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler())


class MinerUAPIError(RuntimeError):
    def __init__(
        self,
        code: int | str,
        message: str = "MinerU request failed",
        *,
        safe_context: str = "MinerU request failed",
    ):
        self.code = str(code)
        self.provider_message = message[:500]
        super().__init__(f"{safe_context} (code {self.code})")


class MinerUUploadError(RuntimeError):
    def __init__(
        self,
        reason: str,
        *,
        curl_exit_code: int | None = None,
        http_status: int | None = None,
        response_detail: str | None = None,
    ):
        self.reason = reason
        self.curl_exit_code = curl_exit_code
        self.http_status = http_status
        self.response_detail = response_detail
        super().__init__("MinerU file upload failed.")


def sanitize_diagnostic_text(value: str, *, max_length: int = 1000) -> str:
    normalized = " ".join(str(value).split())
    normalized = _SENSITIVE_ASSIGNMENT.sub(r"\1=<redacted>", normalized)
    normalized = _HTTP_URL.sub("<url>", normalized)
    normalized = _WINDOWS_PATH.sub("<path>", normalized)
    normalized = _POSIX_PATH.sub("<path>", normalized)
    return normalized[:max_length]


@dataclass(frozen=True)
class MinerUParseResult:
    batch_id: str
    file_name: str
    state: str
    output_dir: Path
    zip_path: Path | None
    full_zip_url: str | None = field(repr=False)
    raw_result: dict[str, Any] = field(repr=False)


class MinerUClient:
    def __init__(self, api_key: str, *, base_url: str = MINERU_BASE_URL, timeout: int = 60):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def parse_local_file(
        self,
        file_path: str | Path,
        output_dir: str | Path,
        *,
        model_version: str = "vlm",
        language: str = "ch",
        wait: bool = True,
        poll_interval: int = 10,
        timeout_seconds: int = 900,
        progress: Callable[[str], None] | None = None,
    ) -> MinerUParseResult:
        file_path = Path(file_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if progress:
            progress("mineru_submit")
        batch_id, upload_url = self.create_upload_url(
            file_name=file_path.name,
            model_version=model_version,
            language=language,
        )
        self.upload_file(upload_url, file_path)
        if progress:
            progress("mineru_poll")
        raw_result = self.poll_batch_result(batch_id, poll_interval=poll_interval, timeout_seconds=timeout_seconds) if wait else self.get_batch_result(batch_id)
        extract_result = _find_extract_result(raw_result, file_path.name)
        state = extract_result.get("state", "unknown")
        if state == "failed":
            raise MinerUAPIError(
                extract_result.get("err_code", extract_result.get("code", "failed")),
                str(extract_result.get("err_msg") or extract_result.get("message") or "解析失败"),
            )
        full_zip_url = extract_result.get("full_zip_url")
        zip_path = None
        if state == "done" and full_zip_url:
            if progress:
                progress("mineru_download")
            zip_path = output_dir / f"{file_path.stem}_mineru.zip"
            result_download_failed = False
            try:
                download_file(full_zip_url, zip_path, timeout=self.timeout)
                extract_zip(zip_path, output_dir)
            except Exception:
                result_download_failed = True
            if result_download_failed:
                raise RuntimeError("MinerU result download failed.")
        return MinerUParseResult(
            batch_id=batch_id,
            file_name=file_path.name,
            state=state,
            output_dir=output_dir,
            zip_path=zip_path,
            full_zip_url=full_zip_url,
            raw_result=raw_result,
        )

    def create_upload_url(self, *, file_name: str, model_version: str = "vlm", language: str = "ch") -> tuple[str, str]:
        payload = {
            "files": [{"name": file_name, "data_id": file_name}],
            "model_version": model_version,
            "language": language,
            "enable_table": True,
            "enable_formula": True,
        }
        data = self._post_json("/api/v4/file-urls/batch", payload)
        if data.get("code") != 0:
            raise MinerUAPIError(
                data.get("code", "unknown"),
                str(data.get("msg") or "生成上传 URL 失败"),
                safe_context="MinerU upload-url request failed",
            )
        batch_id = data["data"]["batch_id"]
        upload_url = data["data"]["file_urls"][0]
        return batch_id, upload_url

    def upload_file(self, upload_url: str, file_path: Path) -> None:
        # OSS signed URLs are sensitive to implicit headers. curl -T matches MinerU's
        # documented upload example more closely than urllib's form-style data path.
        hostname, port, addresses = _validated_public_https_url(upload_url)
        resolve_args: list[str] = []
        for address in addresses:
            rendered = f"[{address}]" if ":" in address else address
            resolve_args.extend(("--resolve", f"{hostname}:{port}:{rendered}"))
        launch_error: MinerUUploadError | None = None
        try:
            result = subprocess.run(
                [
                    "curl",
                    "-sS",
                    "--fail-with-body",
                    "--proto",
                    "=https",
                    "--proto-redir",
                    "=https",
                    "--max-redirs",
                    "0",
                    *resolve_args,
                    "-X",
                    "PUT",
                    "-T",
                    str(file_path),
                    "--write-out",
                    f"\n{_CURL_STATUS_MARKER}%{{http_code}}",
                    upload_url,
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            launch_error = MinerUUploadError("timeout")
        except OSError as exc:
            launch_error = MinerUUploadError(
                "curl_start_failed",
                response_detail=sanitize_diagnostic_text(str(exc)),
            )
        if launch_error is not None:
            raise launch_error
        if result.returncode != 0:
            response_body, marker, status_text = result.stdout.rpartition(
                f"\n{_CURL_STATUS_MARKER}"
            )
            http_status = None
            if marker and status_text.isdigit():
                parsed_status = int(status_text)
                if parsed_status:
                    http_status = parsed_status
            detail = sanitize_diagnostic_text(response_body or result.stderr)
            raise MinerUUploadError(
                "http_error" if http_status is not None else "curl_failed",
                curl_exit_code=result.returncode,
                http_status=http_status,
                response_detail=detail or None,
            )

    def get_batch_result(self, batch_id: str) -> dict[str, Any]:
        return self._get_json(f"/api/v4/extract-results/batch/{batch_id}")

    def poll_batch_result(self, batch_id: str, *, poll_interval: int, timeout_seconds: int) -> dict[str, Any]:
        deadline = time.time() + timeout_seconds
        last: dict[str, Any] | None = None
        while time.time() < deadline:
            last = self.get_batch_result(batch_id)
            results = last.get("data", {}).get("extract_result") or []
            states = [item.get("state") for item in results]
            if states and all(state in {"done", "failed"} for state in states):
                return last
            time.sleep(poll_interval)
        raise TimeoutError(f"Timed out waiting for MinerU batch {batch_id}.")

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers=self._headers(),
            method="POST",
        )
        return self._read_json_response(request)

    def _get_json(self, path: str) -> dict[str, Any]:
        request = urllib.request.Request(f"{self.base_url}{path}", headers=self._headers(), method="GET")
        return self._read_json_response(request)

    def _read_json_response(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            with _NO_REDIRECT_OPENER.open(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            code: int | str = exc.code
            message = "MinerU HTTP request failed"
            try:
                body = json.loads(exc.read().decode("utf-8"))
                code = body.get("code", code)
                message = str(body.get("msg") or body.get("message") or message)
            except Exception:
                pass
            raise MinerUAPIError(code, message) from None
        if payload.get("code") not in (None, 0):
            raise MinerUAPIError(
                payload.get("code", "unknown"),
                str(payload.get("msg") or payload.get("message") or "MinerU request failed"),
            )
        return payload

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "*/*",
        }


def mineru_api_key_from_doc(
    api_doc_path: str | Path,
    *,
    runtime_root: str | Path | None = None,
) -> str:
    api_doc_path = Path(api_doc_path)
    env_value = mineru_api_key_from_environment(runtime_root=runtime_root)
    if env_value:
        return env_value
    if not api_doc_path.exists():
        raise FileNotFoundError(api_doc_path)
    text = api_doc_path.read_text(encoding="utf-8")
    marker = "# MinerU"
    start = text.find(marker)
    section = text[start:] if start >= 0 else text
    key_marker = "API-Key:"
    idx = section.find(key_marker)
    if idx < 0:
        raise ValueError("MinerU API-Key not found")
    lines = section[idx + len(key_marker) :].strip().splitlines()
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    raise ValueError("MinerU API-Key is empty")


def mineru_api_key_from_environment(
    *, runtime_root: str | Path | None = None
) -> str | None:
    return load_runtime_config(root=runtime_root).mineru_token


def download_file(
    url: str,
    output_path: str | Path,
    *,
    timeout: int = 60,
    max_bytes: int = 512 * 1024 * 1024,
    chunk_size: int = 1024 * 1024,
) -> None:
    _validated_public_https_url(url)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.parent / f".{output_path.name}.{uuid.uuid4().hex}.part"
    total = 0
    try:
        with _NO_REDIRECT_OPENER.open(url, timeout=timeout) as response, temporary.open(
            "xb"
        ) as target:
            while True:
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"MinerU download size exceeds {max_bytes} bytes")
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        temporary.replace(output_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _validated_public_https_url(url: str) -> tuple[str, int, tuple[str, ...]]:
    try:
        parsed = urlsplit(str(url).strip())
        hostname = parsed.hostname
        port = parsed.port or 443
    except ValueError:
        raise ValueError("MinerU transfer URL is invalid") from None
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("MinerU transfer URL must be a credential-free HTTPS URL")
    normalized_host = hostname.rstrip(".").casefold()
    if (
        normalized_host == "localhost"
        or normalized_host.endswith(".localhost")
        or normalized_host.endswith(".local")
        or normalized_host.endswith(".internal")
    ):
        raise ValueError("MinerU transfer URL must use a public host")
    try:
        resolved = socket.getaddrinfo(
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError:
        raise ValueError("MinerU transfer URL host could not be resolved safely") from None
    addresses = tuple(sorted({item[4][0].split("%", 1)[0] for item in resolved}))
    if not addresses:
        raise ValueError("MinerU transfer URL host could not be resolved safely")
    for address in addresses:
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError:
            raise ValueError("MinerU transfer URL resolved to an invalid address") from None
        if not parsed_address.is_global:
            raise ValueError("MinerU transfer URL must resolve only to public addresses")
    return hostname, port, addresses


def extract_zip(
    zip_path: str | Path,
    output_dir: str | Path,
    *,
    max_members: int = 10_000,
    max_uncompressed_bytes: int = 2 * 1024 * 1024 * 1024,
    max_compression_ratio: float = 200.0,
) -> None:
    output_dir = Path(output_dir)
    if output_dir.is_symlink():
        raise ValueError("Refusing to extract into a symlinked directory")
    with zipfile.ZipFile(zip_path) as zf:
        members = zf.infolist()
        if len(members) > max_members:
            raise ValueError(f"ZIP member count exceeds {max_members}")
        total_size = 0
        validated: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
        for member in members:
            relative = _validate_zip_member(member)
            total_size += member.file_size
            if total_size > max_uncompressed_bytes:
                raise ValueError(
                    f"ZIP uncompressed size exceeds {max_uncompressed_bytes} bytes"
                )
            if member.file_size:
                ratio = member.file_size / max(member.compress_size, 1)
                if ratio > max_compression_ratio:
                    raise ValueError(
                        f"ZIP compression ratio {ratio:.1f} exceeds {max_compression_ratio}"
                    )
            validated.append((member, relative))

        output_dir.mkdir(parents=True, exist_ok=True)
        extracted = 0
        for member, relative in validated:
            destination = output_dir.joinpath(*relative.parts)
            _reject_existing_symlink_path(output_dir, destination.parent)
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as source, destination.open("xb") as target:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    extracted += len(chunk)
                    if extracted > max_uncompressed_bytes:
                        raise ValueError(
                            f"ZIP uncompressed size exceeds {max_uncompressed_bytes} bytes"
                        )
                    target.write(chunk)


def _validate_zip_member(member: zipfile.ZipInfo) -> PurePosixPath:
    if "\\" in member.filename:
        raise ValueError(f"unsafe ZIP path: {member.filename!r}")
    relative = PurePosixPath(member.filename)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError(f"unsafe ZIP path: {member.filename!r}")
    mode = member.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise ValueError(f"ZIP symlink entries are not allowed: {member.filename!r}")
    if member.flag_bits & 0x1:
        raise ValueError(f"Encrypted ZIP entries are not allowed: {member.filename!r}")
    return relative


def _reject_existing_symlink_path(root: Path, parent: Path) -> None:
    try:
        relative = parent.relative_to(root)
    except ValueError as exc:
        raise ValueError("unsafe ZIP path outside output directory") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Refusing to traverse symlink while extracting ZIP: {current}")


def find_mineru_result_dir(path: str | Path) -> Path:
    path = Path(path)
    if _is_mineru_result_dir(path):
        return path
    for child in path.rglob("*"):
        if child.is_dir() and _is_mineru_result_dir(child):
            return child
    raise FileNotFoundError(f"No MinerU result JSON found under {path}")


def _is_mineru_result_dir(path: Path) -> bool:
    return (
        (path / "block_list.json").exists()
        or (path / "content_list_v2.json").exists()
        or (path / "content_list.json").exists()
        or bool(list(path.glob("*_content_list_v2.json")))
        or bool(list(path.glob("*_content_list.json")))
    )


def _find_extract_result(raw_result: dict[str, Any], file_name: str) -> dict[str, Any]:
    results = raw_result.get("data", {}).get("extract_result") or []
    if not results:
        return {}
    for item in results:
        if item.get("file_name") == file_name:
            return item
    return results[0]
