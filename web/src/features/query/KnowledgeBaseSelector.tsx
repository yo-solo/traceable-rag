import { Database, FileCheck2 } from "lucide-react";

import type { KnowledgeBaseSummary } from "../../api/types";

type KnowledgeBaseSelectorProps = {
  knowledgeBases: KnowledgeBaseSummary[];
  onChange: (ids: string[]) => void;
  selectedIds: string[];
};

export function KnowledgeBaseSelector({
  knowledgeBases,
  onChange,
  selectedIds,
}: KnowledgeBaseSelectorProps) {
  const selected = new Set(selectedIds);

  function toggleKnowledgeBase(id: string) {
    const next = selected.has(id)
      ? selectedIds.filter((item) => item !== id)
      : [...selectedIds, id];
    onChange(next);
  }

  return (
    <fieldset className="knowledge-base-selector">
      <div className="knowledge-base-selector__heading">
        <div>
          <legend>检索范围</legend>
          <p>可同时选择多个知识库，回答只会使用所选范围内的内容。</p>
        </div>
        <span>{selectedIds.length} / {knowledgeBases.length} 已选</span>
      </div>
      <div className="knowledge-base-selector__options">
        {knowledgeBases.map((knowledgeBase) => (
          <label
            className={`knowledge-base-option${selected.has(knowledgeBase.id) ? " is-selected" : ""}`}
            key={knowledgeBase.id}
          >
            <input
              checked={selected.has(knowledgeBase.id)}
              onChange={() => toggleKnowledgeBase(knowledgeBase.id)}
              type="checkbox"
            />
            <span className="knowledge-base-option__icon" aria-hidden="true">
              <Database size={18} />
            </span>
            <span className="knowledge-base-option__copy">
              <strong>{knowledgeBase.name}</strong>
              <small>{knowledgeBase.description || "未填写描述"}</small>
              <span>
                <FileCheck2 aria-hidden="true" size={13} />
                {knowledgeBase.ready_document_count} / {knowledgeBase.document_count} 个文档已就绪
              </span>
            </span>
          </label>
        ))}
      </div>
      <p className={`knowledge-base-selector__requirement${selectedIds.length > 0 ? " is-valid" : ""}`}>
        {selectedIds.length > 0 ? `将在 ${selectedIds.length} 个知识库中查询` : "至少选择 1 个知识库"}
      </p>
    </fieldset>
  );
}
