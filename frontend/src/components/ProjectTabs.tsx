import { useRef, useEffect, useMemo, useState } from "react";
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
  onRemove: (path: string, nodeId: string) => void | Promise<void>;
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
  const [listModalOpen, setListModalOpen] = useState(false);
  const [selectedProjectKeys, setSelectedProjectKeys] = useState<Set<string>>(() => new Set());
  const [deleting, setDeleting] = useState(false);

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
  const projectKeys = useMemo(
    () => projects.map((project) => `${project.node_id || "primary"}::${project.path}`),
    [projects],
  );
  const selectedProjects = projects.filter((project) =>
    selectedProjectKeys.has(`${project.node_id || "primary"}::${project.path}`),
  );
  const allProjectsSelected = projects.length > 0 && selectedProjects.length === projects.length;
  const selectedCount = selectedProjects.length;

  useEffect(() => {
    if (!listModalOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !deleting) setListModalOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [deleting, listModalOpen]);

  const closeListModal = () => {
    if (deleting) return;
    setListModalOpen(false);
  };

  const setProjectSelected = (key: string, checked: boolean) => {
    setSelectedProjectKeys((current) => {
      const next = new Set(current);
      if (checked) next.add(key);
      else next.delete(key);
      return next;
    });
  };

  const setAllProjectsSelected = (checked: boolean) => {
    setSelectedProjectKeys(checked ? new Set(projectKeys) : new Set());
  };

  const deleteSelectedProjects = async () => {
    if (selectedProjects.length === 0 || deleting) return;
    setDeleting(true);
    try {
      for (const project of selectedProjects) {
        await onRemove(project.path, project.node_id || "primary");
      }
      setSelectedProjectKeys(new Set());
      setListModalOpen(false);
    } finally {
      setDeleting(false);
    }
  };

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
      <button
        type="button"
        className="project-tab-manage"
        disabled={disabled}
        title={t("projects.manageTitle")}
        aria-label={t("projects.manageTitle")}
        onClick={() => !disabled && setListModalOpen(true)}
      >
        <Icon name="sliders" size={16} />
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

      {listModalOpen && (
        <div className="modal-overlay project-list-modal-overlay" role="presentation" onMouseDown={closeListModal}>
          <div
            className="modal-content project-list-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="project-list-modal-title"
            onMouseDown={(e) => e.stopPropagation()}
          >
            <div className="modal-header">
              <h2 id="project-list-modal-title">{t("projects.manageTitle")}</h2>
              <button
                type="button"
                className="modal-close"
                aria-label={t("projects.closeManagement")}
                disabled={deleting}
                onClick={closeListModal}
              >
                ×
              </button>
            </div>
            <div className="modal-body project-list-modal-body">
              <label className="project-list-select-all">
                <input
                  type="checkbox"
                  checked={allProjectsSelected}
                  disabled={deleting || projects.length === 0}
                  onChange={(e) => setAllProjectsSelected(e.currentTarget.checked)}
                />
                <span>{t("projects.selectAll")}</span>
              </label>
              <div className="project-list-modal-list">
                {projects.map((project) => {
                  const nodeId = project.node_id || "primary";
                  const key = `${nodeId}::${project.path}`;
                  const label =
                    project.name || project.path.replace(/\/+$/, "").split("/").pop() || project.path;
                  return (
                    <label key={key} className="project-list-modal-row">
                      <input
                        type="checkbox"
                        checked={selectedProjectKeys.has(key)}
                        disabled={deleting}
                        onChange={(e) => setProjectSelected(key, e.currentTarget.checked)}
                      />
                      <span className="project-list-modal-main">
                        <span className="project-list-modal-name">{label}</span>
                        <span className="project-list-modal-path">{project.path}</span>
                      </span>
                      {nodeId !== "primary" && (
                        <span className="project-list-modal-node">{nodeId}</span>
                      )}
                    </label>
                  );
                })}
              </div>
            </div>
            <div className="modal-footer project-list-modal-footer">
              <span className="project-list-modal-count">
                {t("projects.selectedCount", { count: selectedCount })}
              </span>
              <button type="button" className="project-list-modal-secondary" disabled={deleting} onClick={closeListModal}>
                {t("projects.cancel")}
              </button>
              <button
                type="button"
                className="project-list-modal-danger"
                disabled={deleting || selectedCount === 0}
                onClick={deleteSelectedProjects}
              >
                <Icon name="trash" size={15} />
                {deleting
                  ? t("projects.deletingSelected")
                  : t("projects.deleteSelected")}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
