import { describe, expect, it } from "vitest";
import {
  buildTurnNodeTable,
  createTurnNodeTable,
  getTurnNode,
  hasTurnNode,
  upsertTurnNode,
} from "../src/adapter/turnNodeTable";
import type { NodeWire } from "../src/adapter/wire";

function node(id: string, ts: number, seq: number, text = ""): NodeWire {
  return {
    cv: 1,
    node_id: id,
    parent_id: null,
    turn_id: "t1",
    surface_id: "s1",
    kind: "assistant_text",
    ts,
    seq,
    status: "complete",
    payload: { text },
    run_ref: null,
    sidecar_ref: null,
    target_ref: null,
    child_manifest: null,
  };
}

describe("turnNodeTable", () => {
  it("appends new nodes in (ts, seq) order via the fast path", () => {
    const table = createTurnNodeTable();
    upsertTurnNode(table, node("a", 1, 0));
    upsertTurnNode(table, node("b", 2, 0));
    upsertTurnNode(table, node("c", 2, 1));
    expect(table.nodes.map((n) => n.node_id)).toEqual(["a", "b", "c"]);
    expect(table.indexById.get("a")).toBe(0);
    expect(table.indexById.get("b")).toBe(1);
    expect(table.indexById.get("c")).toBe(2);
  });

  it("inserts an out-of-order new node at the correct sorted position and reindexes", () => {
    const table = createTurnNodeTable();
    upsertTurnNode(table, node("a", 1, 0));
    upsertTurnNode(table, node("c", 3, 0));
    upsertTurnNode(table, node("b", 2, 0)); // out of order — must land between a and c
    expect(table.nodes.map((n) => n.node_id)).toEqual(["a", "b", "c"]);
    expect(table.indexById.get("a")).toBe(0);
    expect(table.indexById.get("b")).toBe(1);
    expect(table.indexById.get("c")).toBe(2);
  });

  it("replaces an existing node_id at the same (ts, seq) in place without reordering", () => {
    const table = createTurnNodeTable();
    upsertTurnNode(table, node("a", 1, 0, "hello"));
    upsertTurnNode(table, node("b", 2, 0, "world"));
    upsertTurnNode(table, node("a", 1, 0, "hello there"));
    expect(table.nodes.map((n) => n.node_id)).toEqual(["a", "b"]);
    expect(getTurnNode(table, "a")?.payload).toEqual({ text: "hello there" });
  });

  it("repositions an existing node_id whose (ts, seq) changed", () => {
    const table = createTurnNodeTable();
    upsertTurnNode(table, node("a", 1, 0));
    upsertTurnNode(table, node("b", 2, 0));
    upsertTurnNode(table, node("a", 3, 0)); // a now sorts after b
    expect(table.nodes.map((n) => n.node_id)).toEqual(["b", "a"]);
    expect(table.indexById.get("b")).toBe(0);
    expect(table.indexById.get("a")).toBe(1);
  });

  it("hasTurnNode/getTurnNode reflect current membership", () => {
    const table = createTurnNodeTable();
    expect(hasTurnNode(table, "a")).toBe(false);
    upsertTurnNode(table, node("a", 1, 0));
    expect(hasTurnNode(table, "a")).toBe(true);
    expect(getTurnNode(table, "a")?.node_id).toBe("a");
    expect(getTurnNode(table, "missing")).toBeUndefined();
  });

  it("buildTurnNodeTable bulk-sorts unordered input and keeps indexById consistent", () => {
    const table = buildTurnNodeTable([node("c", 3, 0), node("a", 1, 0), node("b", 2, 0)]);
    expect(table.nodes.map((n) => n.node_id)).toEqual(["a", "b", "c"]);
    table.nodes.forEach((n, i) => expect(table.indexById.get(n.node_id)).toBe(i));
  });
});
