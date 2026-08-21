from __future__ import annotations

import io
import socket
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

from traceable_rag.mineru_client import (
    download_file,
    extract_zip,
    find_mineru_result_dir,
    MinerUClient,
    MinerUParseResult,
    MinerUUploadError,
    mineru_api_key_from_doc,
    mineru_api_key_from_environment,
    sanitize_diagnostic_text,
)


@pytest.fixture(autouse=True)
def public_mineru_transfer_dns(monkeypatch):
    monkeypatch.setattr(
        "traceable_rag.mineru_client.socket.getaddrinfo",
        lambda _host, port, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
        ],
    )


@pytest.mark.parametrize(
    "filename",
    ("content_list_v2.json", "report_content_list_v2.json"),
)
def test_find_mineru_result_dir_accepts_v2_only_output(tmp_path, filename):
    result_dir = tmp_path / "nested" / "document"
    result_dir.mkdir(parents=True)
    (result_dir / filename).write_text("[]", encoding="utf-8")

    assert find_mineru_result_dir(tmp_path) == result_dir


class FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = io.BytesIO(payload)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self.payload.read(size)


def test_mineru_token_prefers_process_canonical_over_dotenv_legacy(
    tmp_path, monkeypatch
):
    (tmp_path / ".env").write_text(
        "MINERU_API_KEY=dotenv-legacy-token\n", encoding="utf-8"
    )
    monkeypatch.setenv("MINERU_TOKEN", "process-canonical-token")

    assert (
        mineru_api_key_from_environment(runtime_root=tmp_path)
        == "process-canonical-token"
    )


def test_mineru_process_token_overrides_legacy_document_value(tmp_path, monkeypatch):
    provider_doc = tmp_path / "provider.txt"
    provider_doc.write_text("# MinerU\nAPI-Key:\ndocument-token\n", encoding="utf-8")
    monkeypatch.setenv("MINERU_TOKEN", "process-token")

    assert (
        mineru_api_key_from_doc(provider_doc, runtime_root=tmp_path)
        == "process-token"
    )


def test_mineru_v4_headers_remain_bearer_token_only():
    from traceable_rag.mineru_client import MinerUClient

    headers = MinerUClient("token-placeholder")._headers()

    assert headers["Authorization"] == "Bearer token-placeholder"
    assert "ak" not in {name.lower() for name in headers}
    assert "sk" not in {name.lower() for name in headers}


def test_mineru_errors_do_not_echo_upstream_payload():
    from traceable_rag.mineru_client import MinerUClient

    secret = "upstream-secret-body"
    client = MinerUClient("test-token")
    client._post_json = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "code": 401,
        "message": secret,
    }

    with pytest.raises(RuntimeError) as error:
        client.create_upload_url(file_name="safe.pdf")

    assert "upload-url request failed" in str(error.value)
    assert secret not in str(error.value)


def test_mineru_upload_error_does_not_echo_subprocess_output(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from traceable_rag.mineru_client import MinerUClient

    secret = "signed-url-or-upstream-secret"
    source = tmp_path / "safe.pdf"
    source.write_bytes(b"pdf")
    monkeypatch.setattr(
        "traceable_rag.mineru_client.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=22,
            stderr=secret,
            stdout=secret,
        ),
    )

    with pytest.raises(RuntimeError) as error:
        MinerUClient("test-token").upload_file("https://signed.example/upload", source)

    assert "file upload failed" in str(error.value)
    assert secret not in str(error.value)


def test_mineru_upload_pins_public_https_and_forbids_redirects(tmp_path, monkeypatch):
    from types import SimpleNamespace

    source = tmp_path / "safe.pdf"
    source.write_bytes(b"pdf")
    captured = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("traceable_rag.mineru_client.subprocess.run", run)

    MinerUClient("test-token").upload_file(
        "https://signed.example:8443/upload?signature=safe", source
    )

    command = captured["command"]
    assert command[command.index("--proto") + 1] == "=https"
    assert command[command.index("--proto-redir") + 1] == "=https"
    assert command[command.index("--max-redirs") + 1] == "0"
    assert "signed.example:8443:93.184.216.34" in command
    assert captured["kwargs"]["check"] is False


@pytest.mark.parametrize(
    "url",
    (
        "file:///tmp/overwrite",
        "http://signed.example/upload",
        "https://user:password@signed.example/upload",
        "https://localhost/upload",
    ),
)
def test_mineru_upload_rejects_unsafe_transfer_urls_without_starting_curl(
    tmp_path, monkeypatch, url
):
    source = tmp_path / "safe.pdf"
    source.write_bytes(b"pdf")
    started = False

    def run(*_args, **_kwargs):
        nonlocal started
        started = True

    monkeypatch.setattr("traceable_rag.mineru_client.subprocess.run", run)

    with pytest.raises(ValueError, match="MinerU transfer URL"):
        MinerUClient("test-token").upload_file(url, source)

    assert started is False


def test_mineru_upload_rejects_hostname_resolving_to_private_address(
    tmp_path, monkeypatch
):
    source = tmp_path / "safe.pdf"
    source.write_bytes(b"pdf")
    monkeypatch.setattr(
        "traceable_rag.mineru_client.socket.getaddrinfo",
        lambda _host, port, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))
        ],
    )

    with pytest.raises(ValueError, match="public addresses"):
        MinerUClient("test-token").upload_file(
            "https://signed.example/upload", source
        )


def test_mineru_upload_error_keeps_safe_http_diagnostics(tmp_path, monkeypatch):
    from types import SimpleNamespace

    source = tmp_path / "safe.pdf"
    source.write_bytes(b"pdf")
    upstream = (
        "<Error><Code>AccessDenied</Code>"
        "<Message>signature=private-secret https://signed.example/private "
        "/private/customer/source.pdf</Message></Error>"
    )
    monkeypatch.setattr(
        "traceable_rag.mineru_client.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=22,
            stderr="curl: (22) provider rejected request",
            stdout=f"{upstream}\n__TRACEABLE_HTTP_STATUS__=403",
        ),
    )

    with pytest.raises(MinerUUploadError) as error:
        MinerUClient("test-token").upload_file(
            "https://signed.example/upload?token=private", source
        )

    assert error.value.reason == "http_error"
    assert error.value.http_status == 403
    assert error.value.curl_exit_code == 22
    assert "AccessDenied" in (error.value.response_detail or "")
    assert "private-secret" not in (error.value.response_detail or "")
    assert "signed.example" not in (error.value.response_detail or "")
    assert "/private/customer" not in (error.value.response_detail or "")


def test_diagnostic_sanitizer_redacts_credentials_urls_and_paths():
    rendered = sanitize_diagnostic_text(
        "token=secret authorization:Bearer-secret "
        "https://signed.example/private /private/customer/source.pdf "
        r"C:\private\customer\source.pdf provider quota exhausted"
    )

    assert "provider quota exhausted" in rendered
    assert "secret" not in rendered
    assert "signed.example" not in rendered
    assert "/private/customer" not in rendered
    assert r"C:\private" not in rendered


def test_mineru_poll_timeout_does_not_echo_last_upstream_result(monkeypatch):
    from traceable_rag.mineru_client import MinerUClient

    secret = "last-upstream-result-secret"
    client = MinerUClient("test-token")
    client.get_batch_result = lambda _batch_id: {  # type: ignore[method-assign]
        "message": secret,
        "data": {"extract_result": [{"state": "running"}]},
    }
    times = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr("traceable_rag.mineru_client.time.time", lambda: next(times))
    monkeypatch.setattr("traceable_rag.mineru_client.time.sleep", lambda _seconds: None)

    with pytest.raises(TimeoutError) as error:
        client.poll_batch_result("safe-batch", poll_interval=0, timeout_seconds=1)

    assert "safe-batch" in str(error.value)
    assert secret not in str(error.value)


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired(
            ["curl", "https://signed.example/timeout-secret"], timeout=1
        ),
        OSError("signed-url-secret-os-error"),
    ],
)
def test_mineru_upload_converts_subprocess_exceptions_to_sanitized_runtime_error(
    tmp_path, monkeypatch, failure
):
    source = tmp_path / "safe.pdf"
    source.write_bytes(b"pdf")
    monkeypatch.setattr(
        "traceable_rag.mineru_client.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(RuntimeError) as error:
        MinerUClient("test-token").upload_file(
            "https://signed.example/secret-query", source
        )

    assert str(error.value) == "MinerU file upload failed."
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert "signed" not in str(error.value).lower()


def test_mineru_parse_result_repr_hides_signed_url_and_raw_result(tmp_path):
    signed_url = "https://signed.example/private-secret"
    raw_secret = "raw-upstream-secret"
    result = MinerUParseResult(
        batch_id="safe-batch",
        file_name="safe.pdf",
        state="done",
        output_dir=tmp_path,
        zip_path=None,
        full_zip_url=signed_url,
        raw_result={"message": raw_secret},
    )

    rendered = repr(result)
    assert signed_url not in rendered
    assert raw_secret not in rendered


def test_download_file_streams_with_limit_and_atomic_cleanup(tmp_path, monkeypatch):
    output = tmp_path / "result.zip"
    monkeypatch.setattr(
        "traceable_rag.mineru_client._NO_REDIRECT_OPENER.open",
        lambda *_args, **_kwargs: FakeResponse(b"123456"),
    )

    with pytest.raises(ValueError, match="download size"):
        download_file("https://example.test/result.zip", output, max_bytes=5, chunk_size=2)

    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_download_file_rejects_unsafe_url_before_creating_partial_file(tmp_path):
    output = tmp_path / "result.zip"

    with pytest.raises(ValueError, match="credential-free HTTPS"):
        download_file("file:///tmp/provider-result.zip", output)

    assert not output.exists()


def test_extract_zip_rejects_path_traversal_without_partial_output(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("safe.txt", "safe")
        zf.writestr("../escape.txt", "bad")

    with pytest.raises(ValueError, match="unsafe ZIP path"):
        extract_zip(archive, tmp_path / "out")

    assert not (tmp_path / "out" / "safe.txt").exists()
    assert not (tmp_path / "escape.txt").exists()


def test_extract_zip_rejects_symlink_and_zip_bomb_metadata(tmp_path):
    symlink_zip = tmp_path / "symlink.zip"
    with zipfile.ZipFile(symlink_zip, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "target")
    with pytest.raises(ValueError, match="symlink"):
        extract_zip(symlink_zip, tmp_path / "symlink-out")

    bomb_zip = tmp_path / "bomb.zip"
    with zipfile.ZipFile(bomb_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("huge.txt", b"0" * 10000)
    with pytest.raises(ValueError, match="compression ratio"):
        extract_zip(bomb_zip, tmp_path / "bomb-out", max_compression_ratio=2)


def test_extract_zip_enforces_member_and_total_size_limits(tmp_path):
    archive = tmp_path / "limits.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.txt", "1234")
        zf.writestr("b.txt", "5678")

    with pytest.raises(ValueError, match="member count"):
        extract_zip(archive, tmp_path / "members", max_members=1)
    with pytest.raises(ValueError, match="uncompressed size"):
        extract_zip(archive, tmp_path / "size", max_uncompressed_bytes=7)


def test_extract_zip_extracts_valid_regular_files(tmp_path):
    archive = tmp_path / "valid.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("nested/result.txt", "ok")

    extract_zip(archive, tmp_path / "out")

    assert (tmp_path / "out" / "nested" / "result.txt").read_text() == "ok"
