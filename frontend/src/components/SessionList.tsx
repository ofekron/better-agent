import { memo, useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent, type UIEvent as ReactUIEvent } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { LayoutGroup, motion } from "framer-motion";
import type { OrchestrationMode, Provider, RequirementTag, Session, SessionFolder, SessionTag, WorkerCreationPolicy, WorkerInfo } from "../types";
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
import { SessionTagSummary } from "./SessionTagSummary";
import { SessionStatsPopover } from "./SessionStatsPopover";
import Icon from "./Icon";
import { SearchInput } from "./SearchInput";
import { TagFilterAutocomplete, type TagFilterOption } from "./TagFilterAutocomplete";
import type { SessionListFilters } from "../hooks/useSession";
import { useLocalStorage } from "../hooks/useLocalStorage";
import { eventBus } from "../lib/eventBus";
import { markSessionUnread } from "../lib/sessionRegistry";
import { sessionMessageCount } from "src/lib/sessionMessageCount";
import { SESSION_SORT_LABEL, sessionSortValue, timeAgo } from "../lib/sessionSort";
import { buildFolderPathMap, sortFolders } from "../sessionFolders";
import { todoProgress } from "./TodosPanel";
import { sessionLinkMarker } from "../utils/linkifyFilePaths";
import { copyToClipboard } from "../utils/clipboard";
import { shouldStartAgentBoardSessionDrag, type SessionDragPoint } from "../utils/sessionDragThreshold";
import { logTiming } from "../lib/frontendLogger";
import { runThreeStateSync } from "../progress/store";
import { groupSessionsByStatusRank, statusGroupI18nKey } from "../lib/sessionStatusGroups";

const SESSION_BULK_SELECT_LONG_PRESS_MS = 500;
interface Props {
  sessions: Session[];
  /** Optional full session list, unfiltered by the parent's project
   * picker. When provided AND AI search is active, the filtered list
   * is computed against THIS array so AI matches from other projects
   * can surface. If omitted, falls back to `sessions`. */
  allSessions?: Session[];
  currentSessionId?: string;
  /** The currently-focused session object. Used as the authoritative
   * fallback for the pinned anchor so an active search filter cannot
   * hide it (the backend-filtered `sessions`/`allSessions` may exclude
   * a non-matching selected session). */
  selectedSession?: Session | null;
  /** DOM node (above the sidebar tabs) where the pinned selected-session
   * anchor is portaled. When null, the anchor renders inline at the top
   * of the list as a fallback. */
  selectedAnchorContainer?: HTMLElement | null;
  providers: Provider[];
  onSelect: SessionSelectHandler;
  onDelete: (id: string) => void;
  /** Bulk-delete every session in `ids` behind a single confirmation. */
  onDeleteMany: (ids: string[]) => void;
  onRename: (id: string, name: string) => void;
  onPin: (id: string, pinned: boolean) => void;
  onArchive: (id: string, archived: boolean) => void;
  onMoveToProject: (id: string) => void;
  onWorkerEligible: (id: string, value: boolean) => void;
  onAgentRenameAllowed: (id: string, value: boolean) => void;
  teamWorkersBySession?: Record<string, WorkerInfo[]>;
  onWorkerCreationPolicyChange?: (id: string, policy: WorkerCreationPolicy) => void;
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
    /** Backend-built sidebar rows in relevance-ranked order.
     * The loaded `sessions`/`allSessions` pool is paginated, so matched
     * sessions are routinely absent from it — these rows fill the gap. */
    results: Session[];
    reasoning: string;
    error: string | null;
  } | null>;
  /** Lets the parent disable its project-picker UI while AI search is
   * filtering across all projects. Fires whenever the AI-active flag
   * flips. */
  onAiActiveChange?: (active: boolean) => void;
  backendProjectPath?: string;
  /** Worktree-level narrowing within `backendProjectPath`. Empty = all
   * worktrees of the repo. */
  backendCwdPrefix?: string;
  onBackendFiltersChange?: (filters: SessionListFilters) => void;
  onUnpinOthers: (keepId: string) => void;
  /** Opens the new-session modal / flow. */
  onCreate?: () => void;
  hasMore?: boolean;
  searching?: boolean;
  loadingMore?: boolean;
  onLoadMore?: () => void;
}

type SessionSelectHandler = (id: string, session?: Session) => void;

// Empty children map for the pinned selected-session anchor, which
// renders as a single row without its sub-session sub-tree.
const EMPTY_CHILDREN: Map<string, Session[]> = new Map();

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
type SessionSource = "user" | "system" | "web" | "cli" | "import" | "extension" | "internal";
const SESSION_SOURCES: SessionSource[] = ["user", "system", "web", "cli", "import", "extension", "internal"];

type PersistedSessionFilters = {
  search: string;
  showArchived: boolean;
  selectedFolderIds: string[];
  selectedTagIds: string[];
  selectedProviderIds: string[];
  selectedModelIds: string[];
  selectedModes: OrchestrationMode[];
  selectedSources: SessionSource[];
  fileEditModeFilter: SessionFileEditModeFilter;
  selectedSearchFields: SessionSearchField[];
};

const SESSION_FILTERS_BY_PROJECT_LS_KEY = "better-agent-session-filters-by-project";
/** Bucket key for filters when no project is selected. */
const SESSION_FILTERS_NO_PROJECT_KEY = "__none__";

function readSessionFiltersByProject(): Record<string, PersistedSessionFilters> {
  try {
    const raw = localStorage.getItem(SESSION_FILTERS_BY_PROJECT_LS_KEY);
    return raw ? (JSON.parse(raw) as Record<string, PersistedSessionFilters>) : {};
  } catch {
    return {};
  }
}

function writeSessionFiltersForProject(projectPath: string, filters: PersistedSessionFilters): void {
  try {
    const all = readSessionFiltersByProject();
    all[projectPath || SESSION_FILTERS_NO_PROJECT_KEY] = filters;
    localStorage.setItem(SESSION_FILTERS_BY_PROJECT_LS_KEY, JSON.stringify(all));
  } catch {
    // localStorage unavailable (private mode, quota) — filters just won't persist.
  }
}

/** Persisted history of committed session-filter search queries, most
 *  recent first. Backs the completion suggestions shown in the filter
 *  field. Capped so localStorage can't grow without bound; the UI only
 *  ever surfaces the top few matches anyway. */
const SESSION_SEARCH_HISTORY_LS_KEY = "better-agent-session-search-history";
/** How many committed queries we retain on disk. */
const SESSION_SEARCH_HISTORY_MAX = 50;
/** How many completion options the filter field surfaces at once. */
const SESSION_SEARCH_HISTORY_SUGGESTIONS = 5;

function readSessionSearchHistory(): string[] {
  try {
    const raw = localStorage.getItem(SESSION_SEARCH_HISTORY_LS_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    const out: string[] = [];
    const seen = new Set<string>();
    for (const entry of parsed) {
      if (typeof entry !== "string") continue;
      const trimmed = entry.trim();
      if (!trimmed) continue;
      const lower = trimmed.toLowerCase();
      if (seen.has(lower)) continue;
      seen.add(lower);
      out.push(trimmed);
      if (out.length >= SESSION_SEARCH_HISTORY_MAX) break;
    }
    return out;
  } catch {
    return [];
  }
}

/** Record a committed query at the front of history. Blank queries are
 *  ignored; an existing (case-insensitive) match is moved to the front
 *  rather than duplicated, so re-running a query bumps its recency. The
 *  original casing of the newest submission is what we keep. */
function pushSessionSearchHistory(query: string): string[] {
  const trimmed = query.trim();
  if (!trimmed) return readSessionSearchHistory();
  const existing = readSessionSearchHistory();
  const lower = trimmed.toLowerCase();
  const deduped = existing.filter((entry) => entry.toLowerCase() !== lower);
  const next = [trimmed, ...deduped].slice(0, SESSION_SEARCH_HISTORY_MAX);
  try {
    localStorage.setItem(SESSION_SEARCH_HISTORY_LS_KEY, JSON.stringify(next));
  } catch {
    // localStorage unavailable — history just won't persist this run.
  }
  return next;
}

/** The completion options for the current filter text: up to
 *  {@link SESSION_SEARCH_HISTORY_SUGGESTIONS} most-recent history
 *  entries that "fit" the typed text (case-insensitive substring).
 *  Empty text fits every entry. The exact current text is excluded —
 *  completing to what's already typed is a no-op. */
function matchingSessionSearchHistory(history: string[], query: string): string[] {
  const trimmed = query.trim();
  const lower = trimmed.toLowerCase();
  const out: string[] = [];
  for (const entry of history) {
    if (entry.toLowerCase() === lower) continue;
    if (lower && !entry.toLowerCase().includes(lower)) continue;
    out.push(entry);
    if (out.length >= SESSION_SEARCH_HISTORY_SUGGESTIONS) break;
  }
  return out;
}

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

/** All session ids in a folder's subtree, including nested subfolders, regardless of collapse state. */
function collectFolderSubtreeSessionIds(node: FolderRenderNode): string[] {
  return [
    ...node.sessions.map((s) => s.id),
    ...node.children.flatMap(collectFolderSubtreeSessionIds),
  ];
}

/** dataTransfer MIME carrying the dragged session id when reassigning
 * folders by drag. Custom type so it can't be confused with plain text. */
export const SESSION_DRAG_MIME = "application/x-better-agent-session-id";

/** True when a drag carries a session id (a folder-reassign drag). */
function isSessionDrag(e: React.DragEvent): boolean {
  return e.dataTransfer.types.includes(SESSION_DRAG_MIME);
}

interface NodeProps {
  session: Session;
  depth: number;
  /** Bumped every 30s by the parent's ticker so memoized rows re-render and
   * their relative "X ago" timestamps advance. Not read directly — its only
   * job is to be a changing prop the memo comparator can see. */
  nowTick: number;
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
  onSelect: SessionSelectHandler;
  onDelete: (id: string) => void;
  onCopy: (id: string) => void;
  onRename: (id: string, name: string) => void;
  onPin: (id: string, pinned: boolean) => void;
  onUnpinOthers: (keepId: string) => void;
  /** Desktop right-click → open the floating context menu with these items. */
  onContextMenuOpen: (e: React.MouseEvent, items: ActionItem[]) => void;
  onArchive: (id: string, archived: boolean) => void;
  onMoveToProject: (id: string) => void;
  onWorkerEligible: (id: string, value: boolean) => void;
  onAgentRenameAllowed: (id: string, value: boolean) => void;
  teamWorkersBySession: Record<string, WorkerInfo[]>;
  onWorkerCreationPolicyChange?: (id: string, policy: WorkerCreationPolicy) => void;
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
  selected: boolean;
  bulkSelectMode: boolean;
  onToggleSelected: (id: string) => void;
  onStartBulkSelect: (id: string) => void;
}

function SessionNodeImpl({
  session,
  depth,
  nowTick,
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
  onMoveToProject,
  onWorkerEligible,
  onAgentRenameAllowed,
  teamWorkersBySession,
  onWorkerCreationPolicyChange,
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
  selected,
  bulkSelectMode,
  onToggleSelected,
  onStartBulkSelect,
}: NodeProps) {
  const { t } = useTranslation();
  const { show: showSheet } = useMobileActionSheet();
  const mode = session.orchestration_mode ?? "team";
  const isManager = mode === "team";
  const teamWorkers = isManager ? (teamWorkersBySession[session.id] ?? []) : [];
  const msgs = sessionMessageCount(session);
  const kids = childrenByParent.get(session.id) ?? [];
  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState(session.name);
  const [teamWorkersOpen, setTeamWorkersOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const rowDragRef = useRef<HTMLDivElement | null>(null);
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
  const [statsPopover, setStatsPopover] = useState<PopoverAnchor | null>(null);
  const menuAnchorRef = useRef<PopoverAnchor | null>(null);
  const folderPopoverTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const tagPopoverTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const statsPopoverTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const bulkSelectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const longPressSelectedRef = useRef(false);
  const sessionDragStartPointRef = useRef<SessionDragPoint | null>(null);
  const sessionDragLastPointRef = useRef<SessionDragPoint | null>(null);
  const agentBoardDragStartedRef = useRef(false);
  const sessionDragDocumentCleanupRef = useRef<(() => void) | null>(null);
  useEffect(() => () => {
    if (renameTimerRef.current) clearTimeout(renameTimerRef.current);
    if (folderPopoverTimerRef.current) clearTimeout(folderPopoverTimerRef.current);
    if (tagPopoverTimerRef.current) clearTimeout(tagPopoverTimerRef.current);
    if (statsPopoverTimerRef.current) clearTimeout(statsPopoverTimerRef.current);
    if (bulkSelectTimerRef.current) clearTimeout(bulkSelectTimerRef.current);
    if (sessionDragDocumentCleanupRef.current) sessionDragDocumentCleanupRef.current();
  }, []);

  const clearBulkSelectTimer = () => {
    if (!bulkSelectTimerRef.current) return;
    clearTimeout(bulkSelectTimerRef.current);
    bulkSelectTimerRef.current = null;
  };

  const isBulkSelectBlockedTarget = (target: EventTarget | null) => {
    if (!(target instanceof HTMLElement)) return false;
    return Boolean(target.closest("button, input, textarea, select, a, [role='button']"));
  };

  const handlePointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0 || isBulkSelectBlockedTarget(e.target)) {
      sessionDragStartPointRef.current = null;
      return;
    }
    const rowEl = e.currentTarget as HTMLElement;
    sessionDragStartPointRef.current = { clientX: e.clientX, clientY: e.clientY };
    agentBoardDragStartedRef.current = false;
    longPressSelectedRef.current = false;
    clearBulkSelectTimer();
    // Once bulk-select mode is active, a press must not re-open the action
    // sheet or re-enter selection — taps should cleanly toggle the row's
    // checkbox (handled by handleRowClick). Arming the long-press timer here
    // would swallow that tap on touch devices, breaking mobile multi-select.
    if (bulkSelectMode) return;
    bulkSelectTimerRef.current = setTimeout(() => {
      longPressSelectedRef.current = true;
      if (isMobileViewport()) {
        menuAnchorRef.current = rowEl.getBoundingClientRect();
        showSheet(buildSessionActions(true, true), session.name);
        return;
      }
      onStartBulkSelect(session.id);
    }, SESSION_BULK_SELECT_LONG_PRESS_MS);
  };

  const handleRowClick = () => {
    clearBulkSelectTimer();
    if (longPressSelectedRef.current) {
      longPressSelectedRef.current = false;
      return;
    }
    if (bulkSelectMode) {
      onToggleSelected(session.id);
      return;
    }
    onSelect(session.id, session);
  };
  // Latest handleRowClick for the native dragend listener, which is
  // registered once in an effect and must not go stale.
  const rowClickRef = useRef(handleRowClick);
  rowClickRef.current = handleRowClick;

  const publishAgentBoardDragStart = useCallback((point: SessionDragPoint) => {
    if (agentBoardDragStartedRef.current) return;
    if (!shouldStartAgentBoardSessionDrag(sessionDragStartPointRef.current, point)) return;
    agentBoardDragStartedRef.current = true;
    eventBus.publish("session_drag_start", { session_id: session.id, name: session.name });
  }, [session.id, session.name]);

  const stopSessionDragDocumentTracking = useCallback(() => {
    if (!sessionDragDocumentCleanupRef.current) return;
    sessionDragDocumentCleanupRef.current();
    sessionDragDocumentCleanupRef.current = null;
  }, []);

  const cleanupSessionDragTracking = useCallback(() => {
    stopSessionDragDocumentTracking();
    sessionDragStartPointRef.current = null;
    agentBoardDragStartedRef.current = false;
  }, [stopSessionDragDocumentTracking]);

  const startSessionDragDocumentTracking = useCallback(() => {
    stopSessionDragDocumentTracking();
    const handleDocumentDragOver = (event: DragEvent) => {
      sessionDragLastPointRef.current = { clientX: event.clientX, clientY: event.clientY };
      publishAgentBoardDragStart({ clientX: event.clientX, clientY: event.clientY });
    };
    const handleDocumentDragDone = () => {
      stopSessionDragDocumentTracking();
      sessionDragStartPointRef.current = null;
      agentBoardDragStartedRef.current = false;
    };
    document.addEventListener("dragover", handleDocumentDragOver);
    document.addEventListener("drop", handleDocumentDragDone);
    document.addEventListener("dragend", handleDocumentDragDone);
    sessionDragDocumentCleanupRef.current = () => {
      document.removeEventListener("dragover", handleDocumentDragOver);
      document.removeEventListener("drop", handleDocumentDragDone);
      document.removeEventListener("dragend", handleDocumentDragDone);
    };
  }, [publishAgentBoardDragStart, stopSessionDragDocumentTracking]);

  useEffect(() => {
    const row = rowDragRef.current;
    if (!row) return;
    const handleDragStart = (event: DragEvent) => {
      clearBulkSelectTimer();
      if (!sessionDragStartPointRef.current) {
        sessionDragStartPointRef.current = { clientX: event.clientX, clientY: event.clientY };
      }
      sessionDragLastPointRef.current = null;
      agentBoardDragStartedRef.current = false;
      event.dataTransfer?.setData(SESSION_DRAG_MIME, session.id);
      if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
      startSessionDragDocumentTracking();
    };
    // The browser suppresses the click event once a native drag starts,
    // and Chromium starts one after only a few px of pointer movement —
    // far below the 48px "meaningful drag" threshold. A press that
    // micro-moves would otherwise select nothing. Treat a dragend that
    // never crossed the threshold, never started an agent-board drag,
    // and was consumed by no drop target as the click it really was.
    // Runs on the row (drag source) BEFORE the document-level cleanup
    // listeners bubble-reset the drag refs.
    const handleDragEnd = (event: DragEvent) => {
      const crossedThreshold = sessionDragLastPointRef.current
        ? shouldStartAgentBoardSessionDrag(
            sessionDragStartPointRef.current,
            sessionDragLastPointRef.current,
          )
        : false;
      const dropConsumed = event.dataTransfer
        ? event.dataTransfer.dropEffect !== "none"
        : false;
      const treatAsClick =
        !agentBoardDragStartedRef.current && !crossedThreshold && !dropConsumed;
      cleanupSessionDragTracking();
      sessionDragLastPointRef.current = null;
      if (treatAsClick) rowClickRef.current();
    };
    row.addEventListener("dragstart", handleDragStart);
    row.addEventListener("dragend", handleDragEnd);
    return () => {
      row.removeEventListener("dragstart", handleDragStart);
      row.removeEventListener("dragend", handleDragEnd);
    };
  }, [session.id, session.name, cleanupSessionDragTracking, startSessionDragDocumentTracking]);

  const toggleSessionTag = (tagId: string) => {
    const current = new Set(sessionTagIds(session));
    if (current.has(tagId)) current.delete(tagId);
    else current.add(tagId);
    onSetTags(session.id, Array.from(current));
  };

  const buildSessionActions = (includePin = false, includeSelect = false): ActionItem[] => {
    const copyTarget = sessionLinkMarker(session.id, session.name || "Untitled");
    return [
      ...(includeSelect
        ? [
            {
              id: "select",
              label: t("session.selectSession"),
              icon: <Icon name="check-circle" size={14} />,
              onClick: () => onStartBulkSelect(session.id),
            },
          ]
        : []),
      ...(includePin
        ? [
            {
              id: "pin",
              label: session.pinned ? t("session.unpinTitle") : t("session.pinTitle"),
              icon: <Icon name="pin" size={14} />,
              onClick: () => onPin(session.id, !session.pinned),
            },
          ]
        : []),
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
        id: "stats",
        label: t("tokens.stats"),
        icon: <Icon name="info" size={14} />,
        onClick: () => {
          // Match the folder/tags timer: wait for the action sheet's
          // fade-out so the popover mounts after the backdrop is gone.
          if (statsPopoverTimerRef.current) clearTimeout(statsPopoverTimerRef.current);
          statsPopoverTimerRef.current = setTimeout(
            () => setStatsPopover(menuAnchorRef.current),
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
      {
        id: "agent-rename-allowed",
        label: session.agent_rename_allowed
          ? t("session.agentRenameAllowedOff")
          : t("session.agentRenameAllowedOn"),
        icon: <Icon name="edit" size={14} />,
        onClick: () =>
          onAgentRenameAllowed(session.id, !session.agent_rename_allowed),
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
        id: "mark-unread",
        label: t("session.markUnreadTitle"),
        icon: <Icon name="circle" size={14} />,
        onClick: () => void markSessionUnread(session.id),
      },
      ...(!session.moved_to_session_id
        ? [
            {
              id: "move-to-project",
              label: t("session.moveToProject"),
              icon: <Icon name="folder" size={14} />,
              onClick: () => onMoveToProject(session.id),
            },
          ]
        : []),
      {
        id: "archive",
        label: session.archivePending
          ? t("session.undoArchiveTitle")
          : session.archived
          ? t("session.unarchiveTitle")
          : t("session.archiveTitle"),
        icon: <Icon name="archive" size={14} />,
        onClick: () =>
          onArchive(session.id, session.archivePending ? false : !session.archived),
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
  const additionalModelCount = (session.model_history || [])
    .filter((model) => model && model !== session.model).length;
  const modelLabel = additionalModelCount > 0
    ? `${session.model} ${t("session.additionalModels", { count: additionalModelCount })}`
    : session.model;

  const todos = session.current_todos ?? [];
  const tasks = session.current_tasks ?? [];
  const requirementTags = session.requirement_tags ?? [];
  const manualTags = session.session_tags ?? [];
  const { total: todoTotal, done: todoDone } = todoProgress(todos, tasks);
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
        ref={rowDragRef}
        layout
        layoutId={`session-row-${session.id}`}
        transition={{ duration: 0.3, ease: [0.4, 0, 0.2, 1] }}
        className={`session-item ${
          session.id === currentSessionId ? "active" : ""
        } ${
          session.id === highlightedSessionId ? "highlighted" : ""
        } ${depth > 0 ? "session-item-child" : ""} ${
          selected ? "session-item-selected" : ""
        } ${
          folderDropOver ? "folder-drop-over" : ""
        } ${
          session.archivePending ? "session-item-archive-pending" : ""
        }`}
        style={{ marginInlineStart: depth * 16 }}
        draggable
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
        onPointerDown={handlePointerDown}
        onPointerUp={() => {
          cleanupSessionDragTracking();
          clearBulkSelectTimer();
        }}
        onPointerLeave={clearBulkSelectTimer}
        onPointerCancel={() => {
          cleanupSessionDragTracking();
          clearBulkSelectTimer();
        }}
        onClick={handleRowClick}
        data-mobile-context-owner="session-row"
        onContextMenu={(e) => {
          // Desktop-only: mobile uses the ⋯ button + long-press. Keep
          // the native menu (don't preventDefault) and add our floating
          // toolbar alongside it, matching the message-area pattern.
          if (isMobileViewport()) return;
          const target = e.target as HTMLElement;
          if (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT") {
            return;
          }
          const items = buildSessionActions(true);
          if (items.length === 0) return;
          menuAnchorRef.current = (e.currentTarget as HTMLElement).getBoundingClientRect();
          onContextMenuOpen(e, items);
        }}
        data-testid="session-item"
        data-session-id={session.id}
        data-active={session.id === currentSessionId ? "true" : "false"}
        data-selected={selected ? "true" : "false"}
      >
        {bulkSelectMode && (
          <label
            className="session-item-select"
            title={t("session.selectSession")}
            aria-label={t("session.selectSession")}
            onClick={(e) => e.stopPropagation()}
          >
            <input
              type="checkbox"
              checked={selected}
              onChange={() => onToggleSelected(session.id)}
            />
          </label>
        )}
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
          {session.moved_to_session_id && (
            <button
              type="button"
              className="role-chip source-badge-cli session-moved-chip"
              title={t("session.movedToTitle")}
              onClick={(e) => {
                e.stopPropagation();
                onSelect(session.moved_to_session_id!);
              }}
            >
              {t("session.movedToChip")}
            </button>
          )}
          {session.moved_from_session_id && (
            <button
              type="button"
              className="role-chip source-badge-cli session-moved-chip"
              title={t("session.movedFromTitle")}
              onClick={(e) => {
                e.stopPropagation();
                onSelect(session.moved_from_session_id!);
              }}
            >
              {t("session.movedFromChip")}
            </button>
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
          {orchestrationLabel(t, mode)}
          {isManager && ` | ${teamWorkers.length} ${t("session.workers")}`}
        </div>
        {isManager && (
          <div className="session-team-summary" onClick={(e) => e.stopPropagation()}>
            <button
              type="button"
              className="session-team-toggle"
              aria-expanded={teamWorkersOpen}
              aria-label={teamWorkersOpen ? t("session.collapseTeamWorkers") : t("session.expandTeamWorkers")}
              onClick={() => setTeamWorkersOpen((value) => !value)}
            >
              <Icon name={teamWorkersOpen ? "chevron-down" : "chevron-right"} size={12} />
              <span>{t("session.teamWorkers")}</span>
              <span className="session-team-count">{teamWorkers.length}</span>
            </button>
            {teamWorkersOpen && (
              <div className="session-team-details">
                <label className="session-team-policy">
                  <span>{t("session.workerCreationPolicy")}</span>
                  <select
                    value={session.worker_creation_policy ?? "ask"}
                    onChange={(event) =>
                      onWorkerCreationPolicyChange?.(
                        session.id,
                        event.target.value as WorkerCreationPolicy,
                      )
                    }
                    disabled={!onWorkerCreationPolicyChange}
                  >
                    <option value="ask">{t("session.workerPolicyAsk")}</option>
                    <option value="approve">{t("session.workerPolicyApprove")}</option>
                    <option value="deny">{t("session.workerPolicyDeny")}</option>
                  </select>
                </label>
                {teamWorkers.length ? (
                  <div className="session-team-workers">
                    {teamWorkers.map((worker) => (
                      <div
                        key={worker.agent_session_id}
                        className="session-team-worker-row"
                      >
                        <span className="session-team-worker-name">{worker.name}</span>
                        {worker.team_role && (
                          <span className="session-team-worker-role">{worker.team_role}</span>
                        )}
                        <span className={`worker-mode-badge worker-mode-${worker.orchestration_mode}`}>
                          {worker.orchestration_mode}
                        </span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="session-team-workers-empty">{t("session.noTeamWorkers")}</div>
                )}
              </div>
            )}
          </div>
        )}
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
        {manualTags.length > 0 && <SessionTagSummary tags={manualTags} />}
        <div className="session-item-meta session-item-meta-row">
          <span className="session-item-meta-text">
            {providerName} | {modelLabel} | {msgs} {t("session.msgs")}
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
            title={t("tokens.stats")}
            aria-label={t("tokens.stats")}
            onClick={(e) => {
              e.stopPropagation();
              setStatsPopover(e.currentTarget.getBoundingClientRect());
            }}
          >
            <Icon name="info" size={12} />
          </button>
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
            title={copiedId === sessionLinkMarker(session.id, session.name || "Untitled") ? t("session.copyTitle") : t("session.copyTitleNot", { id: session.id })}
            aria-label="Copy session id"
            onClick={(e) => {
              e.stopPropagation();
              onCopy(sessionLinkMarker(session.id, session.name || "Untitled"));
            }}
          >
            {copiedId === sessionLinkMarker(session.id, session.name || "Untitled") ? "\u2713" : "\u29C9"}
          </button>
          {session.archivePending ? (
            <button
              className="session-item-archive session-item-archive-undo"
              title={t("session.undoArchiveTitle")}
              aria-label={t("session.undoArchiveTitle")}
              onClick={(e) => {
                e.stopPropagation();
                onArchive(session.id, false);
              }}
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>
              <span className="session-item-archive-undo-label">{t("session.undoArchiveTitle")}</span>
            </button>
          ) : (
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
          )}
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
      {statsPopover && (
        <SessionStatsPopover
          anchor={statsPopover}
          session={session}
          onClose={() => setStatsPopover(null)}
        />
      )}
      {kids.map((child) => (
        <SessionNode
          key={child.id}
          session={child}
          depth={depth + 1}
          nowTick={nowTick}
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
          onMoveToProject={onMoveToProject}
          onWorkerEligible={onWorkerEligible}
          onAgentRenameAllowed={onAgentRenameAllowed}
          teamWorkersBySession={teamWorkersBySession}
          onWorkerCreationPolicyChange={onWorkerCreationPolicyChange}
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
          selected={selected}
          bulkSelectMode={bulkSelectMode}
          onToggleSelected={onToggleSelected}
          onStartBulkSelect={onStartBulkSelect}
        />
      ))}
    </>
  );
}

/** Shallow-equal by identity for a node's own child slice. */
function sameChildSlice(a?: Session[], b?: Session[]): boolean {
  if (a === b) return true;
  if (!a || !b || a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

/** True when NOTHING changed anywhere in the subtree rooted at `id`: the
 * direct child slice is identity-equal AND every descendant slice is too.
 *
 * A node renders its children recursively, and React.memo bails the ENTIRE
 * subtree when a node's comparator returns true — so it is not enough to
 * check only the direct child slice. If a grandchild's session object was
 * replaced (rename, new message, tag/pin change) without a selection change,
 * the intervening ancestor's own slice is still identity-stable; only a full
 * subtree walk catches it and forces the ancestor (hence the whole path down
 * to the changed node) to re-render. */
function subtreeSessionsEqual(
  prevMap: Map<string, Session[]>,
  nextMap: Map<string, Session[]>,
  id: string,
): boolean {
  const prevKids = prevMap.get(id);
  const nextKids = nextMap.get(id);
  if (!sameChildSlice(prevKids, nextKids)) return false;
  if (nextKids) {
    for (const child of nextKids) {
      if (!subtreeSessionsEqual(prevMap, nextMap, child.id)) return false;
    }
  }
  return true;
}

/** Custom memo comparator for SessionNode.
 *
 * Selecting a session re-renders SessionList with a new `currentSessionId`
 * and a freshly-rebuilt `childrenByParent` map (its useMemo keys on
 * currentSessionId), so a plain shallow memo would re-render every row. But
 * only the patched (selected) session object changes identity, and only the
 * two rows whose active/highlight state flips actually need to re-render. So:
 *
 *  - Compare the node's whole subtree by session identity (via
 *    `subtreeSessionsEqual`), never the churning map reference — a memo bail
 *    skips the entire subtree, so a deep descendant change must re-render its
 *    ancestors.
 *  - Treat `currentSessionId`/`highlightedSessionId` as relevant only when
 *    this node's own active/highlight bit flips — EXCEPT a node with
 *    descendants must re-render when they change, since it forwards those ids
 *    to child rows whose highlight may have moved.
 *  - Everything else (session object, handlers — now useCallback-stable —
 *    providers/tags/folders/etc.) is compared by identity/value.
 *
 * This leaves framer-motion's layout props untouched, so the pinned-anchor
 * shared-element animation is unchanged; it just stops N unrelated rows from
 * re-rendering (and re-scheduling their layout projection) on every select. */
function nodePropsEqual(prev: NodeProps, next: NodeProps): boolean {
  if (
    (prev.session.id === prev.currentSessionId) !==
    (next.session.id === next.currentSessionId)
  ) {
    return false;
  }
  if (
    (prev.session.id === prev.highlightedSessionId) !==
    (next.session.id === next.highlightedSessionId)
  ) {
    return false;
  }
  // Any change anywhere in this node's subtree (a descendant session object
  // replaced, or membership changed) must re-render it — React.memo bails the
  // whole subtree, so a stale grandchild would otherwise never reconcile.
  if (
    !subtreeSessionsEqual(
      prev.childrenByParent,
      next.childrenByParent,
      next.session.id,
    )
  ) {
    return false;
  }
  const nextKids = next.childrenByParent.get(next.session.id);
  if (nextKids && nextKids.length > 0) {
    if (prev.currentSessionId !== next.currentSessionId) return false;
    if (prev.highlightedSessionId !== next.highlightedSessionId) return false;
  }
  const keys = Object.keys(next) as (keyof NodeProps)[];
  if (keys.length !== Object.keys(prev).length) return false;
  for (const key of keys) {
    if (
      key === "currentSessionId" ||
      key === "highlightedSessionId" ||
      key === "childrenByParent"
    ) {
      continue;
    }
    if (prev[key] !== next[key]) return false;
  }
  return true;
}

const SessionNode = memo(SessionNodeImpl, nodePropsEqual);

interface FolderSectionProps {
  node: FolderRenderNode;
  depth: number;
  nowTick: number;
  currentSessionId?: string;
  highlightedSessionId?: string | null;
  childrenByParent: Map<string, Session[]>;
  copiedId: string | null;
  providers: Provider[];
  showArchived: boolean;
  scoreMap: Map<string, number>;
  onSelect: SessionSelectHandler;
  onDelete: (id: string) => void;
  onCopy: (id: string) => void;
  onRename: (id: string, name: string) => void;
  onPin: (id: string, pinned: boolean) => void;
  onUnpinOthers: (keepId: string) => void;
  onContextMenuOpen: (e: React.MouseEvent, items: ActionItem[]) => void;
  onArchive: (id: string, archived: boolean) => void;
  onMoveToProject: (id: string) => void;
  onWorkerEligible: (id: string, value: boolean) => void;
  onAgentRenameAllowed: (id: string, value: boolean) => void;
  teamWorkersBySession: Record<string, WorkerInfo[]>;
  onWorkerCreationPolicyChange?: (id: string, policy: WorkerCreationPolicy) => void;
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
  selectedSessionIds: Set<string>;
  bulkSelectMode: boolean;
  onToggleSelected: (id: string) => void;
  onStartBulkSelect: (id: string) => void;
  onToggleGroupSelection: (ids: string[]) => void;
}

function FolderSection({
  node,
  depth,
  nowTick,
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
  onMoveToProject,
  onWorkerEligible,
  onAgentRenameAllowed,
  teamWorkersBySession,
  onWorkerCreationPolicyChange,
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
  selectedSessionIds,
  bulkSelectMode,
  onToggleSelected,
  onStartBulkSelect,
  onToggleGroupSelection,
}: FolderSectionProps) {
  const { t } = useTranslation();
  const collapsed = collapsedFolderIds.has(node.folder.id);
  const [dragOver, setDragOver] = useState(false);
  const groupSessionIds = useMemo(() => collectFolderSubtreeSessionIds(node), [node]);
  const groupSelectedCount = useMemo(
    () => groupSessionIds.reduce((n, id) => n + (selectedSessionIds.has(id) ? 1 : 0), 0),
    [groupSessionIds, selectedSessionIds],
  );
  const groupAllSelected = groupSessionIds.length > 0 && groupSelectedCount === groupSessionIds.length;
  const groupCheckboxRef = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    if (groupCheckboxRef.current) {
      groupCheckboxRef.current.indeterminate = groupSelectedCount > 0 && !groupAllSelected;
    }
  }, [groupSelectedCount, groupAllSelected]);
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
        {bulkSelectMode && groupSessionIds.length > 0 && (
          <label
            className="session-group-select-all"
            title={groupAllSelected ? t("session.deselectGroup") : t("session.selectGroup")}
            aria-label={groupAllSelected ? t("session.deselectGroup") : t("session.selectGroup")}
            onClick={(e) => e.stopPropagation()}
          >
            <input
              ref={groupCheckboxRef}
              type="checkbox"
              checked={groupAllSelected}
              onChange={() => onToggleGroupSelection(groupSessionIds)}
            />
          </label>
        )}
      </button>
      {!collapsed && node.sessions.map((s) => (
        <SessionNode
          key={s.id}
          session={s}
          depth={depth}
          nowTick={nowTick}
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
          onMoveToProject={onMoveToProject}
          onWorkerEligible={onWorkerEligible}
          onAgentRenameAllowed={onAgentRenameAllowed}
          teamWorkersBySession={teamWorkersBySession}
          onWorkerCreationPolicyChange={onWorkerCreationPolicyChange}
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
          selected={selectedSessionIds.has(s.id)}
          bulkSelectMode={bulkSelectMode}
          onToggleSelected={onToggleSelected}
          onStartBulkSelect={onStartBulkSelect}
        />
      ))}
      {!collapsed && node.children.map((child) => (
        <FolderSection
          key={child.folder.id}
          node={child}
          depth={depth + 1}
          nowTick={nowTick}
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
          onMoveToProject={onMoveToProject}
          onWorkerEligible={onWorkerEligible}
          onAgentRenameAllowed={onAgentRenameAllowed}
          teamWorkersBySession={teamWorkersBySession}
          onWorkerCreationPolicyChange={onWorkerCreationPolicyChange}
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
          selectedSessionIds={selectedSessionIds}
          bulkSelectMode={bulkSelectMode}
          onToggleSelected={onToggleSelected}
          onStartBulkSelect={onStartBulkSelect}
          onToggleGroupSelection={onToggleGroupSelection}
        />
      ))}
    </div>
  );
}

export function SessionList({
  sessions,
  allSessions,
  currentSessionId,
  selectedSession: selectedSessionProp,
  selectedAnchorContainer,
  providers,
  onSelect,
  onDelete,
  onDeleteMany,
  onRename,
  onPin,
  onArchive,
  onMoveToProject,
  onWorkerEligible,
  onAgentRenameAllowed,
  teamWorkersBySession = {},
  onWorkerCreationPolicyChange,
  onDetails,
  onResumeEng,
  onAiSearch,
  onAiActiveChange,
  backendProjectPath,
  backendCwdPrefix,
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
  // Committed session-filter queries (most recent first) surfaced as
  // completion options in the filter field. Seeded from localStorage so
  // history survives reloads; kept in state so the dropdown re-renders
  // the moment a new query is committed.
  const [searchHistory, setSearchHistory] = useState<string[]>(() =>
    readSessionSearchHistory(),
  );
  // Which completion option is keyboard-highlighted (-1 = none). The
  // history dropdown owns arrow-key navigation while it's open so it
  // doesn't fight the session-list highlight.
  const [historyHighlight, setHistoryHighlight] = useState(-1);
  // Set to briefly suppress the dropdown right after a commit/pick so
  // it doesn't immediately reopen over the field the user just acted on.
  const [historyDismissed, setHistoryDismissed] = useState(false);
  const [orgPanel, setOrgPanel] = useState<"advanced" | null>(null);
  const [nowTick, setNowTick] = useState(0);
  const projectId = sessions.find((s) => s.cwd)?.cwd ?? "";
  const loadMoreSentinelRef = useRef<HTMLDivElement | null>(null);

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
  const [modelFacet, setModelFacet] = useState<string[]>([]);
  const [ackedOrganizationBySession, setAckedOrganizationBySession] = useState<
    Record<string, AckedSessionOrganization>
  >({});
  const [selectedFolderIds, setSelectedFolderIds] = useState<string[]>([]);
  const [selectedTagIds, setSelectedTagIds] = useState<string[]>([]);
  const [selectedProviderIds, setSelectedProviderIds] = useState<string[]>([]);
  const [selectedModelIds, setSelectedModelIds] = useState<string[]>([]);
  const [selectedModes, setSelectedModes] = useState<OrchestrationMode[]>([]);
  const [selectedSources, setSelectedSources] = useState<SessionSource[]>([]);
  const [fileEditModeFilter, setFileEditModeFilter] = useState<SessionFileEditModeFilter>("any");
  const [selectedSearchFields, setSelectedSearchFields] = useState<SessionSearchField[]>(SESSION_SEARCH_FIELDS);
  const [orgError, setOrgError] = useState<string | null>(null);

  // Search text + advanced filters are stored per project so switching
  // projects restores that project's own filter state instead of
  // leaking the previous project's filters. `null` sentinel forces the
  // load to run on mount (an empty-string project path is itself a
  // valid bucket key for "no project selected").
  const filtersProjectKeyRef = useRef<string | null>(null);
  useEffect(() => {
    const key = backendProjectPath || SESSION_FILTERS_NO_PROJECT_KEY;
    if (filtersProjectKeyRef.current === key) return;
    filtersProjectKeyRef.current = key;
    const stored = readSessionFiltersByProject()[key];
    setSearch(stored?.search ?? "");
    setShowArchived(stored?.showArchived ?? false);
    setSelectedFolderIds(stored?.selectedFolderIds ?? []);
    setSelectedTagIds(stored?.selectedTagIds ?? []);
    setSelectedProviderIds(stored?.selectedProviderIds ?? []);
    setSelectedModelIds(stored?.selectedModelIds ?? []);
    setSelectedModes(stored?.selectedModes ?? []);
    setSelectedSources(stored?.selectedSources ?? []);
    setFileEditModeFilter(stored?.fileEditModeFilter ?? "any");
    setSelectedSearchFields(stored?.selectedSearchFields ?? SESSION_SEARCH_FIELDS);
  }, [backendProjectPath]);
  useEffect(() => {
    const key = filtersProjectKeyRef.current;
    if (key === null) return;
    writeSessionFiltersForProject(key, {
      search,
      showArchived,
      selectedFolderIds,
      selectedTagIds,
      selectedProviderIds,
      selectedModelIds,
      selectedModes,
      selectedSources,
      fileEditModeFilter,
      selectedSearchFields,
    });
  }, [
    search,
    showArchived,
    selectedFolderIds,
    selectedTagIds,
    selectedProviderIds,
    selectedModelIds,
    selectedModes,
    selectedSources,
    fileEditModeFilter,
    selectedSearchFields,
  ]);
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
    const previous = sessionSort;
    setSessionSort(next);
    try {
      await runThreeStateSync({
        operationId: "preferences:session-sort",
        action: t("session.sortBy"),
        reconcile: () => setSessionSort(previous),
        mutate: async () => {
          const res = await fetch(`${API}/api/user-prefs`, {
            method: "PATCH",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ session_sort: next }),
          });
          if (!res.ok) throw new Error("session_sort patch failed");
        },
      });
    } catch { /* canonical sync reconciles */ }
  }, [sessionSort, t]);

  // Status-bucket sort toggle — orthogonal to the timestamp sort above.
  // Backend pref `session_status_sort` is the source of truth.
  const [sessionStatusSort, setSessionStatusSort] = useState(false);
  useEffect(() => {
    let cancelled = false;
    const apply = (v: unknown) => {
      if (!cancelled && typeof v === "boolean") setSessionStatusSort(v);
    };
    fetch(`${API}/api/user-prefs`, { credentials: "include" })
      .then((r) => r.json())
      .then((data: { session_status_sort?: unknown }) => apply(data.session_status_sort))
      .catch(() => {});
    const off = eventBus.subscribe(
      "user_prefs_changed",
      (p) => apply((p as { session_status_sort?: unknown }).session_status_sort),
    );
    return () => {
      cancelled = true;
      off();
    };
  }, []);
  const toggleSessionStatusSort = useCallback(() => {
    const previous = sessionStatusSort;
    const next = !previous;
    setSessionStatusSort(next);
    void runThreeStateSync({
      operationId: "preferences:session-status-sort",
      action: t("session.sortBy"),
      reconcile: () => setSessionStatusSort(previous),
      mutate: async () => {
          const res = await fetch(`${API}/api/user-prefs`, {
            method: "PATCH",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ session_status_sort: next }),
          });
          if (!res.ok) throw new Error("session_status_sort patch failed");
      },
    }).catch(() => {});
  }, [sessionStatusSort, t]);

  const toggleFolderView = useCallback(async () => {
    const next = !folderViewEnabled;
    setFolderViewEnabled(next); // optimistic → backendFilters change → refetch
    try {
      await runThreeStateSync({
        operationId: "preferences:folder-view",
        action: t("session.folder"),
        reconcile: () => setFolderViewEnabled(!next),
        mutate: async () => {
          const res = await fetch(`${API}/api/user-prefs`, { method: "PATCH", credentials: "include", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ folder_view_enabled: next }) });
          if (!res.ok) throw new Error("folder_view_enabled patch failed");
        },
      });
    } catch {
      setFolderViewEnabled(!next); // revert — pref is the authority
    }
  }, [folderViewEnabled, t]);

  // Folders render unless explicitly disabled. `undefined` (pref not yet
  // loaded) defaults to showing folders, matching the backend pref default.
  const showFolders = folderViewEnabled !== false;
  // When sessions are grouped (by folder or by status), the group
  // boundaries reflow as each page arrives, so a scroll-triggered fetch
  // feels unpredictable — auto-load-on-scroll is disabled and an explicit
  // "Load more" button is rendered instead.
  const isGroupedView = showFolders || sessionStatusSort;
  // Pagination via a bottom sentinel observed against the scroll
  // container. The sidebar — not .session-list-items — is the scroll
  // element (the whole menu scrolls as one column), so an Intersection
  // Observer rooted on the sidebar fires regardless of which ancestor
  // actually scrolls. rootMargin prefetches the next page ~160px early.
  useEffect(() => {
    if (isGroupedView || !hasMore || loadingMore || !onLoadMore) return;
    const node = loadMoreSentinelRef.current;
    if (!node) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) onLoadMore();
      },
      { root: node.closest(".sidebar"), rootMargin: "160px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [isGroupedView, hasMore, loadingMore, onLoadMore]);
  const handleItemsScroll = useCallback((e: ReactUIEvent<HTMLDivElement>) => {
    if (isGroupedView || !hasMore || loadingMore || !onLoadMore) return;
    const el = e.currentTarget;
    if (el.scrollHeight - el.scrollTop - el.clientHeight <= 160) onLoadMore();
  }, [isGroupedView, hasMore, loadingMore, onLoadMore]);

  // ── AI search state (transient UI per CLAUDE.md rule 3). No
  // localStorage, no backend persistence — discarded on unmount and
  // on Clear. `aiResult.rows` is the relevance-ranked result list
  // returned by the backend; `filtered` reconciles it to drive
  // the sidebar list. There is NO separate AI-mode input: the ✨ button
  // runs an AI search over whatever is already typed in the single
  // filter box (`search`).
  const [aiLoading, setAiLoading] = useState(false);
  const [aiResult, setAiResult] = useState<{
    ids: string[];
    rows: Session[];
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
  const itemsScrollRef = useRef<HTMLDivElement | null>(null);
  const prevFirstIdRef = useRef<string | null>(null);
  const prevIdsRef = useRef<Set<string>>(new Set());

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
  const [selectedSessionIds, setSelectedSessionIds] = useState<Set<string>>(new Set());
  const [bulkSelectMode, setBulkSelectMode] = useState(false);
  const [bulkFolderPopover, setBulkFolderPopover] = useState<PopoverAnchor | null>(null);
  const [bulkTagPopover, setBulkTagPopover] = useState<PopoverAnchor | null>(null);

  const refreshOrganization = useCallback(async () => {
    if (!projectId) {
      setFolders([]);
      setTags([]);
      setModelFacet([]);
      return;
    }
    try {
      const snapshot = await fetchSessionOrganization(projectId);
      setFolders(
        [...snapshot.folders].sort(sortFolders),
      );
      setTags([...snapshot.tags].sort((a, b) => a.name.localeCompare(b.name)));
      setModelFacet([...(snapshot.models ?? [])].sort((a, b) => a.localeCompare(b)));
      setOrgError(null);
    } catch (err) {
      setOrgError(err instanceof Error ? err.message : "Failed to load organization");
    }
  }, [projectId]);

  useEffect(() => {
    void refreshOrganization();
  }, [refreshOrganization]);

  useEffect(() => {
    if (Object.keys(ackedOrganizationBySession).length === 0) return;
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
  }, [ackedOrganizationBySession, sessions]);

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
  // Unified option list for the tag-filter autocomplete: manual tags and
  // requirement tags share one `selectedTagIds` set, so both are keyed the
  // same way they're matched during selection.
  const tagFilterOptions = useMemo<TagFilterOption[]>(
    () => [
      ...tags.map((tag) => ({ key: tag.id, label: tag.name, kind: "manual" as const })),
      ...requirementTagOptions.map((tag) => ({
        key: reqTagKey(tag),
        label: tag.label,
        kind: tag.kind,
        title: `${tag.kind}: ${tag.label}`,
      })),
    ],
    [tags, requirementTagOptions],
  );
  const toggleTagFilter = useCallback((id: string) => {
    setSelectedTagIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    );
  }, []);
  const toggleFolderFilter = useCallback((id: string) => {
    setSelectedFolderIds((prev) =>
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
  const toggleSearchField = useCallback((field: SessionSearchField) => {
    setSelectedSearchFields((prev) =>
      prev.includes(field) ? prev.filter((x) => x !== field) : [...prev, field],
    );
  }, []);
  const clearAdvancedFilters = useCallback(() => {
    setShowArchived(false);
    setSelectedFolderIds([]);
    setSelectedTagIds([]);
    setSelectedProviderIds([]);
    setSelectedModelIds([]);
    setSelectedModes([]);
    setSelectedSources([]);
    setFileEditModeFilter("any");
    setSelectedSearchFields(SESSION_SEARCH_FIELDS);
  }, []);

  useEffect(() => {
    const validFolders = new Set(folders.map((folder) => folder.id));
    const nextFolderIds = selectedFolderIds.filter((id) => validFolders.has(id));
    if (nextFolderIds.length !== selectedFolderIds.length) {
      setSelectedFolderIds(nextFolderIds);
    }
    const validTagIds = new Set<string>([
      ...tags.map((tag) => tag.id),
      ...requirementTagOptions.map(reqTagKey),
    ]);
    const nextTagIds = selectedTagIds.filter((id) => validTagIds.has(id));
    if (nextTagIds.length !== selectedTagIds.length) {
      setSelectedTagIds(nextTagIds);
    }
  }, [folders, tags, requirementTagOptions, selectedFolderIds, selectedTagIds]);

  // Filter option universes are the FULL set of choices, independent of the
  // currently-loaded (filtered) sessions — so applying one filter never makes
  // the other filters' options collapse and the panel jump. Providers/modes/
  // sources are closed sets known client-side; models come from the backend
  // facet (distinct models across all the project's sessions).
  const providerOptions = useMemo(
    () => {
      const names = new Map(providers.map((provider) => [provider.id, provider.name]));
      for (const session of sessions) {
        const id = session.provider_id?.trim();
        if (id && !names.has(id)) names.set(id, id);
      }
      return Array.from(names, ([id, name]) => ({ id, name }))
        .sort((a, b) => a.name.localeCompare(b.name));
    },
    [providers, sessions],
  );
  const modelOptions = useMemo(() => {
    const models = new Set(modelFacet);
    for (const provider of providers) {
      if (provider.default_model) models.add(provider.default_model);
      for (const model of provider.custom_models ?? []) {
        if (model) models.add(model);
      }
    }
    for (const session of sessions) {
      if (session.model) models.add(session.model);
    }
    return Array.from(models).sort((a, b) => a.localeCompare(b));
  }, [modelFacet, providers, sessions]);
  const modeOptions = useMemo(
    () => ["team", "native", "virtual"] as OrchestrationMode[],
    [],
  );
  const sourceOptions = SESSION_SOURCES;
  const activeSources = useMemo(() => {
    const valid = new Set(sourceOptions);
    return selectedSources.filter((id) => valid.has(id));
  }, [sourceOptions, selectedSources]);
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
    selectedFolderIds.length > 0 ||
    selectedTagIds.length > 0 ||
    activeProviderIds.length > 0 ||
    activeModelIds.length > 0 ||
    activeModes.length > 0 ||
    activeSources.length > 0 ||
    fileEditModeFilter !== "any";
  const searchQueryActive = Boolean(search.trim());
  const searchStatusLoading = searching && searchQueryActive;
  const searchExpanded = Boolean(search || searchFocused);

  // Completion options for the filter field: up to 5 most-recent history
  // entries that fit the typed text. Only surfaced while the field is
  // focused and not just-dismissed.
  const searchHistorySuggestions = useMemo(
    () => matchingSessionSearchHistory(searchHistory, search),
    [searchHistory, search],
  );
  const showSearchHistory =
    searchFocused &&
    !historyDismissed &&
    searchHistorySuggestions.length > 0 &&
    !aiResult &&
    !aiLoading;

  const backendFilters = useMemo<SessionListFilters>(
    () => ({
      projectPath: aiResult ? "" : backendProjectPath || "",
      cwdPrefix: aiResult ? "" : backendCwdPrefix || "",
      search,
      searchFields: selectedSearchFields,
      showArchived,
      folderIds: selectedFolderIds,
      folderView: folderViewEnabled,
      sortBy: sessionSort,
      statusSort: sessionStatusSort,
      tagIds: selectedTagIds,
      providerIds: activeProviderIds,
      modelIds: activeModelIds,
      modes: activeModes,
      sources: activeSources,
      fileEditMode: fileEditModeFilter,
    }),
    [
      activeModelIds,
      activeModes,
      activeProviderIds,
      activeSources,
      aiResult,
      backendProjectPath,
      backendCwdPrefix,
      fileEditModeFilter,
      folderViewEnabled,
      sessionSort,
      sessionStatusSort,
      search,
      selectedSearchFields,
      selectedFolderIds,
      selectedTagIds,
      showArchived,
    ],
  );

  const backendFiltersKey = useMemo(
    () => JSON.stringify(backendFilters),
    [backendFilters],
  );
  const lastBackendFiltersKeyRef = useRef<string | null>(null);
  useEffect(() => {
    if (lastBackendFiltersKeyRef.current === backendFiltersKey) return;
    lastBackendFiltersKeyRef.current = backendFiltersKey;
    onBackendFiltersChange?.(backendFilters);
  }, [backendFilters, backendFiltersKey, onBackendFiltersChange]);

  const applyAckedOrganization = useCallback((
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
  }, [setAckedOrganizationBySession]);

  const moveToFolder = useCallback(async (sessionId: string, folderId: string | null) => {
    try {
      const { result } = await runThreeStateSync({
        operationId: `session:organization:folder:${sessionId}`,
        action: t("session.folder"),
        reconcile: refreshOrganization,
        mutate: () => updateSessionOrganization(sessionId, { folder_id: folderId }),
        isAcknowledged: (response) => response.session_id === sessionId,
      });
      applyAckedOrganization(sessionId, result.organization);
    } catch (err) {
      setOrgError(err instanceof Error ? err.message : "Failed to move session");
    }
  }, [applyAckedOrganization, refreshOrganization, t]);
  const moveSelectedToFolder = async (folderId: string | null) => {
    await Promise.all(selectedSessions.map((session) => moveToFolder(session.id, folderId)));
    setBulkFolderPopover(null);
  };

  const createAndAssignFolder = useCallback(async (sessionId: string, name: string) => {
    const trimmed = name.trim();
    if (!trimmed || !projectId) return;
    try {
      const { result: folder } = await runThreeStateSync({
        operationId: `session:organization:create-folder:${projectId}`,
        action: t("session.folder"),
        reconcile: refreshOrganization,
        mutate: () => createSessionFolder(projectId, trimmed),
      });
      const { result } = await runThreeStateSync({
        operationId: `session:organization:folder:${sessionId}`,
        action: t("session.folder"),
        reconcile: refreshOrganization,
        mutate: () => updateSessionOrganization(sessionId, { folder_id: folder.id }),
        isAcknowledged: (response) => response.session_id === sessionId,
      });
      applyAckedOrganization(sessionId, result.organization);
      await refreshOrganization();
    } catch (err) {
      setOrgError(err instanceof Error ? err.message : "Failed to create folder");
    }
  }, [projectId, applyAckedOrganization, refreshOrganization, t]);

  const setSessionTags = useCallback(async (sessionId: string, tagIds: string[]) => {
    try {
      const { result } = await runThreeStateSync({
        operationId: `session:organization:tags:${sessionId}`,
        action: t("session.tags"),
        reconcile: refreshOrganization,
        mutate: () => updateSessionOrganization(sessionId, { tag_ids: tagIds }),
        isAcknowledged: (response) => response.session_id === sessionId,
      });
      applyAckedOrganization(sessionId, result.organization);
    } catch (err) {
      setOrgError(err instanceof Error ? err.message : "Failed to update tags");
    }
  }, [applyAckedOrganization, refreshOrganization, t]);
  const toggleSelectedTag = async (tagId: string) => {
    const remove = selectedTagIdsForBulk.has(tagId);
    await Promise.all(
      selectedSessions.map((session) => {
        const ids = new Set(sessionTagIds(session));
        if (remove) ids.delete(tagId);
        else ids.add(tagId);
        return setSessionTags(session.id, Array.from(ids));
      }),
    );
  };

  // Create a project tag and assign it to the session in one step (the
  // inline "Create" affordance in the tag popover). The WS broadcast +
  // refreshOrganization bring both the tag pool and the session's tags
  // back in sync; nothing is held as frontend state.
  const createAndAssignTag = useCallback(async (sessionId: string, name: string) => {
    const trimmed = name.trim();
    if (!trimmed || !projectId) return;
    try {
      const { result: tag } = await runThreeStateSync({
        operationId: `session:organization:create-tag:${projectId}`,
        action: t("session.tags"),
        reconcile: refreshOrganization,
        mutate: () => createSessionTag(trimmed, projectId),
      });
      const { result } = await runThreeStateSync({
        operationId: `session:organization:tags:${sessionId}`,
        action: t("session.tags"),
        reconcile: refreshOrganization,
        mutate: () => updateSessionOrganization(sessionId, { add_tag_ids: [tag.id] }),
        isAcknowledged: (response) => response.session_id === sessionId,
      });
      applyAckedOrganization(sessionId, result.organization);
      await refreshOrganization();
    } catch (err) {
      setOrgError(err instanceof Error ? err.message : "Failed to create tag");
    }
  }, [projectId, applyAckedOrganization, refreshOrganization, t]);
  const createAndAssignSelectedTag = async (name: string) => {
    const trimmed = name.trim();
    if (!trimmed || !projectId || selectedSessions.length === 0) return;
    try {
      const tag = await createSessionTag(trimmed, projectId);
      await Promise.all(
        selectedSessions.map((session) => {
          const ids = new Set(sessionTagIds(session));
          ids.add(tag.id);
          return setSessionTags(session.id, Array.from(ids));
        }),
      );
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
      setAiResult({ ids: [], rows: [], reasoning: result.reasoning });
      return;
    }
    setAiResult({
      ids: result.results.map((session) => session.id),
      rows: result.results,
      reasoning: result.reasoning,
    });
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

  // Record the current filter text into search history. Called when the
  // user commits a query — leaving the field or acting on the list —
  // not on every keystroke, so history holds intentional searches only.
  const commitSearchHistory = useCallback((raw: string) => {
    if (!raw.trim()) return;
    setSearchHistory(pushSessionSearchHistory(raw));
  }, []);

  // Fill the filter field from a picked completion, commit it to history
  // (bumping recency), and close the dropdown. Reverts any stale AI
  // result since the query text changed.
  const applySearchHistory = useCallback(
    (entry: string) => {
      setSearch(entry);
      commitSearchHistory(entry);
      setHistoryHighlight(-1);
      setHistoryDismissed(true);
      if (aiResult || aiError) clearAiSearch();
    },
    [aiResult, aiError, commitSearchHistory],
  );

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
    const startedAt = performance.now();
    const source = aiResult && allSessions ? allSessions : sessions;
    const base = source.map((session) => {
      const acked = ackedOrganizationBySession[session.id];
      return acked ? { ...session, ...acked } : session;
    });
    const pool = base;
    if (aiResult) {
      // Resolve each ranked id against the live loaded pool first (kept
      // fresh by WS deltas), falling back to the backend-built row for
      // matches outside the paginated pool. Never intersect with the
      // pool alone — it's a partial projection of the full corpus.
      const byId = new Map<string, Session>();
      aiResult.rows.forEach((row) => {
        const acked = ackedOrganizationBySession[row.id];
        byId.set(row.id, acked ? { ...row, ...acked } : row);
      });
      pool.forEach((s) => {
        if (byId.has(s.id)) byId.set(s.id, s);
      });
      const aiFiltered = aiResult.ids
        .map((id) => byId.get(id))
        .filter((s): s is Session => !!s);
      logTiming("session-list", "filter_sessions", startedAt, {
        source: source.length,
        filtered: aiFiltered.length,
        ai: true,
      }, 25);
      return { filtered: aiFiltered, scoreMap: new Map<string, number>() };
    }
    const result = {
      filtered: pool,
      scoreMap: new Map(
        pool
          .map((s): [string, number] => [s.id, Number(s.search_score) || 0])
          .filter(([, score]) => score > 0),
      ),
    };
    logTiming("session-list", "filter_sessions", startedAt, {
      source: source.length,
      filtered: result.filtered.length,
      score_entries: result.scoreMap.size,
      ai: false,
    }, 25);
    return result;
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
    const startedAt = performance.now();
    // The selected session is shown only in the pinned anchor above the
    // toolbar, so drop it from the list pool. Its sub-sessions reparent
    // to root via the orphan branch below.
    const pool = currentSessionId
      ? filtered.filter((s) => s.id !== currentSessionId)
      : filtered;
    const byId = new Map<string, Session>();
    for (const s of pool) byId.set(s.id, s);
    const childMap = new Map<string, Session[]>();
    const rootList: Session[] = [];
    for (const s of pool) {
      const pid = s.parent_session_id;
      if (pid && byId.has(pid)) {
        const arr = childMap.get(pid) ?? [];
        arr.push(s);
        childMap.set(pid, arr);
      } else {
        rootList.push(s);
      }
    }
    logTiming("session-list", "tree_build", startedAt, {
      filtered: filtered.length,
      roots: rootList.length,
      parent_entries: childMap.size,
    }, 25);
    return { roots: rootList, childrenByParent: childMap };
  }, [filtered, currentSessionId]);
  const selectableSessionIds = useMemo(
    () => new Set(filtered.map((session) => session.id)),
    [filtered],
  );
  useEffect(() => {
    if (selectedSessionIds.size === 0) return;
    const next = new Set<string>();
    for (const id of selectedSessionIds) {
      if (selectableSessionIds.has(id)) next.add(id);
    }
    if (next.size !== selectedSessionIds.size) {
      setSelectedSessionIds(next);
    }
  }, [selectableSessionIds, selectedSessionIds]);

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
  const [collapsedStatusGroups, setCollapsedStatusGroups] = useLocalStorage<number[]>(
    "better-agent-collapsed-status-groups",
    [],
  );
  const collapsedStatusGroupRanks = useMemo(
    () => new Set(collapsedStatusGroups),
    [collapsedStatusGroups],
  );
  const toggleStatusGroup = useCallback(
    (rank: number) => {
      setCollapsedStatusGroups((prev) =>
        prev.includes(rank) ? prev.filter((r) => r !== rank) : [...prev, rank],
      );
    },
    [setCollapsedStatusGroups],
  );
  const { folderRoots, unfiledSessions } = useMemo(
    () => {
      const startedAt = performance.now();
      const result = buildFolderRenderTree(folders, roots);
      logTiming("session-list", "folder_tree", startedAt, {
        folders: folders.length,
        roots: roots.length,
        folder_roots: result.folderRoots.length,
        unfiled: result.unfiledSessions.length,
      }, 25);
      return result;
    },
    [folders, roots],
  );
  const sortedRoots = useMemo(
    () => {
      const startedAt = performance.now();
      const result = [
        ...flattenFolderSessions(folderRoots, collapsedFolderIds),
        ...unfiledSessions,
      ];
      logTiming("session-list", "flatten_folders", startedAt, {
        folder_roots: folderRoots.length,
        collapsed: collapsedFolderIds.size,
        unfiled: unfiledSessions.length,
        sorted: result.length,
      }, 25);
      return result;
    },
    [folderRoots, collapsedFolderIds, unfiledSessions],
  );
  const visibleSelectionIds = useMemo(
    () => (folderViewEnabled !== false ? sortedRoots : roots).map((session) => session.id),
    [folderViewEnabled, sortedRoots, roots],
  );
  const selectedSessions = useMemo(() => {
    const byId = new Map(filtered.map((session) => [session.id, session]));
    return Array.from(selectedSessionIds)
      .map((id) => byId.get(id))
      .filter((session): session is Session => Boolean(session));
  }, [filtered, selectedSessionIds]);
  const selectedCount = selectedSessions.length;
  const selectedTagIdsForBulk = useMemo(() => {
    if (selectedSessions.length === 0) return new Set<string>();
    const [first, ...rest] = selectedSessions;
    const common = new Set(sessionTagIds(first));
    for (const session of rest) {
      const ids = new Set(sessionTagIds(session));
      for (const id of Array.from(common)) {
        if (!ids.has(id)) common.delete(id);
      }
    }
    return common;
  }, [selectedSessions]);
  const selectedFolderIdForBulk = useMemo(() => {
    if (selectedSessions.length === 0) return null;
    const first = selectedSessions[0].folder_id ?? null;
    return selectedSessions.every((session) => (session.folder_id ?? null) === first)
      ? first
      : null;
  }, [selectedSessions]);
  const allSelectedArchived = useMemo(
    () => selectedSessions.length > 0 && selectedSessions.every((session) => session.archived),
    [selectedSessions],
  );
  const toggleSelectedSession = useCallback((id: string) => {
    setSelectedSessionIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      if (next.size === 0) setBulkSelectMode(false);
      return next;
    });
  }, []);
  const startBulkSelect = useCallback((id: string) => {
    setBulkSelectMode(true);
    setSelectedSessionIds((current) => {
      if (current.has(id)) return current;
      const next = new Set(current);
      next.add(id);
      return next;
    });
  }, []);
  const selectVisibleSessions = useCallback(() => {
    setBulkSelectMode(true);
    setSelectedSessionIds((current) => {
      const next = new Set(current);
      for (const id of visibleSelectionIds) next.add(id);
      return next;
    });
  }, [visibleSelectionIds]);
  const clearSelectedSessions = useCallback(() => {
    setBulkSelectMode(false);
    setSelectedSessionIds(new Set());
    setBulkFolderPopover(null);
    setBulkTagPopover(null);
  }, []);
  /** Toggle selection for a whole group (folder subtree or status bucket):
   * selects every id when any are unselected, deselects all when the whole
   * group is already selected. */
  const toggleGroupSelection = useCallback((ids: string[]) => {
    if (ids.length === 0) return;
    setBulkSelectMode(true);
    setSelectedSessionIds((current) => {
      const allSelected = ids.every((id) => current.has(id));
      const next = new Set(current);
      for (const id of ids) {
        if (allSelected) next.delete(id);
        else next.add(id);
      }
      if (next.size === 0) setBulkSelectMode(false);
      return next;
    });
  }, []);
  const archiveSelectedSessions = useCallback(() => {
    if (selectedSessions.length === 0) return;
    // Snapshot the target state once so a mixed selection archives every
    // session in one pass instead of each row toggling its own state. Pending
    // archives are still sent through so the existing archive handler can
    // cancel their grace timer instead of leaving an accidental archive queued.
    const nextArchived = !allSelectedArchived;
    for (const session of selectedSessions) {
      if (!session.archivePending && session.archived === nextArchived) continue;
      onArchive(session.id, nextArchived);
    }
    clearSelectedSessions();
  }, [selectedSessions, allSelectedArchived, onArchive, clearSelectedSessions]);

  // Keep the highlight valid: clear it if its row got filtered out, but
  // do NOT auto-set it — the highlight only appears after the first
  // ArrowDown/Up key press.
  useEffect(() => {
    if (sortedRoots.length === 0) {
      if (highlightedSessionId !== null) setHighlightedSessionId(null);
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
      const highlighted = sortedRoots.find((s) => s.id === highlightedSessionId);
      onSelect(highlightedSessionId, highlighted);
      return;
    }
  };

  const copyId = useCallback(
    async (id: string) => {
      await copyToClipboard(id);
      setCopiedId(id);
      window.setTimeout(() => {
        setCopiedId((prev) => (prev === id ? null : prev));
      }, 1200);
    },
    [setCopiedId],
  );

  // Scroll the list back to the top when a NEW session becomes the
  // first row (e.g. a freshly created session prepended to the list).
  // Keyed off the actually-rendered order — `roots` in flat view, the
  // flattened folder tree in folder view — so it matches what the user
  // sees. Only fires when the topmost id changed to one that wasn't
  // present before, so reordering existing sessions never yanks scroll.
  const renderedOrder = showFolders ? sortedRoots : roots;
  useEffect(() => {
    const firstId = renderedOrder[0]?.id ?? null;
    const prevFirst = prevFirstIdRef.current;
    const prevIds = prevIdsRef.current;
    prevFirstIdRef.current = firstId;
    prevIdsRef.current = new Set(renderedOrder.map((s) => s.id));
    if (!firstId || firstId === prevFirst) return;
    if (prevIds.has(firstId)) return;
    (itemsScrollRef.current?.closest(".sidebar") as HTMLElement | null)?.scrollTo({ top: 0 });
  }, [renderedOrder]);

  // Status-grouped headers only apply to the flat (non-folder) view — folder
  // grouping already provides its own sections.
  const statusGroupRuns = useMemo(
    () => (!showFolders && sessionStatusSort ? groupSessionsByStatusRank(roots) : null),
    [showFolders, sessionStatusSort, roots],
  );

  // Single SessionNode factory so the in-list rows and the pinned
  // selected-session anchor share one prop set.
  const renderNode = (
    s: Session,
    depth: number,
    dragEnabled: boolean,
    childrenMap: Map<string, Session[]> = childrenByParent,
  ) => (
    <SessionNode
      key={s.id}
      session={s}
      depth={depth}
      nowTick={nowTick}
      dragEnabled={dragEnabled}
      currentSessionId={currentSessionId}
      highlightedSessionId={highlightedSessionId}
      childrenByParent={childrenMap}
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
      onMoveToProject={onMoveToProject}
      onWorkerEligible={onWorkerEligible}
      onAgentRenameAllowed={onAgentRenameAllowed}
      teamWorkersBySession={teamWorkersBySession}
      onWorkerCreationPolicyChange={onWorkerCreationPolicyChange}
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
      selected={selectedSessionIds.has(s.id)}
      bulkSelectMode={bulkSelectMode}
      onToggleSelected={toggleSelectedSession}
      onStartBulkSelect={startBulkSelect}
    />
  );

  // Pinned anchor: the currently-selected session, shown above the
  // toolbar when it belongs to the current backend-filtered result set.
  const selectedSession =
    (currentSessionId &&
      (filtered.find((s) => s.id === currentSessionId) ??
        (!searchQueryActive &&
        selectedSessionProp?.id === currentSessionId &&
        (!selectedSessionProp.working_mode ||
          (selectedSessionProp.working_mode === "file_editing" &&
            selectedSessionProp.working_mode_meta?.persistent === true))
          ? selectedSessionProp
          : null))) ||
    null;

  return (
    <div className="session-list" data-testid="session-list">
      {selectedSession &&
        (selectedAnchorContainer
          ? createPortal(
              <div className="session-list-selected" data-testid="session-list-selected">
                {renderNode(selectedSession, 0, false, EMPTY_CHILDREN)}
              </div>,
              selectedAnchorContainer,
            )
          : (
            <div className="session-list-selected" data-testid="session-list-selected">
              {renderNode(selectedSession, 0, false, EMPTY_CHILDREN)}
            </div>
          ))}
      <div className="session-list-header">
        <div className="session-list-toolbar">
          <div className={`session-search${searchExpanded ? " expanded" : ""}`}>
            <div className="session-search-input-wrap">
              <Icon name="search" size={13} className="session-search-icon" />
              <SearchInput
                type="text"
                placeholder={search || searchFocused ? t("session.searchPlaceholder") : ""}
                value={search}
                aria-expanded={showSearchHistory}
                aria-controls="session-search-history-list"
                aria-autocomplete="list"
                onFocus={() => {
                  setSearchFocused(true);
                  // Reopen the completion dropdown on refocus.
                  setHistoryDismissed(false);
                }}
                onBlur={() => {
                  setSearchFocused(false);
                  setHistoryHighlight(-1);
                  // Leaving the field is a commit: record what was typed
                  // so it becomes a future completion option.
                  commitSearchHistory(search);
                }}
                onChange={(e) => {
                  setSearch(e.target.value);
                  // Typing reopens the dropdown and resets any highlight.
                  setHistoryDismissed(false);
                  setHistoryHighlight(-1);
                  // Editing the box reverts to live substring filtering —
                  // the AI result is stale the moment the query changes.
                  if (aiResult || aiError) clearAiSearch();
                }}
                onKeyDown={(e) => {
                  // While the completion dropdown is open it owns the
                  // arrow keys and Enter so it doesn't fight the session
                  // list's own highlight navigation.
                  if (showSearchHistory) {
                    if (e.key === "ArrowDown") {
                      e.preventDefault();
                      setHistoryHighlight((i) =>
                        i + 1 >= searchHistorySuggestions.length ? 0 : i + 1,
                      );
                      return;
                    }
                    if (e.key === "ArrowUp") {
                      e.preventDefault();
                      setHistoryHighlight((i) =>
                        i <= 0 ? searchHistorySuggestions.length - 1 : i - 1,
                      );
                      return;
                    }
                    if (e.key === "Enter" && historyHighlight >= 0) {
                      e.preventDefault();
                      applySearchHistory(searchHistorySuggestions[historyHighlight]);
                      return;
                    }
                    if (e.key === "Escape" && !search) {
                      // With an empty field, Escape just dismisses the
                      // history dropdown. When text is present, fall
                      // through to the existing Escape-to-clear behavior
                      // below so search history doesn't regress it.
                      e.preventDefault();
                      setHistoryDismissed(true);
                      setHistoryHighlight(-1);
                      return;
                    }
                  }
                  if (e.key === "Escape" && search) {
                    e.preventDefault();
                    setSearch("");
                    setHistoryDismissed(true);
                    setHistoryHighlight(-1);
                    clearAiSearch();
                    return;
                  }
                  if (e.key === "Enter") {
                    // Committing via Enter records the query too.
                    commitSearchHistory(search);
                  }
                  handleSearchKeyDown(e);
                }}
                aria-label={t("session.searchPlaceholder")}
              />
              {showSearchHistory && (
                <ul
                  id="session-search-history-list"
                  className="session-search-history"
                  role="listbox"
                  aria-label={t("session.searchHistoryLabel")}
                >
                  {searchHistorySuggestions.map((entry, idx) => (
                    <li key={entry} role="presentation">
                      <button
                        type="button"
                        role="option"
                        aria-selected={idx === historyHighlight}
                        className={`session-search-history-item${idx === historyHighlight ? " highlighted" : ""}`}
                        // preventDefault keeps the input focused so the
                        // pick fires before the blur-commit tears down.
                        onMouseDown={(e) => {
                          e.preventDefault();
                          applySearchHistory(entry);
                        }}
                        onMouseEnter={() => setHistoryHighlight(idx)}
                      >
                        <Icon name="search" size={12} className="session-search-history-icon" />
                        <span className="session-search-history-text">{entry}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
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
                    <span className="ai-search-toggle-icon"><Icon name="assistant-start" size={14} /></span>
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
                  t("session.aiSearchMatches", { count: aiResult.rows.length })}
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
                {t("session.aiSearchMatches", { count: aiResult.rows.length })}
              </div>
            </div>
          </div>
        </div>
      )}
      <LayoutGroup>
      <div
        ref={itemsScrollRef}
        className="session-list-items"
        onScroll={handleItemsScroll}
        onDragStart={(e) => {
          if (isSessionDrag(e)) setIsDraggingSession(true);
        }}
        onDragEnd={() => {
          setIsDraggingSession(false);
          setNewFolderDragOver(false);
          eventBus.publish("session_drag_end", {});
        }}
      >
        {orgPanel === "advanced" && (
          <div className="session-org-bar session-advanced-filter-bar">
            <div className="session-filter-sections">
              <section className="session-filter-section">
                <div className="session-filter-section-title">{t("session.globalFilters")}</div>
                <div className="session-filter-section-body">
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
                    <div className="session-filter-label" title={t("session.groupByStatusHint")}>
                      {t("session.groupByStatus")}
                    </div>
                    <div className="session-tag-filter">
                      <button
                        type="button"
                        className={`session-tag-toggle ${sessionStatusSort ? "active" : ""}`}
                        aria-pressed={sessionStatusSort}
                        title={t("session.groupByStatusHint")}
                        onClick={toggleSessionStatusSort}
                      >
                        {t("session.groupByStatusOn")}
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
                </div>
              </section>
              {(folders.length > 0 || tags.length > 0 || requirementTagOptions.length > 0) && (
                <section className="session-filter-section">
                  <div className="session-filter-section-title">{t("session.projectFilters")}</div>
                  <div className="session-filter-section-body">
                    {folders.length > 0 && (
                      <div className="session-filter-group">
                        <div className="session-filter-label">{t("session.folder")}</div>
                        <div className="session-tag-filter">
                          <button
                            type="button"
                            className={`session-tag-toggle ${selectedFolderIds.length === 0 ? "active" : ""}`}
                            aria-pressed={selectedFolderIds.length === 0}
                            onClick={() => setSelectedFolderIds([])}
                          >
                            {t("session.allFolders")}
                          </button>
                          {folders.map((folder) => {
                            const active = selectedFolderIds.includes(folder.id);
                            return (
                              <button
                                key={folder.id}
                                type="button"
                                className={`session-tag-toggle ${active ? "active" : ""}`}
                                aria-pressed={active}
                                onClick={() => toggleFolderFilter(folder.id)}
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
                        <TagFilterAutocomplete
                          options={tagFilterOptions}
                          selectedTagIds={selectedTagIds}
                          onToggle={toggleTagFilter}
                        />
                      </div>
                    )}
                  </div>
                </section>
              )}
            </div>
            {advancedFilterActive && (
              <button type="button" className="btn-small session-filter-clear" onClick={clearAdvancedFilters}>
                {t("session.clearFilters")}
              </button>
            )}
            {orgError && <div className="session-org-error">{orgError}</div>}
          </div>
        )}
        {selectedCount > 0 && (
          <div className="session-bulk-bar" data-testid="session-bulk-bar">
            <span className="session-bulk-count">
              {t("session.selectedCount", { count: selectedCount })}
            </span>
            <button type="button" className="btn-small" onClick={selectVisibleSessions}>
              {t("session.selectVisible")}
            </button>
            <button
              type="button"
              className="btn-small session-bulk-action"
              onClick={(e) => setBulkFolderPopover(e.currentTarget.getBoundingClientRect())}
            >
              <Icon name="folder" size={12} />
              <span>{t("session.folder")}</span>
            </button>
            <button
              type="button"
              className="btn-small session-bulk-action"
              onClick={(e) => setBulkTagPopover(e.currentTarget.getBoundingClientRect())}
            >
              <Icon name="tag" size={12} />
              <span>{t("session.tags")}</span>
            </button>
            <button
              type="button"
              className="btn-small session-bulk-action"
              onClick={archiveSelectedSessions}
            >
              <Icon name="archive" size={12} />
              <span>
                {allSelectedArchived
                  ? t("session.unarchiveSelected")
                  : t("session.archiveSelected")}
              </span>
            </button>
            <button
              type="button"
              className="btn-small session-bulk-delete"
              onClick={() => {
                onDeleteMany(Array.from(selectedSessionIds));
                clearSelectedSessions();
              }}
            >
              <Icon name="trash" size={12} />
              <span>{t("session.deleteSelected")}</span>
            </button>
            <button type="button" className="btn-small" onClick={clearSelectedSessions}>
              {t("session.clearSelection")}
            </button>
          </div>
        )}
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
            nowTick={nowTick}
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
            onMoveToProject={onMoveToProject}
            onWorkerEligible={onWorkerEligible}
            onAgentRenameAllowed={onAgentRenameAllowed}
            teamWorkersBySession={teamWorkersBySession}
            onWorkerCreationPolicyChange={onWorkerCreationPolicyChange}
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
            selectedSessionIds={selectedSessionIds}
            bulkSelectMode={bulkSelectMode}
            onToggleSelected={toggleSelectedSession}
            onStartBulkSelect={startBulkSelect}
            onToggleGroupSelection={toggleGroupSelection}
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
        {statusGroupRuns
          ? statusGroupRuns.map((run) => {
              const collapsed = collapsedStatusGroupRanks.has(run.rank);
              const groupIds = run.sessions.map((s) => s.id);
              const groupSelectedCount = groupIds.reduce(
                (n, id) => n + (selectedSessionIds.has(id) ? 1 : 0),
                0,
              );
              const groupAllSelected = groupIds.length > 0 && groupSelectedCount === groupIds.length;
              return (
                <div
                  key={`status-group-${run.rank}`}
                  className="session-folder-section session-status-group-section"
                  data-testid="session-status-group-section"
                >
                  <button
                    type="button"
                    className="session-folder-heading session-status-group-heading"
                    onClick={() => toggleStatusGroup(run.rank)}
                    aria-expanded={!collapsed}
                  >
                    <Icon
                      name={collapsed ? "chevron-right" : "chevron-down"}
                      size={12}
                      className="session-folder-chevron"
                    />
                    <span>{t(statusGroupI18nKey(run.rank))}</span>
                    <span className="session-status-group-count">{run.sessions.length}</span>
                    {bulkSelectMode && groupIds.length > 0 && (
                      <label
                        className="session-group-select-all"
                        title={groupAllSelected ? t("session.deselectGroup") : t("session.selectGroup")}
                        aria-label={groupAllSelected ? t("session.deselectGroup") : t("session.selectGroup")}
                        onClick={(e) => e.stopPropagation()}
                      >
                        <input
                          type="checkbox"
                          checked={groupAllSelected}
                          ref={(el) => {
                            if (el) el.indeterminate = groupSelectedCount > 0 && !groupAllSelected;
                          }}
                          onChange={() => toggleGroupSelection(groupIds)}
                        />
                      </label>
                    )}
                  </button>
                  {!collapsed && run.sessions.map((s) => renderNode(s, 0, false))}
                </div>
              );
            })
          : (showFolders ? unfiledSessions : roots).map((s) =>
              renderNode(s, 0, showFolders),
            )}
        {searching && sessions.length === 0 && (
          <div className="session-list-loading">
            <span className="session-list-spinner" aria-hidden="true" />
            <span>{t("session.loading")}</span>
          </div>
        )}
        {!searching && sessions.length === 0 && (
          <div className="session-empty">
            {advancedFilterActive || searchQueryActive
              ? t("session.noSessionsForFilter")
              : t("session.noSessions")}
          </div>
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
        {hasMore && !loadingMore && isGroupedView && (
          <button
            type="button"
            className="load-older-link session-list-load-more-btn"
            onClick={() => onLoadMore?.()}
          >
            {t("session.loadMore")}
          </button>
        )}
        {hasMore && !loadingMore && !isGroupedView && (
          <div
            ref={loadMoreSentinelRef}
            className="session-list-more"
            aria-hidden="true"
          />
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
      {bulkFolderPopover && (
        <SessionFolderPopover
          anchor={bulkFolderPopover}
          folders={folders}
          assignedFolderId={selectedFolderIdForBulk}
          onSelect={(folderId) => void moveSelectedToFolder(folderId)}
          onClose={() => setBulkFolderPopover(null)}
        />
      )}
      {bulkTagPopover && (
        <SessionTagPopover
          anchor={bulkTagPopover}
          tags={tags}
          assignedTagIds={selectedTagIdsForBulk}
          onToggle={(tagId) => void toggleSelectedTag(tagId)}
          onCreateTag={(name) => void createAndAssignSelectedTag(name)}
          onClose={() => setBulkTagPopover(null)}
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
