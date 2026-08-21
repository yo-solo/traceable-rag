import { CheckCircle2, CircleDashed, TerminalSquare } from "lucide-react";

import type { SkillStatus as SkillStatusValue } from "../../api/types";

type SkillStatusProps = {
  publicBaseUrl: string;
  status: SkillStatusValue;
};

export function SkillStatus({ publicBaseUrl, status }: SkillStatusProps) {
  const serviceUrl = publicBaseUrl.replace(/\/$/, "");

  return (
    <section className="settings-section skill-status" aria-labelledby="skill-status-title">
      <header className="settings-section__header">
        <div>
          <h2 id="skill-status-title">Codex Skill</h2>
          <p>查看 Codex 接入状态、服务地址和调用示例。</p>
        </div>
      </header>
      <div className="skill-status__states">
        <div>
          {status.template_available ? (
            <CheckCircle2 aria-hidden="true" size={18} />
          ) : (
            <CircleDashed aria-hidden="true" size={18} />
          )}
          <div>
            <strong>{status.template_available ? "Skill 模板可用" : "Skill 模板缺失"}</strong>
            <p>仓库内接入模板</p>
          </div>
        </div>
        <div>
          {status.installed ? (
            <CheckCircle2 aria-hidden="true" size={18} />
          ) : (
            <CircleDashed aria-hidden="true" size={18} />
          )}
          <div>
            <strong>{status.installed ? "已安装到用户目录" : "尚未安装到用户目录"}</strong>
            <p>本机 Codex 可发现状态</p>
          </div>
        </div>
      </div>
      <div className="skill-status__endpoint">
        <TerminalSquare aria-hidden="true" size={18} />
        <div>
          <span>服务地址</span>
          <code>{serviceUrl || "当前站点同源地址"}</code>
        </div>
      </div>
      <pre aria-label="Codex Skill 调用示例">
        <code>{`POST ${serviceUrl || "<service-base-url>"}/api/retrieval\nGET ${serviceUrl || "<service-base-url>"}/api/sources/{source_id}/context`}</code>
      </pre>
    </section>
  );
}
