import { ProviderConfigSyncPage, type ProviderConfigSyncApiClient } from "@better-agent/provider-config-sync-ui";
import Icon from "./Icon";
import type { OpenConfigPanel } from "../types";

interface Props {
  /** Backend-owned ordered list of open config panels for the session.
   *  Pure projection — the container holds no separate copy. */
  panels: OpenConfigPanel[];
  /** Better Agent REST client for the provider-config-sync API. */
  client: ProviderConfigSyncApiClient;
  /** Subscribe to cross-tab provider-config-sync change events. */
  subscribeExternalChanges?: (cb: () => void) => () => void;
  /** Ask the backend to close a panel (App does the optimistic
   *  applySessionMetadata + DELETE round-trip, same as file panels). */
  onClosePanel: (id: string) => void;
}

/** Stacked embed of the provider-config-sync capability panels popped
 *  into the right side panel from an inline `open_config_panel` widget.
 *
 *  Pure projection of backend `open_config_panels`. Each panel reuses the
 *  same `ProviderConfigSyncPage` component as the configs page (embedded
 *  mode), pre-focused on its capability via `initialCapabilityId`. */
export function ConfigPanels({
  panels,
  client,
  subscribeExternalChanges,
  onClosePanel,
}: Props) {
  if (panels.length === 0) return null;
  return (
    <div className="config-panels">
      {panels.map((panel) => (
        <div key={panel.id} className="config-panel-host">
          <div className="config-panel-host-header">
            <span className="config-panel-host-title">
              {panel.capability_id}
              <span className="config-panel-host-scope"> · {panel.scope}</span>
            </span>
            <button
              type="button"
              className="btn-small"
              onClick={() => onClosePanel(panel.id)}
              aria-label="Close config panel"
            >
              <Icon name="x" size={16} />
            </button>
          </div>
          <div className="config-panel-host-body">
            <ProviderConfigSyncPage
              open
              embedded
              cwd={panel.scope === "project" ? panel.cwd : null}
              initialCapabilityId={panel.capability_id}
              client={client}
              onClose={() => onClosePanel(panel.id)}
              subscribeExternalChanges={subscribeExternalChanges}
            />
          </div>
        </div>
      ))}
    </div>
  );
}
