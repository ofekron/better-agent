import type { QueuedBannerState } from "src/utils/queuedPrompts";

export interface PromptQueueProjection {
  items: QueuedBannerState[];
  awaitingSnapshot: ReadonlySet<string>;
}

export const EMPTY_PROMPT_QUEUE_PROJECTION: PromptQueueProjection = {
  items: [],
  awaitingSnapshot: new Set(),
};

export function queueItemAcknowledged(
  state: PromptQueueProjection,
  item: QueuedBannerState,
): PromptQueueProjection {
  const items = state.items.some((current) => current.id === item.id)
    ? state.items.map((current) => current.id === item.id ? item : current)
    : [...state.items, item];
  return {
    items,
    awaitingSnapshot: new Set([...state.awaitingSnapshot, item.id]),
  };
}

export function queueSnapshotReceived(
  state: PromptQueueProjection,
  snapshot: QueuedBannerState[],
): PromptQueueProjection {
  const snapshotIds = new Set(snapshot.map((item) => item.id));
  const preserved = state.items.filter(
    (item) => state.awaitingSnapshot.has(item.id) && !snapshotIds.has(item.id),
  );
  return {
    items: [...snapshot, ...preserved],
    awaitingSnapshot: new Set(),
  };
}

export function queueItemsConsumed(
  state: PromptQueueProjection,
  ids?: readonly string[],
): PromptQueueProjection {
  if (!ids || ids.length === 0) return EMPTY_PROMPT_QUEUE_PROJECTION;
  const consumed = new Set(ids);
  return {
    items: state.items.filter((item) => !consumed.has(item.id)),
    awaitingSnapshot: new Set(
      [...state.awaitingSnapshot].filter((id) => !consumed.has(id)),
    ),
  };
}

export function queueItemUpdated(
  state: PromptQueueProjection,
  id: string,
  preview: string,
): PromptQueueProjection {
  return {
    ...state,
    items: state.items.map((item) => item.id === id ? { ...item, preview } : item),
  };
}
