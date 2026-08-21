# 部署与运维

本文描述 Release 安装器和 `traceable-rag` 管理入口的安全边界。源码开发使用 `scripts/deploy_local.sh`，不要把开发 Compose 当作最终用户安装方式。

## 发布物与安装

每个 `vX.Y.Z` Git tag 只有在以下门禁全部通过后才发布：

1. tag commit 是 `origin/main` 的祖先；
2. amd64 候选镜像完成真实文档、Retrieval、Source、浏览器、SBOM 和关键漏洞验收；
3. arm64 候选镜像在 QEMU 下实际启动，`/readyz`、`/livez` 和 `libreoffice --version` 通过；
4. 验收之后才登录 GHCR，并发布包含 `linux/amd64`、`linux/arm64` 的 manifest；
5. GitHub Release 同时附带 `install.sh`、`install.ps1`、POSIX/PowerShell 管理入口、Compose bundle 和 `SHA256SUMS`。

安装器从明确的版本 Release 下载 bundle、独立 `release-manifest.json` 和 `SHA256SUMS`，同时核对 bundle 内清单与独立清单完全一致。清单固定版本、源码 commit、多架构 platform digest 和最终 index digest；Compose 中没有 `build`，运行配置直接使用 `image@index_digest`。升级也必须显式给出版本。

仓库与 GHCR package 的 visibility 属于发布管理员决策，workflow 不会擅自改变。公开 Release 和 package 可以通过 HTTPS 匿名下载。受限 Release、私有 fork 或企业镜像需要先用 `gh auth login` 登录有权限的账号，通过 stdin 用最小 `read:packages` token 执行 `docker login ghcr.io`，再用 `gh release download` 下载全部 assets、校验 `SHA256SUMS`、解压 bundle 后运行本地安装器。

若现有 GitHub CLI token 缺少 package 读取权限，先由用户本人执行 `gh auth refresh -h github.com -s read:packages`；组织启用 SSO 时还需在 GitHub 中授权该组织。安装器和 Agent 不应索取或打印 token。

## 默认隔离

默认只接受 `127.0.0.1`、`localhost` 或 `::1`，管理入口直接拒绝非回环地址。Web 服务同时校验受信 Host；浏览器发出的 POST、PUT、PATCH、DELETE 若带 `Origin`，必须与当前请求严格同源，并用 `Referer` / `Sec-Fetch-Site` 兜底阻断跨站副作用。无浏览器头的本机 CLI/Agent 请求仍可使用。页面响应附带 `frame-ancestors 'none'` 与 `X-Frame-Options: DENY`，降低点击劫持风险。

本 bundle 不提供公网暴露开关；如需远程服务，应另行设计认证、TLS、反向代理、可信代理头、限流和资源授权，不能只扩大 bind address。

macOS / Linux 默认目录：

| 用途 | 默认路径 |
| --- | --- |
| 配置与管理脚本 | `~/.config/traceable-rag` |
| 数据 | `~/.local/share/traceable-rag` |
| 状态 | `~/.local/state/traceable-rag` |
| 备份 | `~/.local/state/traceable-rag/backups` |
| 命令链接 | `~/.local/bin/traceable-rag` |

Windows 默认使用 `%LOCALAPPDATA%\TraceableRAG\{config,data,state,backups,bin}`。

可在首次安装前分别设置 `TRACEABLE_RAG_CONFIG_DIR`、`TRACEABLE_RAG_DATA_DIR`、`TRACEABLE_RAG_STATE_DIR`、`TRACEABLE_RAG_BACKUP_DIR`、`TRACEABLE_RAG_PROJECT_NAME` 和 `TRACEABLE_RAG_PORT`，从而并行部署多个互不碰撞的实例。配置、数据和状态目录必须分离。安装器不会认领已有内容但没有 `.traceable-rag-installation` 标记的目录，避免未来 purge 误删用户文件。

端口是已有安装唯一允许通过同版本安装器显式迁移的布局值。首次启动若因默认 `8000` 被占用而失败，macOS / Linux 使用 `TRACEABLE_RAG_PORT=8001 sh '/path/to/install.sh'`，Windows PowerShell 先执行 `$env:TRACEABLE_RAG_PORT = "8001"`，再用原命令运行 `install.ps1`。安装器必须先通过版本、目录 marker、命令入口归属和原安装者校验，随后在同目录生成临时文件并原子替换 `deployment.env`。主密钥、管理 token、数据目录和 installation ID 均不重建；正常自动启动会执行 Compose `up`，由 Compose 按新端口重新创建服务。没有显式端口时始终采用 `deployment.env` 中已经保存的值。

POSIX 安装器把 Compose user 固定为安装者的非 root UID/GID，并预创建 `/data/home` 与 `/data/managed`。Windows Docker Desktop 保留镜像内非 root `10001:10001`。

## Secret 边界

安装器生成：

- 32 个原始随机字节的主密钥，容器内路径 `/run/secrets/traceable-rag-master-key`；
- 32 个随机字节编码成 64 位 hex 的管理 token，容器内路径 `/run/secrets/traceable-rag-admin-token`。

两者在宿主上是独立文件，只读挂载。POSIX 权限为 `0600`；Windows 拒绝 ReparsePoint，并把 ACL 收紧到当前用户和 SYSTEM。`deployment.env` 只有镜像、项目、端口和路径等非 Provider 配置，Provider key 永远不写入该文件。

管理配置 URL 使用 `/setup#token=...` fragment，避免 token 进入 HTTP request line 和 Uvicorn/代理访问日志。页面读取后应立即清除地址栏 fragment。即便如此，也不能把 URL 复制到聊天、工单、截图或日志；如怀疑泄漏：

```bash
traceable-rag admin rotate-token
```

该命令原子替换 token，并在服务运行时强制重建容器，使旧 URL 立即失效。

## 日常命令

```bash
traceable-rag start
traceable-rag stop
traceable-rag status
traceable-rag logs --follow
traceable-rag doctor --json
```

`doctor` 默认不调用外部 Provider。它返回 installation、固定版本/digest、容器状态、`livez`、`readyz`、Provider profile 摘要和 Codex Skill 安装命令。Provider 测试必须逐个显式选择：

```bash
traceable-rag doctor --test-provider embedding
traceable-rag doctor --test-provider reranker --test-provider llm
```

`mineru` test 可能返回 `not_supported`，因为真实 MinerU 验收需要文档处理流程；管理脚本不会把它伪装成已完成 E2E。

## 备份、升级与回滚

```bash
traceable-rag backup
traceable-rag upgrade vX.Y.Z
traceable-rag rollback
traceable-rag recover
```

备份流程会在必要时暂停容器，复制数据目录，再用当前固定镜像内的 Python 对快照数据库执行 `PRAGMA integrity_check`，并流式生成 `checksums.sha256`。验证失败会删除未完成快照并恢复原服务。

普通备份不复制主密钥或管理 token。这样可以避免把 Provider 密文和解密材料放进同一份备份，但也意味着该命令不是完整的异机灾备：必须另外使用受控的 secret 备份流程保存主密钥，否则新的机器无法解密 `providers.db`。当前管理入口只自动消费这些快照完成升级失败恢复和 `rollback`，尚未提供通用的任意快照 restore 命令。

`upgrade` 先下载并校验目标 release bundle。GitHub CLI 已登录时优先调用 `gh release download`，匿名公开发布物或显式自定义下载地址使用 HTTP。随后它以清单中的 index digest 拉取镜像，并把新 manager、Compose、清单和 env 作为同一份控制面记录保存，之后才停服务和备份。只有新版本 `/readyz` 成功后才写入 successful-upgrade pointer；失败会恢复旧数据与旧控制面。`rollback` 同时恢复升级前数据、镜像和管理脚本，并在动作前创建安全快照。

会暂停或改写部署的命令持有 installation-scoped 互斥锁，并写入 pending-operation journal。若进程或主机在备份、升级或回滚中断，`status`、`logs` 和 `doctor` 仍可用于诊断；修复 Docker 后执行 `traceable-rag recover`，管理入口会按 journal 恢复到最后已知正常的一侧，再清除 pending 状态。默认 readiness 等待 900 秒，并每 30 秒报告进度；可在首次安装时用 `TRACEABLE_RAG_READY_TIMEOUT_SECONDS` 调整到 30–86400 秒。

## Codex Skill

```bash
traceable-rag agent install-codex --knowledge-base-id default
traceable-rag agent doctor-codex
```

安装命令通过 `docker cp` 从运行容器的 `/app/integrations/codex-skill/traceable-rag` 复制到宿主 Codex 目录；容器 Web 应用从不写宿主 Skill 目录。已有 `runtime.md` 的默认知识库会保留，base URL 更新为本实例地址。

`doctor-codex` 除了检查文件和 readiness，还会核对 OpenAPI 包含 `/api/retrieval`、`pipeline_used`、`source_url` 和 Source Context 路径。没有文档时不会强行发起检索。

## 给 Agent 的部署合同

把明确版本的 Release 链接和下面的 Prompt 交给 Codex 或其他本机 Agent。需要登录、密钥或冲突决策时应由用户前置处理：

```text
请帮我在当前机器部署 Traceable RAG vX.Y.Z。

先只读预检操作系统与 CPU 架构、Docker 和 Compose v2、8000 端口，以及拟使用的 config、data、state、backup、bin 绝对路径。目录非空但没有本项目匹配的 installation marker 或已有不同 installation ID 时立即停止，不覆盖、不删除。若仅默认端口冲突，先让我选择新端口，再通过 `TRACEABLE_RAG_PORT` 用同一版本安装器重跑；不要手工编辑 `deployment.env`。

只使用 https://github.com/yo-solo/traceable-rag/releases/tag/vX.Y.Z 的发布物。不要从源码构建，不使用 latest，不使用 root/sudo。下载全部 assets，逐项验证 SHA256SUMS，并核对 release-manifest 的版本、source commit、平台 digest 和 index digest；任何不一致都停止。

仓库或 GHCR 需要认证时，不要索取、读取、打印或保存我的 GitHub/GHCR token。请前置告诉我执行 gh 登录、read:packages 或 SSO 授权。不要修改仓库或 package visibility。

保持服务只绑定 127.0.0.1，不修改防火墙、代理、VPN 或 DNS，不开放局域网或公网。不要把 MinerU、Embedding、Reranker 或 LLM 密钥写入 env、命令行、聊天或日志。安装后把本机 setup 页面交给我填写，不要在回复中复制完整 token URL；等我明确说“配置完成”后再继续。

不要执行 purge、强制卸载或删除已有数据。安装后验证固定版本和镜像 digest、非 root 容器、回环监听、/livez、/readyz、doctor，以及 deployment.env 不含 Provider secret。随后使用 traceable-rag agent install-codex 安装 Skill，并运行 doctor-codex；无法确认归属的 Skill 路径不得覆盖。

最终只汇报非敏感信息，包括版本、digest、目录、监听地址、健康状态、Skill 状态、降级项和仍需我手工完成的步骤。不要只凭容器 running 宣称部署完成。
```

## 卸载

```bash
traceable-rag uninstall
```

默认只删除容器/网络和命令入口，保留配置、数据与备份，方便重装。

```bash
traceable-rag uninstall --purge --yes
```

purge 是显式破坏性操作。管理入口会先核对每个目标目录的 installation ID 标记，并拒绝根目录、HOME 或被篡改的路径；没有匹配标记就不删除。
