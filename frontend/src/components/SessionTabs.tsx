import { useRef, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { useAnimatedTabMovement } from "src/hooks/useAnimatedTabMovement";
import type { Provider, Session } from "../types";
import { SessionStatusBadge } from "./SessionStatusBadge";
import { sessionSortValue, timeAgo } from "../lib/sessionSort";

interface Props {
  sessions: Session[];
  providers: Provider[];
  currentSessionId?: string;
  /** Active tabs sort field — its timestamp is shown on each tab. */
  sortField: string;
  onSelect: (id: string) => void;
  onClose: (id: string) => void;
}

export function SessionTabs({
  sessions,
  providers,
  currentSessionId,
  sortField,
  onSelect,
  onClose,
}: Props) {
  const { t } = useTranslation();
  const scrollRef = useRef<HTMLDivElement>(null);
  const movementRef = useAnimatedTabMovement<HTMLDivElement>(
    sessions.map((session) => session.id),
  );
  const activeRef = useRef<HTMLButtonElement>(null);
  const prevFirstIdRef = useRef<string | null>(null);
  const prevIdsRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    activeRef.current?.scrollIntoView({
      block: "nearest",
      inline: "nearest",
    });
  }, [currentSessionId]);

  // Scroll the tabs strip back to the start when a NEW session becomes
  // the first (leftmost) tab. Fires only when the first tab changed to
  // one that wasn't open before, so reordering existing tabs never
  // yanks the scroll position.
  useEffect(() => {
    const firstId = sessions[0]?.id ?? null;
    const prevFirst = prevFirstIdRef.current;
    const prevIds = prevIdsRef.current;
    prevFirstIdRef.current = firstId;
    prevIdsRef.current = new Set(sessions.map((s) => s.id));
    if (!firstId || firstId === prevFirst) return;
    if (prevIds.has(firstId)) return;
    scrollRef.current?.scrollTo({ left: 0 });
  }, [sessions]);

  if (sessions.length === 0) return null;

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
        
        return (
          <div
            key={s.id}
            data-tab-movement-key={s.id}
            className={`session-tab-wrapper${isActive ? " active" : ""}`}
          >
            <button
              ref={isActive ? activeRef : undefined}
              type="button"
              className="session-tab"
              onClick={() => onSelect(s.id)}
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
              className="session-tab-close"
              onClick={(e) => {
                e.stopPropagation();
                onClose(s.id);
              }}
              title="Close tab"
            >
              ×
            </button>
          </div>
        );
      })}
    </div>
  );
}
