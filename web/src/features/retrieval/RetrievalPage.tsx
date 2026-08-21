import { AlertTriangle, ExternalLink, Search, SlidersHorizontal } from "lucide-react";
import { type FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../../api/client";
import {
  requireKnowledgeBaseListResponse,
  requireRetrievalResponse,
} from "../../api/guards";
import type { KnowledgeBaseSummary, RetrievalResponse } from "../../api/types";
import { AsyncState } from "../../components/AsyncState";
import { KnowledgeBaseSelector } from "../query/KnowledgeBaseSelector";

type RetrievalMode = RetrievalResponse["mode_requested"];

const PIPELINE_LABELS: Record<RetrievalResponse["pipeline_used"], string> = {
  keyword: "关键词检索",
  dense: "Dense 检索",
  dense_reranker: "Dense + Reranker",
};

function errorMessage(_error: unknown, fallback: string) {
  return fallback;
}

function scoreLabel(score: number) {
  return score.toFixed(3);
}

export function RetrievalPage() {
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseSummary[]>([]);
  const [knowledgeBasesLoading, setKnowledgeBasesLoading] = useState(true);
  const [knowledgeBasesError, setKnowledgeBasesError] = useState<string | null>(null);
  const [knowledgeBaseRetry, setKnowledgeBaseRetry] = useState(0);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<RetrievalMode>("auto");
  const [topKInput, setTopKInput] = useState("8");
  const [result, setResult] = useState<RetrievalResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestEpoch = useRef(0);
  const requestController = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setKnowledgeBasesLoading(true);
    setKnowledgeBasesError(null);
    void api.get<unknown>("/api/knowledge-bases", { signal: controller.signal })
      .then((value) => {
        if (controller.signal.aborted) {
          return;
        }
        const response = requireKnowledgeBaseListResponse(value);
        setKnowledgeBases(response.knowledge_bases);
        setSelectedIds((current) => {
          const availableIds = new Set(response.knowledge_bases.map(({ id }) => id));
          return current.filter((id) => availableIds.has(id));
        });
      })
      .catch((requestError: unknown) => {
        if (!controller.signal.aborted) {
          setKnowledgeBasesError(errorMessage(requestError, "知识库列表加载失败。"));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setKnowledgeBasesLoading(false);
        }
      });
    return () => controller.abort();
  }, [knowledgeBaseRetry]);

  useEffect(() => () => requestController.current?.abort(), []);

  const topK = useMemo(() => {
    const parsed = Number(topKInput);
    return Number.isSafeInteger(parsed) && parsed >= 1 && parsed <= 50 ? parsed : null;
  }, [topKInput]);
  const canSubmit = query.trim().length > 0 && selectedIds.length > 0 && topK !== null && !loading;

  async function runRetrieval() {
    const normalizedQuery = query.trim();
    if (!normalizedQuery || selectedIds.length === 0 || topK === null) {
      return;
    }
    requestController.current?.abort();
    const controller = new AbortController();
    const epoch = ++requestEpoch.current;
    requestController.current = controller;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const value = await api.post<unknown>("/api/retrieval", {
        signal: controller.signal,
        body: {
          query: normalizedQuery,
          knowledge_base_ids: selectedIds,
          mode,
          top_k: topK,
        },
      });
      if (!controller.signal.aborted && epoch === requestEpoch.current) {
        setResult(requireRetrievalResponse(value));
      }
    } catch (requestError) {
      if (!controller.signal.aborted && epoch === requestEpoch.current) {
        setError(errorMessage(requestError, "检索失败，请检查服务状态后重试。"));
      }
    } finally {
      if (!controller.signal.aborted && epoch === requestEpoch.current) {
        setLoading(false);
      }
    }
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    void runRetrieval();
  }

  return (
    <section className="feature-page query-page" aria-labelledby="retrieval-page-title">
      <header className="feature-page__header query-page__header">
        <div>
          <h1 id="retrieval-page-title">检索调试</h1>
          <p>验证当前检索管线、候选来源与降级状态；不会生成回答或保存查询记录。</p>
        </div>
      </header>

      <form className="query-workbench" onSubmit={handleSubmit}>
        <section className="query-composer" aria-labelledby="retrieval-composer-title">
          <div className="query-composer__title">
            <Search aria-hidden="true" size={20} />
            <div>
              <h2 id="retrieval-composer-title">检索问题</h2>
              <p>查看最终排序后的候选来源，并按需打开原文核验页。</p>
            </div>
          </div>
          <label className="query-field">
            <span>问题</span>
            <textarea
              aria-label="检索问题"
              maxLength={4000}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="例如：文件中关于数据保留期限有哪些明确要求？"
              rows={4}
              value={query}
            />
            <small>{query.length} / 4000</small>
          </label>

          <AsyncState
            error={knowledgeBasesError}
            loading={knowledgeBasesLoading}
            loadingLabel="正在加载知识库范围…"
            onRetry={() => setKnowledgeBaseRetry((value) => value + 1)}
          >
            {knowledgeBases.length === 0 ? (
              <AsyncState empty emptyDescription="请先创建知识库并处理至少一份文档。" emptyTitle="没有可检索的知识库">
                <span />
              </AsyncState>
            ) : (
              <KnowledgeBaseSelector
                knowledgeBases={knowledgeBases}
                onChange={setSelectedIds}
                selectedIds={selectedIds}
              />
            )}
          </AsyncState>

          <details className="query-advanced">
            <summary>
              <SlidersHorizontal aria-hidden="true" size={16} />
              检索参数
            </summary>
            <label>
              <span>候选来源数量</span>
              <input
                aria-label="候选来源数量"
                max={50}
                min={1}
                onBlur={() => {
                  if (topK === null) {
                    setTopKInput("8");
                  }
                }}
                onChange={(event) => setTopKInput(event.target.value)}
                type="number"
                value={topKInput}
              />
              <small>安全范围 1–50，默认 8。</small>
            </label>
            <fieldset className="query-mode-selector">
              <legend>检索方式</legend>
              <label>
                <input
                  checked={mode === "auto"}
                  name="retrieval-mode"
                  onChange={() => setMode("auto")}
                  type="radio"
                />
                智能检索
              </label>
              <label>
                <input
                  checked={mode === "keyword"}
                  name="retrieval-mode"
                  onChange={() => setMode("keyword")}
                  type="radio"
                />
                关键词检索
              </label>
            </fieldset>
          </details>

          <button className="primary-button" disabled={!canSubmit} type="submit">
            <Search aria-hidden="true" size={17} />
            {loading ? "正在检索…" : "运行检索"}
          </button>
        </section>

        <AsyncState error={error} loading={loading} loadingLabel="正在检索并整理候选来源…">
          {result ? (
            <section className="query-composer" aria-labelledby="retrieval-result-title">
              <div className="query-composer__title">
                <Search aria-hidden="true" size={20} />
                <div>
                  <h2 id="retrieval-result-title">候选来源</h2>
                  <p>实际管线：{PIPELINE_LABELS[result.pipeline_used]} · {result.results.length} 条结果</p>
                </div>
              </div>
              {result.degraded || result.warnings.length > 0 ? (
                <div className="trace-availability-warning trace-availability-warning--degraded" role="alert">
                  <AlertTriangle aria-hidden="true" size={19} />
                  <div>
                    <strong>检索已降级或有运行提示</strong>
                    <p>{result.warnings.map((warning) => warning.message).join(" ") || "当前结果使用了降级路径。"}</p>
                  </div>
                </div>
              ) : null}
              {result.results.length === 0 ? (
                <AsyncState empty emptyDescription="可尝试缩小问题范围、更换关键词或选择其他知识库。" emptyTitle="没有找到候选来源">
                  <span />
                </AsyncState>
              ) : (
                <div className="citation-list" aria-label="检索候选来源">
                  {result.results.map((source, index) => (
                    <article className="citation-card" key={source.source_id}>
                      <Link to={source.source_url}>
                        <span className="citation-card__number">{String(index + 1).padStart(2, "0")}</span>
                        <span className="citation-card__copy">
                          <strong>
                            {source.asset ? (
                              <span className="citation-card__modality">
                                {source.asset.modality === "chart" ? "图表" : "图片"}
                              </span>
                            ) : null}
                            {source.document_name} · 第 {source.human_page_number} 页
                          </strong>
                          <small>相关性分数 {scoreLabel(source.score)}</small>
                          <span>{source.text}</span>
                        </span>
                        <ExternalLink aria-hidden="true" size={17} />
                      </Link>
                    </article>
                  ))}
                </div>
              )}
            </section>
          ) : (
            <AsyncState empty emptyDescription="选择知识库并输入问题后，即可查看当前检索管线返回的来源候选。" emptyTitle="等待检索">
              <span />
            </AsyncState>
          )}
        </AsyncState>
      </form>
    </section>
  );
}
