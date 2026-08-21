# ViDoRe V3 Finance EN retrieval benchmark

This document records the public, reproducible retrieval result used in the Traceable RAG README. It keeps the evaluation contract, configuration, metrics, and known regressions in one place without depending on private Git history or local artifact paths.

## Scope

| Item | Fixed value |
|---|---|
| Dataset | `vidore/vidore_v3_finance_en` |
| Dataset revision | `0fe6508053e8aa31b1f1eaec4553f79e3974f59a` |
| Corpus | 6 PDF documents, 2,942 pages |
| Queries | 309 English queries |
| Relevance unit | ViDoRe page `corpus_id` |
| Evaluator | `pytrec_eval.RelevanceEvaluator` |
| Main configuration | [`finance_en_table_row_groups_qwen_prompt_reranker_top100_official.json`](../../evaluation/vidore/configs/finance_en_table_row_groups_qwen_prompt_reranker_top100_official.json) |

The benchmark measures page retrieval. It does not score generated answers, citation wording, OCR accuracy in isolation, or the visual quality of the Source viewer.

## Evaluated pipeline

1. MinerU 3.4.4 parses the six source PDFs.
2. Traceable RAG normalizes Blocks and creates natural cross-page Chunks with a target of 450 tokens and a maximum of 700 tokens.
3. Table title context and row-group indexing records are added with `table_row_groups_v1`.
4. Corpus and query vectors use Qwen3-Embedding-8B at 1,024 dimensions. Queries use the model's documented retrieval instruction.
5. Dense retrieval produces 200 unique Chunk candidates.
6. Qwen3-Reranker-8B reranks those candidates.
7. Chunk scores are projected to page IDs. When multiple Chunks map to one page, the highest score is retained.
8. Each query emits exactly 100 unique pages to the official metric implementation.

The 200-Chunk budget is oversampling before page deduplication. It does not change the evaluator's top-100 page contract. With only 100 Chunk candidates, 101 of 309 queries produced fewer than 100 unique pages because several Chunks can overlap the same page.

## Result

| Metric | Traceable RAG + reranker | Same-embedding page-dense | Difference |
|---|---:|---:|---:|
| nDCG@10 | **0.5785** | 0.5523 | +0.0262 |
| nDCG@100 | **0.6576** | 0.6303 | +0.0274 |
| Recall@5 | **0.5345** | 0.4984 | +0.0361 |
| Recall@10 | **0.6451** | 0.6251 | +0.0200 |
| Recall@20 | **0.7321** | 0.7146 | +0.0175 |
| Recall@50 | **0.8390** | 0.8148 | +0.0242 |
| Recall@100 | **0.8934** | 0.8785 | +0.0149 |
| MAP@10 | **0.4564** | 0.4338 | +0.0226 |
| MAP@100 / MAP | **0.4943** | 0.4692 | +0.0251 |
| Reciprocal rank | **0.6803** | 0.6759 | +0.0044 |

Both runs use the same pinned dataset revision and embedding environment. All 309 queries in both runs contain exactly 100 unique page results.

The published ViDoRe Qwen3-Embedding-8B page-dense result is a useful external reference, but it is not a strict A/B control. That run used a local SentenceTransformers implementation, default vector output, and did not pin the same dataset revision in its result file. Differences against it cannot be assigned solely to chunking, embedding, or reranking.

## Known limitations

- Text queries gained `+0.0273` nDCG@10 against the same-environment page-dense reference. Table queries lost `0.0365`, and chart queries lost `0.1034`.
- Table Recall@100 improved by `0.0120` and chart Recall@100 improved by `0.0389`. Relevant pages often enter the deeper candidate set but are still ranked too low.
- Compare-contrast Recall@100 fell by `0.0185`; Boolean queries fell by `0.0112`.
- The result covers one English financial split. It does not establish performance across every ViDoRe domain, language, document type, or Provider configuration.
- Provider latency and cost are environment-specific and are not presented as portable benchmark results.

These regressions are part of the acceptance record. A higher aggregate score does not justify hiding weaker table, chart, or multi-evidence behavior.

## Reproduction

Install the isolated evaluation package.

```bash
python -m pip install -e '.[dev]' -e ./evaluation
```

Review [`evaluation/vidore/README.md`](../../evaluation/vidore/README.md) for the MinerU validation and benchmark commands. Supply Provider credentials only at runtime. The repository does not contain credentials, embedding caches, raw provider responses, or generated `.artifacts` directories.

The evaluation code pins the dataset revision, page-level qrels contract, configuration, aggregation behavior, and metric names. A reproduction should report its model endpoint, model version, dimensions, parser version, and any changed concurrency or candidate budget alongside the result.
