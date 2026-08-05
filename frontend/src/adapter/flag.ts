// Runtime flag for the Chat Surface Contract v2 thin client. OFF by
// default — pure UI preference (CLAUDE.md state-ownership rule: not
// backend-owned, not persisted server-side), so a plain localStorage /
// query-param read is the correct (and only) source for it.

const STORAGE_KEY = "ba.surface_v2";
const QUERY_KEY = "surface_v2";

export function readFlag(): boolean {
  try {
    if (typeof window !== "undefined" && window.location?.search) {
      const params = new URLSearchParams(window.location.search);
      if (params.get(QUERY_KEY) === "1") return true;
    }
  } catch {
    // Malformed location.search — fall through to the storage check.
  }
  try {
    return localStorage.getItem(STORAGE_KEY) === "1";
  } catch {
    // Private mode / storage disabled — flag stays off.
    return false;
  }
}
