import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { OpenBrowserPanel } from "../types";
import { useViewport } from "../hooks/useViewport";

interface Props {
  /** Backend-owned ordered list of open browser panels for the session.
   * The container reflects this 1:1 — it never holds its own copy. */
  panels: OpenBrowserPanel[];
  /** Ask the backend to close a panel (App does the optimistic
   * applySessionMetadata + DELETE round-trip, same as file panels). */
  onClosePanel: (id: string) => void;
}

function panelLabel(panel: OpenBrowserPanel): string {
  if (panel.title) return panel.title;
  try {
    const u = new URL(panel.url);
    return u.host + (u.pathname !== "/" ? u.pathname : "");
  } catch {
    return panel.url;
  }
}

/** Embeds a live URL sandboxed the same way FilePanels' HTML preview
 * does — no `allow-same-origin` alongside `allow-scripts`, since the
 * URL can be agent- or user-supplied and must not be able to read/write
 * this app's own origin. */
function BrowserPanelFrame({ panel }: { panel: OpenBrowserPanel }) {
  const { t } = useTranslation();
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
  }, [panel.url]);

  return (
    <div className="browser-panels-frame-shell" data-testid="browser-panel-frame">
      <div className="browser-panels-note">{t("browserPanels.sandboxNote")}</div>
      <div className="browser-panels-address-bar" title={panel.url}>
        {panel.url}
      </div>
      {loading && <div className="browser-panels-state">{t("browserPanels.loading")}</div>}
      <iframe
        title={panelLabel(panel)}
        className="browser-panels-iframe"
        sandbox="allow-scripts allow-forms allow-popups allow-modals"
        src={panel.url}
        onLoad={() => setLoading(false)}
      />
    </div>
  );
}

/** Tabbed / split container for the session's open browser panels.
 * Mirrors FilePanels: pure projection of backend `open_browser_panels`,
 * only the active tab + split toggle are local transient UI. */
export function BrowserPanels({ panels, onClosePanel }: Props) {
  const { t } = useTranslation();
  const viewport = useViewport();
  const [activeId, setActiveId] = useState<string | null>(null);
  const previousPanelIdsRef = useRef<string[]>([]);
  const [splitDesktop, setSplitDesktop] = useState(false);
  const split = viewport.mode === "desktop" && splitDesktop;
  const setSplit = setSplitDesktop;

  useEffect(() => {
    const panelIds = panels.map((p) => p.id);
    const previousPanelIds = previousPanelIdsRef.current;
    const lastPanelId = panelIds[panelIds.length - 1] ?? null;
    const previousLastPanelId = previousPanelIds[previousPanelIds.length - 1] ?? null;
    const openedOrReordered =
      panelIds.length > previousPanelIds.length ||
      (panelIds.length === previousPanelIds.length && lastPanelId !== previousLastPanelId);
    previousPanelIdsRef.current = panelIds;
    if (panels.length === 0) {
      if (activeId !== null) setActiveId(null);
      return;
    }
    if (openedOrReordered && activeId !== lastPanelId) {
      setActiveId(lastPanelId);
      return;
    }
    if (!panels.some((p) => p.id === activeId)) {
      setActiveId(panels[panels.length - 1].id);
    }
  }, [panels, activeId]);

  const cycle = useCallback(
    (dir: 1 | -1) => {
      if (panels.length < 2) return;
      const idx = panels.findIndex((p) => p.id === activeId);
      const base = idx < 0 ? 0 : idx;
      const next = (base + dir + panels.length) % panels.length;
      setActiveId(panels[next].id);
    },
    [panels, activeId],
  );

  useEffect(() => {
    if (panels.length < 2 || split) return;
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Tab") {
        e.preventDefault();
        cycle(e.shiftKey ? -1 : 1);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [panels.length, split, cycle]);

  useMemo(() => new Set(panels.map((p) => p.id)), [panels]);

  if (panels.length === 0) return null;

  const active = panels.find((p) => p.id === activeId) ?? panels[panels.length - 1];

  return (
    <div className="browser-panels">
      <div className="browser-panels-tabs">
        {panels.length > 1 && (
          <button
            className="browser-panels-cycle"
            onClick={() => cycle(-1)}
            title={t("browserPanels.prevTab")}
          >
            ‹
          </button>
        )}
        <div className="browser-panels-tablist">
          {panels.map((p) => {
            const isActive = !split && p.id === active.id;
            return (
              <div
                key={p.id}
                className={`browser-panels-tab${isActive ? " active" : ""}`}
                title={p.url}
                onClick={() => {
                  if (split) setSplit(false);
                  setActiveId(p.id);
                }}
              >
                <span className="browser-panels-tab-name">{panelLabel(p)}</span>
                <button
                  className="browser-panels-tab-close"
                  title={t("browserPanels.closeTab")}
                  onClick={(e) => {
                    e.stopPropagation();
                    onClosePanel(p.id);
                  }}
                >
                  ×
                </button>
              </div>
            );
          })}
        </div>
        {panels.length > 1 && (
          <button
            className="browser-panels-cycle"
            onClick={() => cycle(1)}
            title={t("browserPanels.nextTab")}
          >
            ›
          </button>
        )}
        {panels.length > 1 && viewport.mode === "desktop" && (
          <button
            className={`browser-panels-split${split ? " active" : ""}`}
            onClick={() => setSplit((s) => !s)}
            title={split ? t("browserPanels.unsplit") : t("browserPanels.split")}
          >
            {split ? t("browserPanels.unsplit") : t("browserPanels.split")}
          </button>
        )}
      </div>

      <div className={`browser-panels-body${split ? " split" : ""}`}>
        {split ? (
          panels.map((p) => (
            <div key={p.id} className="browser-panels-pane">
              <BrowserPanelFrame panel={p} />
            </div>
          ))
        ) : (
          <div className="browser-panels-pane">
            <BrowserPanelFrame panel={active} />
          </div>
        )}
      </div>
    </div>
  );
}
