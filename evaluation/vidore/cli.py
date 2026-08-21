from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import asdict
from pathlib import Path

from traceable_rag.embedding import provider_from_runtime_config
from traceable_rag.runtime_config import load_runtime_config
from traceable_rag.store import EvidenceStore

from .config import load_config
from .embedding_cache import CachedRetryEmbeddingProvider
from .manifest import filter_english_queries, load_manifest, read_rows
from .metrics import evaluate_run, normalize_qrels
from .mineru_artifacts import extract_and_validate_all
from .official_evaluator import evaluate_run_official
from .pipeline import PageDensePipeline, TraceableChunkPipeline, import_and_index, normalize_corpus_rows
from .reranker import CachedRetryReranker, OpenAICompatibleReranker
from .reporting import write_results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ViDoRe v3 Finance EN evaluation")
    parser.add_argument("--config")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-mineru")
    validate.add_argument("--manifest", required=True)
    validate.add_argument("--archives-root", required=True)
    validate.add_argument("--mineru-root", required=True)
    run = commands.add_parser("run")
    run.add_argument("--manifest", required=True)
    run.add_argument("--mineru-root", required=True)
    run.add_argument("--output")
    run.add_argument("--runtime-root", default=".")
    run.add_argument("--embedding-batch-size", type=int, default=16)
    run.add_argument("--embedding-concurrency", type=int, default=32)
    run.add_argument("--reranker-concurrency", type=int, default=4)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    manifest = load_manifest(args.manifest, config)
    if args.command == "validate-mineru":
        results = extract_and_validate_all(manifest.documents, args.archives_root, args.mineru_root)
        print(json.dumps([asdict(result) for result in results], ensure_ascii=False, default=str, indent=2))
        return 0
    return _run(args, config, manifest)


def _run(args, config, manifest) -> int:
    artifact_root = Path(config.artifact_root)
    runtime = load_runtime_config(root=args.runtime_root)
    reranker = None
    reranker_model = None
    if config.reranker_candidate_chunks:
        reranker_model = runtime.reranker_model
        if not reranker_model:
            raise ValueError("RERANKER_MODEL is required for reranker evaluation")
        if not runtime.embedding_base_url or not runtime.embedding_api_key:
            raise ValueError("Reranker reuses the configured embedding base URL and API key")
        reranker = CachedRetryReranker(
            OpenAICompatibleReranker(
                api_key=runtime.embedding_api_key,
                base_url=runtime.embedding_base_url,
                model_name=reranker_model,
            ),
            artifact_root / "reranker-cache",
            max_concurrency=args.reranker_concurrency,
        )
    execution_fingerprint = _execution_fingerprint(config.fingerprint, reranker_model)
    output = (
        Path(args.output)
        if args.output
        else artifact_root / "runs" / execution_fingerprint
    )
    provider = CachedRetryEmbeddingProvider(
        provider_from_runtime_config(runtime),
        artifact_root / "embedding-cache",
        request_batch_size=args.embedding_batch_size,
        max_concurrency=args.embedding_concurrency,
    )
    corpus = normalize_corpus_rows(
        read_rows(
            manifest.corpus_paths,
            columns=["corpus_id", "doc_id", "markdown", "page_number_in_doc"],
        ),
        page_number_base=config.corpus_page_number_base,
    )
    if len(corpus) != config.expected_pages:
        raise ValueError(f"Expected {config.expected_pages} corpus pages, found {len(corpus)}")
    queries = filter_english_queries(read_rows(manifest.queries_paths), config.expected_queries)
    query_ids = {str(row["query_id"]) for row in queries}
    qrels = {
        query_id: gold
        for query_id, gold in normalize_qrels(read_rows(manifest.qrels_paths)).items()
        if query_id in query_ids
    }
    if set(qrels) != query_ids:
        raise ValueError("English query set and qrels do not match")
    timings: dict[str, float] = {}
    started = time.monotonic()
    store = EvidenceStore(artifact_root / "stores" / config.fingerprint / "traceable.db")
    import_and_index(manifest, args.mineru_root, store, provider, config)
    timings["traceable_index"] = time.monotonic() - started
    started = time.monotonic()
    chunk_run, chunk_details = TraceableChunkPipeline(
        store,
        provider,
        config,
        corpus,
        reranker=reranker,
    ).search(queries)
    timings["traceable_search"] = time.monotonic() - started
    started = time.monotonic()
    page_pipeline = PageDensePipeline(
        corpus,
        provider,
        query_prompt=config.query_prompt,
    )
    timings["page_dense_index"] = time.monotonic() - started
    started = time.monotonic()
    page_run, page_details = page_pipeline.search(queries, top_k=config.top_k_pages)
    timings["page_dense_search"] = time.monotonic() - started
    runs = {"traceable_chunk": chunk_run, "page_dense": page_run}
    if config.evaluator == "official_pytrec_eval":
        metrics = {
            name: evaluate_run_official(qrels, result, metrics=config.metrics)
            for name, result in runs.items()
        }
    else:
        metrics = {name: evaluate_run(qrels, result) for name, result in runs.items()}
    write_results(
        output,
        config={
            **asdict(config),
            "fingerprint": execution_fingerprint,
            "index_fingerprint": config.fingerprint,
            "embedding_batch_size": args.embedding_batch_size,
            "embedding_concurrency": args.embedding_concurrency,
            "reranker_model": reranker_model,
            "reranker_concurrency": args.reranker_concurrency,
        },
        qrels=qrels,
        runs=runs,
        details={"traceable_chunk": chunk_details, "page_dense": page_details},
        metrics=metrics,
        timings=timings,
    )
    print(json.dumps({"output": str(output), "metrics": metrics, "timings": timings}, indent=2))
    return 0


def _execution_fingerprint(
    config_fingerprint: str, reranker_model: str | None
) -> str:
    if not reranker_model:
        return config_fingerprint
    material = f"{config_fingerprint}\0{reranker_model}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


if __name__ == "__main__":
    raise SystemExit(main())
