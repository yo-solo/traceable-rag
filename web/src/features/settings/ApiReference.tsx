import { Braces, ExternalLink, FileJson2 } from "lucide-react";
import { Link } from "react-router-dom";

type ApiReferenceProps = {
  openApiUrl: string;
};

export function ApiReference({ openApiUrl }: ApiReferenceProps) {
  return (
    <section className="settings-section api-reference" aria-labelledby="api-reference-title">
      <header className="settings-section__header">
        <div>
          <h2 id="api-reference-title">API 参考</h2>
          <p>在独立开发者页面浏览全部接口、请求与响应示例和多语言代码。</p>
        </div>
      </header>
      <div className="api-reference-entry">
        <div className="api-reference-entry__icon" aria-hidden="true">
          <Braces size={22} />
        </div>
        <div>
          <strong>完整 OpenAPI 工作台</strong>
          <span>包含全部公开接口、搜索、锚点导航与格式化代码示例，不发送在线请求。</span>
        </div>
        <div className="api-reference-entry__actions">
          <Link className="primary-button" to="/api-reference">
            打开 API 参考
          </Link>
          <a className="ghost-button" href={openApiUrl} rel="noreferrer" target="_blank">
            <FileJson2 aria-hidden="true" size={16} />
            JSON
            <ExternalLink aria-hidden="true" size={13} />
          </a>
        </div>
      </div>
    </section>
  );
}
