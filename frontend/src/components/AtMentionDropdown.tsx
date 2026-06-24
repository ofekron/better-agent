import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import Icon from "./Icon";
import { useTranslation } from "react-i18next";
import type { NodeSnapshot, Project, Session } from "../types";

export interface MentionItem {
  id: string;
  label: string;
  secondary: string;
  kind: "project" | "session";
  nodeId: string;
}

interface Props {
  query: string;
  triggerStart: number;
  projects: Project[];
  sessions: Session[];
  onSelect: (item: MentionItem, triggerStart: number, triggerEnd: number) => void;
  onClose: () => void;
  anchorRect?: DOMRect;
  /** Node the user is currently focused on. Items from other nodes show a badge. */
  currentNodeId?: string;
  /** Machine snapshots for resolving node_id → display name. */
  machines?: NodeSnapshot[];
}

export function buildMentionItems(
  projects: Project[],
  sessions: Session[],
): MentionItem[] {
  const items: MentionItem[] = [];
  for (const p of projects) {
    items.push({
      id: `project:${p.path}`,
      label: p.name,
      secondary: p.path,
      kind: "project",
      nodeId: p.node_id || "primary",
    });
  }
  for (const s of sessions) {
    if (!s.cwd) continue;
    items.push({
      id: `session:${s.id}`,
      label: s.name || s.id,
      secondary: s.cwd,
      kind: "session",
      nodeId: s.node_id || "primary",
    });
  }
  return items;
}

export function formatMentionInsert(item: MentionItem): string {
  return `${item.label} (${item.secondary})`;
}

export function AtMentionDropdown({
  query,
  triggerStart,
  projects,
  sessions,
  onSelect,
  onClose,
  anchorRect,
  currentNodeId = "primary",
  machines = [],
}: Props) {
  const { t } = useTranslation();
  const [selectedIndex, setSelectedIndex] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<Map<number, HTMLDivElement>>(new Map());

  const nodeNames = useMemo(() => {
    const map = new Map<string, string>();
    for (const m of machines) {
      map.set(m.id, m.id === "primary" ? "primary" : m.id);
    }
    return map;
  }, [machines]);

  const allItems = useMemo(
    () => buildMentionItems(projects, sessions),
    [projects, sessions],
  );

  const filtered = useMemo(() => {
    if (!query) return allItems.slice(0, 20);
    const q = query.toLowerCase();
    return allItems
      .filter(
        (item) =>
          item.label.toLowerCase().includes(q) ||
          item.secondary.toLowerCase().includes(q),
      )
      .slice(0, 20);
  }, [allItems, query]);

  const grouped = useMemo(() => {
    const projectItems = filtered.filter((i) => i.kind === "project");
    const sessionItems = filtered.filter((i) => i.kind === "session");
    return { projectItems, sessionItems };
  }, [filtered]);

  useEffect(() => {
    setSelectedIndex(0);
  }, [filtered.length]);

  useEffect(() => {
    const el = itemRefs.current.get(selectedIndex);
    el?.scrollIntoView({ block: "nearest" });
  }, [selectedIndex]);

  const handleSelect = useCallback(
    (index: number) => {
      const item = filtered[index];
      if (!item) return;
      const triggerEnd = triggerStart + 1 + query.length;
      onSelect(item, triggerStart, triggerEnd);
    },
    [filtered, triggerStart, query, onSelect],
  );

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (!filtered.length) return;
      switch (e.key) {
        case "ArrowDown":
          e.preventDefault();
          e.stopPropagation();
          setSelectedIndex((i) => (i + 1) % filtered.length);
          break;
        case "ArrowUp":
          e.preventDefault();
          e.stopPropagation();
          setSelectedIndex((i) => (i - 1 + filtered.length) % filtered.length);
          break;
        case "Enter":
        case "Tab":
          e.preventDefault();
          e.stopPropagation();
          handleSelect(selectedIndex);
          break;
        case "Escape":
          e.preventDefault();
          e.stopPropagation();
          onClose();
          break;
      }
    };
    document.addEventListener("keydown", handler, true);
    return () => document.removeEventListener("keydown", handler, true);
  }, [filtered, selectedIndex, handleSelect, onClose]);

  if (!filtered.length) return null;

  const style: React.CSSProperties = anchorRect
    ? {
        position: "fixed",
        bottom: window.innerHeight - anchorRect.top + 4,
        left: Math.min(anchorRect.left, window.innerWidth - 320),
        width: Math.min(anchorRect.width, 480),
        maxWidth: window.innerWidth - 16,
        maxHeight: 240,
      }
    : {
        position: "absolute",
        bottom: "100%",
        left: 0,
        right: 0,
        maxHeight: 240,
      };

  let renderIndex = 0;

  return (
    <div className="at-mention-dropdown" style={style} ref={listRef}>
      {grouped.projectItems.length > 0 && (
        <>
          <div className="at-mention-header">
            <span className="at-mention-section-label">
              {t("input.mentionProjects")}
            </span>
          </div>
          {grouped.projectItems.map((item) => {
            const idx = renderIndex++;
            return (
              <div
                key={item.id}
                ref={(el) => {
                  if (el) itemRefs.current.set(idx, el);
                  else itemRefs.current.delete(idx);
                }}
                className={`at-mention-item${idx === selectedIndex ? " selected" : ""} kind-${item.kind}`}
                onMouseDown={(e) => {
                  e.preventDefault();
                  handleSelect(idx);
                }}
                onMouseEnter={() => setSelectedIndex(idx)}
              >
                <span className="at-mention-icon"><Icon name="folder" size={14} /></span>
                <span className="at-mention-label">{item.label}</span>
                {item.nodeId !== currentNodeId && (
                  <span className="at-mention-node-badge">
                    {nodeNames.get(item.nodeId) || item.nodeId}
                  </span>
                )}
                <span className="at-mention-secondary" title={item.secondary}>
                  {item.secondary}
                </span>
              </div>
            );
          })}
        </>
      )}
      {grouped.sessionItems.length > 0 && (
        <>
          <div className="at-mention-header">
            <span className="at-mention-section-label">
              {t("input.mentionSessions")}
            </span>
          </div>
          {grouped.sessionItems.map((item) => {
            const idx = renderIndex++;
            return (
              <div
                key={item.id}
                ref={(el) => {
                  if (el) itemRefs.current.set(idx, el);
                  else itemRefs.current.delete(idx);
                }}
                className={`at-mention-item${idx === selectedIndex ? " selected" : ""} kind-${item.kind}`}
                onMouseDown={(e) => {
                  e.preventDefault();
                  handleSelect(idx);
                }}
                onMouseEnter={() => setSelectedIndex(idx)}
              >
                <span className="at-mention-icon"><Icon name="chat" size={14} /></span>
                <span className="at-mention-label">{item.label}</span>
                {item.nodeId !== currentNodeId && (
                  <span className="at-mention-node-badge">
                    {nodeNames.get(item.nodeId) || item.nodeId}
                  </span>
                )}
                <span className="at-mention-secondary" title={item.secondary}>
                  {item.secondary}
                </span>
              </div>
            );
          })}
        </>
      )}
    </div>
  );
}
