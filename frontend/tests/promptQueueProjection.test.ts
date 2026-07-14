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
    const first = queueItemAcknowledged(EMPTY_PROMPT_QUEUE_PROJECTION, item("a"));
    const second = queueItemAcknowledged(first, item("b"));
    expect(second.items).toEqual([item("a"), item("b")]);
  });

  it("does not let a stale snapshot erase a newer acknowledgement", () => {
    const acknowledged = queueItemAcknowledged(
      { items: [item("a")], awaitingSnapshot: new Set() },
      item("b"),
    );
    expect(queueSnapshotReceived(acknowledged, [item("a")]).items).toEqual([
      item("a"),
      item("b"),
    ]);
  });

  it("converges when the authoritative snapshot catches the acknowledgement", () => {
    const acknowledged = queueItemAcknowledged(EMPTY_PROMPT_QUEUE_PROJECTION, item("a"));
    const converged = queueSnapshotReceived(acknowledged, [item("a")]);
    expect(converged.items).toEqual([item("a")]);
    expect(converged.awaitingSnapshot.size).toBe(0);
  });

  it("lets the next authoritative snapshot remove an acknowledged item", () => {
    const acknowledged = queueItemAcknowledged(EMPTY_PROMPT_QUEUE_PROJECTION, item("a"));
    const stale = queueSnapshotReceived(acknowledged, []);
    const current = queueSnapshotReceived(stale, []);
    expect(stale.items).toEqual([item("a")]);
    expect(current.items).toEqual([]);
  });

  it("consumes selected items idempotently without disturbing order", () => {
    const state = {
      items: [item("a"), item("b"), item("c")],
      awaitingSnapshot: new Set(["b"]),
    };
    const once = queueItemsConsumed(state, ["a", "c"]);
    const twice = queueItemsConsumed(once, ["a", "c"]);
    expect(twice.items).toEqual([item("b")]);
    expect([...twice.awaitingSnapshot]).toEqual(["b"]);
  });
});
