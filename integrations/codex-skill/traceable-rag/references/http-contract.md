# HTTP contract

Use JSON with `Accept: application/json`; add `Content-Type: application/json` for POST requests. Never send Provider credentials.

## Discovery

```text
GET /api/health
GET /api/knowledge-bases
GET /api/knowledge-bases/{knowledge_base_id}/documents
```

The document list is optional. Always use an explicit knowledge-base scope for Retrieval.

On follow-up questions, reuse discovery results from the same conversation when the base URL and requested scope have not changed.

## Retrieval

```text
POST /api/retrieval
```

```json
{
  "query": "What retention period applies to personal data?",
  "knowledge_base_ids": ["actual-kb-id"],
  "mode": "auto",
  "top_k": 8,
  "document_ids": ["optional-document-id"]
}
```

Rules:

- One request contains one query. Send independent subqueries as separate parallel calls.
- `mode` is `auto` or `keyword`.
- Omit `document_ids` unless restricting the search to known documents.
- Inspect `pipeline_used`, `degraded`, and structured `warnings`.
- A fallback result is usable but must be judged on its text and Source, not its score.
- If this endpoint returns 404/405 or the response lacks `pipeline_used`, `results`, `source_id`, or `source_url`, treat the server as an incompatible legacy deployment. Stop with an upgrade instruction; do not probe removed APIs or synthesize a citation URL.
- Limit the Agent's first-pass inspection to the best three results even when a larger `top_k` is requested.

Each result contains only the Agent-facing contract:

```json
{
  "source_id": "src_0123456789abcdef0123456789abcdef",
  "source_url": "/sources/src_0123456789abcdef0123456789abcdef",
  "text": "Candidate text used for relevance judgment.",
  "document_id": "document-id",
  "document_name": "Data Policy.pdf",
  "human_page_number": 12,
  "bbox": [72, 160, 510, 240],
  "score": 0.82
}
```

Use `text` to judge relevance. Do not cite a candidate that was not inspected.

An image or chart result adds optional visual fields without changing the text result contract:

```json
{
  "source_id": "src_0123456789abcdef0123456789abcdef",
  "source_url": "/sources/src_0123456789abcdef0123456789abcdef",
  "text": "Figure 4-2 System architecture",
  "document_id": "document-id",
  "document_name": "Architecture.pdf",
  "human_page_number": 8,
  "bbox": [72, 160, 510, 420],
  "score": 0.79,
  "asset": {
    "asset_id": "asset-id",
    "modality": "image",
    "asset_url": "/api/knowledge-bases/kb-id/documents/document-id/assets/asset-id",
    "preview_url": "/api/knowledge-bases/kb-id/documents/document-id/assets/asset-id?variant=preview"
  }
}
```

For `image` and `chart`, `text` is the explicit caption only. It does not prove any value, label, trend, layout, or relationship visible inside the image.

Captionless images and charts remain available only in the document browser. They do not create Retrieval results or Source Context, and ordinary paragraphs are never promoted into inferred captions.

## Source Context

```text
GET /api/sources/{source_id}/context?before=2&after=2
```

- `before` and `after` default to 2 and accept 0–5.
- `source` is the quoted original block.
- `before` and `after` are in forward reading order.
- `title_path`, page, bbox, page size, document identity, `file_url`, and `source_url` are returned for verification.
- `truncated:true` means an item or the total response reached a safety bound.
- `404` means unknown Source; `410` means a previously valid Source is stale.

For an image or chart Source, `source.asset.modality` identifies the visual type and the asset adds `content_type`, `file_hash`, pixel `width` and `height`, and the complete `caption`. Text Sources omit the optional `asset` field.

Do not request the complete document Block graph. Open `source_url` for human verification or `file_url` for the complete PDF.

## Visual asset bytes

```text
GET /api/knowledge-bases/{knowledge_base_id}/documents/{document_id}/assets/{asset_id}
GET /api/knowledge-bases/{knowledge_base_id}/documents/{document_id}/assets/{asset_id}?variant=preview
```

- The server validates the knowledge base, document, and asset together and serves only static JPEG, PNG, or WebP files from managed storage.
- The default `variant` is `original`; `preview` is a bounded derivative for display and triage.
- Successful responses use the actual image content type and include a hash-based `ETag`. A matching `If-None-Match` may return `304`.
- `404 asset_not_found` means the scoped asset does not exist. `404 asset_unavailable` means its file is missing, invalid, unsupported, or failed managed-path validation.
- Use only the returned `asset_url` or `preview_url`; do not construct paths from IDs or local metadata.
- For visual reasoning, download the original bytes locally and attach them as image input. Do not give a private/LAN URL to a cloud model and assume it can fetch it.
- If the current Agent is text-only, or the original asset is unavailable, use the caption only and keep pixel-level claims unsupported.
- Image transfer uses the same authorization decision as document-text transfer; it does not require a second image-only confirmation.
- Cite `source_url` in the final answer. Asset URLs are transport links, not evidence citations.

For a final-answer citation, preserve the returned Source URL and append a presentation-only verification cue:

```text
/sources/{source_id}#insight=<percent-encoded-text>
```

The insight must be one sentence, match the answer language, contain at most 160 Unicode characters, and use only facts or relationships supported by the inspected Source/Context. It may explain why the highlighted facts matter, but it must not contain Markdown, another URL, a line break, scores, internal process, or outside knowledge. Generate one for every citation. The fragment does not change Source identity or lifecycle and is not sent to the server.

## Query examples

Natural-language retrieval:

```json
{"query":"Which conditions and exceptions govern retention?","knowledge_base_ids":["actual-kb-id"],"mode":"auto","top_k":8}
```

Exact-term retrieval:

```json
{"query":"Article 17(3) retention exception","knowledge_base_ids":["actual-kb-id"],"mode":"keyword","top_k":8}
```

## Acceptance prompts

- `What retention period applies, and what exceptions modify it?`
- `Compare the obligations in the two selected policy documents.`
- `Do these documents specify a deployment plan for a Mars fusion reactor?`

The first two require claim-level Source links. For the last, state that the knowledge base does not support a reliable answer when Retrieval and targeted follow-up queries find no evidence.
