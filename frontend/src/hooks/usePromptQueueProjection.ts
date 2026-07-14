import { useCallback, useState } from "react";
import type { QueuedPrompt } from "src/types";
import {
  EMPTY_PROMPT_QUEUE_PROJECTION,
  queueItemAcknowledged,
  queueItemsConsumed,
  queueItemUpdated,
  queueSnapshotReceived,
  type PromptQueueProjection,
} from "src/utils/promptQueueProjection";
import {
  visibleQueuedPromptBanners,
  type QueuedBannerState,
} from "src/utils/queuedPrompts";

export function usePromptQueueProjection() {
  const [bySession, setBySession] = useState<Record<string, PromptQueueProjection>>({});
  const [optimisticBySession, setOptimisticBySession] = useState<Record<string, QueuedBannerState[]>>({});

  const stage = useCallback((sessionId: string, item: QueuedBannerState) => {
    setOptimisticBySession((all) => {
      const current = all[sessionId] ?? [];
      return {
        ...all,
        [sessionId]: [...current.filter((queued) => queued.clientId !== item.clientId), item],
      };
    });
  }, []);

  const applySnapshot = useCallback((sessionId: string, prompts: QueuedPrompt[]) => {
    const snapshot = visibleQueuedPromptBanners(prompts);
    setBySession((all) => ({
      ...all,
      [sessionId]: queueSnapshotReceived(
        all[sessionId] ?? EMPTY_PROMPT_QUEUE_PROJECTION,
        snapshot,
      ),
    }));
  }, []);

  const acknowledge = useCallback((sessionId: string, item: QueuedBannerState) => {
    if (item.clientId) {
      setOptimisticBySession((all) => ({
        ...all,
        [sessionId]: (all[sessionId] ?? []).filter(
          (queued) => queued.clientId !== item.clientId,
        ),
      }));
    }
    setBySession((all) => ({
      ...all,
      [sessionId]: queueItemAcknowledged(
        all[sessionId] ?? EMPTY_PROMPT_QUEUE_PROJECTION,
        item,
      ),
    }));
  }, []);

  const consume = useCallback((sessionId: string, ids?: readonly string[]) => {
    setOptimisticBySession((all) => {
      if (!ids || ids.length === 0) return { ...all, [sessionId]: [] };
      const consumed = new Set(ids);
      return {
        ...all,
        [sessionId]: (all[sessionId] ?? []).filter((item) => !consumed.has(item.id)),
      };
    });
    setBySession((all) => ({
      ...all,
      [sessionId]: queueItemsConsumed(
        all[sessionId] ?? EMPTY_PROMPT_QUEUE_PROJECTION,
        ids,
      ),
    }));
  }, []);

  const update = useCallback((sessionId: string, id: string, preview: string) => {
    setBySession((all) => ({
      ...all,
      [sessionId]: queueItemUpdated(
        all[sessionId] ?? EMPTY_PROMPT_QUEUE_PROJECTION,
        id,
        preview,
      ),
    }));
  }, []);

  const consumeClient = useCallback((sessionId: string, clientId: string) => {
    setOptimisticBySession((all) => ({
      ...all,
      [sessionId]: (all[sessionId] ?? []).filter((item) => item.clientId !== clientId),
    }));
    setBySession((all) => {
      const current = all[sessionId] ?? EMPTY_PROMPT_QUEUE_PROJECTION;
      const ids = current.items
        .filter((item) => item.clientId === clientId)
        .map((item) => item.id);
      if (ids.length === 0) return all;
      return { ...all, [sessionId]: queueItemsConsumed(current, ids) };
    });
  }, []);

  const itemsFor = useCallback((sessionId: string, authoritative?: QueuedPrompt[]) => {
    const projected = bySession[sessionId]?.items ?? visibleQueuedPromptBanners(authoritative);
    const ids = new Set(projected.map((item) => item.id));
    return [
      ...projected,
      ...(optimisticBySession[sessionId] ?? []).filter((item) => !ids.has(item.id)),
    ];
  }, [bySession, optimisticBySession]);

  return { acknowledge, applySnapshot, consume, consumeClient, itemsFor, stage, update };
}
