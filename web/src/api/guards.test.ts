import { describe, expect, it } from "vitest";

import {
  requireDocumentBlocksResponse,
  requireRetrievalResponse,
  requireSourceContextResponse,
} from "./guards";

const block = {
  id: "doc-1:image-1",
  document_id: "doc-1",
  page_idx: 0,
  human_page_number: 1,
  block_idx: 0,
  block_type: "image",
  text: "图 1 合成系统流程图",
  html: null,
  asset_id: "asset-1",
  bbox: [10, 20, 90, 70],
  page_size: [100, 100],
  metadata: {},
};

const page = {
  page_idx: 0,
  human_page_number: 1,
  page_size: [100, 100],
  block_count: 1,
};

const assetReference = {
  asset_id: "asset-1",
  modality: "image",
  asset_url: "/api/knowledge-bases/kb-1/documents/doc-1/assets/asset-1",
  preview_url: "/api/knowledge-bases/kb-1/documents/doc-1/assets/asset-1?variant=preview",
};

describe("visual asset response guards", () => {
  it("normalizes legacy Blocks responses and omitted nullable asset details", () => {
    expect(requireDocumentBlocksResponse({ blocks: [block], pages: [page] }).assets).toEqual({});

    const response = requireDocumentBlocksResponse({
      blocks: [block],
      pages: [page],
      assets: {
        "asset-1": { ...assetReference, block_id: "doc-1:image-1" },
      },
    });
    expect(response.assets["asset-1"]).toMatchObject({
      ...assetReference,
      block_id: "doc-1:image-1",
      content_type: null,
      file_hash: null,
      width: null,
      height: null,
      caption: null,
    });
  });

  it("rejects asset keys and Block links that do not match", () => {
    expect(() => requireDocumentBlocksResponse({
      blocks: [block],
      pages: [page],
      assets: { wrong: { ...assetReference, block_id: "doc-1:image-1" } },
    })).toThrow(/图片资产标识/);
    expect(() => requireDocumentBlocksResponse({
      blocks: [block],
      pages: [page],
      assets: { "asset-1": { ...assetReference, block_id: "missing-block" } },
    })).toThrow(/关联/);
  });

  it("requires image width and height to be present as a pair", () => {
    expect(() => requireDocumentBlocksResponse({
      blocks: [block],
      pages: [page],
      assets: {
        "asset-1": {
          ...assetReference,
          block_id: "doc-1:image-1",
          width: 640,
        },
      },
    })).toThrow(/图片资产详情/);
  });

  it("accepts optional image metadata for Retrieval and Source Context", () => {
    const retrieval = requireRetrievalResponse({
      query: "合成系统流程图",
      knowledge_base_ids: ["kb-1"],
      document_ids: null,
      mode_requested: "keyword",
      pipeline_used: "keyword",
      degraded: false,
      warnings: [],
      results: [{
        source_id: "source-1",
        source_url: "/sources/source-1",
        text: "图 1 合成系统流程图",
        document_id: "doc-1",
        document_name: "项目报告",
        human_page_number: 1,
        bbox: [10, 20, 90, 70],
        score: 0.9,
        asset: assetReference,
      }],
    });
    expect(retrieval.results[0].asset?.asset_url).toBe(assetReference.asset_url);

    const context = requireSourceContextResponse({
      source: {
        source_id: "source-1",
        source_url: "/sources/source-1",
        text: "图 1 合成系统流程图",
        document_id: "doc-1",
        document_name: "项目报告",
        knowledge_base_id: "kb-1",
        human_page_number: 1,
        bbox: [10, 20, 90, 70],
        page_size: [100, 100],
        title_path: [],
        file_url: "/api/knowledge-bases/kb-1/documents/doc-1/file",
        asset: {
          ...assetReference,
          content_type: "image/png",
          file_hash: "image-hash",
          width: 640,
          height: 320,
          caption: "图 1 合成系统流程图",
        },
      },
      before: [],
      after: [],
      truncated: false,
    });
    expect(context.source.asset?.width).toBe(640);
  });

  it("rejects an asset object without its modality", () => {
    expect(() => requireRetrievalResponse({
      query: "合成系统流程图",
      knowledge_base_ids: ["kb-1"],
      document_ids: null,
      mode_requested: "keyword",
      pipeline_used: "keyword",
      degraded: false,
      warnings: [],
      results: [{
        source_id: "source-1",
        source_url: "/sources/source-1",
        text: "图 1 合成系统流程图",
        document_id: "doc-1",
        document_name: "项目报告",
        human_page_number: 1,
        bbox: null,
        score: 0.9,
        asset: { ...assetReference, modality: undefined },
      }],
    })).toThrow(/图片资产/);
  });
});
