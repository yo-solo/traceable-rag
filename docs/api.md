# API 接口文档

Traceable RAG 对外提供 Health、Knowledge Base、Documents、Processing Jobs、Retrieval、Source Context、Settings 与 File/Preview。服务端不生成答案，也不保存 Agent Trace。

OpenAPI 唯一事实源为 FastAPI `app.openapi()`，静态快照见 [openapi.json](openapi.json)。
运行 Web 应用后可通过 `/api-reference` 打开完整 API 参考，搜索全部接口、查看格式化
请求与响应示例和多语言代码。页面仅用于文档浏览，不发送业务 API 请求。

## 通用约定

- 默认基址：`http://127.0.0.1:8000`。
- JSON POST 使用 `Content-Type: application/json`。
- `human_page_number` 为 1-based。
- 所有 Retrieval 请求必须显式传入至少一个知识库 ID。
- 错误通常使用 `detail.code` 与安全的 `detail.message`；Pydantic schema 错误使用标准 `detail` 数组。
- Provider 密钥和原始上游错误不会出现在公共响应中。

## 接口总览

| 方法 | 路径 | 职责 |
|---|---|---|
| GET | `/api/health` | 服务与数据库状态 |
| GET/POST | `/api/knowledge-bases` | 列表与创建 |
| GET/PATCH/DELETE | `/api/knowledge-bases/{knowledge_base_id}` | 知识库详情、更新与删除 |
| GET | `/api/knowledge-bases/{knowledge_base_id}/documents` | 文档列表 |
| POST | `/api/knowledge-bases/{knowledge_base_id}/documents/upload` | 上传 PDF、Office 文档或 JPG/JPEG/PNG 图片 |
| GET/PATCH/DELETE | `/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}` | 文档详情、重命名与删除 |
| POST | `/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}/parse` | 重新提交解析任务 |
| POST | `/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}/index` | 重新提交索引任务 |
| GET | `/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}/blocks` | 调试用文档内容 |
| GET | `/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}/file` | PDF/原文件预览 |
| GET | `/api/knowledge-bases/{knowledge_base_id}/processing-jobs` | 知识库处理任务列表 |
| GET | `/api/processing-jobs/{job_id}` | 单个处理任务状态 |
| POST | `/api/retrieval` | Agent 检索候选 Source |
| GET | `/api/sources/{source_id}/context` | 命中原文与有限上下文 |
| GET/PATCH | `/api/settings` | 非敏感设置与接入信息 |
| POST | `/api/settings/diagnostics` | 服务与 Provider 诊断 |

旧 `/api/query`、`/api/evidence/*`、`/api/traces/*` 返回 404。

## 在线 API 参考

`/api-reference` 使用仓库锁定的 Scalar React 包自托管渲染，不通过 CDN 加载文档、
字体或脚本。页面默认使用简体中文，并提供搜索、锚点、请求/响应示例以及
cURL、JavaScript、Python 等客户端代码。

页面关闭全部在线调试能力：

- Scalar 不显示 Test Request 或 API Client 入口。
- 请求层只允许加载同源 `/openapi.json`；任何业务 API 请求都会在浏览器本地返回
  `403 api_reference_online_debug_disabled`，不会抵达服务端。
- 页面不保存认证信息，也不提供跨域代理。调用示例仅用于复制到开发者自己的工具中。

## `POST /api/retrieval`

请求：

```json
{
  "query": "个人数据的保留期限和例外是什么？",
  "knowledge_base_ids": ["kb-id"],
  "mode": "auto",
  "top_k": 8,
  "document_ids": ["optional-document-id"]
}
```

约束：

- 一个请求只接受一个 `query`。
- `mode` 仅为 `auto` 或 `keyword`。
- `top_k` 范围 1–50。
- `document_ids` 可省略；提供时每个文档必须属于声明的知识库范围。
- `auto` 使用部署中的最佳可用管线。
- `keyword` 用于条款号、数字、原句、标识符和专名。

响应：

```json
{
  "query": "个人数据的保留期限和例外是什么？",
  "knowledge_base_ids": ["kb-id"],
  "document_ids": null,
  "mode_requested": "auto",
  "pipeline_used": "dense_reranker",
  "degraded": false,
  "warnings": [],
  "results": [
    {
      "source_id": "src_0123456789abcdef0123456789abcdef",
      "source_url": "/sources/src_0123456789abcdef0123456789abcdef",
      "text": "Personal data must be kept only for as long as necessary...",
      "document_id": "document-id",
      "document_name": "Data Policy.pdf",
      "human_page_number": 12,
      "bbox": [72, 160, 510, 240],
      "score": 0.82
    }
  ]
}
```

`pipeline_used` 可能为：

- `keyword`
- `dense`
- `dense_reranker`

结构化 warning 常见 code：

- `embedding_unavailable`
- `embedding_index_incompatible`
- `embedding_request_failed`
- `reranker_unavailable`
- `retrieval_degraded`

分数仅用于排序与调试，不代表答案正确，也不表示证据已经充分。

## `GET /api/sources/{source_id}/context`

```text
GET /api/sources/src_xxx/context?before=2&after=2
```

- `before` / `after` 默认 2，范围 0–5。
- `source` 是命中原文。
- `before` / `after` 按正向阅读顺序返回。
- 返回标题路径、页码、bbox、页面尺寸、文档信息、`file_url` 与 `source_url`。
- 大表格或超长内容会截断，并返回 `truncated=true`。
- 不返回整篇文档的全部 Blocks。
- `source_url` 的 API 值保持 `/sources/{source_id}`。Agent 可在最终引用中追加展示层 fragment：`#insight=<percent-encoded-text>`；该 fragment 不发送到服务端，也不改变 Source ID 或生命周期。

响应示例：

```json
{
  "source": {
    "source_id": "src_0123456789abcdef0123456789abcdef",
    "source_url": "/sources/src_0123456789abcdef0123456789abcdef",
    "text": "Personal data must be kept only for as long as necessary...",
    "document_id": "document-id",
    "document_name": "Data Policy.pdf",
    "knowledge_base_id": "kb-id",
    "human_page_number": 12,
    "bbox": [72, 160, 510, 240],
    "page_size": [612, 792],
    "title_path": ["Retention"],
    "file_url": "/api/knowledge-bases/kb-id/documents/document-id/file"
  },
  "before": [],
  "after": [],
  "truncated": false
}
```

Source 生命周期：

- 未知或格式错误：`404 source_not_found`。
- 曾经有效，但文档已删除或重建：`410 source_gone`。
- 旧 Source 不会静默跳转到新文档内容。

## Health 与发现

Agent 开始工作时先调用：

```text
GET /api/health
GET /api/knowledge-bases
GET /api/knowledge-bases/{knowledge_base_id}/documents
```

只有在文档身份、比较集合或可用状态重要时才需要列文档。跨文档比较必须先锁定文件集合，再分别使用 `document_ids` 限定 Retrieval。

## Settings

`GET /api/settings` 只返回：

- 非敏感默认设置；
- 服务、文档和 Provider 配置状态摘要；
- Codex Skill 模板/安装状态；
- Retrieval、Source Context、Health 示例。

设置 API 不返回 Key、Authorization header、内部路径或 Provider 原始错误。

## Python SDK

```python
from traceable_rag.sdk import TraceableRagClient

client = TraceableRagClient(store)
result = client.retrieve(
    "个人数据保留期限",
    knowledge_base_ids=["kb-id"],
    mode="keyword",
)
context = client.get_source_context(result["results"][0]["source_id"])
```

SDK 不提供 `query()`、`search_evidence()`、`get_evidence()` 或 `create_trace()`。

## 安全边界

- 默认监听 `127.0.0.1`。
- 远程部署应由反向代理处理 HTTPS 和认证。
- 服务不内置用户/权限系统。
- Retrieval 不搜索声明范围之外的文档。
- Source 页面只定位真实当前版本原文。
