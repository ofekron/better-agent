// Runtime flag for the native Contract-Node rendering layer (Phase I
// stage 1). OFF by default — this is a new, additive render path behind
// its own opt-in, independent of `ba.surface_v2` (adapter/flag.ts), which
// gates the existing down-mapped thin-client path. Both flags are
// independently readable; `ba.surface_native` only has an effect when the
// legacy render path would otherwise be used for the chat content region
// (see components/Chat.tsx's single branch point).

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
    return localStorage.getItem(STORAGE_KEY) === "1";
  } catch {
    // Private mode / storage disabled — default OFF.
    return false;
  }
}
