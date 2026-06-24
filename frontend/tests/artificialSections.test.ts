import { describe, expect, it } from "vitest";
import {
  parseArtificialSections,
  prettyTagLabel,
} from "../src/utils/artificialSections";

describe("artificial sections", () => {
  it("renders delegated task wrappers as a tagged section", () => {
    const sections = parseArtificialSections(
      '<delegated-task source="test" role="worker"><user_prompt>Check checkout</user_prompt></delegated-task>',
    );

    expect(sections).toHaveLength(1);
    expect(sections[0]).toMatchObject({
      kind: "tag",
      tag: "delegated-task",
      attrs: { source: "test", role: "worker" },
      inner: "<user_prompt>Check checkout</user_prompt>",
    });
    expect(prettyTagLabel("delegated-task")).toBe("Delegated task");
  });
});
