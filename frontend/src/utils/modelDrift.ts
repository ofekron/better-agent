import type { Provider } from "../types";

/**
 * True when `model` is the active/default provider's mirror value that has
 * leaked onto a session whose own provider is DIFFERENT. The global `model`
 * state doubles as a mirror of the default provider's model (shown when no
 * session is focused); on a default-provider switch it can hold the new
 * provider's default (e.g. glm-5.2). Persisting that onto a Claude-provider
 * session corrupts its model (and is now rejected by the backend). The
 * drift-detector uses this to suppress such a write.
 */
export function isLeakedProviderMirror(
  model: string,
  currentProvider: Provider | null,
  defaultProvider: Provider | null,
): boolean {
  if (!model || !currentProvider || !defaultProvider) return false;
  if (currentProvider.id === defaultProvider.id) return false;
  return (
    model === defaultProvider.default_model ||
    model === defaultProvider.last_model
  );
}
