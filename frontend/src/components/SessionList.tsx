import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent as ReactDragEvent, type UIEvent } from "react";
import { useTranslation } from "react-i18next";
import { LayoutGroup, motion } from "framer-motion";
import type { OrchestrationMode, Provider, RequirementTag, Session, SessionFolder, SessionTag } from "../types";
import {
  API,
  createSessionFolder,
  createSessionTag,
  fetchSessionOrganization,
  updateSessionOrganization,
} from "../api";
import { SessionStatusBadge } from "./SessionStatusBadge";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";
import { useMobileActionSheet, isMobileViewport } from "./MobileActionSheet";
import type { ActionItem } from "./MobileActionSheet";
import { SessionFolderPopover } from "./SessionFolderPopover";
import { NewFolderDropPopover } from "./NewFolderDropPopover";
import { SessionTagPopover, type PopoverAnchor } from "./SessionTagPopover";
import Icon from "./Icon";
import type { SessionListFilters } from "../hooks/useSession";
import { useLocalStorage } from "../hooks/useLocalStorage";
import { eventBus } from "../lib/eventBus";
import { SESSION_SORT_LABEL, sessionSortValue, timeAgo } from "../lib/sessionSort";
import { buildFolderPathMap, sortFolders } from "../sessionFolders";

interface Props {
  sessions: Session[];
  /** Optional full session list, unfiltered by the parent's project
   * picker. When provided AND AI search is active, the filtered list
   * is computed against THIS array so AI matches from other projects
   * can surface. If omitted, falls back to `sessions`. */
  allSessions?: Session[];
  currentSessionId?: string;
  providers: Provider[];
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onRename: (id: string, name: string) => void;
  onPin: (id: string, pinned: boolean) => void;
  onArchive: (id: string, archived: boolean) => void;
  onWorkerEligible: (id: string, value: boolean) => void;
  /** Opens the session-level Details panel (monitoring state, provenance,
   * process tree). */
  onDetails: (id: string) => void;
  /** Click handler for the ⚙ badge that appears on rows whose
   * `pending_eng_session_id` is set. Reopens the engineering overlay
   * for that parent's live eng session. */
  onResumeEng?: (parentSessionId: string) => void;
  /** AI-driven search. Sends `query` to the backend; the resolved id
   * list is used as a relevance-ranked filter over the sidebar.
   * Returns `null` when the request was aborted (stale query). */
  onAiSearch?: (
    query: string,
    signal?: AbortSignal,
  ) => Promise<{
    session_ids: string[];
    reasoning: string;
    error: string | null;
  } | null>;
  /** Lets the parent disable its project-picker UI while AI search is
   * filtering across all projects. Fires whenever the AI-active flag
   * flips. */
  onAiActiveChange?: (active: boolean) => void;
  backendProjectPath?: string;
  onBackendFiltersChange?: (filters: SessionListFilters) => void;
  onUnpinOthers: (keepId: string) => void;
  /** Opens the new-session modal / flow. */
  onCreate?: () => void;
  hasMore?: boolean;
  searching?: boolean;
  loadingMore?: boolean;
  onLoadMore?: () => void;
}

function projectName(cwd?: string): string {
  if (!cwd) return "~";
  const trimmed = cwd.replace(/\/+$/, "");
  const base = trimmed.split("/").pop() || trimmed;
  return base || "~";
}

function orchestrationLabel(t: (key: string) => string, mode?: string): string {
  if (mode === "virtual") return "Virtual";
  if (mode === "native") return t("session.native");
  return t("session.manager");
}


function sessionTagIds(session: Session): string[] {
  return (session.session_tags ?? []).map((tag) => tag.id);
}

/** Requirement tags share the manual-tag filter set under a `req:` namespace
 *  so a single `selectedTagIds` array drives both. Manual tag ids are store
 *  UUIDs, so the prefix cannot collide. */
export const REQ_TAG_PREFIX = "req:";
function reqTagKey(tag: RequirementTag): string {
  return `${REQ_TAG_PREFIX}${tag.kind}:${tag.id}`;
}
type AckedSessionOrganization = {
  folder_id?: string | null;
  session_tags?: SessionTag[];
};

type SessionOrganizationAck = {
  folder_id?: string | null;
  tags?: SessionTag[];
};

type SessionSearchField = "content" | "title" | "first_prompt";
const SESSION_SEARCH_FIELDS_ALL: SessionSearchField[] = ["content", "title", "first_prompt"];
const SESSION_SEARCH_FIELDS: SessionSearchField[] = ["title", "first_prompt"];
type SessionFileEditModeFilter = "any" | "yes" | "no";
const SESSION_FILE_EDIT_MODE_FILTERS: SessionFileEditModeFilter[] = ["any", "yes", "no"];
type SessionSource = "web" | "cli" | "import";
const SESSION_SOURCES: SessionSource[] = ["web", "cli", "import"];
type SessionInitiatedBy = "user" | "tool";
const SESSION_INITIATED_BY: SessionInitiatedBy[] = ["user", "tool"];

type FolderRenderNode = {
  folder: SessionFolder;
  children: FolderRenderNode[];
  sessions: Session[];
};

function buildFolderRenderTree(
  folders: SessionFolder[],
  sessions: Session[],
): { folderRoots: FolderRenderNode[]; unfiledSessions: Session[] } {
  const nodeById = new Map<string, FolderRenderNode>();
  for (const folder of folders) {
    nodeById.set(folder.id, { folder, children: [], sessions: [] });
  }

  const unfiledSessions: Session[] = [];
  for (const session of sessions) {
    const folderId = session.folder_id ?? "";
    const node = folderId ? nodeById.get(folderId) : undefined;
    if (node) node.sessions.push(session);
    else unfiledSessions.push(session);
  }

  const allRoots: FolderRenderNode[] = [];
  for (const node of nodeById.values()) {
    const parentId = node.folder.parent_folder_id;
    const parent = parentId ? nodeById.get(parentId) : undefined;
    if (parent && parent.folder.project_id === node.folder.project_id) {
      parent.children.push(node);
    } else {
      allRoots.push(node);
    }
  }

  const sortNodes = (nodes: FolderRenderNode[]): FolderRenderNode[] => {
    nodes.sort((a, b) => sortFolders(a.folder, b.folder));
    for (const node of nodes) sortNodes(node.children);
    return nodes;
  };

  const prune = (nodes: FolderRenderNode[]): FolderRenderNode[] =>
    sortNodes(nodes)
      .map((node) => ({ ...node, children: prune(node.children) }))
      .filter((node) => node.sessions.length > 0 || node.children.length > 0);

  return { folderRoots: prune(allRoots), unfiledSessions };
}

function flattenFolderSessions(
  nodes: FolderRenderNode[],
  collapsedIds: Set<string>,
): Session[] {
  return nodes.flatMap((node) => {
    if (collapsedIds.has(node.folder.id)) return [];
    return [
      ...node.sessions,
      ...flattenFolderSessions(node.children, collapsedIds),
    ];
  });
}

/** dataTransfer MIME carrying the dragged session id when reassigning
 * folders by drag. Custom type so it can't be confused with plain text. */
const SESSION_DRAG_MIME = "application/x-better-agent-session-id";

/** True when a drag carries a session id (a folder-reassign drag). */
function isSessionDrag(e: React.DragEvent): boolean {
  return e.dataTransfer.types.includes(SESSION_DRAG_MIME);
}

interface NodeProps {
  session: Session;
  depth: number;
  /** Folder view on → rows are draggable onto folder headings. */
  dragEnabled?: boolean;
  currentSessionId?: string;
  /** Id of the row that arrow-key navigation in the search input is
   * currently parked on. Receives an extra CSS class so it's visibly
   * distinct from the active session and from plain hover. */
  highlightedSessionId?: string | null;
  childrenByParent: Map<string, Session[]>;
  copiedId: string | null;
  providers: Provider[];
  showArchived: boolean;
  /** Content-search match score (substring occurrence count in session
   * file). Null when this session matched via metadata filter only. */
  contentScore?: number | null;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onCopy: (id: string) => void;
  onRename: (id: string, name: string) => void;
  onPin: (id: string, pinned: boolean) => void;
  onUnpinOthers: (keepId: string) => void;
  /** Desktop right-click → open the floating context menu with these items. */
  onContextMenuOpen: (e: React.MouseEvent, items: ActionItem[]) => void;
  onArchive: (id: string, archived: boolean) => void;
  onWorkerEligible: (id: string, value: boolean) => void;
  onDetails: (id: string) => void;
  onResumeEng?: (parentSessionId: string) => void;
  folders: SessionFolder[];
  tags: SessionTag[];
  onMoveToFolder: (sessionId: string, folderId: string | null) => void;
  onCreateFolder: (sessionId: string, name: string) => void;
  onSetTags: (sessionId: string, tagIds: string[]) => void;
  /** Create a new project tag and assign it to the session. */
  onCreateTag: (sessionId: string, name: string) => void;
  /** Requirement-tag filter keys currently active (subset of selectedTagIds). */
  selectedReqTagKeys: Set<string>;
  onToggleReqTag: (key: string) => void;
  /** Active sidebar sort field — its timestamp is shown on each row. */
  sortField: string;
}

function SessionNode({
  session,
  depth,
  dragEnabled,
  currentSessionId,
  highlightedSessionId,
  childrenByParent,
  copiedId,
  providers,
  showArchived,
  contentScore,
  onSelect,
  onDelete,
  onCopy,
  onRename,
  onPin,
  onUnpinOthers,
  onContextMenuOpen,
  onArchive,
  onWorkerEligible,
  onDetails,
  onResumeEng,
  folders,
  tags,
  onMoveToFolder,
  onCreateFolder,
  onSetTags,
  onCreateTag,
  selectedReqTagKeys,
  onToggleReqTag,
  sortField,
}: NodeProps) {
  const { t } = useTranslation();
  const { show: showSheet } = useMobileActionSheet();
  const mode = session.orchestration_mode ?? "team";
  const isManager = mode === "team";
  const msgs = session.message_count ?? session.messages?.length ?? 0;
  const kids = childrenByParent.get(session.id) ?? [];
  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState(session.name);
  const inputRef = useRef<HTMLInputElement | null>(null);
  // Drop highlight when another session is dragged onto this row. A row
  // only accepts drops while in folder view and when it belongs to a
  // folder — dropping moves the dragged session into this row's folder.
  const [folderDropOver, setFolderDropOver] = useState(false);
  const isFolderDropTarget = dragEnabled && !!session.folder_id;
  // Deferred-rename timer: started when the mobile action sheet's
  // "Rename" item is tapped, fires `setEditing(true)` after the sheet
  // has unmounted. We hold the id so we can clear it if this row
  // unmounts in the gap (e.g. session deleted from another tab via WS,
  // sidebar collapsed, list re-ordered) — otherwise the timeout fires
  // a setState on an unmounted component.
  const renameTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Tag editor popover. Fixed-position overlay so it escapes the sidebar's
  // overflow-y container. `tagPopoverTimerRef` defers the open until the
  // mobile action sheet has unmounted (same reason as the rename timer) so
  // the popover's click-away listener doesn't immediately close it.
  const [tagPopover, setTagPopover] = useState<PopoverAnchor | null>(null);
  const [folderPopover, setFolderPopover] = useState<PopoverAnchor | null>(null);
  const menuAnchorRef = useRef<PopoverAnchor | null>(null);
  const folderPopoverTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const tagPopoverTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => {
    if (renameTimerRef.current) clearTimeout(renameTimerRef.current);
    if (folderPopoverTimerRef.current) clearTimeout(folderPopoverTimerRef.current);
    if (tagPopoverTimerRef.current) clearTimeout(tagPopoverTimerRef.current);
  }, []);

  const toggleSessionTag = (tagId: string) => {
    const current = new Set(sessionTagIds(session));
    if (current.has(tagId)) current.delete(tagId);
    else current.add(tagId);
    onSetTags(session.id, Array.from(current));
  };

  const buildSessionActions = (): ActionItem[] => {
    const copyTarget = session.file_path || session.id;
    return [
      {
        id: "pin",
        label: session.pinned ? t("session.unpinTitle") : t("session.pinTitle"),
        icon: <Icon name="pin" size={14} />,
        onClick: () => onPin(session.id, !session.pinned),
      },
      ...(session.pinned
        ? [
            {
              id: "unpin-others",
              label: t("session.unpinOthersTitle"),
              icon: <Icon name="pin" size={14} />,
              onClick: () => onUnpinOthers(session.id),
            },
          ]
        : []),
      {
        id: "folder",
        label: t("session.folder"),
        icon: <Icon name="folder" size={14} />,
        onClick: () => {
          if (folderPopoverTimerRef.current) clearTimeout(folderPopoverTimerRef.current);
          folderPopoverTimerRef.current = setTimeout(
            () => setFolderPopover(menuAnchorRef.current),
            220,
          );
        },
      },
      {
        id: "worker-eligible",
        label: session.worker_eligible
          ? t("session.workerEligibleOff")
          : t("session.workerEligibleOn"),
        icon: <Icon name="check-circle" size={14} />,
        onClick: () => onWorkerEligible(session.id, !session.worker_eligible),
      },
      ...(tags.length > 0
        ? [
            {
              id: "tags",
              label: t("session.tagsControl"),
              icon: <Icon name="tag" size={14} />,
              onClick: () => {
                // Match the rename timer: wait for the action sheet's
                // fade-out so the popover mounts after the backdrop is
                // gone, else its click-away fires on the closing sheet.
                if (tagPopoverTimerRef.current) clearTimeout(tagPopoverTimerRef.current);
                tagPopoverTimerRef.current = setTimeout(
                  () => setTagPopover(menuAnchorRef.current),
                  220,
                );
              },
            },
          ]
        : []),
      {
        id: "rename",
        label: t("session.renameTitle"),
        icon: <Icon name="edit" size={14} />,
        onClick: () => {
          setEditName(session.name);
          // Wait for the action sheet's fade-out (handleClose uses a
          // 200ms timeout before unmount) so the input mounts AFTER the
          // backdrop is gone — otherwise the autoFocus fires while the
          // sheet still covers the screen on iOS and the keyboard
          // doesn't pop reliably. Timer is tracked + cleared on
          // unmount; double-taps overwrite the prior timer.
          if (renameTimerRef.current) clearTimeout(renameTimerRef.current);
          renameTimerRef.current = setTimeout(() => setEditing(true), 220);
        },
      },
      {
        id: "copy",
        label: t("session.copyAction"),
        icon: <Icon name="clipboard" size={14} />,
        onClick: () => onCopy(copyTarget),
      },
      {
        id: "details",
        label: t("session.detailsTitle"),
        icon: <Icon name="memo" size={14} />,
        onClick: () => onDetails(session.id),
      },
      {
        id: "archive",
        label: session.archived
          ? t("session.unarchiveTitle")
          : t("session.archiveTitle"),
        icon: <Icon name="archive" size={14} />,
        onClick: () => onArchive(session.id, !session.archived),
      },
      {
        id: "delete",
        label: t("session.deleteTitle"),
        icon: <Icon name="trash" size={14} />,
        danger: true,
        onClick: () => onDelete(session.id),
      },
    ];
  };

  const openMobileMenu = (e: React.MouseEvent) => {
    e.stopPropagation();
    menuAnchorRef.current = (e.currentTarget as HTMLElement).getBoundingClientRect();
    showSheet(buildSessionActions(), session.name);
  };

  const startEdit = (e: React.MouseEvent) => {
    e.stopPropagation();
    setEditName(session.name);
    setEditing(true);
  };

  const commitEdit = () => {
    const trimmed = editName.trim();
    setEditing(false);
    if (trimmed && trimmed !== session.name) {
      onRename(session.id, trimmed);
    }
  };

  const cancelEdit = () => {
    setEditing(false);
    setEditName(session.name);
  };

  const provider = providers.find(p => p.id === session.provider_id);
  const providerName = provider?.name ?? session.provider_id?.split('/')[0] ?? 'unknown';

  const todos = session.current_todos ?? [];
  const requirementTags = session.requirement_tags ?? [];
  const manualTags = session.session_tags ?? [];
  const todoTotal = todos.length;
  const todoDone = todos.filter((td) => td.status === "completed").length;
  const todoBadge: { text: string; className: string; label: string; state: string } =
    todoTotal === 0
      ? {
          text: "?",
          className: "role-chip session-todo-badge session-todo-badge-empty",
          label: t("session.todoNoneTitle"),
          state: "empty",
        }
      : todoDone === todoTotal
        ? {
            text: "✓",
            className: "role-chip session-todo-badge session-todo-badge-done",
            label: t("session.todoAllDoneTitle"),
            state: "done",
          }
        : {
            text: `${todoDone}/${todoTotal}`,
            className: "role-chip session-todo-badge session-todo-badge-progress",
            label: t("session.todoProgressTitle", { done: todoDone, total: todoTotal }),
            state: "progress",
          };

  return (
    <>
      <motion.div
        layout
        transition={{ duration: 0.3, ease: [0.4, 0, 0.2, 1] }}
        className={`session-item ${
          session.id === currentSessionId ? "active" : ""
        } ${
          session.id === highlightedSessionId ? "highlighted" : ""
        } ${depth > 0 ? "session-item-child" : ""} ${
          folderDropOver ? "folder-drop-over" : ""
        }`}
        style={{ marginInlineStart: depth * 16 }}
        draggable={dragEnabled}
        onDragStart={(e) => {
          if (!dragEnabled) return;
          // framer-motion forwards onDrag* handlers to the DOM when
          // `draggable` is set (filterProps special-case), so `e` is in
          // practice a native React.DragEvent even though motion's types
          // still label it as a motion pointer-drag event. Cast through
          // unknown to access dataTransfer without widening the prop type.
          const ev = e as unknown as ReactDragEvent<HTMLDivElement>;
          ev.dataTransfer.setData(SESSION_DRAG_MIME, session.id);
          ev.dataTransfer.effectAllowed = "move";
        }}
        onDragOver={(e) => {
          if (!isFolderDropTarget || !isSessionDrag(e)) return;
          e.preventDefault();
          e.dataTransfer.dropEffect = "move";
          if (!folderDropOver) setFolderDropOver(true);
        }}
        onDragLeave={() => setFolderDropOver(false)}
        onDrop={(e) => {
          if (!isFolderDropTarget || !isSessionDrag(e)) return;
          e.preventDefault();
          e.stopPropagation();
          setFolderDropOver(false);
          const id = e.dataTransfer.getData(SESSION_DRAG_MIME);
          if (id && id !== session.id) onMoveToFolder(id, session.folder_id ?? null);
        }}
        onClick={() => onSelect(session.id)}
        onContextMenu={(e) => {
          // Desktop-only: mobile uses the ⋯ button + long-press. Keep
          // the native menu (don't preventDefault) and add our floating
          // toolbar alongside it, matching the message-area pattern.
          if (isMobileViewport()) return;
          const target = e.target as HTMLElement;
          if (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT") {
            return;
          }
          const items = buildSessionActions();
          if (items.length === 0) return;
          menuAnchorRef.current = (e.currentTarget as HTMLElement).getBoundingClientRect();
          onContextMenuOpen(e, items);
        }}
        data-testid="session-item"
        data-session-id={session.id}
        data-active={session.id === currentSessionId ? "true" : "false"}
      >
        <div className="session-item-name">
          {session.source === "cli" && (
            <span
              className="role-chip source-badge-cli"
              title={t("session.cliBadgeTitle")}
            >
              CLI
            </span>
          )}
          {session.offline_pending && (
            <span className="role-chip status-offline" title="Waiting to sync">
              OFFLINE
            </span>
          )}
          {session.parent_session_id && (
            <span className="role-chip source-badge-cli" title={t("session.forkBadgeTitle")}>
              FORK
            </span>
          )}
          {session.pending_eng_session_id && onResumeEng && (
            <button
              type="button"
              className="role-chip eng-resume-badge"
              title={t("session.resumeEngTitle")}
              data-testid="eng-resume-badge"
              data-parent-session-id={session.id}
              onClick={(e) => {
                e.stopPropagation();
                onResumeEng(session.id);
              }}
            >
              {t("session.resumeEngButton")}
            </button>
          )}
          {editing ? (
            <input
              ref={(el) => { inputRef.current = el; }}
              className="session-rename-input"
              value={editName}
              onChange={(e) => setEditName(e.target.value)}
              onKeyDown={(e) => {
                e.stopPropagation();
                if (e.key === "Enter") commitEdit();
                if (e.key === "Escape") cancelEdit();
              }}
              onBlur={commitEdit}
              onClick={(e) => e.stopPropagation()}
              autoFocus
            />
          ) : (
            session.name
          )}
        </div>
        <div className="session-item-meta">
          {projectName(session.cwd)} | {orchestrationLabel(t, mode)}
          {isManager && ` | ${session.worker_count ?? 0} ${t("session.workers")}`}
          {session.rearranger_enabled && " | rearranger"}
        </div>
        {requirementTags.length > 0 && (
          <div
            className="session-requirement-tags"
            onClick={(e) => e.stopPropagation()}
          >
            {requirementTags.map((tag) => {
              const key = reqTagKey(tag);
              const active = selectedReqTagKeys.has(key);
              return (
                <button
                  key={key}
                  type="button"
                  className={`role-chip session-requirement-tag session-requirement-tag-${tag.kind} ${active ? "session-requirement-tag-active" : ""}`}
                  title={`${tag.kind}: ${tag.label}`}
                  aria-pressed={active}
                  onClick={() => onToggleReqTag(key)}
                >
                  {tag.label}
                </button>
              );
            })}
          </div>
        )}
        {manualTags.length > 0 && (
          <div className="session-tags" onClick={(e) => e.stopPropagation()}>
            {manualTags.slice(0, 5).map((tag) => (
              <span key={tag.id} className="session-tag-chip" title={tag.name}>
                {tag.name}
              </span>
            ))}
            {manualTags.length > 5 && (
              <span
                className="session-tag-count"
                title={manualTags.slice(5).map((tg) => tg.name).join(", ")}
              >
                {t("session.tagsMore", { count: manualTags.length - 5 })}
              </span>
            )}
          </div>
        )}
        <div className="session-item-meta session-item-meta-row">
          <span className="session-item-meta-text">
            {providerName} | {session.model?.split("-").slice(-2).join("-")} | {msgs} {t("session.msgs")}
          </span>
          <span
            className={todoBadge.className}
            title={todoBadge.label}
            aria-label={todoBadge.label}
            role="img"
            data-testid="session-todo-badge"
            data-todo-state={todoBadge.state}
          >
            {todoBadge.text}
          </span>
          {contentScore != null && contentScore > 0 && (
            <span className="role-chip content-score-badge" title={`Found ${contentScore} time${contentScore !== 1 ? "s" : ""} in session content`}>
              {contentScore}×
            </span>
          )}
        </div>
        <div className="session-item-meta">
          {t("session.created")}{timeAgo(t, session.created_at)} | {t(SESSION_SORT_LABEL[sortField] ?? "session.modified")} {timeAgo(t, sessionSortValue(session, sortField) || session.updated_at)}
        </div>
        <div className="session-item-status">
          <SessionStatusBadge sid={session.id} />
        </div>
        <button
          className={`session-item-pin ${session.pinned ? "pinned" : ""}`}
          title={session.pinned ? t("session.unpinTitle") : t("session.pinTitle")}
          aria-label={session.pinned ? t("session.unpinTitle") : t("session.pinTitle")}
          onClick={(e) => {
            e.stopPropagation();
            onPin(session.id, !session.pinned);
          }}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M16 9V4l1 0c.55 0 1-.45 1-1s-.45-1-1-1H7c-.55 0-1 .45-1 1s.45 1 1 1l1 0v5c0 1.66-1.34 3-3 3v2h5.97v7l1 1 1-1v-7H19v-2c-1.66 0-3-1.34-3-3z"/></svg>
        </button>
        <button
          className={`session-item-worker-eligible ${session.worker_eligible ? "active" : ""}`}
          title={session.worker_eligible ? t("session.workerEligibleOff") : t("session.workerEligibleOn")}
          aria-label={session.worker_eligible ? t("session.workerEligibleOff") : t("session.workerEligibleOn")}
          onClick={(e) => {
            e.stopPropagation();
            onWorkerEligible(session.id, !session.worker_eligible);
          }}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg>
        </button>
        <div className="session-item-actions">
          <button
            className="session-item-tag-control"
            title={t("session.folder")}
            aria-label={t("session.folder")}
            onClick={(e) => {
              e.stopPropagation();
              setFolderPopover(e.currentTarget.getBoundingClientRect());
            }}
          >
            <Icon name="folder" size={12} />
          </button>
          {tags.length > 0 && (
            <button
              className="session-item-tag-control"
              title={t("session.tagsControl")}
              aria-label={t("session.tagsControl")}
              onClick={(e) => {
                e.stopPropagation();
                setTagPopover(e.currentTarget.getBoundingClientRect());
              }}
            >
              <Icon name="tag" size={12} />
            </button>
          )}
          <button
            className="session-item-rename"
            title={t("session.renameTitle")}
            aria-label={t("session.renameTitle")}
            onClick={startEdit}
          >
            <Icon name="edit" size={14} />
          </button>
          <button
            className="session-item-copy"
            title={copiedId === (session.file_path || session.id) ? t("session.copyTitle") : t("session.copyTitleNot", { id: session.id })}
            aria-label="Copy session id"
            onClick={(e) => {
              e.stopPropagation();
              onCopy(session.file_path || session.id);
            }}
          >
            {copiedId === (session.file_path || session.id) ? "\u2713" : "\u29C9"}
          </button>
          <button
            className="session-item-archive"
            title={session.archived ? t("session.unarchiveTitle") : t("session.archiveTitle")}
            aria-label={session.archived ? t("session.unarchiveTitle") : t("session.archiveTitle")}
            onClick={(e) => {
              e.stopPropagation();
              onArchive(session.id, !session.archived);
            }}
          >
            {session.archived
              ? <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>
              : <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/><path d="M10 12h4"/></svg>
            }
          </button>
          <button
            className="session-item-delete"
            title={t("session.deleteTitle")}
            aria-label={t("session.deleteTitle")}
            onClick={(e) => {
              e.stopPropagation();
              onDelete(session.id);
            }}
          >
            x
          </button>
        </div>
        <button
          className="session-item-menu"
          title={t("session.menuTitle")}
          aria-label={t("session.menuTitle")}
          onClick={openMobileMenu}
        >
          ⋯
        </button>
      </motion.div>
      {folderPopover && (
        <SessionFolderPopover
          anchor={folderPopover}
          folders={folders}
          assignedFolderId={session.folder_id ?? null}
          onSelect={(folderId) => {
            onMoveToFolder(session.id, folderId);
            setFolderPopover(null);
          }}
          onCreateFolder={(name) => onCreateFolder(session.id, name)}
          onClose={() => setFolderPopover(null)}
        />
      )}
      {tagPopover && (
        <SessionTagPopover
          anchor={tagPopover}
          tags={tags}
          assignedTagIds={new Set(sessionTagIds(session))}
          onToggle={toggleSessionTag}
          onCreateTag={(name) => onCreateTag(session.id, name)}
          onClose={() => setTagPopover(null)}
        />
      )}
      {kids.map((child) => (
        <SessionNode
          key={child.id}
          session={child}
          depth={depth + 1}
          dragEnabled={dragEnabled}
          currentSessionId={currentSessionId}
          highlightedSessionId={highlightedSessionId}
          childrenByParent={childrenByParent}
          copiedId={copiedId}
          providers={providers}
          showArchived={showArchived}
          contentScore={contentScore}
          onSelect={onSelect}
          onDelete={onDelete}
          onCopy={onCopy}
          onRename={onRename}
          onPin={onPin}
          onUnpinOthers={onUnpinOthers}
          onContextMenuOpen={onContextMenuOpen}
          onArchive={onArchive}
          onWorkerEligible={onWorkerEligible}
          onDetails={onDetails}
          onResumeEng={onResumeEng}
          folders={folders}
          tags={tags}
          onMoveToFolder={onMoveToFolder}
          onCreateFolder={onCreateFolder}
          onSetTags={onSetTags}
          onCreateTag={onCreateTag}
          selectedReqTagKeys={selectedReqTagKeys}
          onToggleReqTag={onToggleReqTag}
          sortField={sortField}
        />
      ))}
    </>
  );
}

interface FolderSectionProps {
  node: FolderRenderNode;
  depth: number;
  currentSessionId?: string;
  highlightedSessionId?: string | null;
  childrenByParent: Map<string, Session[]>;
  copiedId: string | null;
  providers: Provider[];
  showArchived: boolean;
  scoreMap: Map<string, number>;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onCopy: (id: string) => void;
  onRename: (id: string, name: string) => void;
  onPin: (id: string, pinned: boolean) => void;
  onUnpinOthers: (keepId: string) => void;
  onContextMenuOpen: (e: React.MouseEvent, items: ActionItem[]) => void;
  onArchive: (id: string, archived: boolean) => void;
  onWorkerEligible: (id: string, value: boolean) => void;
  onDetails: (id: string) => void;
  onResumeEng?: (parentSessionId: string) => void;
  folders: SessionFolder[];
  tags: SessionTag[];
  onMoveToFolder: (sessionId: string, folderId: string | null) => void;
  onCreateFolder: (sessionId: string, name: string) => void;
  onSetTags: (sessionId: string, tagIds: string[]) => void;
  onCreateTag: (sessionId: string, name: string) => void;
  selectedReqTagKeys: Set<string>;
  onToggleReqTag: (key: string) => void;
  collapsedFolderIds: Set<string>;
  onToggleFolder: (folderId: string) => void;
  sortField: string;
}

function FolderSection({
  node,
  depth,
  currentSessionId,
  highlightedSessionId,
  childrenByParent,
  copiedId,
  providers,
  showArchived,
  scoreMap,
  onSelect,
  onDelete,
  onCopy,
  onRename,
  onPin,
  onUnpinOthers,
  onContextMenuOpen,
  onArchive,
  onWorkerEligible,
  onDetails,
  onResumeEng,
  folders,
  tags,
  onMoveToFolder,
  onCreateFolder,
  onSetTags,
  onCreateTag,
  selectedReqTagKeys,
  onToggleReqTag,
  collapsedFolderIds,
  onToggleFolder,
  sortField,
}: FolderSectionProps) {
  const collapsed = collapsedFolderIds.has(node.folder.id);
  const [dragOver, setDragOver] = useState(false);
  return (
    <div className="session-folder-section" data-testid="session-folder-section">
      <button
        type="button"
        className={`session-folder-heading ${dragOver ? "drag-over" : ""}`}
        style={{ marginInlineStart: depth * 12 }}
        onClick={() => onToggleFolder(node.folder.id)}
        aria-expanded={!collapsed}
        onDragOver={(e) => {
          if (!isSessionDrag(e)) return;
          e.preventDefault();
          e.dataTransfer.dropEffect = "move";
          if (!dragOver) setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          if (!isSessionDrag(e)) return;
          e.preventDefault();
          setDragOver(false);
          const id = e.dataTransfer.getData(SESSION_DRAG_MIME);
          if (id && id !== node.folder.id) onMoveToFolder(id, node.folder.id);
        }}
      >
        <Icon
          name={collapsed ? "chevron-right" : "chevron-down"}
          size={12}
          className="session-folder-chevron"
        />
        <Icon name="folder" size={12} />
        <span>{node.folder.name}</span>
      </button>
      {!collapsed && node.sessions.map((s) => (
        <SessionNode
          key={s.id}
          session={s}
          depth={depth}
          dragEnabled
          currentSessionId={currentSessionId}
          highlightedSessionId={highlightedSessionId}
          childrenByParent={childrenByParent}
          copiedId={copiedId}
          providers={providers}
          showArchived={showArchived}
          contentScore={scoreMap.get(s.id) ?? null}
          onSelect={onSelect}
          onDelete={onDelete}
          onCopy={onCopy}
          onRename={onRename}
          onPin={onPin}
          onUnpinOthers={onUnpinOthers}
          onContextMenuOpen={onContextMenuOpen}
          onArchive={onArchive}
          onWorkerEligible={onWorkerEligible}
          onDetails={onDetails}
          onResumeEng={onResumeEng}
          folders={folders}
          tags={tags}
          onMoveToFolder={onMoveToFolder}
          onCreateFolder={onCreateFolder}
          onSetTags={onSetTags}
          onCreateTag={onCreateTag}
          selectedReqTagKeys={selectedReqTagKeys}
          onToggleReqTag={onToggleReqTag}
          sortField={sortField}
        />
      ))}
      {!collapsed && node.children.map((child) => (
        <FolderSection
          key={child.folder.id}
          node={child}
          depth={depth + 1}
          currentSessionId={currentSessionId}
          highlightedSessionId={highlightedSessionId}
          childrenByParent={childrenByParent}
          copiedId={copiedId}
          providers={providers}
          showArchived={showArchived}
          scoreMap={scoreMap}
          onSelect={onSelect}
          onDelete={onDelete}
          onCopy={onCopy}
          onRename={onRename}
          onPin={onPin}
          onUnpinOthers={onUnpinOthers}
          onContextMenuOpen={onContextMenuOpen}
          onArchive={onArchive}
          onWorkerEligible={onWorkerEligible}
          onDetails={onDetails}
          onResumeEng={onResumeEng}
          folders={folders}
          tags={tags}
          onMoveToFolder={onMoveToFolder}
          onCreateFolder={onCreateFolder}
          onSetTags={onSetTags}
          onCreateTag={onCreateTag}
          selectedReqTagKeys={selectedReqTagKeys}
          onToggleReqTag={onToggleReqTag}
          collapsedFolderIds={collapsedFolderIds}
          onToggleFolder={onToggleFolder}
          sortField={sortField}
        />
      ))}
    </div>
  );
}

export function SessionList({
  sessions,
  allSessions,
  currentSessionId,
  providers,
  onSelect,
  onDelete,
  onRename,
  onPin,
  onArchive,
  onWorkerEligible,
  onDetails,
  onResumeEng,
  onAiSearch,
  onAiActiveChange,
  backendProjectPath,
  onBackendFiltersChange,
  onUnpinOthers,
  onCreate,
  hasMore = false,
  searching = false,
  loadingMore = false,
  onLoadMore,
}: Props) {
  const { t } = useTranslation();
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const [searchFocused, setSearchFocused] = useState(false);
  const [orgPanel, setOrgPanel] = useState<"advanced" | null>(null);
  const [, setNowTick] = useState(0);
  const projectId = sessions.find((s) => s.cwd)?.cwd ?? "";
  const handleItemsScroll = useCallback(
    (event: UIEvent<HTMLDivElement>) => {
      if (!hasMore || loadingMore || !onLoadMore) return;
      const el = event.currentTarget;
      if (el.scrollHeight - el.scrollTop - el.clientHeight > 160) return;
      onLoadMore();
    },
    [hasMore, loadingMore, onLoadMore],
  );

  // Desktop right-click context menu for session rows. State is lifted
  // here (one open menu for the whole list) so right-clicking a second
  // row replaces the first instead of stacking two. Items are built per
  // row in SessionNode and passed up, so the same list drives the mobile
  // ⋯ sheet and this desktop floating menu (single source of truth).
  const [ctxMenu, setCtxMenu] = useState<{
    x: number;
    y: number;
    items: ActionItem[];
  } | null>(null);
  const openSessionContextMenu = useCallback(
    (e: React.MouseEvent, items: ActionItem[]) => {
      const menuH = items.length * 36 + 16;
      const x = Math.max(8, Math.min(e.clientX - 10, window.innerWidth - 200 - 8));
      const y = Math.max(8, e.clientY - menuH - 4);
      setCtxMenu({ x, y, items });
    },
    [],
  );
  useEffect(() => {
    if (!ctxMenu) return;
    const close = () => setCtxMenu(null);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    // Defer attaching so the opening contextmenu event cycle can't
    // immediately close the menu it just opened.
    const timer = setTimeout(() => {
      document.addEventListener("click", close);
      document.addEventListener("keydown", onKey);
    }, 0);
    return () => {
      clearTimeout(timer);
      document.removeEventListener("click", close);
      document.removeEventListener("keydown", onKey);
    };
  }, [ctxMenu]);
  const [folders, setFolders] = useState<SessionFolder[]>([]);
  const [tags, setTags] = useState<SessionTag[]>([]);
  const [ackedOrganizationBySession, setAckedOrganizationBySession] = useState<
    Record<string, AckedSessionOrganization>
  >({});
  const [selectedFolderId, setSelectedFolderId] = useState("");
  const [selectedTagIds, setSelectedTagIds] = useState<string[]>([]);
  const [selectedProviderIds, setSelectedProviderIds] = useState<string[]>([]);
  const [selectedModelIds, setSelectedModelIds] = useState<string[]>([]);
  const [selectedModes, setSelectedModes] = useState<OrchestrationMode[]>([]);
  const [selectedSources, setSelectedSources] = useState<SessionSource[]>([]);
  const [selectedInitiatedBy, setSelectedInitiatedBy] = useState<SessionInitiatedBy[]>([]);
  const [fileEditModeFilter, setFileEditModeFilter] = useState<SessionFileEditModeFilter>("any");
  const [selectedSearchFields, setSelectedSearchFields] = useState<SessionSearchField[]>(SESSION_SEARCH_FIELDS);
  const [orgError, setOrgError] = useState<string | null>(null);
  // Folder view: group sessions into folders (on) vs flat list (off).
  // Persistent backend pref (`folder_view_enabled`) is the source of truth;
  // this state is its reflection. `undefined` until the pref loads — until
  // then no `folder_view` param is sent, so the backend sorts by the pref
  // (correct from the first fetch, no race). Toggling PATCHes the pref and
  // flips the backend sort via `backendFilters`.
  const [folderViewEnabled, setFolderViewEnabled] = useState<boolean | undefined>(undefined);
  useEffect(() => {
    let cancelled = false;
    fetch(`${API}/api/user-prefs`, { credentials: "include" })
      .then((r) => r.json())
      .then((data: { folder_view_enabled?: unknown }) => {
        if (cancelled) return;
        if (typeof data.folder_view_enabled === "boolean") {
          setFolderViewEnabled(data.folder_view_enabled);
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);
  // Session sort: by last modification (`updated_at`) vs last user
  // prompt (`last_user_prompt_at`). Backend pref is the source of truth;
  // `undefined` until loaded so the first fetch uses the backend default.
  const [sessionSort, setSessionSort] = useState<string | undefined>(undefined);
  useEffect(() => {
    let cancelled = false;
    const applySort = (v: unknown) => {
      if (!cancelled && typeof v === "string") setSessionSort(v);
    };
    fetch(`${API}/api/user-prefs`, { credentials: "include" })
      .then((r) => r.json())
      .then((data: { session_sort?: unknown }) => applySort(data.session_sort))
      .catch(() => {});
    // Keep the list's sort pref live when changed from another tab.
    const off = eventBus.subscribe(
      "user_prefs_changed",
      (p) => applySort((p as { session_sort?: unknown }).session_sort),
    );
    return () => {
      cancelled = true;
      off();
    };
  }, []);
  const changeSessionSort = useCallback(async (next: string) => {
    setSessionSort((prev) => {
      void (async () => {
        try {
          const res = await fetch(`${API}/api/user-prefs`, {
            method: "PATCH",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ session_sort: next }),
          });
          if (!res.ok) throw new Error("session_sort patch failed");
        } catch {
          setSessionSort(prev); // revert — pref is the authority
        }
      })();
      return next; // optimistic → backendFilters change → refetch
    });
  }, []);

  const toggleFolderView = useCallback(async () => {
    const next = !folderViewEnabled;
    setFolderViewEnabled(next); // optimistic → backendFilters change → refetch
    try {
      const res = await fetch(`${API}/api/user-prefs`, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder_view_enabled: next }),
      });
      if (!res.ok) throw new Error("folder_view_enabled patch failed");
    } catch {
      setFolderViewEnabled(!next); // revert — pref is the authority
    }
  }, [folderViewEnabled]);

  // ── AI search state (transient UI per CLAUDE.md rule 3). No
  // localStorage, no backend persistence — discarded on unmount and
  // on Clear. `aiResult.session_ids` is the relevance-ranked id list
  // returned by the backend; `filtered` intersects with it to drive
  // the sidebar list. There is NO separate AI-mode input: the ✨ button
  // runs an AI search over whatever is already typed in the single
  // filter box (`search`).
  const [aiLoading, setAiLoading] = useState(false);
  const [aiResult, setAiResult] = useState<{
    ids: string[];
    reasoning: string;
  } | null>(null);
  const [aiError, setAiError] = useState<string | null>(null);
  // Modal toggle for the "View full reasoning" affordance. Pure UI
  // toggle — closes on backdrop click / ESC / × button. Transient.
  const [aiDetailsOpen, setAiDetailsOpen] = useState(false);
  useBackButtonDismiss(aiDetailsOpen, () => setAiDetailsOpen(false));
  // Latest-wins on the frontend too — a new submit aborts the prior
  // fetch so its stale response can't overwrite the new state.
  const aiAbortRef = useRef<AbortController | null>(null);

  // Keyboard navigation: which session id is currently highlighted in
  // the rendered list. Driven by ArrowUp/ArrowDown when either search
  // input is focused; Enter activates `onSelect` on it. Transient UI.
  const [highlightedSessionId, setHighlightedSessionId] = useState<string | null>(null);
  // Transient drag-over highlight for the "Unfiled" drop target (drag a
  // session row out of a folder back to unfiled).
  const [unfiledDragOver, setUnfiledDragOver] = useState(false);
  // True while a session row is being dragged in folder view — gates the
  // "New folder" drop target that lets a drag create a folder on the fly.
  const [isDraggingSession, setIsDraggingSession] = useState(false);
  const [newFolderDragOver, setNewFolderDragOver] = useState(false);
  // A session dropped onto the "New folder" target opens this naming
  // popover (window.prompt is unsupported in the pywebview shell).
  const [newFolderDrop, setNewFolderDrop] = useState<{
    sessionId: string;
    anchor: PopoverAnchor;
  } | null>(null);

  const refreshOrganization = useCallback(async () => {
    if (!projectId) {
      setFolders([]);
      setTags([]);
      return;
    }
    try {
      const snapshot = await fetchSessionOrganization(projectId);
      setFolders(
        [...snapshot.folders].sort(sortFolders),
      );
      setTags([...snapshot.tags].sort((a, b) => a.name.localeCompare(b.name)));
      setOrgError(null);
    } catch (err) {
      setOrgError(err instanceof Error ? err.message : "Failed to load organization");
    }
  }, [projectId]);

  useEffect(() => {
    void refreshOrganization();
  }, [refreshOrganization]);

  useEffect(() => {
    setAckedOrganizationBySession((current) => {
      let changed = false;
      const next = { ...current };
      for (const session of sessions) {
        const acked = next[session.id];
        if (!acked) continue;
        const folderMatches =
          acked.folder_id === undefined ||
          (session.folder_id ?? null) === (acked.folder_id ?? null);
        const tagIds = acked.session_tags?.map((tag) => tag.id);
        const tagsMatch =
          tagIds === undefined ||
          tagIds.join("\u0000") === sessionTagIds(session).join("\u0000");
        if (folderMatches && tagsMatch) {
          delete next[session.id];
          changed = true;
        }
      }
      return changed ? next : current;
    });
  }, [sessions]);

  // Distinct requirement tags across all sessions, for the tag filter panel.
  const requirementTagOptions = useMemo(() => {
    const seen = new Map<string, RequirementTag>();
    for (const s of sessions) {
      for (const tag of s.requirement_tags ?? []) {
        seen.set(reqTagKey(tag), tag);
      }
    }
    return Array.from(seen.values());
  }, [sessions]);

  const selectedReqTagKeys = useMemo(
    () => new Set(selectedTagIds.filter((id) => id.startsWith(REQ_TAG_PREFIX))),
    [selectedTagIds],
  );
  const toggleTagFilter = useCallback((id: string) => {
    setSelectedTagIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    );
  }, []);
  const toggleProviderFilter = useCallback((id: string) => {
    setSelectedProviderIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    );
  }, []);
  const toggleModelFilter = useCallback((id: string) => {
    setSelectedModelIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    );
  }, []);
  const toggleModeFilter = useCallback((id: OrchestrationMode) => {
    setSelectedModes((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    );
  }, []);
  const toggleSourceFilter = useCallback((id: SessionSource) => {
    setSelectedSources((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    );
  }, []);
  const toggleInitiatedByFilter = useCallback((id: SessionInitiatedBy) => {
    setSelectedInitiatedBy((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    );
  }, []);
  const toggleSearchField = useCallback((field: SessionSearchField) => {
    setSelectedSearchFields((prev) =>
      prev.includes(field) ? prev.filter((x) => x !== field) : [...prev, field],
    );
  }, []);
  const clearAdvancedFilters = useCallback(() => {
    setShowArchived(false);
    setSelectedFolderId("");
    setSelectedTagIds([]);
    setSelectedProviderIds([]);
    setSelectedModelIds([]);
    setSelectedModes([]);
    setSelectedSources([]);
    setSelectedInitiatedBy([]);
    setFileEditModeFilter("any");
    setSelectedSearchFields(SESSION_SEARCH_FIELDS);
  }, []);

  useEffect(() => {
    const validFolders = new Set(folders.map((folder) => folder.id));
    if (selectedFolderId && !validFolders.has(selectedFolderId)) {
      setSelectedFolderId("");
    }
    const validTagIds = new Set<string>([
      ...tags.map((tag) => tag.id),
      ...requirementTagOptions.map(reqTagKey),
    ]);
    setSelectedTagIds((prev) => prev.filter((id) => validTagIds.has(id)));
  }, [folders, tags, selectedFolderId, requirementTagOptions]);

  const providerOptions = useMemo(
    () => {
      const providerById = new Map(providers.map((provider) => [provider.id, provider]));
      return Array.from(
        new Set(sessions.map((session) => session.provider_id).filter((id): id is string => !!id)),
      )
        .map((id) => ({
          id,
          name: providerById.get(id)?.name ?? id.split("/")[0] ?? id,
        }))
        .sort((a, b) => a.name.localeCompare(b.name));
    },
    [providers, sessions],
  );
  const modelOptions = useMemo(
    () =>
      Array.from(
        new Set(sessions.map((session) => session.model).filter((model): model is string => !!model)),
      ).sort((a, b) => a.localeCompare(b)),
    [sessions],
  );
  const modeOptions = useMemo(() => {
    const present = new Set(sessions.map((session) => session.orchestration_mode ?? "team"));
    return (["team", "native", "virtual"] as OrchestrationMode[]).filter((mode) => present.has(mode));
  }, [sessions]);
  const sourceOptions = useMemo(() => {
    const present = new Set(
      sessions.map((session) => (session.source ?? "web") as SessionSource),
    );
    return SESSION_SOURCES.filter((src) => present.has(src));
  }, [sessions]);
  const activeSources = useMemo(() => {
    const valid = new Set(sourceOptions);
    return selectedSources.filter((id) => valid.has(id));
  }, [sourceOptions, selectedSources]);
  const initiatedByOptions = useMemo(() => {
    const present = new Set(
      sessions.map((session) => (session.initiated_by ?? "user") as SessionInitiatedBy),
    );
    return SESSION_INITIATED_BY.filter((v) => present.has(v));
  }, [sessions]);
  const activeInitiatedBy = useMemo(() => {
    const valid = new Set(initiatedByOptions);
    return selectedInitiatedBy.filter((id) => valid.has(id));
  }, [initiatedByOptions, selectedInitiatedBy]);
  const folderPathById = useMemo(() => buildFolderPathMap(folders), [folders]);

  const activeProviderIds = useMemo(() => {
    const valid = new Set(providerOptions.map((provider) => provider.id));
    return selectedProviderIds.filter((id) => valid.has(id));
  }, [providerOptions, selectedProviderIds]);
  const activeModelIds = useMemo(() => {
    const valid = new Set(modelOptions);
    return selectedModelIds.filter((id) => valid.has(id));
  }, [modelOptions, selectedModelIds]);
  const activeModes = useMemo(() => {
    const valid = new Set(modeOptions);
    return selectedModes.filter((id) => valid.has(id));
  }, [modeOptions, selectedModes]);
  const advancedFilterActive =
    showArchived ||
    selectedSearchFields.length !== SESSION_SEARCH_FIELDS.length ||
    !SESSION_SEARCH_FIELDS.every((f) => selectedSearchFields.includes(f)) ||
    selectedFolderId !== "" ||
    selectedTagIds.length > 0 ||
    activeProviderIds.length > 0 ||
    activeModelIds.length > 0 ||
    activeModes.length > 0 ||
    activeSources.length > 0 ||
    activeInitiatedBy.length > 0 ||
    fileEditModeFilter !== "any";
  const searchQueryActive = Boolean(search.trim());
  const searchStatusLoading = searching && searchQueryActive;
  const searchExpanded = Boolean(search || searchFocused);

  const backendFilters = useMemo<SessionListFilters>(
    () => ({
      projectPath: aiResult ? "" : backendProjectPath || "",
      search,
      searchFields: selectedSearchFields,
      showArchived,
      folderId: selectedFolderId,
      folderView: folderViewEnabled,
      sortBy: sessionSort,
      tagIds: selectedTagIds,
      providerIds: activeProviderIds,
      modelIds: activeModelIds,
      modes: activeModes,
      sources: activeSources,
      initiatedBy: activeInitiatedBy,
      fileEditMode: fileEditModeFilter,
    }),
    [
      activeModelIds,
      activeModes,
      activeProviderIds,
      activeSources,
      activeInitiatedBy,
      aiResult,
      backendProjectPath,
      fileEditModeFilter,
      folderViewEnabled,
      sessionSort,
      search,
      selectedSearchFields,
      selectedFolderId,
      selectedTagIds,
      showArchived,
    ],
  );

  useEffect(() => {
    onBackendFiltersChange?.(backendFilters);
  }, [backendFilters, onBackendFiltersChange]);

  const applyAckedOrganization = (
    sessionId: string,
    organization: SessionOrganizationAck,
  ) => {
    setAckedOrganizationBySession((current) => ({
      ...current,
      [sessionId]: {
        ...current[sessionId],
        ...(organization.folder_id !== undefined
          ? { folder_id: organization.folder_id }
          : {}),
        ...(organization.tags !== undefined
          ? { session_tags: organization.tags }
          : {}),
      },
    }));
  };

  const moveToFolder = async (sessionId: string, folderId: string | null) => {
    try {
      const result = await updateSessionOrganization(sessionId, { folder_id: folderId });
      applyAckedOrganization(sessionId, result.organization);
    } catch (err) {
      setOrgError(err instanceof Error ? err.message : "Failed to move session");
    }
  };

  const createAndAssignFolder = async (sessionId: string, name: string) => {
    const trimmed = name.trim();
    if (!trimmed || !projectId) return;
    try {
      const folder = await createSessionFolder(projectId, trimmed);
      const result = await updateSessionOrganization(sessionId, { folder_id: folder.id });
      applyAckedOrganization(sessionId, result.organization);
      await refreshOrganization();
    } catch (err) {
      setOrgError(err instanceof Error ? err.message : "Failed to create folder");
    }
  };

  const setSessionTags = async (sessionId: string, tagIds: string[]) => {
    try {
      const result = await updateSessionOrganization(sessionId, { tag_ids: tagIds });
      applyAckedOrganization(sessionId, result.organization);
    } catch (err) {
      setOrgError(err instanceof Error ? err.message : "Failed to update tags");
    }
  };

  // Create a project tag and assign it to the session in one step (the
  // inline "Create" affordance in the tag popover). The WS broadcast +
  // refreshOrganization bring both the tag pool and the session's tags
  // back in sync; nothing is held as frontend state.
  const createAndAssignTag = async (sessionId: string, name: string) => {
    const trimmed = name.trim();
    if (!trimmed || !projectId) return;
    try {
      const tag = await createSessionTag(trimmed, projectId);
      const result = await updateSessionOrganization(sessionId, { add_tag_ids: [tag.id] });
      applyAckedOrganization(sessionId, result.organization);
      await refreshOrganization();
    } catch (err) {
      setOrgError(err instanceof Error ? err.message : "Failed to create tag");
    }
  };

  // Run an AI search over the current filter text. Triggered by the
  // ✨ button (no mode toggle). No-op when the box is empty.
  const runAiSearch = async () => {
    if (!onAiSearch) return;
    const q = search.trim();
    if (!q) return;
    aiAbortRef.current?.abort();
    const ctrl = new AbortController();
    aiAbortRef.current = ctrl;
    setAiLoading(true);
    setAiError(null);
    const result = await onAiSearch(q, ctrl.signal);
    if (ctrl.signal.aborted || result === null) return;
    setAiLoading(false);
    if (result.error) {
      setAiError(result.error);
      setAiResult({ ids: [], reasoning: result.reasoning });
      return;
    }
    setAiResult({ ids: result.session_ids, reasoning: result.reasoning });
  };

  // Drop the AI result and revert to live substring filtering on the
  // current `search` text. Does NOT clear the filter box — that's the
  // input's own × affordance.
  const clearAiSearch = () => {
    aiAbortRef.current?.abort();
    aiAbortRef.current = null;
    setAiResult(null);
    setAiError(null);
    setAiLoading(false);
    setAiDetailsOpen(false);
  };

  // Close the details modal on ESC. Bind only while it's open so the
  // listener doesn't leak across renders.
  useEffect(() => {
    if (!aiDetailsOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        setAiDetailsOpen(false);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [aiDetailsOpen]);

  // Tick every 30s so relative timestamps ("5m ago") stay fresh even
  // when nothing else in the session list updates.
  useEffect(() => {
    const id = window.setInterval(() => setNowTick((n) => n + 1), 30_000);
    return () => window.clearInterval(id);
  }, []);

  const { filtered, scoreMap } = useMemo(() => {
    const source = aiResult && allSessions ? allSessions : sessions;
    const base = source.map((session) => {
      const acked = ackedOrganizationBySession[session.id];
      return acked ? { ...session, ...acked } : session;
    });
    const pool = base;
    if (aiResult) {
      const order = new Map<string, number>();
      aiResult.ids.forEach((id, i) => order.set(id, i));
      const aiFiltered = pool
        .filter((s) => order.has(s.id))
        .sort((a, b) => (order.get(a.id) ?? 0) - (order.get(b.id) ?? 0));
      return { filtered: aiFiltered, scoreMap: new Map<string, number>() };
    }
    return {
      filtered: pool,
      scoreMap: new Map(
        pool
          .map((s): [string, number] => [s.id, Number(s.search_score) || 0])
          .filter(([, score]) => score > 0),
      ),
    };
  }, [
    sessions,
    allSessions,
    aiResult,
    ackedOrganizationBySession,
  ]);

  // Emit AI-active changes so the parent can disable its project
  // picker UI (which is now bypassed by the filter logic above).
  useEffect(() => {
    onAiActiveChange?.(!!aiResult);
  }, [aiResult, onAiActiveChange]);

  const { roots, childrenByParent } = useMemo(() => {
    const byId = new Map<string, Session>();
    for (const s of filtered) byId.set(s.id, s);
    const childMap = new Map<string, Session[]>();
    const rootList: Session[] = [];
    for (const s of filtered) {
      const pid = s.parent_session_id;
      if (pid && byId.has(pid)) {
        const arr = childMap.get(pid) ?? [];
        arr.push(s);
        childMap.set(pid, arr);
      } else {
        rootList.push(s);
      }
    }
    return { roots: rootList, childrenByParent: childMap };
  }, [filtered]);

  const [collapsedFolders, setCollapsedFolders] = useLocalStorage<string[]>(
    "better-agent-collapsed-folders",
    [],
  );
  const folderIdSet = useMemo(() => new Set(folders.map((f) => f.id)), [folders]);
  // Drop stale ids from deleted folders so storage stays bounded and a
  // recycled id can't accidentally hide a new folder.
  const collapsedFolderIds = useMemo(
    () => new Set(collapsedFolders.filter((id) => folderIdSet.has(id))),
    [collapsedFolders, folderIdSet],
  );
  const toggleFolder = useCallback(
    (folderId: string) => {
      setCollapsedFolders((prev) =>
        prev.includes(folderId)
          ? prev.filter((id) => id !== folderId)
          : [...prev, folderId],
      );
    },
    [setCollapsedFolders],
  );
  const { folderRoots, unfiledSessions } = useMemo(
    () => buildFolderRenderTree(folders, roots),
    [folders, roots],
  );
  const sortedRoots = useMemo(
    () => [
      ...flattenFolderSessions(folderRoots, collapsedFolderIds),
      ...unfiledSessions,
    ],
    [folderRoots, collapsedFolderIds, unfiledSessions],
  );

  // Keep the highlight valid: clear it if its row got filtered out, but
  // do NOT auto-set it — the highlight only appears after the first
  // ArrowDown/Up key press.
  useEffect(() => {
    if (sortedRoots.length === 0) {
      setHighlightedSessionId(null);
      return;
    }
    if (
      highlightedSessionId &&
      !sortedRoots.some((s) => s.id === highlightedSessionId)
    ) {
      setHighlightedSessionId(null);
    }
  }, [sortedRoots, highlightedSessionId]);

  // Keep the highlighted row in view as ArrowUp/Down moves through
  // the list. Pure cosmetic — DOM lookup by data-session-id is the
  // contract SessionNode already sets.
  useEffect(() => {
    if (!highlightedSessionId) return;
    const el = document.querySelector(
      `[data-session-id="${CSS.escape(highlightedSessionId)}"]`
    );
    if (el && "scrollIntoView" in el) {
      (el as HTMLElement).scrollIntoView({ block: "nearest" });
    }
  }, [highlightedSessionId]);

  // Shared arrow-key handler for both search inputs. Wires up the
  // command-palette pattern: ↑/↓ move the highlight through
  // `sortedRoots`; Enter activates `onSelect`. In AI mode WITHOUT a
  // returned result we let Enter fall through to the search submit;
  // once a result exists Enter selects the highlighted match.
  const handleSearchKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (sortedRoots.length === 0) return;
      e.preventDefault();
      const idx = highlightedSessionId
        ? sortedRoots.findIndex((s) => s.id === highlightedSessionId)
        : -1;
      const delta = e.key === "ArrowDown" ? 1 : -1;
      const nextIdx =
        idx < 0
          ? e.key === "ArrowDown"
            ? 0
            : sortedRoots.length - 1
          : (idx + delta + sortedRoots.length) % sortedRoots.length;
      setHighlightedSessionId(sortedRoots[nextIdx].id);
      return;
    }
    if (e.key === "Enter") {
      // Enter selects the highlighted row (command-palette pattern).
      // AI search is run explicitly via the ✨ button, not Enter.
      if (!highlightedSessionId) return;
      e.preventDefault();
      onSelect(highlightedSessionId);
      return;
    }
  };

  const copyId = async (id: string) => {
    try {
      await navigator.clipboard.writeText(id);
    } catch {
      // Fallback for insecure contexts / older browsers.
      const ta = document.createElement("textarea");
      ta.value = id;
      ta.style.position = "fixed";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
      } catch {
        // ignore
      }
      document.body.removeChild(ta);
    }
    setCopiedId(id);
    window.setTimeout(() => {
      setCopiedId((prev) => (prev === id ? null : prev));
    }, 1200);
  };

  // Folders render unless explicitly disabled. `undefined` (pref not yet
  // loaded) defaults to showing folders, matching the backend pref default.
  const showFolders = folderViewEnabled !== false;

  return (
    <div className="session-list" data-testid="session-list">
      <div className="session-list-header">
        <span>{t("session.header")}</span>
        <div className="session-list-toolbar">
          <div className={`session-search${searchExpanded ? " expanded" : ""}`}>
            <div className="session-search-input-wrap">
              <Icon name="search" size={13} className="session-search-icon" />
              <input
                type="text"
                placeholder={search || searchFocused ? t("session.searchPlaceholder") : ""}
                value={search}
                onFocus={() => setSearchFocused(true)}
                onBlur={() => setSearchFocused(false)}
                onChange={(e) => {
                  setSearch(e.target.value);
                  // Editing the box reverts to live substring filtering —
                  // the AI result is stale the moment the query changes.
                  if (aiResult || aiError) clearAiSearch();
                }}
                onKeyDown={(e) => {
                  if (e.key === "Escape" && search) {
                    e.preventDefault();
                    setSearch("");
                    clearAiSearch();
                    return;
                  }
                  handleSearchKeyDown(e);
                }}
                aria-label={t("session.searchPlaceholder")}
              />
              {search && (
                <button
                  type="button"
                  className="session-search-clear-input"
                  onClick={() => {
                    setSearch("");
                    clearAiSearch();
                  }}
                  title={t("session.clearInput")}
                  aria-label={t("session.clearInput")}
                  tabIndex={-1}
                >
                  ×
                </button>
              )}
            </div>
            {onAiSearch && searchExpanded && (
              <button
                className={`btn-small ai-search-toggle ${aiResult ? "active" : ""}`}
                title={t("session.aiSearchRun")}
                onClick={() => void runAiSearch()}
                disabled={aiLoading || !search.trim()}
              >
                {aiLoading ? (
                  <span className="ai-search-toggle-spinner">…</span>
                ) : (
                  <>
                    <span className="ai-search-toggle-icon"><Icon name="sparkles" size={12} /></span>
                    <span className="ai-search-toggle-label">AI</span>
                  </>
                )}
              </button>
            )}
            <button
              className={`btn-small session-org-toggle ${showFolders ? "active" : ""}`}
              title={showFolders ? t("session.folderViewOn") : t("session.folderViewOff")}
              aria-label={showFolders ? t("session.folderViewOn") : t("session.folderViewOff")}
              aria-pressed={showFolders}
              onClick={() => void toggleFolderView()}
            >
              <Icon name="folder" size={13} />
            </button>
            <button
              className={`btn-small session-org-toggle ${orgPanel === "advanced" || advancedFilterActive ? "active" : ""}`}
              title={t("session.advancedFilterPanel")}
              aria-label={t("session.advancedFilterPanel")}
              onClick={() => setOrgPanel((current) => current === "advanced" ? null : "advanced")}
            >
              <Icon name="sliders" size={13} />
            </button>
          </div>
          {onCreate && (
            <button className="btn-small session-new-button" onClick={onCreate}>
              {t("session.newButton")}
            </button>
          )}
        </div>
      </div>
      {orgPanel === "advanced" && (
        <div className="session-org-bar session-advanced-filter-bar">
          <div className="session-filter-group">
            <div className="session-filter-label">{t("session.sortBy")}</div>
            <div className="session-tag-filter">
              <button
                type="button"
                className={`session-tag-toggle ${(sessionSort ?? "updated_at") === "updated_at" ? "active" : ""}`}
                aria-pressed={(sessionSort ?? "updated_at") === "updated_at"}
                onClick={() => void changeSessionSort("updated_at")}
              >
                {t("session.sortByModified")}
              </button>
              <button
                type="button"
                className={`session-tag-toggle ${sessionSort === "last_user_prompt_at" ? "active" : ""}`}
                aria-pressed={sessionSort === "last_user_prompt_at"}
                onClick={() => void changeSessionSort("last_user_prompt_at")}
              >
                {t("session.sortByUserPrompt")}
              </button>
              <button
                type="button"
                className={`session-tag-toggle ${sessionSort === "last_opened_at" ? "active" : ""}`}
                aria-pressed={sessionSort === "last_opened_at"}
                onClick={() => void changeSessionSort("last_opened_at")}
              >
                {t("session.sortByOpened")}
              </button>
            </div>
          </div>
          <div className="session-filter-group">
            <div className="session-filter-label">{t("session.showArchived")}</div>
            <div className="session-tag-filter">
              <button
                type="button"
                className={`session-tag-toggle ${showArchived ? "active" : ""}`}
                aria-pressed={showArchived}
                onClick={() => setShowArchived((v) => !v)}
              >
                {showArchived ? t("session.hideArchived") : t("session.showArchived")}
              </button>
            </div>
          </div>
          <div className="session-filter-group">
            <div className="session-filter-label">{t("session.searchIn")}</div>
            <div className="session-tag-filter session-search-field-filter">
              {SESSION_SEARCH_FIELDS_ALL.map((field) => {
                const active = selectedSearchFields.includes(field);
                return (
                  <label key={field} className={`session-search-field-toggle ${active ? "active" : ""}`}>
                    <input
                      type="checkbox"
                      checked={active}
                      onChange={() => toggleSearchField(field)}
                    />
                    <span>{t(`session.searchField.${field}`)}</span>
                  </label>
                );
              })}
            </div>
          </div>
          {folders.length > 0 && (
            <div className="session-filter-group">
              <div className="session-filter-label">{t("session.folder")}</div>
              <div className="session-tag-filter">
                <button
                  type="button"
                  className={`session-tag-toggle ${selectedFolderId === "" ? "active" : ""}`}
                  aria-pressed={selectedFolderId === ""}
                  onClick={() => setSelectedFolderId("")}
                >
                  {t("session.allFolders")}
                </button>
                {folders.map((folder) => {
                  const active = selectedFolderId === folder.id;
                  return (
                    <button
                      key={folder.id}
                      type="button"
                      className={`session-tag-toggle ${active ? "active" : ""}`}
                      aria-pressed={active}
                      onClick={() => setSelectedFolderId(active ? "" : folder.id)}
                    >
                      {folderPathById.get(folder.id) ?? folder.name}
                    </button>
                  );
                })}
              </div>
            </div>
          )}
          {(tags.length > 0 || requirementTagOptions.length > 0) && (
            <div className="session-filter-group">
              <div className="session-filter-label">{t("session.tags")}</div>
              {tags.length > 0 && (
                <div className="session-tag-filter">
                  {tags.map((tag) => {
                    const active = selectedTagIds.includes(tag.id);
                    return (
                      <button
                        key={tag.id}
                        type="button"
                        className={`session-tag-toggle ${active ? "active" : ""}`}
                        aria-pressed={active}
                        onClick={() => toggleTagFilter(tag.id)}
                      >
                        {tag.name}
                      </button>
                    );
                  })}
                </div>
              )}
              {requirementTagOptions.length > 0 && (
                <div className="session-tag-filter session-requirement-filter">
                  {requirementTagOptions.map((tag) => {
                    const key = reqTagKey(tag);
                    const active = selectedReqTagKeys.has(key);
                    return (
                      <button
                        key={key}
                        type="button"
                        className={`role-chip session-requirement-tag session-requirement-tag-${tag.kind} ${active ? "session-requirement-tag-active" : ""}`}
                        title={`${tag.kind}: ${tag.label}`}
                        aria-pressed={active}
                        onClick={() => toggleTagFilter(key)}
                      >
                        {tag.label}
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          )}
          {providerOptions.length > 0 && (
            <div className="session-filter-group">
              <div className="session-filter-label">{t("session.providerFilter")}</div>
              <div className="session-tag-filter">
                {providerOptions.map((provider) => {
                  const active = selectedProviderIds.includes(provider.id);
                  return (
                    <button
                      key={provider.id}
                      type="button"
                      className={`session-tag-toggle ${active ? "active" : ""}`}
                      aria-pressed={active}
                      onClick={() => toggleProviderFilter(provider.id)}
                    >
                      {provider.name}
                    </button>
                  );
                })}
              </div>
            </div>
          )}
          {modelOptions.length > 0 && (
            <div className="session-filter-group">
              <div className="session-filter-label">{t("session.modelFilter")}</div>
              <div className="session-tag-filter">
                {modelOptions.map((model) => {
                  const active = selectedModelIds.includes(model);
                  return (
                    <button
                      key={model}
                      type="button"
                      className={`session-tag-toggle ${active ? "active" : ""}`}
                      aria-pressed={active}
                      onClick={() => toggleModelFilter(model)}
                    >
                      {model}
                    </button>
                  );
                })}
              </div>
            </div>
          )}
          {modeOptions.length > 0 && (
            <div className="session-filter-group">
              <div className="session-filter-label">{t("session.modeFilter")}</div>
              <div className="session-tag-filter">
                {modeOptions.map((mode) => {
                  const active = selectedModes.includes(mode);
                  return (
                    <button
                      key={mode}
                      type="button"
                      className={`session-tag-toggle ${active ? "active" : ""}`}
                      aria-pressed={active}
                      onClick={() => toggleModeFilter(mode)}
                    >
                      {orchestrationLabel(t, mode)}
                    </button>
                  );
                })}
              </div>
            </div>
          )}
          {sourceOptions.length > 0 && (
            <div className="session-filter-group">
              <div className="session-filter-label">{t("session.sourceFilter")}</div>
              <div className="session-tag-filter">
                {sourceOptions.map((src) => {
                  const active = selectedSources.includes(src);
                  return (
                    <button
                      key={src}
                      type="button"
                      className={`session-tag-toggle ${active ? "active" : ""}`}
                      aria-pressed={active}
                      onClick={() => toggleSourceFilter(src)}
                    >
                      {t(`session.source.${src}`, src)}
                    </button>
                  );
                })}
              </div>
            </div>
          )}
          {initiatedByOptions.length > 0 && (
            <div className="session-filter-group">
              <div className="session-filter-label">{t("session.initiatedByFilter")}</div>
              <div className="session-tag-filter">
                {initiatedByOptions.map((kind) => {
                  const active = selectedInitiatedBy.includes(kind);
                  return (
                    <button
                      key={kind}
                      type="button"
                      className={`session-tag-toggle ${active ? "active" : ""}`}
                      aria-pressed={active}
                      onClick={() => toggleInitiatedByFilter(kind)}
                    >
                      {t(`session.initiatedBy.${kind}`, kind)}
                    </button>
                  );
                })}
              </div>
            </div>
          )}
          <div className="session-filter-group">
            <div className="session-filter-label">{t("session.fileEditModeFilter")}</div>
            <div className="session-tag-filter">
              {SESSION_FILE_EDIT_MODE_FILTERS.map((value) => {
                const active = fileEditModeFilter === value;
                return (
                  <button
                    key={value}
                    type="button"
                    className={`session-tag-toggle ${active ? "active" : ""}`}
                    aria-pressed={active}
                    onClick={() => setFileEditModeFilter(value)}
                  >
                    {t(`session.fileEditMode.${value}`)}
                  </button>
                );
              })}
            </div>
          </div>
          {advancedFilterActive && (
            <button type="button" className="btn-small session-filter-clear" onClick={clearAdvancedFilters}>
              {t("session.clearFilters")}
            </button>
          )}
          {orgError && <div className="session-org-error">{orgError}</div>}
        </div>
      )}
      {(aiResult || aiError || aiLoading || searchStatusLoading) && (
        <div className="ai-search-status">
          {aiLoading && (
            <span className="ai-search-loading">{t("session.aiSearchLoading")}</span>
          )}
          {!aiLoading && searchStatusLoading && (
            <span className="ai-search-loading">{t("session.searching")}</span>
          )}
          {!aiLoading && aiError && (
            <span className="ai-search-error">{aiError}</span>
          )}
          {!aiLoading && !searchStatusLoading && !aiError && aiResult && (
            <>
              <span className="ai-search-reasoning" title={aiResult.reasoning}>
                {aiResult.reasoning ||
                  t("session.aiSearchMatches", { count: aiResult.ids.length })}
              </span>
              {aiResult.reasoning && (
                <button
                  className="btn-small ai-search-info"
                  onClick={() => setAiDetailsOpen(true)}
                  title={t("session.aiSearchViewFull")}
                  aria-label={t("session.aiSearchViewFull")}
                >
                  ⓘ
                </button>
              )}
              <button
                className="btn-small ai-search-clear"
                onClick={clearAiSearch}
                title={t("session.aiSearchClear")}
              >
                ×
              </button>
            </>
          )}
        </div>
      )}
      {aiDetailsOpen && aiResult && (
        <div
          className="ai-search-modal-backdrop"
          onClick={() => setAiDetailsOpen(false)}
          role="dialog"
          aria-modal="true"
        >
          <div
            className="ai-search-modal"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="ai-search-modal-header">
              <span>{t("session.aiSearchDetailsTitle")}</span>
              <button
                className="btn-small ai-search-modal-close"
                onClick={() => setAiDetailsOpen(false)}
                aria-label={t("session.aiSearchClose")}
              >
                ×
              </button>
            </div>
            {search && (
              <div className="ai-search-modal-section">
                <div className="ai-search-modal-label">
                  {t("session.aiSearchQueryLabel")}
                </div>
                <div className="ai-search-modal-query">{search}</div>
              </div>
            )}
            <div className="ai-search-modal-section">
              <div className="ai-search-modal-label">
                {t("session.aiSearchReasoningLabel")}
              </div>
              <div className="ai-search-modal-reasoning">
                {aiResult.reasoning}
              </div>
            </div>
            <div className="ai-search-modal-section">
              <div className="ai-search-modal-label">
                {t("session.aiSearchMatches", { count: aiResult.ids.length })}
              </div>
            </div>
          </div>
        </div>
      )}
      <LayoutGroup>
      <div
        className="session-list-items"
        onScroll={handleItemsScroll}
        onDragStart={(e) => {
          if (isSessionDrag(e)) setIsDraggingSession(true);
        }}
        onDragEnd={() => {
          setIsDraggingSession(false);
          setNewFolderDragOver(false);
        }}
      >
        {searching && sessions.length > 0 && (
          <div className="session-list-loading session-list-loading-top">
            <span className="session-list-spinner" aria-hidden="true" />
            <span>{t("session.loading")}</span>
          </div>
        )}
        {showFolders && isDraggingSession && (
          <div
            className={`session-folder-heading session-new-folder-heading ${
              newFolderDragOver ? "drag-over" : ""
            }`}
            onDragOver={(e) => {
              if (!isSessionDrag(e)) return;
              e.preventDefault();
              e.dataTransfer.dropEffect = "move";
              if (!newFolderDragOver) setNewFolderDragOver(true);
            }}
            onDragLeave={() => setNewFolderDragOver(false)}
            onDrop={(e) => {
              if (!isSessionDrag(e)) return;
              e.preventDefault();
              setNewFolderDragOver(false);
              const id = e.dataTransfer.getData(SESSION_DRAG_MIME);
              if (!id) return;
              setNewFolderDrop({
                sessionId: id,
                anchor: e.currentTarget.getBoundingClientRect(),
              });
            }}
          >
            <Icon name="folder-plus" size={12} />
            <span>{t("session.newFolder")}</span>
          </div>
        )}
        {showFolders && folderRoots.map((node) => (
          <FolderSection
            key={node.folder.id}
            node={node}
            depth={0}
            currentSessionId={currentSessionId}
            highlightedSessionId={highlightedSessionId}
            childrenByParent={childrenByParent}
            copiedId={copiedId}
            providers={providers}
            showArchived={showArchived}
            scoreMap={scoreMap}
            onSelect={onSelect}
            onDelete={onDelete}
            onCopy={copyId}
            onRename={onRename}
            onPin={onPin}
            onUnpinOthers={onUnpinOthers}
            onContextMenuOpen={openSessionContextMenu}
            onArchive={onArchive}
            onWorkerEligible={onWorkerEligible}
            onDetails={onDetails}
            onResumeEng={onResumeEng}
            folders={folders}
            tags={tags}
            onMoveToFolder={moveToFolder}
            onCreateFolder={createAndAssignFolder}
            onSetTags={setSessionTags}
            onCreateTag={createAndAssignTag}
            selectedReqTagKeys={selectedReqTagKeys}
            onToggleReqTag={toggleTagFilter}
            collapsedFolderIds={collapsedFolderIds}
            onToggleFolder={toggleFolder}
            sortField={sessionSort ?? "updated_at"}
          />
        ))}
        {showFolders && unfiledSessions.length > 0 && folderRoots.length > 0 && (
          <div
            className={`session-folder-heading session-unfiled-heading ${unfiledDragOver ? "drag-over" : ""}`}
            onDragOver={(e) => {
              if (!isSessionDrag(e)) return;
              e.preventDefault();
              e.dataTransfer.dropEffect = "move";
              if (!unfiledDragOver) setUnfiledDragOver(true);
            }}
            onDragLeave={() => setUnfiledDragOver(false)}
            onDrop={(e) => {
              if (!isSessionDrag(e)) return;
              e.preventDefault();
              setUnfiledDragOver(false);
              const id = e.dataTransfer.getData(SESSION_DRAG_MIME);
              if (id) moveToFolder(id, null);
            }}
          >
            <span>{t("session.unfiled")}</span>
          </div>
        )}
        {(showFolders ? unfiledSessions : roots).map((s) => (
          <SessionNode
            key={s.id}
            session={s}
            depth={0}
            dragEnabled={showFolders}
            currentSessionId={currentSessionId}
            highlightedSessionId={highlightedSessionId}
            childrenByParent={childrenByParent}
            copiedId={copiedId}
            providers={providers}
            showArchived={showArchived}
            contentScore={scoreMap.get(s.id) ?? null}
            onSelect={onSelect}
            onDelete={onDelete}
            onCopy={copyId}
            onRename={onRename}
            onPin={onPin}
            onUnpinOthers={onUnpinOthers}
            onContextMenuOpen={openSessionContextMenu}
            onArchive={onArchive}
            onWorkerEligible={onWorkerEligible}
            onDetails={onDetails}
            onResumeEng={onResumeEng}
            folders={folders}
            tags={tags}
            onMoveToFolder={moveToFolder}
            onCreateFolder={createAndAssignFolder}
            onSetTags={setSessionTags}
            onCreateTag={createAndAssignTag}
            selectedReqTagKeys={selectedReqTagKeys}
            onToggleReqTag={toggleTagFilter}
            sortField={sessionSort ?? "updated_at"}
          />
        ))}
        {searching && sessions.length === 0 && (
          <div className="session-list-loading">
            <span className="session-list-spinner" aria-hidden="true" />
            <span>{t("session.loading")}</span>
          </div>
        )}
        {!searching && sessions.length === 0 && (
          <div className="session-empty">{t("session.noSessions")}</div>
        )}
        {sessions.length > 0 && filtered.length === 0 && !searching && (
          <div className="session-empty">{t("session.noMatch")}</div>
        )}
        {loadingMore && (
          <div className="session-list-loading session-list-loading-more">
            <span className="session-list-spinner" aria-hidden="true" />
            <span>{t("session.loadingMore")}</span>
          </div>
        )}
        {hasMore && !loadingMore && (
          <div className="session-list-more" aria-hidden="true" />
        )}
      </div>
      </LayoutGroup>
      {newFolderDrop && (
        <NewFolderDropPopover
          anchor={newFolderDrop.anchor}
          onCreate={(name) => createAndAssignFolder(newFolderDrop.sessionId, name)}
          onClose={() => setNewFolderDrop(null)}
        />
      )}
      {ctxMenu && (
        <div
          className="ctx-menu"
          data-testid="session-context-menu"
          style={{ position: "fixed", left: ctxMenu.x, top: ctxMenu.y, zIndex: 10000 }}
          onClick={(e) => e.stopPropagation()}
        >
          {ctxMenu.items.map((item) => (
            <button
              key={item.id}
              className="ctx-menu-item"
              style={item.danger ? { color: "var(--error)" } : undefined}
              onClick={() => {
                setCtxMenu(null);
                item.onClick();
              }}
            >
              {item.icon && <span className="ctx-menu-icon">{item.icon}</span>}
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
