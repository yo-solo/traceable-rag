export type ViewerDeepLink = {
  page: number;
  blockId?: string | null;
};

function parsePage(value: string | null) {
  if (!value || !/^\d+$/.test(value)) {
    return 1;
  }
  const page = Number(value);
  return Number.isSafeInteger(page) && page >= 1 ? page : 1;
}

export function parseViewerSearch(search: string): ViewerDeepLink {
  const params = new URLSearchParams(search);
  const location: ViewerDeepLink = { page: parsePage(params.get("page")) };
  const blockId = params.get("block");
  if (blockId) {
    location.blockId = blockId;
  }
  return location;
}

export function buildViewerSearch(
  location: ViewerDeepLink,
  currentSearch = "",
): string {
  const params = new URLSearchParams(currentSearch);
  params.set("page", String(Math.max(1, Math.trunc(location.page))));

  if (location.blockId) {
    params.set("block", location.blockId);
  } else {
    params.delete("block");
  }
  params.delete("block_token");
  params.delete("evidence");

  const query = params.toString();
  return query ? `?${query}` : "";
}
