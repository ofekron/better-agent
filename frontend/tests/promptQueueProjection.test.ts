import { describe, expect, it } from "vitest";
import {
  EMPTY_PROMPT_QUEUE_PROJECTION,
  queueItemAcknowledged,
  queueItemsConsumed,
  queueSnapshotReceived,
} from "../src/utils/promptQueueProjection";

const item = (id: string) => ({ id, preview: `prompt ${id}` });

describe("prompt queue projection", () => {
  it("keeps rapid acknowledgements distinct and ordered", () => {
    const first = queueItemAcknowledged(EMPTY_PROMPT_QUEUE_PROJECTION, item("a"), 1);
    const second = queueItemAcknowledged(first, item("b"), 2);
    expect(second.items).toEqual([item("a"), item("b")]);
  });

  it("does not let a stale snapshot erase a newer acknowledgement", () => {
    const acknowledged = queueItemAcknowledged(
      { items: [item("a")], revision: 1, acknowledgedAt: new Map() },
      item("b"),
      3,
    );
    expect(queueSnapshotReceived(acknowledged, [item("a")], 2).items).toEqual([
      item("a"),
      item("b"),
    ]);
  });

  it("converges when the authoritative snapshot catches the acknowledgement", () => {
    const acknowledged = queueItemAcknowledged(EMPTY_PROMPT_QUEUE_PROJECTION, item("a"), 1);
    const converged = queueSnapshotReceived(acknowledged, [item("a")], 1);
    expect(converged.items).toEqual([item("a")]);
    expect(converged.acknowledgedAt.size).toBe(0);
  });

  it("rejects any number of snapshots older than the acknowledgement", () => {
    const acknowledged = queueItemAcknowledged(EMPTY_PROMPT_QUEUE_PROJECTION, item("a"), 4);
    const stale = queueSnapshotReceived(acknowledged, [], 3);
    const staleAgain = queueSnapshotReceived(stale, [], 3);
    expect(staleAgain.items).toEqual([item("a")]);
    expect(staleAgain.revision).toBe(4);
  });

  it("accepts authoritative removal at a newer revision", () => {
    const acknowledged = queueItemAcknowledged(EMPTY_PROMPT_QUEUE_PROJECTION, item("a"), 4);
    expect(queueSnapshotReceived(acknowledged, [], 5).items).toEqual([]);
  });

  it("does not resurrect a removed item from a delayed older acknowledgement", () => {
    const acknowledged = queueItemAcknowledged(EMPTY_PROMPT_QUEUE_PROJECTION, item("a"), 1);
    const removed = queueSnapshotReceived(acknowledged, [], 2);
    expect(queueItemAcknowledged(removed, item("a"), 1)).toBe(removed);
    expect(removed.items).toEqual([]);
  });

  it("consumes selected items idempotently without disturbing order", () => {
    const state = {
      items: [item("a"), item("b"), item("c")],
      revision: 3,
      acknowledgedAt: new Map([["b", 3]]),
    };
    const once = queueItemsConsumed(state, ["a", "c"]);
    const twice = queueItemsConsumed(once, ["a", "c"]);
    expect(twice.items).toEqual([item("b")]);
    expect([...twice.acknowledgedAt]).toEqual([["b", 3]]);
  });
});
