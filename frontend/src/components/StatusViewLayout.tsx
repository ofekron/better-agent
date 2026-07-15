import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useResizable } from "../hooks/useResizable";

interface Props {
  /** Conversation, docked into the bottom-left panel. */
  chatSlot: ReactNode;
  /** Live status workspace filling the remaining area. */
  statusSlot: ReactNode;
  /** Per-session id used to scope the persisted panel size. */
  storageScope: string;
}

/**
 * Status view: the conversation is docked in a small bottom-left panel
 * (default ~1/4 of the viewport) and the rest of the main panel is a
 * live status workspace. Both the chat panel width (right edge) and
 * height (top edge) are user-resizable and persisted per session via
 * `useResizable`, mirroring the WorkingModeLayout / sidebar pattern.
 *
 * The layout is anchored to its own container (not `position: fixed`),
 * so it stays inside the app shell and respects the sidebar/right-panel
 * columns around it.
 */
export function StatusViewLayout({ chatSlot, statusSlot, storageScope }: Props) {
  const { t } = useTranslation();
  const chatWidth = useResizable({
    storageKey: `${storageScope}.chatW`,
    defaultSize: Math.round((typeof window !== "undefined" ? window.innerWidth : 1280) * 0.42),
    min: 300,
    max: 900,
    axis: "x",
    direction: "forward",
  });
  const chatHeight = useResizable({
    storageKey: `${storageScope}.chatH`,
    defaultSize: Math.round((typeof window !== "undefined" ? window.innerHeight : 800) * 0.45),
    min: 180,
    max: 760,
    axis: "y",
    direction: "reverse",
  });

  return (
    <div className="status-view-layout" data-testid="status-view-layout">
      <div className="status-view-workspace-scroll">{statusSlot}</div>

      <aside
        className="status-view-chat-dock"
        style={{ width: chatWidth.size, height: chatHeight.size }}
        data-testid="status-view-chat-dock"
      >
        <div
          className="status-view-resizer status-view-resizer-y"
          onMouseDown={chatHeight.onMouseDown}
          title={t("statusView.dragToResize")}
        />
        <div className="status-view-chat-body">{chatSlot}</div>
        <div
          className="status-view-resizer status-view-resizer-x"
          onMouseDown={chatWidth.onMouseDown}
          title={t("statusView.dragToResize")}
        />
      </aside>
    </div>
  );
}
