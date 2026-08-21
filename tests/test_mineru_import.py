import json
import shutil
from pathlib import Path

import pytest
from PIL import Image

import traceable_rag.mineru_import as mineru_import_module
from traceable_rag.chunking import BlockWindowChunker
from traceable_rag.indexing import build_index_records
from traceable_rag.mineru_import import (
    NORMALIZATION_VERSION,
    import_mineru_directory,
    parse_mineru_directory,
)
from traceable_rag.store import DEFAULT_KNOWLEDGE_BASE_ID, EvidenceStore


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mineru" / "minimal-v2"
VIDORE_PDF = (
    Path(__file__).parents[1]
    / "examples"
    / "vidore"
    / "documents"
    / "openstax-introduction-to-python-programming-p334-335.pdf"
)


def test_import_mineru_directory_preserves_scoped_document_blocks_assets_and_bbox(tmp_path):
    store = EvidenceStore(tmp_path / "traceable_v2.db")
    managed_root = tmp_path / "managed"

    result = import_mineru_directory(
        FIXTURE_DIR,
        store=store,
        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
        document_id="sample-document",
        original_filename=VIDORE_PDF.name,
        display_name="Python inheritance examples",
        source_file_path=VIDORE_PDF,
        preview_file_path=VIDORE_PDF,
        source_type="public_test_fixture",
        managed_root=managed_root,
    )

    assert result.document.page_count == 2
    assert result.document.original_filename == VIDORE_PDF.name
    assert result.document.display_name == "Python inheritance examples"
    assert Path(result.document.source_file_path or "").is_relative_to(managed_root)
    assert Path(result.document.preview_file_path or "").is_relative_to(managed_root)
    assert result.document.processing_status == "parsed"
    assert result.block_count == 5
    assert result.asset_count == 2
    assert result.document.normalization_version == NORMALIZATION_VERSION
    assert result.document.parser_version == "synthetic-fixture-v1"

    loaded = store.get_document(DEFAULT_KNOWLEDGE_BASE_ID, result.document.id)
    assert loaded is not None
    assert loaded.processing_status == "parsed"

    blocks = store.list_blocks(DEFAULT_KNOWLEDGE_BASE_ID, result.document.id)
    assert any(block.block_type == "title" for block in blocks)
    assert any(block.block_type == "table" for block in blocks)
    assert not any(
        block.block_type == "table" and not block.text and not block.html
        for block in blocks
    )
    assert any(block.block_type == "formula" for block in blocks)
    assert not {
        "header",
        "footer",
        "page_number",
        "aside",
    }.intersection(block.block_type for block in blocks)

    located = next(block for block in blocks if block.bbox and block.page_size)
    assert isinstance(located.page_idx, int)
    assert len(located.bbox) == 4
    assert len(located.page_size) == 2

    assets = store.list_assets(DEFAULT_KNOWLEDGE_BASE_ID, result.document.id)
    assert any(asset.asset_type == "table" and asset.related_text for asset in assets)
    assert any(asset.asset_type == "formula" and asset.related_text for asset in assets)


def _parse_fixture(result_dir: Path, tmp_path: Path, document_id: str = "fixture"):
    source = tmp_path / f"{document_id}.docx"
    preview = tmp_path / f"{document_id}.pdf"
    source.write_bytes(b"source")
    preview.write_bytes(b"preview")
    return parse_mineru_directory(
        result_dir,
        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
        document_id=document_id,
        original_filename=source.name,
        display_name="内容归一化",
        source_file_path=source,
        preview_file_path=preview,
    )


def test_content_list_v2_maps_natural_types_merges_annotations_and_filters_noise(tmp_path):
    result_dir = tmp_path / "v2"
    images = result_dir / "images"
    images.mkdir(parents=True)
    (images / "visual.png").write_bytes(b"png")
    items = [
        {"type": "title", "content": {"title_content": [{"type": "text", "content": "一级标题"}], "level": 1}, "bbox": [1, 1, 90, 10]},
        {"type": "paragraph", "content": {"paragraph_content": [{"type": "text", "content": "正文内容"}, {"type": "equation_inline", "content": "P_t=L_t+\\epsilon_t"}, {"type": "text", "content": "用于拟合"}]}, "bbox": [1, 11, 90, 20]},
        {"type": "list", "content": {"list_type": "text_list", "list_items": [{"item_content": [{"type": "text", "content": "列表项"}]}]}, "bbox": [1, 21, 90, 30]},
        {"type": "list", "content": {"list_type": "reference", "list_items": [{"item_content": [{"type": "text", "content": "参考文献一"}]}]}, "bbox": [1, 31, 90, 40]},
        {"type": "index", "content": {"list_items": [{"item_content": [{"type": "text", "content": "目录项"}]}]}, "bbox": [1, 41, 90, 50]},
        {"type": "image", "content": {"image_source": {"path": "images/visual.png"}, "content": "不应进入知识库的图片分析", "image_caption": [{"content": "图片说明"}], "image_footnote": [{"content": "图片注释"}]}, "bbox": [1, 51, 90, 60]},
        {"type": "chart", "content": {"image_source": {"path": "images/visual.png"}, "chart_caption": [{"content": "图表说明"}], "chart_footnote": [{"content": "图表注释"}]}, "bbox": [1, 61, 90, 70]},
        {"type": "table", "content": {"image_source": {"path": "images/visual.png"}, "table_caption": [{"content": "表格说明"}], "html": "<table><tr><td>数值</td></tr></table>", "table_footnote": [{"content": "表格注释"}]}, "bbox": [1, 71, 90, 80]},
        {"type": "equation_interline", "content": {"math_content": "x = 1"}, "bbox": [1, 81, 90, 90]},
        {"type": "code", "content": {"code_caption": [{"content": "代码说明"}], "code_content": "print(1)", "code_footnote": [{"content": "代码注释"}]}, "bbox": [1, 91, 90, 100]},
        {"type": "algorithm", "content": {"algorithm_caption": [{"content": "算法说明"}], "algorithm_body": "步骤一", "algorithm_footnote": [{"content": "算法注释"}]}, "bbox": [1, 101, 90, 110]},
        {"type": "page_footnote", "content": {"footnote_content": [{"content": "页面脚注"}]}, "bbox": [1, 111, 90, 120]},
        {"type": "callout", "content": {"callout_content": [{"content": "未知内容"}]}, "bbox": [1, 121, 90, 130]},
        {"type": "page_header", "content": {"page_header_content": [{"content": "页眉噪声"}]}, "bbox": [1, 0, 90, 1]},
        {"type": "page_footer", "content": {"page_footer_content": [{"content": "页脚噪声"}]}, "bbox": [1, 131, 90, 132]},
        {"type": "page_number", "content": {"page_number_content": [{"content": "1"}]}, "bbox": [45, 133, 46, 134]},
        {"type": "aside_text", "content": {"aside_text_content": [{"content": "旁注噪声"}]}, "bbox": [91, 1, 99, 130]},
    ]
    (result_dir / "content_list_v2.json").write_text(
        json.dumps([items], ensure_ascii=False), encoding="utf-8"
    )

    parsed = _parse_fixture(result_dir, tmp_path)
    by_type = {block.block_type: block for block in parsed.blocks}

    assert parsed.document.normalization_version == NORMALIZATION_VERSION
    assert [block.block_type for block in parsed.blocks] == [
        "title", "paragraph", "list", "reference", "index", "image",
        "chart", "table", "formula", "code", "algorithm", "footnote", "content",
    ]
    assert by_type["title"].metadata["level"] == 1
    assert by_type["paragraph"].text == r"正文内容\(P_t=L_t+\epsilon_t\)用于拟合"
    assert by_type["reference"].text == "参考文献一"
    assert by_type["content"].text == "未知内容"
    for block_type, expected in {
        "image": ("图片说明", "图片注释"),
        "chart": ("图表说明", "图表注释"),
        "table": ("表格说明", "表格注释"),
        "code": ("代码说明", "代码注释"),
        "algorithm": ("算法说明", "算法注释"),
    }.items():
        block = by_type[block_type]
        assert block.metadata["caption"] == [expected[0]]
        assert block.metadata["footnotes"] == [expected[1]]
        assert expected[0] in (block.text or "")
        if block_type in {"image", "chart"}:
            assert block.text == expected[0]
            assert expected[1] not in (block.text or "")
        else:
            assert expected[1] in (block.text or "")
    assert "不应进入知识库的图片分析" not in (by_type["image"].text or "")
    assert "数值" in (by_type["table"].text or "")
    assert "print(1)" in (by_type["code"].text or "")
    assert "步骤一" in (by_type["algorithm"].text or "")
    assert by_type["footnote"].text == "页面脚注"
    assert {asset.asset_type for asset in parsed.assets} == {
        "image", "chart", "table", "formula"
    }


def test_content_list_v2_drops_empty_cross_page_table_continuations(tmp_path):
    result_dir = tmp_path / "v2-cross-page-table"
    result_dir.mkdir()
    complete_html = (
        "<table><tr><td>结构</td><td>父类数</td></tr>"
        "<tr><td>层次继承</td><td>1</td></tr></table>"
    )
    pages = [
        [
            {
                "type": "table",
                "content": {
                    "image_source": {"path": "images/table.png"},
                    "table_caption": [{"content": "跨页继承结构表"}],
                    "table_footnote": [],
                    "html": complete_html,
                },
                "bbox": [100, 800, 900, 980],
            },
            {
                "type": "page_number",
                "content": {"page_number_content": [{"content": "1"}]},
                "bbox": [490, 990, 510, 1000],
            },
        ],
        [
            {
                "type": "table",
                "content": {
                    "image_source": {"path": "images/"},
                    "table_caption": [],
                    "table_footnote": [],
                    "html": "",
                },
                "bbox": [100, 20, 900, 420],
            },
            {
                "type": "paragraph",
                "content": {
                    "paragraph_content": [{"content": "表格后的正文"}],
                },
                "bbox": [100, 450, 900, 520],
            },
        ],
    ]
    (result_dir / "content_list_v2.json").write_text(
        json.dumps(pages, ensure_ascii=False), encoding="utf-8"
    )

    parsed = _parse_fixture(result_dir, tmp_path, "cross-page-table")

    assert parsed.document.page_count == 2
    assert [block.block_type for block in parsed.blocks] == ["table", "paragraph"]
    table = parsed.blocks[0]
    assert table.page_idx == 0
    assert table.html == complete_html
    assert "跨页继承结构表" in (table.text or "")
    assert table.metadata["bbox_by_page"] == {
        "0": [[100.0, 800.0, 900.0, 980.0]],
        "1": [[100.0, 20.0, 900.0, 420.0]],
    }
    assert table.metadata["bbox_page_size_by_page"] == {
        "0": [1000.0, 1000.0],
        "1": [1000.0, 1000.0],
    }
    assert parsed.blocks[1].page_idx == 1
    assert parsed.blocks[1].text == "表格后的正文"
    assert len(parsed.assets) == 1


def test_content_list_v2_merges_empty_cross_page_paragraph_continuations(tmp_path):
    result_dir = tmp_path / "v2-cross-page-paragraph"
    result_dir.mkdir()
    pages = [
        [
            {
                "type": "paragraph",
                "content": {
                    "paragraph_content": [{"content": "跨页段落的完整正文"}],
                },
                "bbox": [100, 780, 900, 980],
            },
        ],
        [
            {
                "type": "paragraph",
                "content": {"paragraph_content": []},
                "bbox": [100, 20, 900, 300],
            },
            {
                "type": "paragraph",
                "content": {
                    "paragraph_content": [{"content": "下一段正文"}],
                },
                "bbox": [100, 320, 900, 420],
            },
        ],
    ]
    (result_dir / "content_list_v2.json").write_text(
        json.dumps(pages, ensure_ascii=False), encoding="utf-8"
    )

    parsed = _parse_fixture(result_dir, tmp_path, "cross-page-paragraph")

    assert [block.text for block in parsed.blocks] == [
        "跨页段落的完整正文",
        "下一段正文",
    ]
    assert parsed.blocks[0].metadata["bbox_by_page"] == {
        "0": [[100.0, 780.0, 900.0, 980.0]],
        "1": [[100.0, 20.0, 900.0, 300.0]],
    }


def test_content_list_v2_preserves_trailing_page_count_when_last_page_has_only_filtered_content(
    tmp_path,
):
    result_dir = tmp_path / "trailing-filtered-page"
    result_dir.mkdir()
    (result_dir / "content_list_v2.json").write_text(
        json.dumps(
            [
                [
                    {
                        "type": "paragraph",
                        "content": {
                            "paragraph_content": [{"content": "正文"}],
                        },
                    }
                ],
                [{"type": "page_number", "content": {"text": "2"}}],
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    parsed = _parse_fixture(result_dir, tmp_path, "trailing-filtered-page")

    assert parsed.document.page_count == 2
    assert [block.page_idx for block in parsed.blocks] == [0]


def test_processing_manifest_supplies_parser_version_when_layout_is_absent(tmp_path):
    result_dir = tmp_path / "manifest-version"
    result_dir.mkdir()
    (result_dir / "content_list_v2.json").write_text(
        json.dumps(
            [
                [
                    {
                        "type": "paragraph",
                        "content": {"paragraph_content": [{"content": "正文"}]},
                    }
                ]
            ]
        ),
        encoding="utf-8",
    )
    (result_dir / "processing_manifest.json").write_text(
        json.dumps({"mineru": {"version": "3.4.4"}}), encoding="utf-8"
    )

    parsed = _parse_fixture(result_dir, tmp_path, "manifest-version")

    assert parsed.document.parser_version == "3.4.4"


def test_content_list_v1_merges_parent_annotations_and_drops_sibling_noise(tmp_path):
    result_dir = tmp_path / "v1"
    result_dir.mkdir()
    (result_dir / "sample_content_list.json").write_text(
        json.dumps(
            [
                {"type": "table", "page_idx": 0, "bbox": [1, 10, 90, 40], "table_caption": ["表题"], "table_body": "<table><tr><td>正文</td></tr></table>", "table_footnote": ["表注"]},
                {"type": "image", "page_idx": 0, "bbox": [1, 50, 90, 80], "image_caption": ["图题"], "image_footnote": ["图注"]},
                {"type": "list", "sub_type": "reference", "page_idx": 0, "bbox": [1, 81, 90, 90], "list_items": ["参考条目"]},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (result_dir / "block_list.json").write_text(
        json.dumps(
            {"pdfData": [[
                {"id": "header", "type": "header", "text": "页眉", "page_idx": 0},
                {"id": "caption", "type": "table_caption", "text": "表题", "page_idx": 0},
                {"id": "table", "type": "table_body", "page_idx": 0, "bbox": [1, 10, 90, 40], "page_size": [100, 100]},
                {"id": "image-caption", "type": "image_caption", "text": "图题", "page_idx": 0},
                {"id": "image", "type": "image", "page_idx": 0, "bbox": [1, 50, 90, 80], "page_size": [100, 100]},
                {"id": "reference", "type": "list", "sub_type": "reference", "page_idx": 0, "bbox": [1, 81, 90, 90], "page_size": [100, 100]},
                {"id": "unknown", "type": "custom_box", "text": "通用原文", "page_idx": 0, "bbox": [1, 91, 90, 99], "page_size": [100, 100]},
                {"id": "discarded", "type": "text", "text": "被丢弃的页面辅助内容", "is_discarded": True, "page_idx": 0},
                {"id": "number", "type": "page_number", "text": "1", "page_idx": 0},
            ]]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    parsed = _parse_fixture(result_dir, tmp_path, "v1-fixture")

    assert [block.block_type for block in parsed.blocks] == [
        "table", "image", "reference", "content"
    ]
    table, image, reference, content = parsed.blocks
    assert table.metadata["caption"] == ["表题"]
    assert table.metadata["footnotes"] == ["表注"]
    assert "正文" in (table.text or "")
    assert image.metadata["caption"] == ["图题"]
    assert image.metadata["footnotes"] == ["图注"]
    assert reference.text == "参考条目"
    assert content.text == "通用原文"


def test_detached_visual_captions_bind_once_on_both_sides_without_guessing_text(tmp_path):
    result_dir = tmp_path / "detached-captions"
    result_dir.mkdir()
    visuals = [
        {"type": "image", "page_idx": 0, "bbox": [10, 20, 90, 30], "image_caption": []},
        {"type": "image", "page_idx": 0, "bbox": [10, 50, 90, 60], "image_caption": []},
        {"type": "chart", "page_idx": 0, "bbox": [10, 80, 90, 90], "chart_caption": []},
        {"type": "image", "page_idx": 0, "bbox": [10, 120, 90, 130], "image_caption": []},
        {"type": "image", "page_idx": 0, "bbox": [10, 160, 90, 170], "image_caption": []},
    ]
    (result_dir / "content_list.json").write_text(
        json.dumps(visuals, ensure_ascii=False), encoding="utf-8"
    )
    (result_dir / "block_list.json").write_text(
        json.dumps(
            {
                "pdfData": [[
                    {"id": "caption-before", "type": "image_caption", "text": "前置图题", "page_idx": 0, "bbox": [20, 10, 80, 15]},
                    {"id": "image-a", "type": "image", "page_idx": 0, "bbox": [10, 20, 90, 30], "page_size": [100, 200]},
                    {"id": "image-b", "type": "image", "page_idx": 0, "bbox": [10, 50, 90, 60], "page_size": [100, 200]},
                    {"id": "caption-after", "type": "image_caption", "text": "后置图题", "page_idx": 0, "bbox": [20, 63, 80, 67]},
                    {"id": "chart-c", "type": "chart", "page_idx": 0, "bbox": [10, 80, 90, 90], "page_size": [100, 200]},
                    {"id": "caption-between", "type": "chart_caption", "text": "夹心图题", "page_idx": 0, "bbox": [20, 93, 80, 97]},
                    {"id": "image-d", "type": "image", "page_idx": 0, "bbox": [10, 120, 90, 130], "page_size": [100, 200]},
                    {"id": "image-e", "type": "image", "page_idx": 0, "bbox": [10, 160, 90, 170], "page_size": [100, 200]},
                    {"id": "ordinary", "type": "text", "text": "普通正文", "page_idx": 0, "bbox": [10, 173, 90, 178], "page_size": [100, 200]},
                    {"id": "blocked-caption", "type": "image_caption", "text": "不得越过正文", "page_idx": 0, "bbox": [20, 180, 80, 185]},
                ]]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    parsed = _parse_fixture(result_dir, tmp_path, "detached-caption-doc")
    visuals = [
        block for block in parsed.blocks if block.block_type in {"image", "chart"}
    ]

    assert [block.text for block in visuals] == [
        "前置图题",
        "后置图题",
        "夹心图题",
        None,
        None,
    ]
    assert parsed.blocks[-1].text == "普通正文"
    assert all("不得越过正文" not in (block.text or "") for block in parsed.blocks)


def test_same_visual_merges_before_and_after_captions_in_reading_order(tmp_path):
    result_dir = tmp_path / "two-sided-caption"
    result_dir.mkdir()
    (result_dir / "content_list.json").write_text(
        json.dumps(
            [
                {
                    "type": "image",
                    "page_idx": 0,
                    "bbox": [10, 30, 90, 70],
                    "image_caption": [],
                }
            ]
        ),
        encoding="utf-8",
    )
    (result_dir / "block_list.json").write_text(
        json.dumps(
            {
                "pdfData": [[
                    {"id": "before", "type": "image_caption", "text": "图 1", "page_idx": 0, "bbox": [20, 20, 80, 25]},
                    {"id": "visual", "type": "image", "page_idx": 0, "bbox": [10, 30, 90, 70], "page_size": [100, 100]},
                    {"id": "after", "type": "image_caption", "text": "项目总体技术路线图", "page_idx": 0, "bbox": [20, 75, 80, 80]},
                ]]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    parsed = _parse_fixture(result_dir, tmp_path, "two-sided-caption-doc")

    assert parsed.blocks[0].text == "图 1 项目总体技术路线图"
    assert parsed.blocks[0].metadata["caption"] == ["图 1", "项目总体技术路线图"]


@pytest.mark.parametrize("content_format", ["v1", "v2"])
def test_visual_caption_positions_cover_before_after_both_and_between(
    tmp_path, content_format
):
    result_dir = tmp_path / f"caption-positions-{content_format}"
    result_dir.mkdir()
    page = [
        ("caption-a", "image_caption", "前置图题", [20, 5, 80, 10]),
        ("image-a", "image", None, [10, 15, 90, 35]),
        ("image-b", "image", None, [10, 45, 90, 65]),
        ("caption-b", "image_caption", "后置图题", [20, 68, 80, 72]),
        ("caption-c-before", "image_caption", "双侧上图题", [20, 78, 80, 82]),
        ("image-c", "image", None, [10, 85, 90, 115]),
        ("caption-c-after", "image_caption", "双侧下图题", [20, 118, 80, 122]),
        ("image-d", "image", None, [10, 130, 90, 150]),
        ("caption-between", "image_caption", "夹心图题", [20, 158, 80, 162]),
        ("image-e", "image", None, [10, 164, 90, 184]),
    ]
    if content_format == "v1":
        (result_dir / "content_list.json").write_text(
            json.dumps(
                [
                    {
                        "id": identifier,
                        "type": "image",
                        "page_idx": 0,
                        "bbox": bbox,
                        "image_caption": [],
                    }
                    for identifier, block_type, _text, bbox in page
                    if block_type == "image"
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (result_dir / "block_list.json").write_text(
            json.dumps(
                {
                    "pdfData": [[
                        {
                            "id": identifier,
                            "type": block_type,
                            "text": text,
                            "page_idx": 0,
                            "bbox": bbox,
                            "page_size": [100, 200],
                        }
                        for identifier, block_type, text, bbox in page
                    ]]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    else:
        (result_dir / "content_list_v2.json").write_text(
            json.dumps(
                [[
                    {
                        "id": identifier,
                        "type": block_type,
                        "bbox": bbox,
                        "content": (
                            {"image_caption": [], "image_footnote": []}
                            if block_type == "image"
                            else {
                                "image_caption_content": [{"content": text}]
                            }
                        ),
                    }
                    for identifier, block_type, text, bbox in page
                ]],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    parsed = _parse_fixture(
        result_dir, tmp_path, f"caption-positions-{content_format}-doc"
    )
    visuals = [block for block in parsed.blocks if block.block_type == "image"]

    assert [block.text for block in visuals] == [
        "前置图题",
        "后置图题",
        "双侧上图题 双侧下图题",
        None,
        "夹心图题",
    ]


@pytest.mark.parametrize("content_format", ["v1", "v2"])
def test_visual_parent_relation_wins_over_closer_adjacent_visual(
    tmp_path, content_format
):
    result_dir = tmp_path / f"parent-{content_format}"
    result_dir.mkdir()
    if content_format == "v1":
        (result_dir / "content_list.json").write_text(
            json.dumps(
                [
                    {"id": "image-parent", "type": "image", "page_idx": 0, "bbox": [0, 0, 40, 40], "image_caption": []},
                    {"id": "image-near", "type": "image", "page_idx": 0, "bbox": [60, 0, 100, 40], "image_caption": []},
                ]
            ),
            encoding="utf-8",
        )
        (result_dir / "block_list.json").write_text(
            json.dumps(
                {
                    "pdfData": [[
                        {"id": "image-parent", "type": "image", "page_idx": 0, "bbox": [0, 0, 40, 40], "page_size": [100, 100], "children": ["caption"]},
                        {"id": "image-near", "type": "image", "page_idx": 0, "bbox": [60, 0, 100, 40], "page_size": [100, 100]},
                        {"id": "caption", "type": "image_caption", "parent_id": "image-parent", "text": "父子图题", "page_idx": 0, "bbox": [65, 42, 95, 48]},
                    ]]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    else:
        (result_dir / "content_list_v2.json").write_text(
            json.dumps(
                [[
                    {"id": "image-parent", "type": "image", "bbox": [0, 0, 40, 40], "children": ["caption"], "content": {"image_caption": [], "image_footnote": []}},
                    {"id": "image-near", "type": "image", "bbox": [60, 0, 100, 40], "content": {"image_caption": [], "image_footnote": []}},
                    {"id": "caption", "type": "image_caption", "parent_id": "image-parent", "bbox": [65, 42, 95, 48], "content": {"image_caption_content": [{"content": "父子图题"}]}},
                ]],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    parsed = _parse_fixture(result_dir, tmp_path, f"parent-{content_format}-doc")
    visuals = [block for block in parsed.blocks if block.block_type == "image"]

    assert [block.text for block in visuals] == ["父子图题", None]


@pytest.mark.parametrize("content_format", ["v1", "v2"])
def test_image_subtype_chart_keeps_attached_and_detached_image_captions(
    tmp_path, content_format
):
    result_dir = tmp_path / f"image-subtype-chart-{content_format}"
    result_dir.mkdir()
    if content_format == "v1":
        (result_dir / "content_list.json").write_text(
            json.dumps(
                [
                    {
                        "type": "image",
                        "sub_type": "chart",
                        "page_idx": 0,
                        "bbox": [0, 0, 40, 40],
                        "image_caption": ["内嵌图表题"],
                    },
                    {
                        "type": "image",
                        "sub_type": "chart",
                        "page_idx": 0,
                        "bbox": [60, 0, 100, 40],
                        "image_caption": [],
                    },
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (result_dir / "block_list.json").write_text(
            json.dumps(
                {
                    "pdfData": [[
                        {
                            "id": "chart-attached",
                            "type": "image",
                            "sub_type": "chart",
                            "page_idx": 0,
                            "bbox": [0, 0, 40, 40],
                            "page_size": [100, 100],
                        },
                        {
                            "id": "chart-detached",
                            "type": "image",
                            "sub_type": "chart",
                            "page_idx": 0,
                            "bbox": [60, 0, 100, 40],
                            "page_size": [100, 100],
                        },
                        {
                            "id": "chart-caption",
                            "type": "image_caption",
                            "text": "分离图表题",
                            "page_idx": 0,
                            "bbox": [65, 45, 95, 50],
                        },
                    ]]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    else:
        (result_dir / "content_list_v2.json").write_text(
            json.dumps(
                [[
                    {
                        "id": "chart-attached",
                        "type": "image",
                        "sub_type": "chart",
                        "bbox": [0, 0, 40, 40],
                        "content": {
                            "image_caption": [{"content": "内嵌图表题"}],
                            "image_footnote": [],
                        },
                    },
                    {
                        "id": "chart-detached",
                        "type": "image",
                        "sub_type": "chart",
                        "bbox": [60, 0, 100, 40],
                        "content": {"image_caption": [], "image_footnote": []},
                    },
                    {
                        "id": "chart-caption",
                        "type": "image_caption",
                        "bbox": [65, 45, 95, 50],
                        "content": {
                            "image_caption_content": [{"content": "分离图表题"}]
                        },
                    },
                ]],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    parsed = _parse_fixture(result_dir, tmp_path, f"image-subtype-chart-{content_format}")

    assert [block.block_type for block in parsed.blocks] == ["chart", "chart"]
    assert [block.text for block in parsed.blocks] == ["内嵌图表题", "分离图表题"]


@pytest.mark.parametrize(
    ("pillow_format", "suffix", "content_type"),
    [
        ("JPEG", ".jpg", "image/jpeg"),
        ("PNG", ".png", "image/png"),
        ("WEBP", ".webp", "image/webp"),
    ],
)
def test_visual_asset_uses_relative_keys_and_best_effort_preview(
    tmp_path, pillow_format, suffix, content_type
):
    result_dir = tmp_path / "visual-preview"
    images_dir = result_dir / "images"
    images_dir.mkdir(parents=True)
    source_image = images_dir / f"visual{suffix}"
    Image.new("RGB", (1800, 900), "white").save(
        source_image, format=pillow_format
    )
    (result_dir / "content_list.json").write_text(
        json.dumps(
            [
                {
                    "type": "image",
                    "page_idx": 0,
                    "bbox": [1, 2, 90, 80],
                    "img_path": f"images/visual{suffix}",
                    "image_caption": ["带预览的图片"],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    parsed = _parse_fixture(result_dir, tmp_path, "preview-doc")
    asset = parsed.assets[0]

    assert asset.file_path == f"images/visual{suffix}"
    assert asset.content_type == content_type
    assert asset.metadata["width"] == 1800
    assert asset.metadata["height"] == 900
    preview_key = asset.metadata["preview_file_path"]
    assert len(asset.metadata["preview_file_hash"]) == 64
    assert not Path(preview_key).is_absolute()
    preview = result_dir / preview_key
    assert preview.is_file()
    with Image.open(preview) as image:
        assert image.format == pillow_format
        assert max(image.size) == 1280


@pytest.mark.parametrize("failure_kind", ["missing", "damaged"])
def test_visual_asset_missing_or_damaged_file_keeps_caption_without_preview(
    tmp_path, failure_kind
):
    result_dir = tmp_path / f"visual-{failure_kind}"
    images_dir = result_dir / "images"
    images_dir.mkdir(parents=True)
    image_path = images_dir / "visual.png"
    if failure_kind == "damaged":
        image_path.write_bytes(b"not-a-real-image")
    (result_dir / "content_list.json").write_text(
        json.dumps(
            [
                {
                    "type": "image",
                    "page_idx": 0,
                    "bbox": [1, 2, 90, 80],
                    "img_path": "images/visual.png",
                    "image_caption": ["仍可核验的图题"],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    parsed = _parse_fixture(result_dir, tmp_path, f"visual-{failure_kind}-doc")
    block = parsed.blocks[0]
    asset = parsed.assets[0]

    assert block.text == "仍可核验的图题"
    assert asset.related_text == "仍可核验的图题"
    assert asset.content_type is None
    assert "preview_file_path" not in asset.metadata
    if failure_kind == "missing":
        assert asset.file_path is None
    else:
        assert asset.file_path == "images/visual.png"
        assert asset.metadata["renderable"] is False


def test_managed_import_promotes_external_result_and_versions_asset_identity(tmp_path):
    external_result = tmp_path / "external-result"
    images = external_result / "images"
    images.mkdir(parents=True)
    image_path = images / "visual.png"
    Image.new("RGB", (200, 100), "white").save(image_path)
    (external_result / "content_list.json").write_text(
        json.dumps(
            [
                {
                    "type": "image",
                    "page_idx": 0,
                    "bbox": [1, 2, 90, 80],
                    "img_path": "images/visual.png",
                    "image_caption": ["受管图片"],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    source = tmp_path / "source.pdf"
    preview = tmp_path / "preview.pdf"
    source.write_bytes(b"%PDF-source")
    preview.write_bytes(b"%PDF-preview")
    managed_root = tmp_path / "managed"
    store = EvidenceStore(tmp_path / "managed-import.db")

    first = import_mineru_directory(
        external_result,
        store,
        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
        document_id="managed-import-doc",
        original_filename=source.name,
        display_name="受管导入",
        source_file_path=source,
        preview_file_path=preview,
        managed_root=managed_root,
    )
    first_asset = store.list_assets(
        DEFAULT_KNOWLEDGE_BASE_ID, first.document.id
    )[0]
    first_result_dir = Path(first.document.mineru_result_dir or "")
    assert first_result_dir.is_relative_to(managed_root.resolve())
    assert Path(first.document.source_file_path or "").is_relative_to(
        managed_root.resolve()
    )
    assert Path(first.document.preview_file_path or "").is_relative_to(
        managed_root.resolve()
    )
    assert not Path(first_asset.file_path or "").is_absolute()
    assert (first_result_dir / (first_asset.file_path or "")).is_file()

    Image.new("RGB", (200, 100), "black").save(image_path)
    second = import_mineru_directory(
        external_result,
        store,
        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
        document_id="managed-import-doc",
        original_filename=source.name,
        display_name="受管导入",
        source_file_path=source,
        preview_file_path=preview,
        managed_root=managed_root,
    )
    second_asset = store.list_assets(
        DEFAULT_KNOWLEDGE_BASE_ID, second.document.id
    )[0]

    assert second_asset.id != first_asset.id
    assert not first_result_dir.exists()
    shutil.rmtree(external_result)
    second_result_dir = Path(second.document.mineru_result_dir or "")
    assert (second_result_dir / (second_asset.file_path or "")).is_file()


def test_managed_import_cleanup_failure_does_not_hide_committed_graph(
    tmp_path, monkeypatch
):
    result_dir = tmp_path / "external-result"
    result_dir.mkdir()
    (result_dir / "content_list.json").write_text(
        '[{"type":"text","text":"已提交正文","page_idx":0}]',
        encoding="utf-8",
    )
    source = tmp_path / "source.pdf"
    preview = tmp_path / "preview.pdf"
    source.write_bytes(b"%PDF-source")
    preview.write_bytes(b"%PDF-preview")
    store = EvidenceStore(tmp_path / "managed-import.db")

    def fail_cleanup(*_args):
        raise OSError("cleanup failed")

    monkeypatch.setattr(
        mineru_import_module,
        "_cleanup_managed_import_runs",
        fail_cleanup,
    )

    result = import_mineru_directory(
        result_dir,
        store,
        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
        document_id="cleanup-warning-doc",
        original_filename=source.name,
        display_name="清理告警仍成功",
        source_file_path=source,
        preview_file_path=preview,
        managed_root=tmp_path / "managed",
    )

    assert store.get_document(
        DEFAULT_KNOWLEDGE_BASE_ID, result.document.id
    ) is not None
    assert Path(result.document.mineru_result_dir or "").is_dir()


def test_import_retry_replaces_existing_blocks_assets_chunks_and_index(tmp_path):
    store = EvidenceStore(tmp_path / "traceable_v2.db")
    result_dir = tmp_path / "mineru"
    result_dir.mkdir()
    source = tmp_path / "source.docx"
    preview = tmp_path / "preview.pdf"
    source.write_bytes(b"docx")
    preview.write_bytes(b"pdf")

    def write_content(text: str) -> None:
        (result_dir / "content_list.json").write_text(
            '[{"type":"text","text":"'
            + text
            + '","page_idx":0,"bbox":[1,2,3,4],"page_size":[100,200]}]',
            encoding="utf-8",
        )

    write_content("old")
    import_mineru_directory(
        result_dir,
        store=store,
        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
        document_id="retry-doc",
        original_filename=source.name,
        display_name="Retry",
        source_file_path=source,
        preview_file_path=preview,
    )
    old_chunks = BlockWindowChunker().chunk(
        "retry-doc", store.list_blocks(DEFAULT_KNOWLEDGE_BASE_ID, "retry-doc")
    )
    store.replace_chunks(DEFAULT_KNOWLEDGE_BASE_ID, "retry-doc", old_chunks)
    store.replace_index_records(
        DEFAULT_KNOWLEDGE_BASE_ID, "retry-doc", build_index_records(old_chunks)
    )

    write_content("new")
    import_mineru_directory(
        result_dir,
        store=store,
        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
        document_id="retry-doc",
        original_filename=source.name,
        display_name="Retry",
        source_file_path=source,
        preview_file_path=preview,
    )

    blocks = store.list_blocks(DEFAULT_KNOWLEDGE_BASE_ID, "retry-doc")
    assert [block.text for block in blocks] == ["new"]
    assert store.list_chunks(DEFAULT_KNOWLEDGE_BASE_ID, "retry-doc") == []
    assert store.list_index_records(DEFAULT_KNOWLEDGE_BASE_ID, "retry-doc") == []


@pytest.mark.parametrize("malicious_kind", ["absolute", "traversal", "symlink"])
def test_mineru_image_paths_cannot_escape_or_follow_symlinks(tmp_path, malicious_kind):
    result_dir = tmp_path / "result"
    result_dir.mkdir()
    images = result_dir / "images"
    images.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    if malicious_kind == "absolute":
        img_path = str(outside)
    elif malicious_kind == "traversal":
        img_path = "../outside.png"
    else:
        link = images / "link.png"
        link.symlink_to(outside)
        img_path = "images/link.png"
    (result_dir / "content_list.json").write_text(
        json.dumps(
            [
                {
                    "type": "image",
                    "page_idx": 0,
                    "bbox": [1, 2, 3, 4],
                    "page_size": [100, 200],
                    "img_path": img_path,
                }
            ]
        ),
        encoding="utf-8",
    )
    source = tmp_path / "source.docx"
    preview = tmp_path / "preview.pdf"
    source.write_bytes(b"docx")
    preview.write_bytes(b"pdf")
    store = EvidenceStore(tmp_path / "db.sqlite")

    result = import_mineru_directory(
        result_dir,
        store=store,
        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
        document_id=f"doc-{malicious_kind}",
        original_filename=source.name,
        display_name="Unsafe image",
        source_file_path=source,
        preview_file_path=preview,
    )

    assets = store.list_assets(DEFAULT_KNOWLEDGE_BASE_ID, result.document.id)
    assert len(assets) == 1
    assert assets[0].file_path is None
