import { useRef, useEffect, useState, type MouseEvent } from "react";
import { useTranslation } from "react-i18next";
import { useAnimatedTabMovement } from "src/hooks/useAnimatedTabMovement";
import { scrollHorizontalItemToCenter } from "src/utils/tabScroll";
import { sessionLinkMarker } from "src/utils/linkifyFilePaths";
import type { Provider, Session } from "../types";
import { SessionStatusBadge } from "./SessionStatusBadge";
import { sessionSortValue, timeAgo } from "../lib/sessionSort";
import Icon from "./Icon";

interface Props {
  sessions: Session[];
  providers: Provider[];
  currentSessionId?: string;
  /** Active tabs sort field — its timestamp is shown on each tab. */
  sortField: string;
  onSelect: (id: string) => void;
  onClose: (id: string) => void;
  onCloseOthers: (id: string) => void;
  onToggleTopbarPin: (id: string, pinned: boolean) => void;
}

export function SessionTabs({
  sessions,
  providers,
  currentSessionId,
  sortField,
  onSelect,
  onClose,
  onCloseOthers,
  onToggleTopbarPin,
}: Props) {
  const { t } = useTranslation();
  const scrollRef = useRef<HTMLDivElement>(null);
  const movementRef = useAnimatedTabMovement<HTMLDivElement>(
    sessions.map((session) => session.id),
  );
  const activeRef = useRef<HTMLDivElement>(null);
  const [contextMenu, setContextMenu] = useState<{
    sessionId: string;
    x: number;
    y: number;
  } | null>(null);

  useEffect(() => {
    scrollHorizontalItemToCenter(scrollRef.current, activeRef.current);
  }, [currentSessionId]);

  useEffect(() => {
    if (!contextMenu) return;
    const close = () => setContextMenu(null);
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    window.addEventListener("click", close);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [contextMenu]);

  if (sessions.length === 0) return null;
  const contextSession = contextMenu
    ? sessions.find((session) => session.id === contextMenu.sessionId)
    : null;

  const openContextMenuAt = (sessionId: string, x: number, y: number) => {
    const width = 190;
    const height = 92;
    setContextMenu({
      sessionId,
      x: Math.min(x, Math.max(0, window.innerWidth - width - 8)),
      y: Math.min(y, Math.max(0, window.innerHeight - height - 8)),
    });
  };

  const openContextMenu = (e: MouseEvent, sessionId: string) => {
    e.preventDefault();
    e.stopPropagation();
    openContextMenuAt(sessionId, e.clientX, e.clientY);
  };

  const copySessionMarker = async (session: Session) => {
    await window.navigator.clipboard.writeText(
      sessionLinkMarker(session.id, session.name || "Untitled"),
    );
    setContextMenu(null);
  };

  return (
    <div
      className="session-tabs"
      ref={(node) => {
        scrollRef.current = node;
        movementRef.current = node;
      }}
    >
      {sessions.map((s) => {
        const isActive = s.id === currentSessionId;
        const projectName = s.cwd.replace(/\/+$/, "").split("/").pop() || s.cwd;
        const providerName =
          providers.find((provider) => provider.id === s.provider_id)?.name
          ?? s.provider_id
          ?? "";
        const providerModel = [providerName, s.model].filter(Boolean).join(" / ");
        const topbarPinned = Boolean(s.topbar_pinned);
        
        return (
          <div
            ref={isActive ? activeRef : undefined}
            key={s.id}
            data-tab-movement-key={s.id}
            className={`session-tab-wrapper${isActive ? " active" : ""}${topbarPinned ? " topbar-pinned" : ""}`}
            onContextMenu={(e) => openContextMenu(e, s.id)}
          >
            <button
              type="button"
              className="session-tab"
              onClick={() => onSelect(s.id)}
              onKeyDown={(e) => {
                if (e.key === "ContextMenu" || (e.shiftKey && e.key === "F10")) {
                  const rect = e.currentTarget.getBoundingClientRect();
                  e.preventDefault();
                  openContextMenuAt(s.id, rect.left + 12, rect.bottom + 4);
                }
              }}
              title={`${s.name} (${s.cwd})`}
            >
              <div className="session-tab-content">
                <span className="session-tab-text">
                  <span className="session-tab-name">
                    <span className="session-tab-name-text">{s.name || "Untitled"}</span>
                  </span>
                  <span className="session-tab-project">{projectName}</span>
                  {providerModel && (
                    <span className="session-tab-provider-model">{providerModel}</span>
                  )}
                  {timeAgo(t, sessionSortValue(s, sortField)) && (
                    <span className="session-tab-time">
                      {timeAgo(t, sessionSortValue(s, sortField))}
                    </span>
                  )}
                </span>
                <span className="session-tab-status">
                  <SessionStatusBadge sid={s.id} />
                </span>
              </div>
            </button>
            <button
              type="button"
              className={`session-tab-pin${topbarPinned ? " pinned" : ""}`}
              onClick={(e) => {
                e.stopPropagation();
                onToggleTopbarPin(s.id, !topbarPinned);
              }}
              title={topbarPinned ? t("session.unpinTopbarTitle") : t("session.pinTopbarTitle")}
              aria-label={topbarPinned ? t("session.unpinTopbarTitle") : t("session.pinTopbarTitle")}
            >
              <Icon name="pin" size={13} />
            </button>
            {!topbarPinned && (
              <button
                type="button"
                className="session-tab-close"
                onClick={(e) => {
                  e.stopPropagation();
                  onClose(s.id);
                }}
                title={t("session.closeTabTitle")}
                aria-label={t("session.closeTabTitle")}
              >
                ×
              </button>
            )}
          </div>
        );
      })}
      {contextMenu && contextSession && (
        <div
          className="session-tab-context-menu"
          role="menu"
          style={{ left: contextMenu.x, top: contextMenu.y }}
          onClick={(e) => e.stopPropagation()}
          onContextMenu={(e) => e.preventDefault()}
        >
          <button
            type="button"
            role="menuitem"
            className="session-tab-context-item"
            data-session-marker={sessionLinkMarker(
              contextSession.id,
              contextSession.name || "Untitled",
            )}
            onMouseDown={(e) => {
              e.preventDefault();
              void copySessionMarker(contextSession);
            }}
          >
            <Icon name="clipboard" size={14} />
            <span>{t("session.copyAction")}</span>
          </button>
          {sessions.length > 1 && (
            <button
              type="button"
              role="menuitem"
              className="session-tab-context-item"
              onMouseDown={(e) => {
                e.preventDefault();
                setContextMenu(null);
                onCloseOthers(contextSession.id);
              }}
            >
              <Icon name="x-circle" size={14} />
              <span>{t("session.closeOtherTabsTitle")}</span>
            </button>
          )}
        </div>
      )}
    </div>
  );
}
