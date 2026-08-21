import json
from pathlib import Path

from traceable_rag.openapi import build_openapi_spec
from traceable_rag.web_app import app


def test_openapi_spec_uses_the_fastapi_application_as_its_only_source():
    assert build_openapi_spec() == app.openapi()


def test_openapi_spec_exposes_only_current_retrieval_and_source_paths():
    paths = build_openapi_spec()["paths"]
    expected_operations = {
        ("GET", "/livez"),
        ("GET", "/readyz"),
        ("GET", "/api/health"),
        ("GET", "/api/provider-profiles"),
        ("PUT", "/api/provider-profiles/{role}"),
        ("DELETE", "/api/provider-profiles/{role}"),
        ("POST", "/api/provider-profiles/{role}/test"),
        ("GET", "/api/settings"),
        ("PATCH", "/api/settings"),
        ("POST", "/api/settings/diagnostics"),
        ("GET", "/api/knowledge-bases"),
        ("POST", "/api/knowledge-bases"),
        ("GET", "/api/knowledge-bases/{knowledge_base_id}"),
        ("PATCH", "/api/knowledge-bases/{knowledge_base_id}"),
        ("DELETE", "/api/knowledge-bases/{knowledge_base_id}"),
        ("GET", "/api/knowledge-bases/{knowledge_base_id}/documents"),
        ("POST", "/api/knowledge-bases/{knowledge_base_id}/documents/upload"),
        ("GET", "/api/processing-jobs/{job_id}"),
        ("GET", "/api/knowledge-bases/{knowledge_base_id}/processing-jobs"),
        (
            "GET",
            "/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}",
        ),
        (
            "PATCH",
            "/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}",
        ),
        (
            "DELETE",
            "/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}",
        ),
        (
            "GET",
            "/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}/file",
        ),
        (
            "GET",
            "/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}/assets/{asset_id}",
        ),
        (
            "GET",
            "/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}/blocks",
        ),
        (
            "POST",
            "/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}/parse",
        ),
        (
            "POST",
            "/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}/index",
        ),
        ("POST", "/api/retrieval"),
        ("GET", "/api/sources/{source_id}/context"),
    }
    other_legacy_paths = {
        "/api/retrieval/search",
        "/api/rag/ask",
        "/api/agent/investigate",
        "/kb/documents/{document_id}",
    }

    actual_operations = {
        (method.upper(), path)
        for path, path_item in paths.items()
        for method in path_item
        if method in {"get", "post", "put", "patch", "delete"}
    }

    assert len(paths) == 22
    assert actual_operations == expected_operations
    assert not any(
        path == "/api/documents" or path.startswith("/api/documents/")
        for path in paths
    )
    assert other_legacy_paths.isdisjoint(paths)
    assert {
        "/api/evidence/search",
        "/api/evidence/{evidence_id}",
        "/api/query",
        "/api/traces",
        "/api/traces/{trace_id}",
    }.isdisjoint(paths)


def test_removed_query_evidence_and_trace_routes_return_404():
    from fastapi.testclient import TestClient

    client = TestClient(app)
    assert client.post("/api/query", json={}).status_code == 404
    assert client.post("/api/evidence/search", json={}).status_code == 404
    assert client.get("/api/evidence/old").status_code == 404
    assert client.get("/api/traces").status_code == 404
    assert client.get("/api/traces/old").status_code == 404


def test_static_openapi_json_is_reproducible_and_matches_runtime_spec():
    static_path = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"
    raw = static_path.read_text(encoding="utf-8")

    assert raw.endswith("\n")
    assert json.loads(raw) == build_openapi_spec()
    assert raw == json.dumps(
        build_openapi_spec(), ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"


def test_openapi_description_positions_verifiable_agent_retrieval():
    description = build_openapi_spec()["info"]["description"].lower()

    assert "verifiable document retrieval" in description
    assert "ai agents" in description
    assert "source context" in description


def test_openapi_operations_are_grouped_and_documented_for_the_reference_ui():
    spec = build_openapi_spec()
    assert spec["servers"] == [{"url": "/", "description": "当前同源部署"}]
    assert [tag["name"] for tag in spec["tags"]] == [
        "系统",
        "知识库",
        "文档",
        "处理任务",
        "检索",
        "原文定位",
        "设置",
        "Provider",
    ]

    for path_item in spec["paths"].values():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            assert len(operation["tags"]) == 1
            assert operation["summary"].strip()
            assert operation["description"].strip()


def test_openapi_operation_summaries_are_developer_facing():
    spec = build_openapi_spec()
    expected = {
        ("get", "/livez"): "进程存活探针",
        ("get", "/readyz"): "服务就绪探针",
        ("get", "/api/health"): "查看服务健康状态",
        ("get", "/api/provider-profiles"): "获取安全化 Provider 配置",
        ("put", "/api/provider-profiles/{role}"): "保存 Provider 配置",
        ("delete", "/api/provider-profiles/{role}"): "删除持久化 Provider 配置",
        ("post", "/api/provider-profiles/{role}/test"): "测试 Provider 连接",
        ("get", "/api/settings"): "获取设置和运行状态",
        ("patch", "/api/settings"): "更新非敏感设置",
        ("post", "/api/settings/diagnostics"): "运行依赖诊断",
        ("get", "/api/knowledge-bases"): "获取知识库列表",
        ("post", "/api/knowledge-bases"): "创建知识库",
        ("get", "/api/knowledge-bases/{knowledge_base_id}"): "获取知识库详情",
        ("patch", "/api/knowledge-bases/{knowledge_base_id}"): "更新知识库信息",
        ("delete", "/api/knowledge-bases/{knowledge_base_id}"): "删除知识库",
        ("get", "/api/knowledge-bases/{knowledge_base_id}/documents"): "获取知识库文档列表",
        ("post", "/api/knowledge-bases/{knowledge_base_id}/documents/upload"): "上传并处理文档",
        ("get", "/api/processing-jobs/{job_id}"): "获取处理任务状态",
        ("get", "/api/knowledge-bases/{knowledge_base_id}/processing-jobs"): "获取知识库处理任务",
        ("get", "/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}"): "获取文档详情",
        ("patch", "/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}"): "重命名文档",
        ("delete", "/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}"): "删除文档",
        ("get", "/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}/file"): "获取文档 PDF 预览",
        ("get", "/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}/assets/{asset_id}"): "获取文档图片",
        ("get", "/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}/blocks"): "获取文档结构化内容（Blocks）",
        ("post", "/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}/parse"): "重新解析文档",
        ("post", "/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}/index"): "重新生成文档索引",
        ("post", "/api/retrieval"): "检索可核验原文（Sources）",
        ("get", "/api/sources/{source_id}/context"): "获取 Source 原文上下文",
    }

    assert {
        (method, path): path_item[method]["summary"]
        for path, path_item in spec["paths"].items()
        for method in ("get", "post", "put", "patch", "delete")
        if method in path_item
    } == expected

    asset_description = spec["paths"][
        "/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}/assets/{asset_id}"
    ]["get"]["description"]
    assert "默认返回原图" in asset_description
    assert "`variant=preview`" in asset_description
    assert "`If-None-Match`" in asset_description

    retrieval_description = spec["paths"]["/api/retrieval"]["post"]["description"]
    assert "`source_id`" in retrieval_description
    assert "`degraded`" in retrieval_description
    assert "可能调用外部模型并产生费用" in retrieval_description


def test_openapi_documents_manual_request_bodies_and_binary_file_response():
    paths = build_openapi_spec()["paths"]
    upload = paths[
        "/api/knowledge-bases/{knowledge_base_id}/documents/upload"
    ]["post"]
    upload_schema = upload["requestBody"]["content"]["multipart/form-data"][
        "schema"
    ]
    assert upload_schema["required"] == ["file"]
    assert upload_schema["properties"]["file"]["format"] == "binary"
    assert "JPG、JPEG、PNG" in upload["description"]
    assert "原图直接提交 MinerU" in upload["description"]
    assert "JPG、JPEG、PNG" in upload_schema["properties"]["file"]["description"]

    settings = paths["/api/settings"]["patch"]
    diagnostics = paths["/api/settings/diagnostics"]["post"]
    assert settings["requestBody"]["content"]["application/json"]["schema"]
    assert diagnostics["requestBody"]["content"]["application/json"]["schema"]

    file_response = paths[
        "/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}/file"
    ]["get"]["responses"]["200"]
    assert file_response["content"]["application/pdf"]["schema"] == {
        "type": "string",
        "format": "binary",
    }

    asset_operation = paths[
        "/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}/assets/{asset_id}"
    ]["get"]
    asset_response = asset_operation["responses"]["200"]
    assert set(asset_response["content"]) == {
        "image/jpeg",
        "image/png",
        "image/webp",
    }
    assert all(
        content["schema"] == {"type": "string", "format": "binary"}
        for content in asset_response["content"].values()
    )
    assert asset_response["headers"]["ETag"]["schema"] == {"type": "string"}
    not_modified = asset_operation["responses"]["304"]
    assert "content" not in not_modified
    assert not_modified["headers"]["ETag"]["schema"] == {"type": "string"}
    variant = next(
        parameter
        for parameter in asset_operation["parameters"]
        if parameter["name"] == "variant"
    )
    assert variant["required"] is False
    assert variant["schema"]["default"] == "original"
    assert set(variant["schema"]["enum"]) == {"original", "preview"}


def test_openapi_contains_anonymous_examples_without_sensitive_authentication_material():
    spec = build_openapi_spec()
    schemas = spec["components"]["schemas"]
    assert schemas["KnowledgeBaseCreateRequest"]["examples"]
    assert schemas["RetrievalRequest"]["examples"]
    assert schemas["SourceContextResponseModel"]["examples"]

    knowledge_base_example = schemas["KnowledgeBaseCreateRequest"]["examples"][0]
    assert knowledge_base_example == {
        "name": "某季度财报知识库",
        "description": "用于归档某季度财务报告和经营分析资料",
    }
    assert schemas["KnowledgeBasePatchRequest"]["examples"][0]["name"] == (
        "某季度财报知识库"
    )
    assert schemas["KnowledgeBaseDeleteRequest"]["examples"][0] == {
        "confirm_name": "某季度财报知识库"
    }
    assert schemas["DocumentPatchRequest"]["examples"][0] == {
        "display_name": "某季度财务报告.pdf"
    }
    retrieval_example = schemas["RetrievalRequest"]["examples"][0]
    assert retrieval_example["query"] == "本季度营业收入和净利润分别是多少？"
    source_example = schemas["RetrievalSourceModel"]["examples"][0]
    assert source_example["document_name"] == "某季度财务报告.pdf"
    assert source_example["text"] == (
        "本季度营业收入和净利润均较上年同期增长，具体金额见合并利润表。"
    )
    context_example = schemas["SourceContextResponseModel"]["examples"][0]["source"]
    assert context_example["document_name"] == "某季度财务报告.pdf"
    assert context_example["text"] == source_example["text"]
    assert context_example["title_path"] == ["主要财务指标"]

    examples = [
        example
        for schema in schemas.values()
        for example in schema.get("examples", [])
    ]
    serialized = json.dumps(examples, ensure_ascii=False).casefold()
    for forbidden in ("api_key", "authorization", "bearer", "sk-"):
        assert forbidden not in serialized
    for private_example in ("内部客户样例", "私有验收文档"):
        assert private_example not in serialized
    assert not spec.get("security")


def test_openapi_documents_retrieval_and_source_contracts():
    spec = build_openapi_spec()
    schemas = spec["components"]["schemas"]

    retrieval_request = schemas["RetrievalRequest"]["properties"]
    assert set(retrieval_request["mode"]["enum"]) == {"auto", "keyword"}
    assert retrieval_request["document_ids"]
    retrieval_response = schemas["RetrievalApiResponseModel"]["properties"]
    assert retrieval_response["pipeline_used"]
    assert retrieval_response["degraded"]
    assert retrieval_response["warnings"]
    assert retrieval_response["results"]
    retrieval_source = schemas["RetrievalSourceModel"]
    assert "asset" in retrieval_source["properties"]
    assert "asset" not in retrieval_source["required"]
    assert "modality" not in retrieval_source["properties"]

    visual_reference = schemas["VisualAssetReferenceModel"]
    assert set(visual_reference["properties"]) == {
        "asset_id",
        "modality",
        "asset_url",
        "preview_url",
    }
    assert set(visual_reference["properties"]["modality"]["enum"]) == {
        "image",
        "chart",
    }

    context_response = schemas["SourceContextResponseModel"]["properties"]
    assert context_response["source"]
    assert context_response["before"]
    assert context_response["after"]
    assert context_response["truncated"]
    source = schemas["SourceContextSourceModel"]
    assert "asset" in source["properties"]
    assert "asset" not in source["required"]
    assert "modality" not in source["properties"]

    visual_detail = schemas["VisualAssetDetailModel"]
    assert {
        "content_type",
        "file_hash",
        "width",
        "height",
        "caption",
    }.issubset(visual_detail["properties"])
    assert "block_id" not in visual_detail["properties"]

    blocks_response = schemas["DocumentBlocksResponseModel"]
    assert {"blocks", "pages", "assets"}.issubset(blocks_response["properties"])
    assert "assets" in blocks_response["required"]
    block_asset_ref = blocks_response["properties"]["assets"][
        "additionalProperties"
    ]["$ref"]
    block_asset_schema = schemas[block_asset_ref.rsplit("/", 1)[-1]]
    assert "block_id" in block_asset_schema["properties"]

    document_properties = schemas["DocumentPayloadResponse"]["properties"]
    assert document_properties["embedding_record_count"]
    assert document_properties["embedding_ready"]
    assert document_properties["embedding_status"]
    assert document_properties["embedding_warning"]
