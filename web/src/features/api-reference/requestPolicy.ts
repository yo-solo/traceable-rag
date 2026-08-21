export const API_REFERENCE_DEBUG_DISABLED_CODE =
  "api_reference_online_debug_disabled";

const DEBUG_DISABLED_MESSAGE =
  "API 参考仅用于浏览文档；所有接口请求均在浏览器本地拦截，不会发送到服务端。";

function requestUrl(input: RequestInfo | URL, origin: string) {
  if (input instanceof Request) {
    return new URL(input.url, origin);
  }
  return new URL(String(input), origin);
}

function requestMethod(input: RequestInfo | URL, init?: RequestInit) {
  return (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
}

export function isApiReferenceRequestAllowed(
  input: RequestInfo | URL,
  init: RequestInit | undefined,
  origin: string,
) {
  const url = requestUrl(input, origin);
  if (url.origin !== origin) {
    return false;
  }
  const method = requestMethod(input, init);
  return method === "GET" && url.pathname === "/openapi.json";
}

function debuggingDisabledResponse(url: string) {
  const response = new Response(
    JSON.stringify({
      detail: {
        code: API_REFERENCE_DEBUG_DISABLED_CODE,
        message: DEBUG_DISABLED_MESSAGE,
      },
    }),
    {
      status: 403,
      statusText: "Blocked by API Reference",
      headers: { "content-type": "application/json; charset=utf-8" },
    },
  );
  Object.defineProperty(response, "url", { value: url });
  return response;
}

export function createApiReferenceFetch(
  fetchImplementation: typeof fetch,
  origin: string,
): typeof fetch {
  return async (input, init) => {
    const url = requestUrl(input, origin);
    if (!isApiReferenceRequestAllowed(input, init, origin)) {
      return debuggingDisabledResponse(url.toString());
    }
    const resolvedInput =
      input instanceof Request ? input : new URL(String(input), origin);
    const request = new Request(resolvedInput, {
      ...init,
      credentials: "same-origin",
    });
    return fetchImplementation(request);
  };
}
