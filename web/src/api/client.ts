import type {
  ApiErrorPayload,
  ApiJsonRequestOptions,
  ApiRequestOptions,
  ApiUploadOptions,
} from "./types";

export class ApiError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly payload: unknown;

  constructor(message: string, status: number, code?: string, payload?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.payload = payload;
  }
}

async function parseResponse<T>(response: Response): Promise<T | undefined> {
  const contentType = response.headers.get("content-type") ?? "";
  const rawBody = await response.text();
  const hasBody = ![204, 205].includes(response.status) && rawBody.trim() !== "";
  let payload: unknown;

  if (hasBody && contentType.includes("json")) {
    try {
      payload = JSON.parse(rawBody);
    } catch {
      const message = response.ok
        ? "服务返回了无效的 JSON 响应"
        : rawBody.trim() || response.statusText || "请求失败";
      throw new ApiError(
        message,
        response.status,
        "invalid_json_response",
        rawBody,
      );
    }
  } else if (hasBody) {
    payload = rawBody;
  }

  if (!response.ok) {
    const apiPayload = payload as ApiErrorPayload;
    const detail = apiPayload && typeof apiPayload === "object" ? apiPayload.detail : undefined;
    const message =
      (typeof detail === "object" && detail?.message) ||
      (typeof detail === "string" && detail) ||
      (apiPayload && typeof apiPayload === "object" && apiPayload.message) ||
      (typeof payload === "string" && payload.trim()) ||
      response.statusText ||
      "请求失败";
    const code = typeof detail === "object" ? detail?.code : undefined;
    throw new ApiError(message, response.status, code, payload);
  }

  return payload as T;
}

async function requestJson<T>(
  method: string,
  path: string,
  options: ApiJsonRequestOptions = {},
): Promise<T | undefined> {
  const headers = new Headers(options.headers);
  headers.set("Accept", "application/json");
  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(path, {
    method,
    headers,
    signal: options.signal,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  return parseResponse<T>(response);
}

export const api = {
  get<T>(path: string, options: ApiRequestOptions = {}) {
    return requestJson<T>("GET", path, options);
  },
  post<T>(path: string, options: ApiJsonRequestOptions = {}) {
    return requestJson<T>("POST", path, options);
  },
  put<T>(path: string, options: ApiJsonRequestOptions = {}) {
    return requestJson<T>("PUT", path, options);
  },
  patch<T>(path: string, options: ApiJsonRequestOptions = {}) {
    return requestJson<T>("PATCH", path, options);
  },
  delete<T>(path: string, options: ApiJsonRequestOptions = {}) {
    return requestJson<T>("DELETE", path, options);
  },
  async upload<T>(path: string, file: File, options: ApiUploadOptions = {}) {
    const formData = new FormData();
    formData.append(options.fieldName ?? "file", file);
    if (options.displayName) {
      formData.append("display_name", options.displayName);
    }

    const headers = new Headers(options.headers);
    headers.set("Accept", "application/json");
    headers.delete("Content-Type");
    const response = await fetch(path, {
      method: "POST",
      headers,
      signal: options.signal,
      body: formData,
    });
    return parseResponse<T>(response);
  },
};
