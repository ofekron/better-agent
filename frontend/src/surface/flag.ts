// Runtime flag for the native Contract-Node rendering layer (Phase I
// stage 2b). ON by default — the native surface is now the sole chat
// content plane; the legacy down-mapped thin-client and raw WS-ingested
// render paths have been deleted. An explicit opt-out (`ba.surface_native`
// === "0" in localStorage, or `?surface_native=0`) is the kill-switch,
// same pattern `ba.surface_v2` used during its own rollout (adapter/flag.ts,
// now dead — the v2-vs-legacy distinction it gated no longer exists).

const STORAGE_KEY = "ba.surface_native";
const QUERY_KEY = "surface_native";

export function readNativeSurfaceFlag(): boolean {
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
