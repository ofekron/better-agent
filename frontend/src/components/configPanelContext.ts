import { createContext, useContext } from "react";
import type { ProviderConfigSyncApiClient } from "@better-agent/provider-config-sync-ui";
import type { OpenConfigPanel } from "../types";

export type OpenConfigPanelInput = Pick<OpenConfigPanel, "capability_id" | "scope" | "cwd">;

/** App-level handles that an inline `open_config_panel` tool widget
 *  (rendered deep inside the message tree by ToolCall) needs to (a) render
 *  the embedded configs-page editor and (b) pop itself into the right side
 *  panel. Carried via context so the callback + client don't have to be
 *  threaded through every message-rendering helper.
 *
 *  Also carries the inline "one-live-panel" registry: only the most recently
 *  mounted inline config panel stays expanded; older ones collapse to a
 *  "closed" marker. `claimInline`/`releaseInline` are called on widget
 *  mount/unmount; `activeInlineId` is the id of the currently-live one. */
export interface ConfigPanelContextValue {
  client: ProviderConfigSyncApiClient;
  subscribeExternalChanges?: (cb: () => void) => () => void;
  open: (panel: OpenConfigPanelInput) => void;
  activeInlineId: string | null;
  claimInline: (id: string) => void;
  releaseInline: (id: string) => void;
}

export const ConfigPanelContext = createContext<ConfigPanelContextValue | null>(null);

export function useConfigPanelContext(): ConfigPanelContextValue | null {
  return useContext(ConfigPanelContext);
}
