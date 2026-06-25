import { describe, expect, it } from "vitest";
import { additionalSessionSubscriptionIds } from "../src/utils/sessionSubscriptions";

describe("additionalSessionSubscriptionIds", () => {
  it("keeps a focused nested pane subscribed when the primary target is the root", () => {
    expect(
      additionalSessionSubscriptionIds(["root", "fork-a", "fork-b"], "root"),
    ).toEqual(["fork-a", "fork-b"]);
  });

  it("keeps the root subscribed when the primary target is a focused nested pane", () => {
    expect(
      additionalSessionSubscriptionIds(["root", "fork-a", "fork-b"], "fork-a"),
    ).toEqual(["root", "fork-b"]);
  });

  it("matches the old subscription set when focus and primary target match", () => {
    expect(
      additionalSessionSubscriptionIds(["root", "fork-a"], "fork-a"),
    ).toEqual(["root"]);
  });

  it("deduplicates ids without changing first-seen order", () => {
    expect(
      additionalSessionSubscriptionIds(["root", "fork-a", "fork-a", ""], "root"),
    ).toEqual(["fork-a"]);
  });
});
