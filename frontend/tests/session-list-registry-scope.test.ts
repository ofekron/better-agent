import { describe, it, expect } from "vitest";

import { isGlobalUnfilteredFetch } from "../src/hooks/useSession";
import type { SessionListFilters } from "../src/hooks/useSession";

/**
 * Regression guard for the project-switch aggregate wipe.
 *
 * The sessionRegistry is the ALL-projects source of truth for the
 * per-project running/unread badges. Only a full, UNFILTERED global list
 * page may `replaceFromRows` (which evicts everything not in the page).
 * A fetch narrowed to one project — or by search/tag/folder/etc. — is a
 * subset; replacing from it wiped every OTHER project's sessions out of
 * the registry, zeroing their badges until a fresh WS delta happened to
 * re-materialize them (steadily-running sessions with no state flip never
 * did). `isGlobalUnfilteredFetch` gates that: narrowed fetches must return
 * false so the caller seeds (fill-only) instead of replacing.
 */
describe("isGlobalUnfilteredFetch — registry replace vs seed gate", () => {
  it("true only when no narrowing filter is active", () => {
    expect(isGlobalUnfilteredFetch({})).toBe(true);
    // Non-narrowing view/sort knobs don't change the session universe.
    expect(
      isGlobalUnfilteredFetch({
        sortBy: "last_opened",
        statusSort: true,
        folderView: true,
        searchFields: ["name"],
      }),
    ).toBe(true);
  });

  it("false for a project-scoped fetch (the switch-project case)", () => {
    expect(isGlobalUnfilteredFetch({ projectPath: "/repo/a" })).toBe(false);
  });

  it("false for every other narrowing filter", () => {
    const narrowing: SessionListFilters[] = [
      { search: "bug" },
      { showArchived: true },
      { fileEditMode: "yes" },
      { fileEditMode: "no" },
      { folderIds: ["f1"] },
      { tagIds: ["t1"] },
      { providerIds: ["claude"] },
      { modelIds: ["opus"] },
      { modes: ["team"] },
      { sources: ["cli"] },
    ];
    for (const f of narrowing) {
      expect(isGlobalUnfilteredFetch(f)).toBe(false);
    }
  });

  it("empty/whitespace search and 'any' fileEditMode are NOT narrowing", () => {
    expect(isGlobalUnfilteredFetch({ search: "   " })).toBe(true);
    expect(isGlobalUnfilteredFetch({ fileEditMode: "any" })).toBe(true);
    expect(
      isGlobalUnfilteredFetch({ folderIds: [], tagIds: [], providerIds: [] }),
    ).toBe(true);
  });
});
