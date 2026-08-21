from __future__ import annotations

from typing import Iterable

from .metrics import Qrels
from .pipeline import Run


OFFICIAL_METRICS = (
    "ndcg_cut_1",
    "ndcg_cut_5",
    "ndcg_cut_10",
    "ndcg_cut_20",
    "ndcg_cut_100",
    "recall_1",
    "recall_5",
    "recall_10",
    "recall_20",
    "recall_50",
    "recall_100",
    "P_1",
    "P_5",
    "P_10",
    "P_20",
    "map",
    "map_cut_1",
    "map_cut_10",
    "map_cut_100",
    "recip_rank",
)


def evaluate_run_official(
    qrels: Qrels,
    run: Run,
    *,
    metrics: Iterable[str] = OFFICIAL_METRICS,
) -> dict[str, float]:
    try:
        import pytrec_eval
    except ImportError as exc:
        raise RuntimeError(
            "Official ViDoRe evaluation requires pytrec-eval-terrier"
        ) from exc
    metric_names = tuple(metrics)
    integer_qrels: dict[str, dict[str, int]] = {}
    for query_id, judgments in qrels.items():
        integer_qrels[query_id] = {}
        for document_id, relevance in judgments.items():
            integer_relevance = int(relevance)
            if integer_relevance != relevance:
                raise ValueError("Official evaluator requires integer qrels")
            integer_qrels[query_id][document_id] = integer_relevance
    evaluator = pytrec_eval.RelevanceEvaluator(integer_qrels, set(metric_names))
    per_query = evaluator.evaluate(
        {query_id: run.get(query_id, {}) for query_id in qrels}
    )
    if set(per_query) != set(qrels):
        raise ValueError("Official evaluator did not return every query")
    return {
        metric: sum(per_query[query_id][metric] for query_id in qrels)
        / len(qrels)
        for metric in metric_names
    }
