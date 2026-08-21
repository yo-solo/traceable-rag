---
name: traceable-rag
description: Query a Traceable RAG knowledge base for evidence-grounded answers with clickable Source citations. Use when Codex must retrieve from the user's document library, verify legal, medical, regulatory, financial, or other rigorous claims against original pages, compare documents, inspect missing context, or answer without relying on unsupported general knowledge.
---

# Traceable RAG

Skill version: `0.5.0`

Use Traceable RAG as an external HTTP retrieval and source-verification service. Keep one Agent responsible for the evidence coverage ledger; parallelize independent retrieval requests, not autonomous agents.

## Resolve configuration and scope

Choose one base URL in order:

1. URL explicitly supplied by the user.
2. `TRACEABLE_RAG_BASE_URL` from the environment.
3. `base_url` in `references/runtime.md` when installed locally.
4. `http://127.0.0.1:8000`.

The optional runtime file may also define `knowledge_base_id`. Never store Provider keys or other credentials in this Skill.

Call `GET /api/health`, then `GET /api/knowledge-bases`. Use the user's explicit scope, the configured default, or the only available knowledge base. If several choices remain genuinely ambiguous, ask before retrieval. Use `GET /api/knowledge-bases/{knowledge_base_id}/documents` when document identity or availability matters.

For a follow-up in the same conversation, reuse the already confirmed base URL, knowledge-base scope, and document set unless the user changes them or a request fails. Do not reread this Skill, relist unchanged discovery data, or search Codex memory for answer evidence. The knowledge base is the only answer source.

For comparisons, conflicts, or questions naming two or more documents, determine the comparison set before retrieval. Match user-supplied filenames or titles exactly when possible. If the knowledge base contains exactly the requested number of unambiguous documents, use them; otherwise ask the user instead of silently choosing documents from search results. Once selected, retrieve each document with its own `document_ids` scope.

Treat an immediate private/LAN connection error as possibly sandbox-related. Retry the identical read-only health request once through the environment's approved escalation mechanism. Never change proxy, VPN, routing, firewall, or service configuration to gain access.

Read [references/http-contract.md](references/http-contract.md) before sending requests.

If the service lacks the Retrieval or Source Context contract, report that the server is an incompatible legacy version and stop. Do not inspect OpenAPI to invent an alternate workflow, fall back to removed Query/Evidence/Trace APIs, or construct document deep links yourself.

## Choose query depth

- **Quick**: one Retrieval call and at most one Context call. Use for a simple fact or when the user says “快速查一下”.
- **Standard**: up to three parallel subqueries and two rounds. Default for multi-condition questions.
- **Deep**: up to three rounds, twelve total Retrieval calls, and twelve total Context calls. Use when the user requests comprehensive verification or the question requires cross-document comparison, exceptions, or conflicting sources.

Stop early when every material evidence need is sufficiently supported. Do not mechanically spend the full budget.

## Keep latency bounded

- For Standard mode, start with one or two queries that correspond to distinct evidence needs. Add a third only when a separate need remains uncovered.
- Inspect the best three candidates first. Expand Context for only the best one or two relevant but incomplete candidates.
- When one Source directly covers all material parts of a simple question, answer without another query rewrite.
- Treat query rewriting as a response to a named gap, not as automatic synonym generation.
- Run independent Retrieval calls in parallel and avoid serial discovery calls that repeat confirmed state.

## Keep the user informed

- Send one short opening update before a multi-step Standard or Deep query. Quick queries may answer directly.
- During active work, do not leave the user without an update for more than about 30–45 seconds.
- Send an update when the evidence strategy materially changes: scope is locked, a useful Source needs Context, an exact-term rewrite is required, sources conflict, or the server safely degrades.
- State only what is confirmed, what gap remains, and what will be checked next. Do not expose scores, IDs, tool syntax, or hidden reasoning.
- Prefer two or three meaningful updates over narration of every request.

## Maintain an evidence coverage ledger

Before retrieval, decompose the question into independently verifiable needs such as:

- fact or definition;
- condition and exception;
- date or applicability period;
- exact number, clause, or named entity;
- comparison dimension or cross-document relationship.

For each need, track one of: `sufficient`, `partial`, `conflict`, or `not_found`. For a cross-document question, use a matrix of evidence need × document. Every document needs its own Source for each material comparison dimension; evidence from one document never covers another document's cell. This is working state, not content for the final answer.

## Retrieval loop

1. Write one clear retrieval question per unmet evidence need.
2. Use `POST /api/retrieval` with `mode:"auto"` for natural-language questions.
3. Use `mode:"keyword"` for clause numbers, exact quotations, identifiers, numbers, and specialist names.
4. Send independent queries in parallel. Each HTTP request accepts exactly one `query`.
5. Judge **relevance** first: does the returned text answer the intended need?
6. Judge **context completeness** separately: is a definition, exception, table header, unit, preceding condition, or following sentence missing?
7. Call `GET /api/sources/{source_id}/context` only for relevant candidates whose context is incomplete. Default to `before=2&after=2`; increase within the API limit only when the missing information is likely adjacent. Inspect at most one or two best candidates per evidence need per round; do not expand every top-k result.
8. Update the coverage ledger. Start another round only for a specific remaining gap, conflict, or exact-term miss.
9. Stop after the selected depth budget. Deep mode stops after three rounds, twelve Retrieval calls, or twelve Context calls even if gaps remain.

Do not use outside general knowledge to fill a knowledge-base gap. Do not treat retrieval scores as proof. Do not expose scores, internal IDs, bbox values, hidden reasoning, or the coverage ledger in the final answer.

## Handle visual Sources conservatively

Retrieval results may include an `asset` object whose `modality` is `image` or `chart`. Their `text` is only the explicit figure caption used as a retrieval proxy; it is not a transcription or interpretation of the pixels.

Captionless images and charts are intentionally browse-only and never appear in Retrieval or Source Context. Do not try to discover them by broadening the query or by inferring a caption from nearby ordinary paragraphs.

- A text-only Agent may use the caption to establish that a named figure exists, but must not claim values, trends, spatial relationships, labels, or other pixel-level details. Keep such an evidence need `partial` and state the limitation.
- Fetch Source Context before reading pixels. It returns the detailed asset metadata and confirms the Source, document, page, caption, and current asset URL.
- Download the original `asset.asset_url` only when the user's question materially requires visual inspection and the current Agent or selected model accepts image input. Use `preview_url` for display or triage, not as the authoritative visual input when the original is available.
- Resolve relative asset URLs against the confirmed base URL, download the bytes locally, and attach those bytes as image input. Never assume that a cloud model can reach a private or LAN URL, and never ask it to fetch that URL itself.
- Image transfer inherits the same authorization boundary already used for the document's text. Do not request a second confirmation solely because the authorized payload is an image, and do not send it to a separate external model when the document content was not authorized for that model.
- If `asset.asset_url` is absent, the download fails, or the model lacks vision, fall back to the caption and Source/PDF verification surface. Do not infer the missing visual details.

The final citation still uses the returned `source_url`, never `asset_url` or `preview_url`.

## Source handling

Use only the returned `source_url`; make relative links absolute with the selected base URL. A Source is a current-version locator:

- `404 source_not_found`: the ID is unknown or malformed.
- `410 source_gone`: the document changed or was removed. Never substitute newer content silently.
- `truncated:true`: the Context response was bounded. If omitted content could change a number, exception, condition, or comparison, do not mark that need `sufficient`; try a narrower or exact-term retrieval, then state the limitation if it remains unresolved.

Every citation link must add a short verification insight without changing the returned Source path or ID. Append it as a percent-encoded URL fragment:

```text
/sources/{source_id}#insight=<percent-encoded-text>
```

The insight is a one-sentence reading cue in the answer language, with at most 160 Unicode characters. Point to the decisive number, condition, comparison, or relationship that the highlighted source lets the user verify. Explanatory inference is allowed only when every part is grounded in the inspected Source or its requested Context. Do not include Markdown, URLs, line breaks, retrieval scores, internal process, or outside knowledge. Generate an insight for every citation, including simple factual citations. The fragment is presentation-only: preserve the exact returned `source_url`, then append `#insight=`; never invent or replace a Source ID.

## Final answer

- Lead with the conclusion; expand only as needed.
- Put a citation immediately after each independently verifiable material claim.
- Reuse one citation for adjacent short sentences only when the same Source directly supports all of them.
- Cite different numbers, clauses, conditions, or documents separately.
- State limitations when support is partial.
- Show material source conflicts instead of resolving them silently.
- When evidence is insufficient, say that the knowledge base cannot support a reliable answer and cite only the limited facts actually found.
- Prefer short prose, a compact list, or a small comparison table.

Citation format:

```markdown
Personal data may be retained only while necessary for the stated purpose. [Data Policy · p.12](http://service-host/sources/src_example#insight=The%20highlighted%20rule%20limits%20retention%20to%20the%20period%20needed%20for%20the%20stated%20purpose.)
```

The link is the audit surface. Do not append a separate Trace, Block list, bbox dump, retrieval log, or chain-of-thought.
