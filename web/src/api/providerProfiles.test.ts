import { describe, expect, it } from "vitest";

import {
  requireProviderProfile,
  requireProviderProfilesResponse,
} from "./guards";

const profile = {
  role: "embedding",
  adapter: "siliconflow",
  source: "stored",
  display_name: "Embedding",
  base_url: "https://api.siliconflow.cn",
  model: "Qwen/Qwen3-Embedding-8B",
  options: {
    timeout: 60,
    batch_size: 16,
    query_prompt: "Represent the query",
  },
  configured: true,
  secret_present: true,
  status: "configured",
  last_test_status: "success",
  last_tested_at: "2026-08-10T08:00:00Z",
  api_key: "must-be-dropped",
};

describe("provider profile response guards", () => {
  it("returns only the non-sensitive profile contract", () => {
    const guarded = requireProviderProfile(profile, "embedding");
    expect(guarded).not.toHaveProperty("api_key");
    expect(guarded.options).toEqual(profile.options);
  });

  it("rejects role-adapter mismatches and unknown option keys", () => {
    expect(() => requireProviderProfile({ ...profile, adapter: "cohere" }, "embedding"))
      .toThrow(/外部服务配置/);
    expect(() => requireProviderProfile({
      ...profile,
      options: { ...profile.options, api_key: "unsafe" },
    }, "embedding")).toThrow(/高级选项/);
  });

  it("requires all four role profiles and preserves an unavailable store state", () => {
    const response = requireProviderProfilesResponse({
      storage_available: false,
      profiles: {
        mineru: {
          ...profile,
          role: "mineru",
          adapter: "mineru_official",
          options: { timeout: 60, language: "ch" },
        },
        embedding: profile,
        reranker: {
          ...profile,
          role: "reranker",
          adapter: "cohere",
          options: { timeout: 120 },
        },
        llm: {
          ...profile,
          role: "llm",
          adapter: "openai_compatible",
          options: { timeout: 30 },
        },
      },
    });
    expect(response.storage_available).toBe(false);
    expect(Object.keys(response.profiles)).toEqual(["mineru", "embedding", "reranker", "llm"]);
  });
});
