from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable

from .pipeline import Run


Qrels = dict[str, dict[str, float]]


def normalize_qrels(rows: Iterable[dict[str, Any]]) -> Qrels:
    qrels: Qrels = defaultdict(dict)
    for row in rows:
        raw_query_id = row.get("query_id")
        raw_corpus_id = row.get("corpus_id")
        query_id = "" if raw_query_id is None else str(raw_query_id)
        corpus_id = "" if raw_corpus_id is None else str(raw_corpus_id)
        score = float(row.get("score", 0))
        if not query_id or not corpus_id:
            raise ValueError("Qrel is missing query_id or corpus_id")
        qrels[query_id][corpus_id] = max(score, qrels[query_id].get(corpus_id, -math.inf))
    return dict(qrels)


def evaluate_run(qrels: Qrels, run: Run) -> dict[str, float]:
    if not qrels:
        raise ValueError("Qrels are empty")
    per_query = [evaluate_query(gold, run.get(query_id, {})) for query_id, gold in qrels.items()]
    return {
        metric: sum(item[metric] for item in per_query) / len(per_query)
        for metric in ("ndcg@10", "recall@5", "recall@10", "map")
    }


def evaluate_query(gold: dict[str, float], scores: dict[str, float]) -> dict[str, float]:
    ranked = [item[0] for item in sorted(scores.items(), key=lambda item: (-item[1], item[0]))]
    relevant = {corpus_id for corpus_id, relevance in gold.items() if relevance > 0}
    return {
        "ndcg@10": _ndcg(ranked[:10], gold, 10),
        "recall@5": _recall(ranked[:5], relevant),
        "recall@10": _recall(ranked[:10], relevant),
        "map": _average_precision(ranked, relevant),
    }


def _recall(ranked: list[str], relevant: set[str]) -> float:
    return len(relevant.intersection(ranked)) / len(relevant) if relevant else 0.0


def _average_precision(ranked: list[str], relevant: set[str]) -> float:
    if not relevant:
        return 0.0
    hits = 0
    total = 0.0
    for rank, corpus_id in enumerate(ranked, 1):
        if corpus_id in relevant:
            hits += 1
            total += hits / rank
    return total / len(relevant)


def _ndcg(ranked: list[str], gold: dict[str, float], k: int) -> float:
    dcg = sum((2 ** gold.get(corpus_id, 0.0) - 1) / math.log2(rank + 1) for rank, corpus_id in enumerate(ranked[:k], 1))
    ideal = sorted((score for score in gold.values() if score > 0), reverse=True)[:k]
    idcg = sum((2 ** score - 1) / math.log2(rank + 1) for rank, score in enumerate(ideal, 1))
    return dcg / idcg if idcg else 0.0
