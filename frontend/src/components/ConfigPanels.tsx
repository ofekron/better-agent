import Icon from "./Icon";
import type { OpenConfigPanel } from "../types";

interface Props {
  /** Backend-owned ordered list of open config panels for the session.
   *  Pure projection — the container holds no separate copy. */
  panels: OpenConfigPanel[];
  /** Ask the backend to close a panel (App does the optimistic
   *  applySessionMetadata + DELETE round-trip, same as file panels). */
  onClosePanel: (id: string) => void;
}

/** Stacked host for config panels popped into the right side panel from an
 *  inline `open_config_panel` widget. Pure projection of backend
 *  `open_config_panels`; the panel body is an extension point a config
 *  provider plugs its editor into. */
export function ConfigPanels({ panels, onClosePanel }: Props) {
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
        </div>
      ))}
    </div>
  );
}
