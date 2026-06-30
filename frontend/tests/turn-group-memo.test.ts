import { describe, expect, it } from "vitest";

import { TurnGroup } from "../src/components/MessageBubble";
import { turnGroupPropsEqual } from "../src/components/turnGroupPropsEqual";

// Drives memo(TurnGroup). Inputs are deliberately partial — the
// comparator only inspects keys present in `prev`, matching how React
// always hands a fixed prop shape to a component.
type Props = Parameters<typeof turnGroupPropsEqual>[0];
const props = (o: object): Props => o as unknown as Props;

const run1 = { target_message_id: "m1" };
const run2 = { target_message_id: "m2" };

describe("turnGroupPropsEqual (TurnGroup memo comparator)", () => {
  it("holds when every prop is the same reference", () => {
    const p = props({ runs: [run1], defaultCollapsed: false });
    expect(turnGroupPropsEqual(p, p)).toBe(true);
  });

  // Regression: a streaming token rebuilds Chat.tsx's `groups` useMemo
  // (its `allMessages` dep churns per token), which mints a fresh `runs`
  // array for every run-bearing group even when the run set is unchanged.
  // Default memo's reference compare would re-render that group — and its
  // AssistantMessage subtree — on every token. Equal content must hold.
  it("holds when `runs` is a new array with the same element refs", () => {
    expect(
      turnGroupPropsEqual(
        props({ runs: [run1, run2] }),
        props({ runs: [run1, run2] }),
      ),
    ).toBe(true);
  });

  it("bails when `runs` length differs (a run was added/removed)", () => {
    expect(
      turnGroupPropsEqual(props({ runs: [run1] }), props({ runs: [run1, run2] })),
    ).toBe(false);
  });

  it("bails when a `runs` element ref differs", () => {
    expect(
      turnGroupPropsEqual(props({ runs: [run1] }), props({ runs: [run2] })),
    ).toBe(false);
  });

  it("bails when a non-runs prop changes identity (threadColorMap)", () => {
    const a = new Map([["m1", "#fff"]]);
    const b = new Map([["m1", "#fff"]]);
    expect(
      turnGroupPropsEqual(
        props({ runs: [run1], threadColorMap: a }),
        props({ runs: [run1], threadColorMap: b }),
      ),
    ).toBe(false);
  });

  it("bails when a scalar prop changes value", () => {
    expect(
      turnGroupPropsEqual(
        props({ runs: [run1], defaultCollapsed: false }),
        props({ runs: [run1], defaultCollapsed: true }),
      ),
    ).toBe(false);
  });

  // Guards the WIRING: TurnGroup must actually pass the comparator to
  // memo(). React exposes it on the memo object at runtime. A revert that
  // drops the 2nd memo arg would leave compare === null → this fails.
  it("TurnGroup is wired with turnGroupPropsEqual", () => {
    const compare = (
      TurnGroup as unknown as { compare?: unknown }
    ).compare;
    expect(compare).toBe(turnGroupPropsEqual);
  });
});
