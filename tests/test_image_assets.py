from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from traceable_rag.image_assets import sha256_path
from traceable_rag.models import (
    Asset,
    Block,
    Chunk,
    Document,
    IndexRecord,
    KnowledgeBase,
)
from traceable_rag.sdk import TraceableRagClient
from traceable_rag.store import DEFAULT_KNOWLEDGE_BASE_ID, EvidenceStore
from traceable_rag.web_app import create_app


def _seed_visual_document(tmp_path, *, caption: str = "图 4-2 项目总体技术路线图"):
    managed_root = tmp_path / "managed"
    result_dir = managed_root / "kb" / "doc" / "runs" / "run-1" / "mineru" / "result"
    images = result_dir / "images"
    previews = result_dir / ".traceable-rag" / "previews"
    images.mkdir(parents=True)
    previews.mkdir(parents=True)
    original = images / "visual.png"
    preview = previews / "visual.png"
    Image.new("RGB", (640, 320), "white").save(original)
    Image.new("RGB", (320, 160), "white").save(preview)
    original_hash = sha256_path(original)
    preview_hash = sha256_path(preview)

    db_path = tmp_path / "traceable.db"
    store = EvidenceStore(db_path)
    document = Document(
        id="doc-image",
        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
        original_filename="visual.pdf",
        display_name="视觉报告",
        file_hash="document-hash",
        source_type="upload",
        source_file_path=None,
        preview_file_path=None,
        mineru_result_dir=str(result_dir),
        page_count=1,
        processing_status="ready",
        processing_error=None,
        parser_provider="mineru",
        normalization_version="mineru-content.v4",
    )
    block = Block(
        id="doc-image:image-block",
        document_id=document.id,
        page_idx=0,
        block_idx=0,
        block_type="image",
        text=caption or None,
        asset_id="asset-image",
        bbox=[10, 20, 90, 70],
        page_size=[100, 100],
        metadata={"caption": [caption] if caption else []},
    )
    asset = Asset(
        id="asset-image",
        document_id=document.id,
        block_id=block.id,
        asset_type="image",
        file_path="images/visual.png",
        content_type="image/png",
        file_hash=original_hash,
        page_idx=0,
        bbox=block.bbox,
        related_text=caption or None,
        metadata={
            "renderable": True,
            "width": 640,
            "height": 320,
            "preview_file_path": ".traceable-rag/previews/visual.png",
            "preview_file_hash": preview_hash,
            "preview_content_type": "image/png",
        },
    )
    chunks = []
    records = []
    if caption:
        chunk = Chunk(
            id="image-chunk",
            document_id=document.id,
            chunk_idx=0,
            strategy="block_window_with_title_hints",
            block_ids=[block.id],
            title_context=None,
            inferred_heading_path=[],
            chunk_text=caption,
            indexing_text=caption,
            page_range=[0],
            bbox_by_page={"0": [block.bbox]},
            modality_mix=["image"],
            token_count=10,
            strategy_version="block_window_with_title_hints.v2",
        )
        record = IndexRecord(
            id="image-record",
            evidence_id="image-evidence",
            document_id=document.id,
            chunk_id=chunk.id,
            block_ids=[block.id],
            modality="image",
            indexing_text=caption,
            vector=None,
            embedding_provider=None,
            embedding_model=None,
            embedding_dimension=None,
            index_version="m2.v2",
        )
        chunks = [chunk]
        records = [record]
    store.replace_document_graph(
        DEFAULT_KNOWLEDGE_BASE_ID,
        document,
        [block],
        [asset],
        chunks,
        records,
    )
    return store, managed_root, document, block, asset


def test_blocks_and_asset_endpoint_return_scoped_static_images_with_etag(tmp_path):
    store, managed_root, document, block, _asset = _seed_visual_document(tmp_path)
    client = TestClient(
        create_app(
            db_path=store.db_path,
            managed_root=managed_root,
            settings_path=tmp_path / "settings.json",
        )
    )
    base = (
        f"/api/knowledge-bases/{DEFAULT_KNOWLEDGE_BASE_ID}/documents/"
        f"{document.id}"
    )

    blocks_response = client.get(f"{base}/blocks")
    original = client.get(f"{base}/assets/asset-image")
    preview = client.get(f"{base}/assets/asset-image?variant=preview")

    assert blocks_response.status_code == 200
    payload = blocks_response.json()
    assert payload["blocks"][0]["asset_id"] == "asset-image"
    public_asset = payload["assets"]["asset-image"]
    assert public_asset["block_id"] == block.id
    assert public_asset["modality"] == "image"
    assert public_asset["asset_url"] == f"{base}/assets/asset-image"
    assert public_asset["preview_url"].endswith("?variant=preview")
    assert "file_path" not in public_asset
    assert str(managed_root) not in str(payload)
    assert original.status_code == 200
    assert original.headers["content-type"].startswith("image/png")
    assert original.headers["x-content-type-options"] == "nosniff"
    assert preview.status_code == 200
    assert preview.headers["etag"] != original.headers["etag"]
    etag = original.headers["etag"]
    assert client.get(
        f"{base}/assets/asset-image", headers={"If-None-Match": etag}
    ).status_code == 304
    preview_path = (
        managed_root
        / "kb"
        / "doc"
        / "runs"
        / "run-1"
        / "mineru"
        / "result"
        / ".traceable-rag"
        / "previews"
        / "visual.png"
    )
    Image.new("RGB", (320, 160), "black").save(preview_path)
    assert client.get(f"{base}/assets/asset-image?variant=preview").status_code == 404
    assert client.get(
        f"/api/knowledge-bases/missing/documents/{document.id}/assets/asset-image"
    ).status_code == 404
    assert client.get(f"{base}/assets/missing").status_code == 404


def test_asset_endpoint_enforces_knowledge_base_document_and_asset_scope(tmp_path):
    store, managed_root, document, block, asset = _seed_visual_document(tmp_path)
    other_kb = KnowledgeBase(id="other-kb", name="其他知识库")
    store.create_knowledge_base(other_kb)
    other_result = (
        managed_root
        / "other-kb"
        / "other-doc"
        / "runs"
        / "run-1"
        / "mineru"
        / "result"
    )
    other_images = other_result / "images"
    other_previews = other_result / ".traceable-rag" / "previews"
    other_images.mkdir(parents=True)
    other_previews.mkdir(parents=True)
    other_original = other_images / "visual.png"
    other_preview = other_previews / "visual.png"
    Image.new("RGB", (64, 32), "blue").save(other_original)
    Image.new("RGB", (32, 16), "blue").save(other_preview)
    other_document = replace(
        document,
        id="other-doc",
        knowledge_base_id=other_kb.id,
        display_name="其他视觉报告",
        mineru_result_dir=str(other_result),
    )
    other_block = replace(
        block,
        id="other-doc:image-block",
        document_id=other_document.id,
        asset_id="other-asset",
    )
    other_asset = replace(
        asset,
        id="other-asset",
        document_id=other_document.id,
        block_id=other_block.id,
        file_hash=sha256_path(other_original),
        metadata={
            **asset.metadata,
            "width": 64,
            "height": 32,
            "preview_file_hash": sha256_path(other_preview),
        },
    )
    store.replace_document_graph(
        other_kb.id,
        other_document,
        [other_block],
        [other_asset],
        [],
        [],
    )
    client = TestClient(
        create_app(
            db_path=store.db_path,
            managed_root=managed_root,
            settings_path=tmp_path / "settings.json",
        )
    )

    correct = client.get(
        f"/api/knowledge-bases/{other_kb.id}/documents/{other_document.id}"
        f"/assets/{other_asset.id}"
    )
    wrong_document = client.get(
        f"/api/knowledge-bases/{DEFAULT_KNOWLEDGE_BASE_ID}/documents/{document.id}"
        f"/assets/{other_asset.id}"
    )
    wrong_knowledge_base = client.get(
        f"/api/knowledge-bases/{DEFAULT_KNOWLEDGE_BASE_ID}/documents/"
        f"{other_document.id}/assets/{other_asset.id}"
    )

    assert correct.status_code == 200
    assert wrong_document.status_code == 404
    assert wrong_knowledge_base.status_code == 404


def test_asset_endpoint_rejects_traversal_symlinks_and_mime_mismatch(tmp_path):
    store, managed_root, document, _block, asset = _seed_visual_document(tmp_path)
    client = TestClient(
        create_app(
            db_path=store.db_path,
            managed_root=managed_root,
            settings_path=tmp_path / "settings.json",
        )
    )
    base = (
        f"/api/knowledge-bases/{DEFAULT_KNOWLEDGE_BASE_ID}/documents/"
        f"{document.id}/assets/{asset.id}"
    )
    result_dir = managed_root / "kb" / "doc" / "runs" / "run-1" / "mineru" / "result"

    store.replace_assets(
        DEFAULT_KNOWLEDGE_BASE_ID,
        document.id,
        [replace(asset, file_path="../outside.png")],
    )
    assert client.get(base).status_code == 404

    replacement = result_dir / "images" / "replacement.png"
    Image.new("RGB", (640, 320), "black").save(replacement)
    store.replace_assets(
        DEFAULT_KNOWLEDGE_BASE_ID,
        document.id,
        [replace(asset, file_path="images/replacement.png")],
    )
    assert client.get(base).status_code == 404

    link = result_dir / "images" / "link.png"
    link.symlink_to(result_dir / "images" / "visual.png")
    store.replace_assets(
        DEFAULT_KNOWLEDGE_BASE_ID,
        document.id,
        [replace(asset, file_path="images/link.png")],
    )
    assert client.get(base).status_code == 404

    spoofed = result_dir / "images" / "spoofed.png"
    Image.new("RGB", (10, 10), "white").save(spoofed, format="JPEG")
    store.replace_assets(
        DEFAULT_KNOWLEDGE_BASE_ID,
        document.id,
        [replace(asset, file_path="images/spoofed.png", content_type="image/png")],
    )
    assert client.get(base).status_code == 404

    missing = result_dir / "images" / "missing.png"
    store.replace_assets(
        DEFAULT_KNOWLEDGE_BASE_ID,
        document.id,
        [
            replace(
                asset,
                file_path="images/missing.png",
                file_hash="0" * 64,
                content_type="image/png",
            )
        ],
    )
    assert not missing.exists()
    assert client.get(base).status_code == 404

    damaged = result_dir / "images" / "damaged.png"
    damaged.write_bytes(b"not-a-real-png")
    store.replace_assets(
        DEFAULT_KNOWLEDGE_BASE_ID,
        document.id,
        [
            replace(
                asset,
                file_path="images/damaged.png",
                file_hash=sha256_path(damaged),
                content_type="image/png",
            )
        ],
    )
    assert client.get(base).status_code == 404


def test_visual_retrieval_and_context_add_asset_without_changing_text_contract(tmp_path):
    store, _managed_root, document, _block, _asset = _seed_visual_document(tmp_path)
    client = TraceableRagClient(store)

    retrieval = client.retrieve(
        "项目总体技术路线图",
        knowledge_base_ids=[DEFAULT_KNOWLEDGE_BASE_ID],
        mode="keyword",
        document_ids=[document.id],
    )
    source = retrieval["results"][0]
    context = client.get_source_context(source["source_id"])

    assert source["text"] == "图 4-2 项目总体技术路线图"
    assert source["asset"] == {
        "asset_id": "asset-image",
        "modality": "image",
        "asset_url": (
            "/api/knowledge-bases/default/documents/doc-image/assets/asset-image"
        ),
        "preview_url": (
            "/api/knowledge-bases/default/documents/doc-image/assets/asset-image"
            "?variant=preview"
        ),
    }
    assert context["source"]["asset"]["caption"] == source["text"]
    assert context["source"]["asset"]["content_type"] == "image/png"
    assert "block_id" not in source["asset"]
    assert "block_id" not in context["source"]["asset"]


def test_captionless_visual_is_browseable_but_has_no_retrieval_graph(tmp_path):
    store, _managed_root, document, block, asset = _seed_visual_document(
        tmp_path, caption=""
    )

    assert store.list_blocks(DEFAULT_KNOWLEDGE_BASE_ID, document.id) == [block]
    assert store.list_assets(DEFAULT_KNOWLEDGE_BASE_ID, document.id) == [asset]
    assert store.list_chunks(DEFAULT_KNOWLEDGE_BASE_ID, document.id) == []
    assert store.list_index_records(DEFAULT_KNOWLEDGE_BASE_ID, document.id) == []
