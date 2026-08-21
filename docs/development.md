# 开发与验收

## 本地质量门槛

```bash
.venv/bin/python -m pytest -q

cd web
npm test -- --run
npm run build
npm run test:e2e
```

改动 OpenAPI 后运行 `.venv/bin/python scripts/generate_openapi.py` 重新生成
`docs/openapi.json`，并运行 `tests/test_openapi.py`。

## 文档处理验收

- 上传返回持久化 job。
- 同一文档只能有一个活动处理任务。
- 状态覆盖 queued、parsing、indexing、ready、failed。
- 超页数、配置缺失与 Provider 失败返回安全错误。
- 失败可重试，不泄露密钥、上游响应或内部路径。
- 重建文档图会使旧 Source 返回 410。
- PDF 页数、Block 页码、bbox 和页面尺寸保持一致。

## 全新公开样例验收

这一轮禁止复用旧 V2 数据库、旧 MinerU 目录或历史索引。

默认语料位于 `examples/vidore/documents`，包含四份可公开分发的 ViDoRe
PDF 节选。命令也接受 DOCX，但只会处理当前目录下的 PDF 与 DOCX 文件。

```bash
export TRACEABLE_ACCEPTANCE_ROOT="$(mktemp -d)"

.venv/bin/python -m traceable_rag.cli acceptance-corpus \
  --db "$TRACEABLE_ACCEPTANCE_ROOT/traceable_v3.db" \
  --storage-root "$TRACEABLE_ACCEPTANCE_ROOT/managed" \
  --report "$TRACEABLE_ACCEPTANCE_ROOT/acceptance.json" \
  --expected-document-count 4
```

报告应验证预览 PDF、Blocks、Chunks、Index Records、Embedding 覆盖或关键词回退。
内置问题分别检查编译器中端优化、跨页继承表、收入不平等曲线和未来职业中的性别隔离。
第五个问题与语料无关，用来检查证据不足时的保守处理。

## Retrieval 验收

- `auto`、`keyword` 与文档范围隔离。
- Dense/关键词候选与 Reranker 失败降级。
- 实际 `pipeline_used` 和结构化 warning。
- 查询级、内容类型、文档维度诊断。
- Source 结果只包含公开 Agent 字段。
- 未索引范围返回稳定领域错误。

## Source 验收

- Source URL 可打开率 100%。
- Context 前后边界 0 到 5。
- 前后内容按阅读顺序返回。
- 大内容有长度上限与 `truncated=true`。
- 未知 Source 404。
- 文档删除或重建后的 Source 410。
- PDF 自动跳页与 bbox 高亮。
- Codex 侧栏窄屏无横向溢出、滚动陷阱或遮挡。

## Skill 验收

- 简单问题走快速路径。
- 多条件问题拆成独立 query。
- 相关性与上下文完整性分别判断。
- Context 只在明确缺口时调用。
- 跨文档比较锁定文档集合并维护覆盖矩阵。
- 最多 3 轮、12 次 Retrieval、12 次 Context。
- 无答案时克制，不使用知识库外知识补证据。
- 关键陈述就近使用可点击 Source 引用。
- 冲突、部分支持、410 和截断均有保守处理。

## 发布 smoke

`scripts/release_api_smoke.py` 会依次验证下面的接口。

```text
Health
→ Knowledge Bases / Documents
→ Retrieval probes
→ Source Context
→ Source verification HTML route
```

它不调用内置 LLM，也不创建 Query/Trace。Release 流水线从 smoke 报告读取运行时返回的 Source ID，生成 Retrieval 与 Source 页面截图。

## 发布门槛

- Python 全量测试通过。
- 前端单元测试、构建与 E2E 通过。
- Docker 构建与健康检查通过。
- 全新 V3 数据库验收通过。
- OpenAPI 与运行接口一致。
- `/api/query`、`/api/evidence/*`、`/api/traces/*` 返回 404。
- 临时文件、评测产物与缓存审计通过。
- 密钥扫描通过。
- README 命令可从全新 clone 复现。
