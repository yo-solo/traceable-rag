import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("api client", () => {
  it("serializes JSON requests and forwards the AbortSignal", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    await expect(
      api.post<{ ok: boolean }>("/api/retrieval", {
        body: { query: "income inequality" },
        signal: controller.signal,
      }),
    ).resolves.toEqual({ ok: true });

    const [path, request] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(request.headers);
    expect(path).toBe("/api/retrieval");
    expect(request.method).toBe("POST");
    expect(request.signal).toBe(controller.signal);
    expect(request.body).toBe(JSON.stringify({ query: "income inequality" }));
    expect(headers.get("Accept")).toBe("application/json");
    expect(headers.get("Content-Type")).toBe("application/json");
  });

  it("normalizes structured API failures", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: { code: "knowledge_base_not_found", message: "知识库不存在" },
          }),
          {
            status: 404,
            headers: { "content-type": "application/json" },
          },
        ),
      ),
    );

    await expect(api.get("/api/knowledge-bases/missing")).rejects.toMatchObject({
      name: "ApiError",
      status: 404,
      code: "knowledge_base_not_found",
      message: "知识库不存在",
    } satisfies Partial<ApiError>);
  });

  it.each([204, 205])("returns undefined for an empty %s response", async (status) => {
    const response = new Response(null, { status });
    const textSpy = vi.spyOn(response, "text");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response));

    await expect(api.post<void>("/api/action")).resolves.toBeUndefined();
    expect(textSpy).toHaveBeenCalledOnce();
  });

  it("wraps malformed JSON success responses without leaking SyntaxError", async () => {
    const response = new Response("{not-json", {
      status: 200,
      headers: { "content-type": "application/json" },
    });
    const textSpy = vi.spyOn(response, "text");
    const jsonSpy = vi.spyOn(response, "json");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response));

    await expect(api.get("/api/broken-json")).rejects.toMatchObject({
      name: "ApiError",
      status: 200,
      code: "invalid_json_response",
      message: "服务返回了无效的 JSON 响应",
      payload: "{not-json",
    } satisfies Partial<ApiError>);
    expect(textSpy).toHaveBeenCalledOnce();
    expect(jsonSpy).not.toHaveBeenCalled();
  });

  it("preserves malformed JSON error text inside ApiError", async () => {
    const response = new Response("上游服务返回了无法解析的内容", {
      status: 502,
      statusText: "Bad Gateway",
      headers: { "content-type": "application/json" },
    });
    const textSpy = vi.spyOn(response, "text");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response));

    await expect(api.get("/api/upstream")).rejects.toMatchObject({
      name: "ApiError",
      status: 502,
      message: "上游服务返回了无法解析的内容",
      payload: "上游服务返回了无法解析的内容",
    } satisfies Partial<ApiError>);
    expect(textSpy).toHaveBeenCalledOnce();
  });

  it("preserves text error responses inside ApiError", async () => {
    const response = new Response("服务暂时不可用", {
      status: 503,
      statusText: "Service Unavailable",
      headers: { "content-type": "text/plain" },
    });
    const textSpy = vi.spyOn(response, "text");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response));

    await expect(api.get("/api/unavailable")).rejects.toMatchObject({
      name: "ApiError",
      status: 503,
      message: "服务暂时不可用",
      payload: "服务暂时不可用",
    } satisfies Partial<ApiError>);
    expect(textSpy).toHaveBeenCalledOnce();
  });

  it("uploads FormData without overriding the browser multipart boundary", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ uploaded: true }), {
        status: 201,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();
    const file = new File(["report"], "report.docx", {
      type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    });

    await api.upload("/api/knowledge-bases/kb-1/documents/upload", file, {
      displayName: "研究报告",
      signal: controller.signal,
    });

    const [, request] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(request.method).toBe("POST");
    expect(request.signal).toBe(controller.signal);
    expect(request.body).toBeInstanceOf(FormData);
    expect((request.body as FormData).get("file")).toBe(file);
    expect((request.body as FormData).get("display_name")).toBe("研究报告");
    expect(new Headers(request.headers).has("Content-Type")).toBe(false);
  });
});
