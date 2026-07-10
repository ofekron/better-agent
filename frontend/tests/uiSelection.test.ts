import { describe, it, expect } from "vitest";
import { routedSessionMatchesProject } from "../src/utils/uiSelection";
import type { Session } from "../src/types";

/**
 * Regression test — the route↔project sync effect in App.tsx must not
 * redirect the user away from an archived session they explicitly
 * navigated to (e.g. clicking its tab in SessionTabs). Before the fix,
 * `routedSessionMatchesProject`'s inline predecessor also required
 * `!routed.archived`, so opening an archived session's own tab bounced
 * straight back out to a different session for the same project.
 */
describe("routedSessionMatchesProject", () => {
  const mk = (overrides: Partial<Session> = {}): Pick<Session, "cwd" | "node_id"> => ({
    cwd: "/a",
    node_id: "primary",
    ...overrides,
  });

  it("matches an archived session whose cwd/node already equal the selected project", () => {
    const routed = mk({ archived: true } as Partial<Session>);
    expect(routedSessionMatchesProject(routed, "/a", "primary")).toBe(true);
  });

  it("matches a non-archived session whose cwd/node already equal the selected project", () => {
    const routed = mk();
    expect(routedSessionMatchesProject(routed, "/a", "primary")).toBe(true);
  });

  it("does not match when cwd differs", () => {
    const routed = mk({ cwd: "/b" });
    expect(routedSessionMatchesProject(routed, "/a", "primary")).toBe(false);
  });

  it("does not match when node_id differs", () => {
    const routed = mk({ node_id: "secondary" });
    expect(routedSessionMatchesProject(routed, "/a", "primary")).toBe(false);
  });

  it("treats a missing node_id as primary", () => {
    const routed = mk({ node_id: undefined });
    expect(routedSessionMatchesProject(routed, "/a", "primary")).toBe(true);
  });
});
