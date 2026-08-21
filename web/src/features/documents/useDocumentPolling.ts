import { useEffect, useRef } from "react";

import type { KnowledgeBaseDocument } from "../../api/types";

const TRANSITIONAL_STATUSES = new Set(["uploading", "queued", "parsing", "indexing"]);

export function hasTransitionalDocument(documents: KnowledgeBaseDocument[]) {
  return documents.some((document) => TRANSITIONAL_STATUSES.has(document.processing_status));
}

export function useDocumentPolling(
  documents: KnowledgeBaseDocument[],
  poll: () => Promise<void>,
  intervalMs = 2_000,
) {
  usePolling(hasTransitionalDocument(documents), poll, intervalMs);
}

export function usePolling(
  shouldPoll: boolean,
  poll: () => Promise<void>,
  intervalMs = 2_000,
) {
  const pollRef = useRef(poll);
  pollRef.current = poll;

  useEffect(() => {
    if (!shouldPoll) {
      return undefined;
    }

    let cancelled = false;
    let timeoutId: number | undefined;

    const scheduleNextPoll = () => {
      timeoutId = window.setTimeout(() => {
        void (async () => {
          try {
            await pollRef.current();
          } catch {
            // A transient refresh failure must not stop later status checks.
          } finally {
            if (!cancelled) {
              scheduleNextPoll();
            }
          }
        })();
      }, intervalMs);
    };

    scheduleNextPoll();

    return () => {
      cancelled = true;
      if (timeoutId !== undefined) {
        window.clearTimeout(timeoutId);
      }
    };
  }, [intervalMs, shouldPoll]);
}
