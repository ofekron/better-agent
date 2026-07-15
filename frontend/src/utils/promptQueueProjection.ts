import type { QueuedBannerState } from "src/utils/queuedPrompts";

export interface PromptQueueProjection {
  items: QueuedBannerState[];
  revision: number;
  acknowledgedAt: ReadonlyMap<string, number>;
}

export const EMPTY_PROMPT_QUEUE_PROJECTION: PromptQueueProjection = {
  items: [],
  revision: 0,
  acknowledgedAt: new Map(),
};

export function queueItemAcknowledged(
  state: PromptQueueProjection,
  item: QueuedBannerState,
  revision: number,
): PromptQueueProjection {
  if (revision < state.revision) return state;
  const items = state.items.some((current) => current.id === item.id)
    ? state.items.map((current) => current.id === item.id ? item : current)
    : [...state.items, item];
  return {
    items,
    revision: Math.max(state.revision, revision),
    acknowledgedAt: new Map([...state.acknowledgedAt, [item.id, revision]]),
  };
}

export function queueSnapshotReceived(
  state: PromptQueueProjection,
  snapshot: QueuedBannerState[],
  revision: number,
): PromptQueueProjection {
  if (revision < state.revision) return state;
  const snapshotIds = new Set(snapshot.map((item) => item.id));
  const preserved = state.items.filter(
    (item) => (state.acknowledgedAt.get(item.id) ?? -1) > revision && !snapshotIds.has(item.id),
  );
  const acknowledgedAt = new Map(state.acknowledgedAt);
  for (const id of snapshotIds) acknowledgedAt.delete(id);
  for (const [id, acknowledgedRevision] of acknowledgedAt) {
    if (acknowledgedRevision <= revision) acknowledgedAt.delete(id);
  }
  return {
    items: [...snapshot, ...preserved],
    revision,
    acknowledgedAt,
  };
}

export function queueItemsConsumed(
  state: PromptQueueProjection,
  ids?: readonly string[],
): PromptQueueProjection {
  if (!ids || ids.length === 0) {
    return { items: [], revision: state.revision, acknowledgedAt: new Map() };
  }
  const consumed = new Set(ids);
  return {
    items: state.items.filter((item) => !consumed.has(item.id)),
    revision: state.revision,
    acknowledgedAt: new Map(
      [...state.acknowledgedAt].filter(([id]) => !consumed.has(id)),
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
