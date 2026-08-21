from __future__ import annotations

import json
from pathlib import Path

from scripts import install_codex_skill


SKILL_ROOT = Path("integrations/codex-skill/traceable-rag")
SKILL_PATH = SKILL_ROOT / "SKILL.md"
OPENAI_YAML_PATH = SKILL_ROOT / "agents/openai.yaml"
CONTRACT_PATH = SKILL_ROOT / "references/http-contract.md"


def _skill_text() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def _contract_text() -> str:
    return CONTRACT_PATH.read_text(encoding="utf-8")


def _all_text() -> str:
    return _skill_text() + "\n" + _contract_text()


def test_skill_uses_retrieval_then_context_without_legacy_trace_contract():
    text = _all_text()
    ordered = [
        "GET /api/health",
        "GET /api/knowledge-bases",
        "POST /api/retrieval",
        "GET /api/sources/{source_id}/context",
    ]

    positions = [text.index(endpoint) for endpoint in ordered]
    assert positions == sorted(positions)
    for removed in (
        "/api/evidence",
        "/api/query",
        "/api/traces",
        "read_events",
        "support_level",
        "Block block-id",
    ):
        assert removed not in text


def test_skill_separates_relevance_context_completeness_and_gap_search():
    text = _skill_text().lower()

    assert "judge **relevance** first" in text
    assert "judge **context completeness** separately" in text
    assert "definition, exception, table header, unit" in text
    assert "specific remaining gap" in text
    assert "sufficient" in text
    assert "partial" in text
    assert "conflict" in text
    assert "not_found" in text


def test_skill_has_bounded_quick_standard_and_deep_loops():
    text = _skill_text()

    assert "**Quick**" in text
    assert "one Retrieval call and at most one Context call" in text
    assert "**Standard**" in text
    assert "up to three parallel subqueries and two rounds" in text
    assert "**Deep**" in text
    assert "up to three rounds, twelve total Retrieval calls, and twelve total Context calls" in text
    assert "one or two best candidates per evidence need per round" in text
    assert "Stop early" in text


def test_skill_uses_fast_followups_and_fails_fast_on_legacy_servers():
    text = _all_text()

    assert "reuse the already confirmed base URL" in text
    assert "Do not reread this Skill" in text
    assert "search Codex memory for answer evidence" in text
    assert "one or two queries that correspond to distinct evidence needs" in text
    assert "best three candidates first" in text
    assert "incompatible legacy version" in text
    assert "Do not inspect OpenAPI" in text
    assert "do not probe removed APIs" in text


def test_skill_requires_concise_progress_updates_during_long_queries():
    text = _skill_text()

    assert "30–45 seconds" in text
    assert "evidence strategy materially changes" in text
    assert "what is confirmed, what gap remains, and what will be checked next" in text
    assert "two or three meaningful updates" in text


def test_skill_locks_comparison_scope_and_tracks_per_document_coverage():
    text = _skill_text()

    assert "determine the comparison set before retrieval" in text
    assert "ask the user instead of silently choosing documents" in text
    assert "retrieve each document with its own `document_ids` scope" in text
    assert "matrix of evidence need × document" in text
    assert "evidence from one document never covers another document's cell" in text


def test_skill_organizes_queries_and_uses_keyword_only_for_exact_needs():
    text = _all_text()

    assert 'mode:"auto"' in text
    assert 'mode:"keyword"' in text
    assert "clause numbers, exact quotations, identifiers, numbers" in text
    assert "Each HTTP request accepts exactly one `query`" in text
    assert "Send independent subqueries as separate parallel calls" in text
    assert '"document_ids"' in text


def test_skill_requires_claim_level_clickable_sources_and_conservative_answers():
    text = _skill_text()

    assert "citation immediately after each independently verifiable material claim" in text
    assert "same Source directly supports all" in text
    assert "Show material source conflicts" in text
    assert "cannot support a reliable answer" in text
    assert "Do not use outside general knowledge" in text
    assert "[Data Policy · p.12](http://service-host/sources/src_example#insight=" in text
    assert "Do not expose scores" in text


def test_skill_requires_bounded_verification_insights_without_changing_source_identity():
    text = _all_text()

    assert "#insight=<percent-encoded-text>" in text
    assert "at most 160 Unicode characters" in text
    assert "Generate an insight for every citation" in text
    assert "preserve the exact returned `source_url`" in text
    assert "never invent or replace a Source ID" in text
    assert "must not contain Markdown" in text


def test_skill_treats_visual_text_as_caption_and_fetches_pixels_only_when_needed():
    text = _all_text()

    assert "whose `modality` is `image` or `chart`" in text
    assert "explicit figure caption" in text
    assert "not a transcription or interpretation of the pixels" in text
    assert "Captionless images and charts are intentionally browse-only" in text
    assert "never appear in Retrieval or Source Context" in text
    assert "inferring a caption from nearby ordinary paragraphs" in text
    assert "text-only Agent" in text
    assert "must not claim values, trends, spatial relationships" in text
    assert "Fetch Source Context before reading pixels" in text
    assert "Download the original `asset.asset_url` only when" in text
    assert "attach those bytes as image input" in text
    assert "Never assume that a cloud model can reach a private or LAN URL" in text
    assert "same authorization boundary" in text
    assert "Do not request a second confirmation solely" in text
    assert "final citation still uses the returned `source_url`" in text


def test_http_contract_documents_scoped_visual_asset_transport_and_failures():
    text = _contract_text()

    assert (
        "GET /api/knowledge-bases/{knowledge_base_id}/documents/{document_id}/assets/{asset_id}"
        in text
    )
    assert "?variant=preview" in text
    assert "static JPEG, PNG, or WebP" in text
    assert "hash-based `ETag`" in text
    assert "404 asset_not_found" in text
    assert "404 asset_unavailable" in text
    assert "Asset URLs are transport links, not evidence citations" in text


def test_skill_defines_source_lifecycle_and_context_truncation():
    text = _all_text()

    assert "404 source_not_found" in text
    assert "410 source_gone" in text
    assert "Never substitute newer content silently" in text
    assert "truncated:true" in text
    assert "do not mark that need `sufficient`" in text
    assert "before=2&after=2" in text
    assert "accept 0–5" in text


def test_skill_has_portable_runtime_configuration_and_updated_ui_metadata():
    text = _skill_text()

    assert "TRACEABLE_RAG_BASE_URL" in text
    assert "references/runtime.md" in text
    assert "http://127.0.0.1:8000" in text
    metadata = OPENAI_YAML_PATH.read_text(encoding="utf-8")
    assert 'display_name: "Traceable RAG"' in metadata
    assert "可点击的原文级引用" in metadata
    assert "$traceable-rag" in metadata


def test_installer_copies_skill_and_preserves_runtime_on_update(tmp_path):
    target = tmp_path / "skills" / "traceable-rag"
    installed = install_codex_skill.install_skill(
        target,
        base_url="http://127.0.0.1:9000/",
        knowledge_base_id="kb-legal",
    )

    assert installed == {
        "skill_version": "0.5.0",
        "base_url": "http://127.0.0.1:9000",
        "knowledge_base_id": "kb-legal",
    }
    assert (target / "SKILL.md").read_text(encoding="utf-8") == _skill_text()
    runtime = (target / "references/runtime.md").read_text(encoding="utf-8")
    assert "base_url: http://127.0.0.1:9000" in runtime
    assert "knowledge_base_id: kb-legal" in runtime

    updated = install_codex_skill.install_skill(target)
    assert updated == installed


def test_installer_check_validates_files_and_health(tmp_path, monkeypatch):
    target = tmp_path / "traceable-rag"
    install_codex_skill.install_skill(target)

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"ok": True}).encode("utf-8")

    monkeypatch.setattr(
        install_codex_skill.urllib.request,
        "urlopen",
        lambda request, timeout: Response(),
    )

    result = install_codex_skill.check_installation(
        target, base_url="http://127.0.0.1:8000"
    )

    assert result == {
        "installed": True,
        "health": True,
        "base_url": "http://127.0.0.1:8000",
        "skill_version": "0.5.0",
    }
