import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useResizable } from "../hooks/useResizable";

interface Props {
  /** Unique prefix for localStorage key. */
  storagePrefix: string;
  /** Initial file-viewer width (px) on first open (no localStorage value
   * yet). Defaults to 520 to match the prior behavior. Callers that
   * want a 50/50-of-available split (file-edit overlay) compute it
   * once at mount and pass it in. */
  defaultSize?: number;
  /** Top bar: badge text + action buttons. */
  badge: ReactNode;
  actions: ReactNode;
  /** Main content slots. */
  chatSlot: ReactNode;
  fileViewerSlot: ReactNode;
  fileFirst?: boolean;
  /** Bottom bar content. Omit to skip rendering the bottom bar entirely. */
  bottomBar?: ReactNode;
  /** CSS class modifier for the overlay root. */
  className?: string;
  /** data-testid for the overlay root. */
  testId?: string;
}

export function WorkingModeLayout({
  storagePrefix,
  defaultSize = 520,
  badge,
  actions,
  chatSlot,
  fileViewerSlot,
  fileFirst = false,
  bottomBar,
  className = "working-mode-overlay",
  testId,
}: Props) {
  const { t } = useTranslation();
  const fileViewer = useResizable({
    storageKey: `${storagePrefix}.fileViewerWidth`,
    defaultSize,
    min: 280,
    max: 1200,
    axis: "x",
    direction: "reverse",
  });

  return (
    <div className={className} data-testid={testId}>
      <div className="prompt-eng-topbar">
        <span className="prompt-eng-badge">{badge}</span>
        <div className="prompt-eng-topbar-spacer" />
        {actions}
      </div>

      <div className="prompt-eng-body">
        {fileFirst ? (
          <>
            <div
              className="prompt-eng-fileviewer prompt-eng-fileviewer-primary"
              style={{ width: fileViewer.size, minWidth: fileViewer.size }}
            >
              {fileViewerSlot}
            </div>
            <div
              className="prompt-eng-resizer"
              onMouseDown={fileViewer.onMouseDown}
              data-testid={`${storagePrefix}-resizer`}
              title={t("workingMode.dragToResize")}
            />
            <div className="prompt-eng-chat prompt-eng-chat-secondary">{chatSlot}</div>
          </>
        ) : (
          <>
            <div className="prompt-eng-chat">{chatSlot}</div>
            <div
              className="prompt-eng-resizer"
              onMouseDown={fileViewer.onMouseDown}
              data-testid={`${storagePrefix}-resizer`}
              title={t("workingMode.dragToResize")}
            />
            <div
              className="prompt-eng-fileviewer"
              style={{ width: fileViewer.size, minWidth: fileViewer.size }}
            >
              {fileViewerSlot}
            </div>
          </>
        )}
      </div>

      {bottomBar && (
        <div className="prompt-eng-bottombar">
          {bottomBar}
        </div>
      )}
    </div>
  );
}
