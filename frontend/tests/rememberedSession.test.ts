import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  getRememberedSessionId,
  pickSessionForProject,
  setRememberedSessionId,
} from "../src/utils/uiSelection";
import type { Session } from "../src/types";

const mk = (
  id: string,
  cwd: string,
  opts: Partial<Pick<Session, "node_id" | "archived">> = {},
): Session => ({
  id,
  name: id,
  model: "m",
  cwd,
  node_id: opts.node_id,
  archived: opts.archived,
  created_at: "",
  updated_at: "",
  messages: [],
});

function stubUiSelectionPatch(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(new Response("{}", { status: 200 }))),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("rememberedSession storage", () => {
  beforeEach(() => {
    localStorage.clear();
    stubUiSelectionPatch();
  });

  it("round-trips a remembered id per project+node", () => {
    setRememberedSessionId("/a", "primary", "s1");
    expect(getRememberedSessionId("/a", "primary")).toBe("s1");
  });

  it("scopes by project path", () => {
    setRememberedSessionId("/a", "primary", "s1");
    expect(getRememberedSessionId("/b", "primary")).toBeNull();
  });

  it("scopes by node id", () => {
    setRememberedSessionId("/a", "primary", "s1");
    expect(getRememberedSessionId("/a", "remote-1")).toBeNull();
  });

  it("overwrites the previous value for the same project+node", () => {
    setRememberedSessionId("/a", "primary", "s1");
    setRememberedSessionId("/a", "primary", "s2");
    expect(getRememberedSessionId("/a", "primary")).toBe("s2");
  });
});

describe("pickSessionForProject", () => {
  beforeEach(() => localStorage.clear());

  it("returns the remembered session when it is still present", () => {
    const sessions = [mk("a1", "/a"), mk("a2", "/a")];
    const picked = pickSessionForProject(sessions, "/a", "primary", "a2");
    expect(picked?.id).toBe("a2");
  });

  it("falls back to the first session when the remembered id is gone", () => {
    const sessions = [mk("a1", "/a"), mk("a2", "/a")];
    const picked = pickSessionForProject(sessions, "/a", "primary", "deleted");
    expect(picked?.id).toBe("a1");
  });

  it("returns the first session when nothing is remembered", () => {
    const sessions = [mk("a1", "/a"), mk("a2", "/a")];
    const picked = pickSessionForProject(sessions, "/a", "primary", null);
    expect(picked?.id).toBe("a1");
  });

  it("returns null when the project has no sessions", () => {
    expect(pickSessionForProject([], "/a", "primary", null)).toBeNull();
  });

  it("ignores a remembered id from a different project", () => {
    const sessions = [mk("a1", "/a")];
    const picked = pickSessionForProject(sessions, "/a", "primary", "b1");
    expect(picked?.id).toBe("a1");
  });

  it("ignores a remembered id from a different node", () => {
    const sessions = [mk("a1", "/a", { node_id: "primary" })];
    const picked = pickSessionForProject(
      sessions,
      "/a",
      "remote-1",
      "a1",
    );
    expect(picked).toBeNull();
  });

  it("falls back to the first non-archived when the remembered is archived", () => {
    const sessions = [
      mk("a1", "/a"),
      mk("a2", "/a", { archived: true }),
    ];
    const picked = pickSessionForProject(sessions, "/a", "primary", "a2");
    expect(picked?.id).toBe("a1");
  });

  it("skips archived sessions entirely", () => {
    const sessions = [mk("a1", "/a", { archived: true })];
    const picked = pickSessionForProject(sessions, "/a", "primary", null);
    expect(picked).toBeNull();
  });
});

// Guards against cross-project clobber: only sessions whose cwd+node match
// the selected project are ever eligible.
describe("pickSessionForProject project boundary", () => {
  it("never returns a session belonging to another project", () => {
    const sessions = [mk("b1", "/b"), mk("b2", "/b")];
    const picked = pickSessionForProject(sessions, "/a", "primary", "b1");
    expect(picked).toBeNull();
  });
});
