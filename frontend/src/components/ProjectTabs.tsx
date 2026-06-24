import { useRef, useEffect, useState } from "react";
import Icon from "./Icon";
import { useTranslation } from "react-i18next";
import { useAnimatedTabMovement } from "src/hooks/useAnimatedTabMovement";
import type { Project } from "../types";
import { ProjectStatusBadge } from "./ProjectStatusBadge";

interface Props {
  projects: Project[];
  currentPath: string;
  currentNodeId: string;
  onSelect: (path: string, nodeId: string) => void;
  /** Add a new project (opens the dir picker). */
  onAdd: () => void;
  /** Remove a project. */
  onRemove: (path: string, nodeId: string) => void;
  /** Open per-project settings. */
  onOpenSettings: (path: string, nodeId: string) => void;
  /** Per-project unseen project structure updates count. */
  projectUpdatesCounts?: Record<string, number>;
  /** True while AI search is filtering across all projects. The tabs
   * are dimmed and non-interactive so the user doesn't expect them to
   * narrow the result set (they don't — AI bypasses the project
   * filter). */
  disabled?: boolean;
}

export function ProjectTabs({
  projects,
  currentPath,
  currentNodeId,
  onSelect,
  onAdd,
  onRemove,
  onOpenSettings,
  projectUpdatesCounts = {},
  disabled,
}: Props) {
  const { t } = useTranslation();
  const scrollRef = useRef<HTMLDivElement>(null);
  const movementRef = useAnimatedTabMovement<HTMLDivElement>(
    projects.map((project) => `${project.node_id || "primary"}::${project.path}`),
  );
  const activeRef = useRef<HTMLButtonElement>(null);
  // Which tab's config menu is open, and where to anchor it. `pos` is
  // viewport coordinates so the menu can render `position: fixed` and
  // escape the tabs' horizontal-scroll overflow clip.
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [menuPos, setMenuPos] = useState<{ x: number; y: number }>({ x: 0, y: 0 });

  // Scroll the active tab into view on mount and when selection changes.
  useEffect(() => {
    activeRef.current?.scrollIntoView({
      block: "nearest",
      inline: "nearest",
    });
  }, [currentPath, currentNodeId]);

  // Close the config menu on outside click or Escape.
  useEffect(() => {
    if (!menuFor) return;
    const close = () => setMenuFor(null);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenuFor(null);
    };
    window.addEventListener("mousedown", close);
    window.addEventListener("keydown", onKey);
    window.addEventListener("resize", close);
    window.addEventListener("scroll", close, true);
    return () => {
      window.removeEventListener("mousedown", close);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", close, true);
    };
  }, [menuFor]);

  const menuProject = projects.find(
    (p) => `${p.node_id || "primary"}::${p.path}` === menuFor,
  );

  return (
    <div
      className={`project-tabs${disabled ? " disabled" : ""}`}
      ref={(node) => {
        scrollRef.current = node;
        movementRef.current = node;
      }}
      aria-disabled={disabled || undefined}
    >
      {projects.map((p) => {
        const projNode = p.node_id || "primary";
        const isActive = p.path === currentPath && projNode === currentNodeId;
        const key = `${projNode}::${p.path}`;
        const label =
          p.name || p.path.replace(/\/+$/, "").split("/").pop() || p.path;
        return (
          <div
            key={key}
            data-tab-movement-key={key}
            className={`project-tab${isActive ? " active" : ""}`}
          >
            <button
              ref={isActive ? activeRef : undefined}
              type="button"
              className="project-tab-select"
              onClick={() => !disabled && onSelect(p.path, projNode)}
              disabled={disabled}
              title={
                disabled
                  ? `${p.path} — AI search bypasses the project filter`
                  : p.path
              }
            >
              <span className="project-tab-label">{label}</span>
              <span className="project-tab-status">
                <ProjectStatusBadge path={p.path} nodeId={projNode} />
                {(() => {
                  const encoded = p.path.replace(/\//g, "-").replace(/_/g, "-") || "root";
                  const count = projectUpdatesCounts[encoded];
                  return count ? (
                    <span className="project-tab-updates-badge" title={`${count} project structure updates`}>
                      {count}
                    </span>
                  ) : null;
                })()}
              </span>
            </button>
            <button
              type="button"
              className="project-tab-config"
              disabled={disabled}
              title={t("projects.settingsTitle")}
              aria-label={t("projects.settingsTitle")}
              onMouseDown={(e) => e.stopPropagation()}
              onClick={(e) => {
                e.stopPropagation();
                const r = e.currentTarget.getBoundingClientRect();
                setMenuPos({ x: r.left, y: r.bottom });
                setMenuFor((cur) => (cur === key ? null : key));
              }}
            >
              <Icon name="settings" size={16} />
            </button>
          </div>
        );
      })}

      <button
        type="button"
        className="project-tab-add"
        disabled={disabled}
        title={t("projects.addTitle")}
        aria-label={t("projects.addTitle")}
        onClick={() => !disabled && onAdd()}
      >
        +
      </button>

      {menuFor && menuProject && (
        <div
          className="project-tab-menu"
          style={{ left: menuPos.x, top: menuPos.y }}
          onMouseDown={(e) => e.stopPropagation()}
        >
          <button
            type="button"
            className="project-tab-menu-item"
            onClick={() => {
              const node = menuProject.node_id || "primary";
              setMenuFor(null);
              onOpenSettings(menuProject.path, node);
            }}
          >
            {t("projects.settingsTitle")}
          </button>
          <button
            type="button"
            className="project-tab-menu-item danger"
            onClick={() => {
              const node = menuProject.node_id || "primary";
              setMenuFor(null);
              onRemove(menuProject.path, node);
            }}
          >
            {t("projects.removeTitle")}
          </button>
        </div>
      )}
    </div>
  );
}
