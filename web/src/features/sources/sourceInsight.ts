export const MAX_SOURCE_INSIGHT_CHARS = 160;

export function normalizeSourceInsight(value: string | null | undefined) {
  if (!value) {
    return null;
  }
  const normalized = value.replace(/\s+/gu, " ").trim();
  if (!normalized) {
    return null;
  }
  return Array.from(normalized).slice(0, MAX_SOURCE_INSIGHT_CHARS).join("");
}

export function parseSourceInsight(hash: string) {
  const fragment = hash.startsWith("#") ? hash.slice(1) : hash;
  if (!fragment) {
    return null;
  }
  return normalizeSourceInsight(new URLSearchParams(fragment).get("insight"));
}

export function buildSourceInsightFragment(value: string) {
  const insight = normalizeSourceInsight(value);
  return insight ? `#insight=${encodeURIComponent(insight)}` : "";
}
