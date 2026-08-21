import { ArrowRight, ShieldCheck, TerminalSquare } from "lucide-react";
import { Link } from "react-router-dom";

import { useAdminTokenFromUrl } from "./adminToken";
import { ProviderManager } from "./ProviderManager";

export function SetupPage() {
  useAdminTokenFromUrl();

  return (
    <div className="setup-page">
      <a className="skip-link" href="#setup-content">跳到配置内容</a>
      <header className="setup-page__topbar">
        <Link className="setup-page__brand" to="/knowledge-bases" aria-label="Traceable RAG 首页">
          <img alt="" aria-hidden="true" src="/brand/traceable-rag-mark.svg" />
          <span>Traceable RAG</span>
        </Link>
        <Link className="ghost-button" to="/knowledge-bases">
          暂时跳过，进入知识库
          <ArrowRight aria-hidden="true" size={15} />
        </Link>
      </header>

      <main className="setup-page__main" id="setup-content">
        <section className="setup-page__hero" aria-labelledby="setup-page-title">
          <div>
            <span className="setup-page__eyebrow">部署初始化</span>
            <h1 id="setup-page-title">按需连接外部服务</h1>
            <p>
              这不是启动前的强制关卡。你可以不填写任何 Provider，先使用关键词检索；需要文档自动解析、语义检索或重排时再回来补充。
            </p>
          </div>
          <div className="setup-page__session-note">
            <ShieldCheck aria-hidden="true" size={21} />
            <div>
              <strong>管理授权仅保留在当前标签页会话</strong>
              <p>安装器链接中的 token 会立即从地址栏移除，只在保存、删除和测试请求中使用。</p>
            </div>
          </div>
        </section>

        <ProviderManager context="setup" />

        <section className="setup-page__host-skill" aria-labelledby="setup-host-skill-title">
          <TerminalSquare aria-hidden="true" size={22} />
          <div>
            <h2 id="setup-host-skill-title">Codex Skill 仍由宿主环境安装</h2>
            <p>
              网页不会直接安装宿主 Codex Skill。完成服务配置后，可在“设置 → 开发者接入”查看宿主命令、模板与安装状态。
            </p>
          </div>
          <Link className="ghost-button" to="/settings">查看宿主命令与状态</Link>
        </section>

        <div className="setup-page__finish">
          <div>
            <strong>已经可以开始</strong>
            <p>配置会在保存后生效；跳过时继续使用关键词检索。</p>
          </div>
          <Link className="primary-button primary-button--large" to="/knowledge-bases">
            进入知识库
            <ArrowRight aria-hidden="true" size={16} />
          </Link>
        </div>
      </main>
    </div>
  );
}
