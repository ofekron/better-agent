// Runtime flag for the Chat Surface Contract v2 thin client. ON by
// default — pure UI preference (CLAUDE.md state-ownership rule: not
// backend-owned, not persisted server-side), so a plain localStorage /
// query-param read is the correct (and only) source for it. An explicit
// opt-out (`ba.surface_v2`==="0" in localStorage, or `?surface_v2=0`) is
// the kill-switch back to the legacy render path.

const STORAGE_KEY = "ba.surface_v2";
const QUERY_KEY = "surface_v2";

export function readFlag(): boolean {
  try {
    if (typeof window !== "undefined" && window.location?.search) {
      const params = new URLSearchParams(window.location.search);
      const value = params.get(QUERY_KEY);
      if (value === "1") return true;
      if (value === "0") return false;
    }
  } catch {
    // Malformed location.search — fall through to the storage check.
  }
  try {
    return localStorage.getItem(STORAGE_KEY) !== "0";
  } catch {
    // Private mode / storage disabled — default ON.
    return true;
  }
}
