import { describe, expect, it, vi } from "vitest";

import {
  API_REFERENCE_DEBUG_DISABLED_CODE,
  createApiReferenceFetch,
  isApiReferenceRequestAllowed,
} from "./requestPolicy";

const ORIGIN = "http://localhost";

describe("API reference request policy", () => {
  it("allows only the same-origin OpenAPI document request", () => {
    expect(isApiReferenceRequestAllowed("/openapi.json", undefined, ORIGIN)).toBe(true);
  });

  it.each([
    ["/api/health", undefined],
    ["/api/knowledge-bases/kb/documents/doc/file", { method: "HEAD" }],
    ["/api/retrieval", { method: "POST", body: "{}" }],
    ["https://example.com/openapi.json", undefined],
    ["https://example.com/api/health", undefined],
    ["/api/knowledge-bases", { method: "POST" }],
    ["/api/settings", { method: "PATCH" }],
    ["/api/settings/diagnostics", { method: "POST" }],
    ["/api/knowledge-bases/kb", { method: "DELETE" }],
  ])("blocks every API or cross-origin request %s", (input, init) => {
    expect(isApiReferenceRequestAllowed(input, init, ORIGIN)).toBe(false);
  });

  it("returns a local debugging-disabled response without calling the network", async () => {
    const fetchImplementation = vi.fn<typeof fetch>();
    const guardedFetch = createApiReferenceFetch(fetchImplementation, ORIGIN);

    const response = await guardedFetch("/api/health");

    expect(fetchImplementation).not.toHaveBeenCalled();
    expect(response.status).toBe(403);
    expect(response.url).toBe("http://localhost/api/health");
    expect(await response.json()).toEqual({
      detail: {
        code: API_REFERENCE_DEBUG_DISABLED_CODE,
        message: expect.stringContaining("所有接口请求"),
      },
    });
  });

  it("loads the OpenAPI document with same-origin credentials", async () => {
    const fetchImplementation = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        headers: { "content-type": "application/json" },
      }),
    );
    const guardedFetch = createApiReferenceFetch(fetchImplementation, ORIGIN);

    await guardedFetch("/openapi.json");

    const request = fetchImplementation.mock.calls[0]?.[0];
    expect(request).toBeInstanceOf(Request);
    expect((request as Request).url).toBe("http://localhost/openapi.json");
    expect((request as Request).credentials).toBe("same-origin");
  });
});
