from traceable_rag.embedding import EmbeddingProvider
import pytest

from traceable_rag.indexing import build_index_records, validate_embedding_vectors
from traceable_rag.models import Block, Chunk


class FakeEmbeddingProvider(EmbeddingProvider):
    provider_name = "fake"
    model_name = "fake-embedding"

    def embed_texts(self, texts):
        return [[float(len(text)), float(text.count("编"))] for text in texts]


def test_build_index_records_with_embedding_provider():
    chunks = [
        Chunk(
            id="c1",
            document_id="doc1",
            chunk_idx=0,
            strategy="block_window_with_title_hints",
            block_ids=["b1"],
            title_context="标题",
            inferred_heading_path=[],
            chunk_text="编译器阶段分析",
            indexing_text="标题\n编译器阶段分析",
            page_range=[0],
            bbox_by_page={"0": [[0, 0, 1, 1]]},
            modality_mix=["text"],
            token_count=8,
            strategy_version="v1",
        )
    ]

    records = build_index_records(chunks, FakeEmbeddingProvider(), index_version="test")

    assert len(records) == 1
    assert records[0].vector == [10.0, 1.0]
    assert records[0].embedding_provider == "fake"
    assert records[0].embedding_model == "fake-embedding"
    assert records[0].block_ids == ["b1"]


def test_evidence_id_is_stable_across_rebuilds_and_index_versions():
    chunk = Chunk(
        id="stable-chunk",
        document_id="doc1",
        chunk_idx=0,
        strategy="block_window_with_title_hints",
        block_ids=["b1"],
        title_context="标题",
        inferred_heading_path=[],
        chunk_text="层次继承结构示例",
        indexing_text="标题\n层次继承结构示例",
        page_range=[0],
        bbox_by_page={"0": [[0, 0, 1, 1]]},
        modality_mix=["text"],
        token_count=10,
        strategy_version="v1",
    )

    first = build_index_records([chunk], index_version="index-v1")[0]
    rebuilt = build_index_records([chunk], index_version="index-v1")[0]
    reindexed = build_index_records([chunk], index_version="index-v2")[0]

    assert first.evidence_id == rebuilt.evidence_id == reindexed.evidence_id
    assert first.id == rebuilt.id
    assert first.id != reindexed.id


def test_table_blocks_keep_full_record_and_add_header_repeated_row_groups():
    rows = "".join(
        f"<tr><td>Series {index}</td><td>{'value ' * 90}</td></tr>"
        for index in range(4)
    )
    title = Block("title", "doc1", 0, 0, "title", "Operating metrics")
    table = Block(
        "table",
        "doc1",
        0,
        1,
        "table",
        "",
        html=(
            "<table><tr><th>Series</th><th>Value</th></tr>"
            f"{rows}</table>"
        ),
        metadata={"caption": ["Quarterly results"]},
    )
    chunk = Chunk(
        id="table-chunk",
        document_id="doc1",
        chunk_idx=0,
        strategy="block_window_with_title_hints",
        block_ids=["title", "table"],
        title_context="Operating metrics",
        inferred_heading_path=[],
        chunk_text="Series Value " + "value " * 360,
        indexing_text="Operating metrics\nSeries Value " + "value " * 360,
        page_range=[0],
        bbox_by_page={},
        modality_mix=["table", "text"],
        token_count=365,
        strategy_version="v1",
    )

    first = build_index_records([chunk], blocks=[title, table], index_version="table-v1")
    rebuilt = build_index_records([chunk], blocks=[title, table], index_version="table-v1")

    assert [record.id for record in first] == [record.id for record in rebuilt]
    assert first[0].indexing_text.startswith("Operating metrics\nQuarterly results")
    assert first[0].block_ids == ["title", "table"]
    row_groups = [record for record in first if "Table columns:" in record.indexing_text]
    assert len(row_groups) >= 2
    assert all("Table columns: Series | Value" in record.indexing_text for record in row_groups)
    assert all(record.block_ids == chunk.block_ids for record in row_groups)
    assert all(record.modality == "mixed" for record in first)


def test_blocks_only_enable_table_variants_for_table_chunks():
    paragraph = Block("paragraph", "doc1", 0, 0, "paragraph", "Body")
    chunk = Chunk(
        id="text-chunk",
        document_id="doc1",
        chunk_idx=0,
        strategy="block_window_with_title_hints",
        block_ids=["paragraph"],
        title_context="Heading",
        inferred_heading_path=[],
        chunk_text="Body",
        indexing_text="Heading\nBody",
        page_range=[0],
        bbox_by_page={},
        modality_mix=["text"],
        token_count=2,
        strategy_version="v1",
    )

    baseline = build_index_records([chunk], index_version="table-v1")
    with_blocks = build_index_records(
        [chunk], blocks=[paragraph], index_version="table-v1"
    )

    assert with_blocks == baseline


def test_embedding_validation_rejects_zero_norm_vectors():
    with pytest.raises(ValueError, match="zero norm"):
        validate_embedding_vectors([[0.0, 0.0]], expected_count=1)
