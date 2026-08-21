import { describe, expect, it } from "vitest";

import {
  MAX_SOURCE_INSIGHT_CHARS,
  buildSourceInsightFragment,
  normalizeSourceInsight,
  parseSourceInsight,
} from "./sourceInsight";

describe("source insight links", () => {
  it("normalizes whitespace and round-trips through a URL fragment", () => {
    const fragment = buildSourceInsightFragment("  关键数字\n 直接解释了差异  ");
    expect(fragment).toContain("#insight=");
    expect(parseSourceInsight(fragment)).toBe("关键数字 直接解释了差异");
  });

  it("keeps legacy and empty fragments bubble-free", () => {
    expect(parseSourceInsight("")).toBeNull();
    expect(parseSourceInsight("#other=value")).toBeNull();
    expect(normalizeSourceInsight(" \n\t ")).toBeNull();
  });

  it("bounds untrusted insight text by Unicode characters", () => {
    const value = "证".repeat(MAX_SOURCE_INSIGHT_CHARS + 20);
    expect(Array.from(parseSourceInsight(buildSourceInsightFragment(value)) ?? "")).toHaveLength(
      MAX_SOURCE_INSIGHT_CHARS,
    );
  });
});
