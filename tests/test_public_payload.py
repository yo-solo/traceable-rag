import json

from traceable_rag.mineru_import import load_cross_page_block_merges
from traceable_rag.models import Block
from traceable_rag.public_payload import public_document_blocks


def test_public_document_blocks_merge_cross_page_parser_parts_and_keep_all_boxes():
    blocks = [
        Block(
            id="doc:block",
            document_id="doc",
            page_idx=3,
            block_idx=13,
            block_type="paragraph",
            text="跨页段落的完整正文",
            bbox=[10, 700, 580, 820],
            page_size=[600, 840],
            metadata={"mineru_id": "shared-parser-id"},
        ),
        Block(
            id="doc:block:4:14",
            document_id="doc",
            page_idx=4,
            block_idx=14,
            block_type="paragraph",
            bbox=[10, 20, 580, 180],
            page_size=[600, 840],
            metadata={"mineru_id": "shared-parser-id"},
        ),
    ]

    payloads = public_document_blocks(blocks)

    assert len(payloads) == 1
    assert payloads[0]["id"] == "doc:block"
    assert payloads[0]["text"] == "跨页段落的完整正文"
    assert payloads[0]["bbox_by_page"] == {
        "3": [[10, 700, 580, 820]],
        "4": [[10, 20, 580, 180]],
    }
    assert payloads[0]["bbox_page_size_by_page"] == {
        "3": [600, 840],
        "4": [600, 840],
    }
    assert payloads[0]["metadata"]["cross_page"] is True


def test_public_document_blocks_do_not_merge_unrelated_blocks_without_parser_id():
    blocks = [
        Block(
            id=f"doc:block-{index}",
            document_id="doc",
            page_idx=0,
            block_idx=index,
            block_type="paragraph",
            text=f"正文 {index}",
            bbox=[10, 20 + index * 40, 580, 50 + index * 40],
            page_size=[600, 840],
        )
        for index in range(2)
    ]

    assert [payload["id"] for payload in public_document_blocks(blocks)] == [
        "doc:block-0",
        "doc:block-1",
    ]


def test_legacy_v2_geometry_recovers_different_ids_for_empty_continuation(tmp_path):
    result_dir = tmp_path / "mineru-result"
    result_dir.mkdir()
    (result_dir / "content_list_v2.json").write_text(
        json.dumps(
            [
                [{
                    "type": "paragraph",
                    "content": {
                        "paragraph_content": [{"content": "跨页段落正文"}],
                    },
                    "bbox": [100, 780, 900, 980],
                }],
                [{
                    "type": "paragraph",
                    "content": {"paragraph_content": []},
                    "bbox": [100, 20, 900, 300],
                }],
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    blocks = [
        Block(
            id="doc:primary",
            document_id="doc",
            page_idx=0,
            block_idx=0,
            block_type="paragraph",
            text="跨页段落正文",
            bbox=[100, 780, 900, 980],
            page_size=[1000, 1000],
        ),
        Block(
            id="doc:continuation",
            document_id="doc",
            page_idx=1,
            block_idx=1,
            block_type="paragraph",
            bbox=[100, 20, 900, 300],
            page_size=[1000, 1000],
        ),
    ]

    geometry, merge_into = load_cross_page_block_merges(result_dir, blocks)
    payloads = public_document_blocks(
        blocks,
        geometry_overrides=geometry,
        merge_into=merge_into,
    )

    assert merge_into == {"doc:continuation": "doc:primary"}
    assert len(payloads) == 1
    assert payloads[0]["bbox_by_page"] == {
        "0": [[100, 780, 900, 980]],
        "1": [[100, 20, 900, 300]],
    }
