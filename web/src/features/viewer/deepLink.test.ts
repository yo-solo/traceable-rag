import { describe, expect, it } from "vitest";

import { buildViewerSearch, parseViewerSearch } from "./deepLink";

describe("viewer deep links", () => {
  it("parses a 1-based page with a document block identifier", () => {
    expect(
      parseViewerSearch("?page=4&block=doc%3Ablock-4&evidence=evidence%2F4"),
    ).toEqual({
      page: 4,
      blockId: "doc:block-4",
    });
  });

  it("ignores removed Evidence and block-token parameters", () => {
    expect(
      parseViewerSearch("?page=4&block_token=stable-token&evidence=evidence%2F4"),
    ).toEqual({ page: 4 });
  });

  it.each(["0", "-1", "1.5", "NaN", ""])(
    "falls back to page one for invalid page value %s",
    (page) => {
      expect(parseViewerSearch(`?page=${encodeURIComponent(page)}`).page).toBe(1);
    },
  );

  it("writes page and block while preserving unrelated query parameters", () => {
    const search = buildViewerSearch(
      {
        page: 3,
        blockId: "doc:block 3",
      },
      "?mode=inspect&block=stale",
    );
    const params = new URLSearchParams(search);

    expect(Object.fromEntries(params)).toEqual({
      mode: "inspect",
      page: "3",
      block: "doc:block 3",
    });
  });

  it("removes stale optional parameters when a selection is cleared", () => {
    const search = buildViewerSearch(
      { page: 2, blockId: null },
      "?page=9&block=old&evidence=old-evidence",
    );
    const params = new URLSearchParams(search);

    expect(params.get("page")).toBe("2");
    expect(params.has("block")).toBe(false);
    expect(params.has("evidence")).toBe(false);
  });

  it("writes the actual block id and removes legacy locator parameters", () => {
    const search = buildViewerSearch(
      {
        page: 4,
        blockId: "mineru:very-long-real-block-id",
      },
      "?block=stale&block_token=stable-token&evidence=evidence%2F4",
    );
    const params = new URLSearchParams(search);

    expect(Object.fromEntries(params)).toEqual({
      page: "4",
      block: "mineru:very-long-real-block-id",
    });
  });
});
