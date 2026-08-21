from traceable_rag.chunking import BlockWindowChunker
from traceable_rag.models import Block


def block(idx, kind, text, page=0, bbox=None):
    return Block(
        id=f"b{idx}",
        document_id="doc1",
        page_idx=page,
        block_idx=idx,
        block_type=kind,
        text=text,
        bbox=bbox or [0, idx, 10, idx + 1],
        page_size=[100, 100],
    )


def test_block_window_chunking_uses_title_hints_without_heading_tree():
    blocks = [
        block(0, "title", "一、研究内容"),
        block(1, "paragraph", "第一段内容" * 30),
        block(2, "paragraph", "第二段内容" * 30),
        block(3, "title", "1.1 预期目标"),
        block(4, "paragraph", "目标内容" * 20, page=1),
    ]

    chunks = BlockWindowChunker(target_tokens=40, max_tokens=80).chunk("doc1", blocks)

    assert chunks
    assert all(chunk.strategy == "block_window_with_title_hints" for chunk in chunks)
    assert all(chunk.block_ids for chunk in chunks)
    assert any(chunk.title_context == "一、研究内容" for chunk in chunks)
    assert any(chunk.title_context == "1.1 预期目标" for chunk in chunks)
    assert all(chunk.inferred_heading_path == [] for chunk in chunks)
    assert all(chunk.bbox_by_page for chunk in chunks)


def test_chunking_keeps_table_image_formula_atomic():
    blocks = [
        block(0, "title", "多模态部分"),
        block(1, "paragraph", "导语" * 10),
        block(2, "table", "表格内容" * 80),
        block(3, "image", "项目总体技术路线图"),
        block(4, "formula", "$$x+y=z$$"),
    ]

    chunks = BlockWindowChunker(target_tokens=30, max_tokens=50).chunk("doc1", blocks)

    for atomic_id in {"b2", "b3", "b4"}:
        owners = [chunk for chunk in chunks if atomic_id in chunk.block_ids]
        assert len(owners) == 1
        assert atomic_id in owners[0].block_ids
    assert any("table" in chunk.modality_mix for chunk in chunks)
    assert any("image" in chunk.modality_mix for chunk in chunks)
    image_chunk = next(chunk for chunk in chunks if chunk.block_ids == ["b3"])
    assert image_chunk.indexing_text == "项目总体技术路线图"
    assert image_chunk.title_context == "多模态部分"


def test_captionless_visuals_remain_blocks_but_do_not_create_chunks():
    blocks = [
        block(0, "paragraph", "正文内容"),
        block(1, "image", None),
        block(2, "chart", "图 2 未来职业分布"),
        block(3, "paragraph", "后续正文"),
    ]

    chunks = BlockWindowChunker(target_tokens=30, max_tokens=50).chunk("doc1", blocks)

    assert all("b1" not in chunk.block_ids for chunk in chunks)
    chart = next(chunk for chunk in chunks if chunk.block_ids == ["b2"])
    assert chart.indexing_text == "图 2 未来职业分布"
    assert chart.modality_mix == ["chart"]
