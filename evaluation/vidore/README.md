# ViDoRe Finance EN retrieval benchmark

This package evaluates the current Traceable-RAG MinerU/Block/Chunk retrieval
pipeline against a same-embedding Markdown page baseline. Both tracks use the
fixed `vidore/vidore_v3_finance_en` revision declared in
`configs/finance_en.json`.

## Install the isolated evaluation package

```bash
python -m pip install -e '.[dev]' -e ./evaluation
```

## Validate a returned MinerU batch

The PC handoff archive must contain `mineru_output/<doc_id>/...` and each
document directory must contain a successful `processing_manifest.json`.

```bash
traceable-rag-vidore validate-mineru \
  --manifest .artifacts/vidore/finance_en/source/vidore_v3_finance_en_0fe6508053e8aa31b1f1eaec4553f79e3974f59a_manifest.json \
  --archives-root /path/to/vidore_v3_finance_en_mineru_<version>_<timestamp>.tar.gz \
  --mineru-root .artifacts/vidore/finance_en/mineru/validated
```

## Run both retrieval tracks

Configure the existing runtime embedding provider as `Qwen3-Embedding-8B`,
then run:

```bash
traceable-rag-vidore run \
  --manifest .artifacts/vidore/finance_en/source/vidore_v3_finance_en_0fe6508053e8aa31b1f1eaec4553f79e3974f59a_manifest.json \
  --mineru-root .artifacts/vidore/finance_en/mineru/validated \
  --runtime-root .
```

Evaluation-only embedding requests use batches of 16 with 32 concurrent
requests by default. Override them with `--embedding-batch-size` and
`--embedding-concurrency`; this does not change production embedding behavior.

The run writes its database, embedding cache, complete per-query output,
compact failure summaries, metrics JSON, and Markdown report below
`.artifacts/vidore/finance_en/`.

The canonical public result, metric contract, and known regressions are recorded
in [`docs/benchmarks/vidore-finance-en.md`](../../docs/benchmarks/vidore-finance-en.md).
The optional `configs/finance_en_top20_diagnostic.json` configuration isolates
the effect of increasing the Chunk candidate budget while keeping page@10
unchanged.

The table title and row-group indexing treatment is configured by
`configs/finance_en_table_row_groups_v1.json`. The public benchmark document
summarizes the final controlled comparison without relying on private run
directories or repository history.

The official Qwen3 query-prompt treatment is configured by
`configs/finance_en_table_row_groups_qwen_query_prompt.json`. It applies the
prompt to queries only and leaves all corpus and Chunk embeddings unchanged.

The API reranker treatment is configured by
`configs/finance_en_table_row_groups_qwen_prompt_reranker.json`. It retrieves
100 unique dense Chunk candidates and reranks their selected indexing records
before returning the final top-10 Chunks. Configure `RERANKER_MODEL`; the
reranker reuses the embedding base URL and API key. Requests are cached by the
model, raw query, and complete candidate text list. The default reranker
concurrency is 4 because 100-document requests were stable at that setting in
the first full run; it remains independently configurable from embedding
concurrency.

For the official top-100 page run, use
`configs/finance_en_table_row_groups_qwen_prompt_reranker_top100_official.json`.
It reranks 200 Chunk candidates to compensate for page overlap, emits exactly
100 unique pages per query, and evaluates them with the same
`pytrec_eval.RelevanceEvaluator` metric set as the ViDoRe pipeline CLI.
