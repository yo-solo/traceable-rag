# 快速上手

本文帮助最终用户用已验收镜像跑起 Traceable RAG，并完成“配置 Provider → 导入文档 → Retrieval → Source 核验 → Codex Skill”的最小闭环。源码开发流程放在文末。

## 1. 安装已发布版本

准备 Docker Desktop，或 Linux Docker Engine + Compose v2。把 `vX.Y.Z` 替换为 Releases 页面中的明确版本。

公开 Release 与 GHCR package 发布以后，安装器可以匿名下载固定版本发布物。首个公开 Release 尚未发布时，请使用本文末尾的源码开发入口。受限镜像、私有 fork 或企业镜像若要求认证，先登录 GitHub CLI 与 GHCR，再下载、校验并运行本地发布物。

需要 GitHub 认证时的 macOS / Linux 安装方式如下。

```bash
gh auth login
gh auth refresh -h github.com -s read:packages
gh auth token | docker login ghcr.io -u "$(gh api user --jq .login)" --password-stdin
tmp=$(mktemp -d)
gh release download vX.Y.Z --repo yo-solo/traceable-rag --dir "$tmp"
if command -v sha256sum >/dev/null 2>&1; then
  (cd "$tmp" && sha256sum -c SHA256SUMS)
else
  (cd "$tmp" && shasum -a 256 -c SHA256SUMS)
fi
tar -xzf "$tmp/traceable-rag-deployment-vX.Y.Z.tar.gz" -C "$tmp"
chmod 700 "$tmp/install.sh"
"$tmp/install.sh"
```

需要 GitHub 认证时的 Windows PowerShell 安装方式如下。

```powershell
gh auth login
gh auth refresh -h github.com -s read:packages
$Version = "vX.Y.Z"
$User = gh api user --jq .login
gh auth token | docker login ghcr.io -u $User --password-stdin
$Temp = Join-Path ([IO.Path]::GetTempPath()) "traceable-rag-$([guid]::NewGuid())"
New-Item -ItemType Directory -Path $Temp | Out-Null
gh release download $Version --repo yo-solo/traceable-rag --dir $Temp
Get-Content (Join-Path $Temp "SHA256SUMS") | ForEach-Object {
  if ($_ -notmatch '^([0-9a-f]{64})\s+\*?(.+)$') { throw "Invalid SHA256SUMS entry" }
  $Actual = (Get-FileHash -LiteralPath (Join-Path $Temp $Matches[2]) -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($Actual -ne $Matches[1]) { throw "Checksum mismatch: $($Matches[2])" }
}
Expand-Archive -LiteralPath (Join-Path $Temp "traceable-rag-deployment-$Version.zip") -DestinationPath $Temp -Force
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File (Join-Path $Temp "install.ps1")
```

Release 与 package 公开后，可以分别简化为 `curl .../install.sh | sh` 或 `irm .../install.ps1 | iex`。安全敏感环境仍建议先下载 `SHA256SUMS` 并校验，再运行本地安装器。安装器不会绕过受限发布物的访问权限。

安装器只拉取固定版本的 `linux/amd64` 或 `linux/arm64` GHCR 镜像，不在本机编译项目。默认绑定 `127.0.0.1:8000`，独立创建配置、数据、状态、备份目录，并生成不会写入 env 的 32-byte 主密钥和管理 token。受限 Release 或 package 需先完成对应的 GitHub 下载授权与 `docker login ghcr.io`。

如果首次启动提示端口冲突，不要编辑安装目录内的文件，也不要删除已有数据。用同一版本安装器显式换一个端口重跑：

```bash
TRACEABLE_RAG_PORT=8001 sh '/path/to/install.sh'
```

```powershell
$env:TRACEABLE_RAG_PORT = "8001"
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File 'C:\path\to\install.ps1'
```

重跑会先核对原安装版本、installation marker 和原安装者，再原子更新 `deployment.env` 并让 Compose 在新端口重新创建容器；数据、主密钥、管理 token 和 installation ID 不会变化。未显式传入端口时，安装器沿用已保存的端口。

安装器会打印管理命令的绝对路径。如果 macOS 当前 PATH 不含 `~/.local/bin`，先按输出的 `export PATH=...` 和 zsh/bash 持久化提示处理；在此之前直接使用打印的绝对路径即可。

安装完成会在交互式终端显示管理配置 URL。不要把这个带 token 的 URL粘贴到工单、聊天或日志中；如果泄漏，立即执行：

```bash
traceable-rag admin rotate-token
```

在管理配置页保存 MinerU、Embedding、Reranker 和 LLM Provider 配置。Provider secret 由主密钥加密后写入独立 Provider store，安装器和 `deployment.env` 都不接收 Provider key。

## 2. 启动与自检

安装器默认自动启动。之后使用统一管理入口：

```bash
traceable-rag start
traceable-rag status
traceable-rag doctor --json
traceable-rag logs
```

`doctor` 默认只读取本地 installation、容器、`/livez`、`/readyz` 和 Provider profile 摘要，不调用外部 Provider。只有显式指定角色才会测试：

```bash
traceable-rag doctor --test-provider embedding --test-provider reranker
```

打开 `http://127.0.0.1:8000`。

## 3. 导入文档

在“知识库”页创建或选择知识库，可多选 PDF、Office 文档或 JPG/JPEG/PNG 图片上传。图片会生成单页 PDF 预览，并以原图直接提交 MinerU；后端队列随后执行页数预检、MinerU、归一化和索引，失败项可以重试。

## 4. 调试 Retrieval

打开 `/retrieval`，选择知识库、可选文档范围、`auto` 或 `keyword`，执行查询后检查：

- 实际 `pipeline_used`；
- 降级警告；
- 文档名、页码、候选文本与分数；
- Source 页面是否可打开。

HTTP 示例：

```bash
curl -s http://127.0.0.1:8000/api/retrieval \
  -H 'Content-Type: application/json' \
  -d '{"query":"What happened to EU income inequality after its 2014-15 peak?","knowledge_base_ids":["default"],"mode":"auto","top_k":8}'
```

## 5. 核验 Source

点击 Retrieval 结果或直接打开 `/sources/{source_id}`。页面以沉浸式 PDF 为核心，连续显示文档页面，并自动放大、居中和高亮真实 bbox 区域。Codex 引用可以附加 `#insight=<percent-encoded-text>`，在高亮旁显示可收起的简短核验提示；旧链接不带提示时仍可正常定位。

Source 页不会默认展示 Block 或原文转录面板。PDF 无法载入或 bbox 缺失时会明确报告失败，不使用文本匹配伪造更细的定位。

Agent 需要更多相邻内容时调用：

```bash
curl -s 'http://127.0.0.1:8000/api/sources/src_xxx/context?before=2&after=2'
```

未知 Source 返回 404；文档删除或重建后的旧 Source 返回 410，不会静默指向新内容。

## 6. 安装 Codex Skill

```bash
traceable-rag agent install-codex --knowledge-base-id default
traceable-rag agent doctor-codex
```

管理入口从正在运行的固定版本镜像中复制 Skill 到宿主 `$CODEX_HOME/skills/traceable-rag`，保留已有默认知识库选择并写入当前 base URL。`doctor-codex` 同时核对 Retrieval、`pipeline_used`、`source_url` 和 Source Context 的 OpenAPI 契约；无文档时不会伪造检索测试。

## 7. 备份、升级与回滚

```bash
traceable-rag backup
traceable-rag upgrade vX.Y.Z
traceable-rag rollback
```

升级必须指定确切版本。管理入口先拉取并解析镜像 digest，再停服务、复制数据、执行 SQLite `PRAGMA integrity_check`、生成 SHA-256 清单，验收通过后才切换；新版本 readiness 失败会自动恢复旧 digest 和升级前数据。主密钥与管理 token 不进入普通数据备份，避免把密文和解密材料放在同一份快照中。

## 8. 测试 Prompt

```text
使用 $traceable-rag 全面核验两份监管文件对个人数据保留期限的要求与例外，并让每个关键结论带可点击来源。
```

把部署交给 Codex 或其他 Agent 时，可使用：

```text
请按 https://github.com/yo-solo/traceable-rag/releases/tag/vX.Y.Z 部署 Traceable RAG。只使用该 Release 的校验和、manifest 和固定 digest；不要读取或代填任何密钥，不要开放远程访问。安装后把 /setup 页面交给我；等我确认配置完成，再安装并检查 Codex Skill，最后按 live、ready、Provider、Skill、Retrieval/Source 分项汇报。
```

## 9. 源码开发

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'

cd web
npm install
npm run build
cd ..

./scripts/deploy_local.sh

.venv/bin/python -m pytest -q
cd web
npm test -- --run
npm run build
npm run test:e2e
```

开发部署会构建当前 checkout，自动创建隔离的 dev 数据与 secret 文件，并按宿主非 root UID/GID 运行；不固定 CPU 架构。`.env` Provider 变量仅用于兼容性测试/发布验收，不是最终用户的密钥存储方案。

旧 `/api/query`、`/api/evidence/*`、`/api/traces/*`、CLI `search/ask-rag`、MCP 与 Dify 均不属于当前契约。
