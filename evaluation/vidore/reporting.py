from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .metrics import Qrels, evaluate_query
from .pipeline import QueryResultDetail, Run


def write_results(
    output_dir: str | Path,
    *,
    config: dict[str, Any],
    qrels: Qrels,
    runs: dict[str, Run],
    details: dict[str, list[QueryResultDetail]],
    metrics: dict[str, dict[str, float]],
    timings: dict[str, float] | None = None,
    error_limit: int = 50,
) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, run in runs.items():
        (output_dir / f"{name}.run.json").write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
        detail_payload = [detail.__dict__ for detail in details[name]]
        (output_dir / f"{name}.details.json").write_text(json.dumps(detail_payload, ensure_ascii=False), encoding="utf-8")
        errors = _error_rows(qrels, run, details[name], error_limit)
        (output_dir / f"{name}.errors.json").write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")
    metric_names = list(next(iter(metrics.values()))) if metrics else []
    lines = [
        "# ViDoRe Finance EN Benchmark",
        "",
        "| Pipeline | " + " | ".join(metric_names) + " |",
        "|---|" + "---:|" * len(metric_names),
    ]
    for name, values in metrics.items():
        lines.append(
            f"| {name} | "
            + " | ".join(f"{values[metric]:.4f}" for metric in metric_names)
            + " |"
        )
    if timings:
        lines.extend(["", "## Timings", ""] + [f"- {key}: {value:.3f}s" for key, value in timings.items()])
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _error_rows(qrels: Qrels, run: Run, details: list[QueryResultDetail], limit: int) -> list[dict[str, Any]]:
    detail_by_id = {detail.query_id: detail for detail in details}
    rows = []
    for query_id, gold in qrels.items():
        query_metrics = evaluate_query(gold, run.get(query_id, {}))
        if query_metrics["recall@10"] >= 1.0:
            continue
        detail = detail_by_id.get(query_id)
        rows.append(
            {
                "query_id": query_id,
                "query": detail.query if detail else "",
                "gold": gold,
                "retrieved": run.get(query_id, {}),
                "recall@10": query_metrics["recall@10"],
                "ndcg@10": query_metrics["ndcg@10"],
                "chunks": list(detail.chunks) if detail else [],
            }
        )
    rows.sort(key=lambda row: (row["recall@10"], row["ndcg@10"], row["query_id"]))
    return rows[:limit]
