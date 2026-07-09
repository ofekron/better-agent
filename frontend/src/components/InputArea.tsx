import { useState, useRef, useCallback, useEffect, useLayoutEffect, useMemo, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useViewport } from "../hooks/useViewport";
import { useLocalStorage } from "../hooks/useLocalStorage";
import Icon from "./Icon";
import { ExtensionModuleSlot, useExtensionFrontendModules } from "./ExtensionSlots";
import type { CapabilityContext, PastedImage, FileAttachment } from "../types";
import { fileToPastedImage, imageFilesFromClipboard } from "../utils/imageAttach";
import { mergeIncomingImages } from "../utils/shareAttach";
import { fileToAttachment } from "../utils/fileAttach";
import { linkifyFilePaths } from "../utils/linkifyFilePaths";
import { splitPreview, applyQueuedEdit } from "../utils/queuedPreview";
import {
  AtMentionDropdown,
  buildMentionItems,
  formatMentionInsert,
  type MentionItem,
} from "./AtMentionDropdown";
import { ScheduleSendPopover, type ScheduleSendPayload } from "./ScheduleSendPopover";
import type { NodeSnapshot, Project, Session } from "../types";

export type { PastedImage, FileAttachment } from "../types";

export type PromptMentionPart =
  | { kind: "text"; text: string }
  | { kind: "mention"; text: string; mentionKind: MentionItem["kind"] };

export function splitPromptMentionParts(
  text: string,
  mentionItems: MentionItem[],
): PromptMentionPart[] {
  if (!text) return [];
  const candidates = Array.from(
    new Map(
      mentionItems
        .map((item) => [formatMentionInsert(item), item.kind] as const)
        .filter(([insert]) => insert.length > 0),
    ).entries(),
  )
    .map(([insert, mentionKind]) => ({ insert, mentionKind }))
    .sort((a, b) => b.insert.length - a.insert.length);
  if (candidates.length === 0) return [{ kind: "text", text }];

  const parts: PromptMentionPart[] = [];
  let cursor = 0;
  while (cursor < text.length) {
    let next:
      | { index: number; insert: string; mentionKind: MentionItem["kind"] }
      | null = null;
    for (const candidate of candidates) {
      const index = text.indexOf(candidate.insert, cursor);
      if (index < 0) continue;
      if (
        next === null ||
        index < next.index ||
        (index === next.index && candidate.insert.length > next.insert.length)
      ) {
        next = { index, insert: candidate.insert, mentionKind: candidate.mentionKind };
      }
    }
    if (next === null) {
      parts.push({ kind: "text", text: text.slice(cursor) });
      break;
    }
    if (next.index > cursor) {
      parts.push({ kind: "text", text: text.slice(cursor, next.index) });
    }
    parts.push({ kind: "mention", text: next.insert, mentionKind: next.mentionKind });
    cursor = next.index + next.insert.length;
  }
  return parts;
}

interface Props {
  onSend: (prompt: string, images: PastedImage[], files: FileAttachment[]) => boolean | Promise<boolean>;
  onSteer?: (prompt: string, images: PastedImage[], files: FileAttachment[]) => boolean | Promise<boolean>;
  onInterrupt?: (prompt: string, images: PastedImage[], files: FileAttachment[]) => boolean | Promise<boolean>;
  canSteer?: boolean;
  isStreaming: boolean;
  disabled: boolean;
  tagCount?: number;
  draft: string;
  onDraftChange: (value: string) => void;
  onEngineer?: (draft: string) => void;
  onSendToNewSession?: (prompt: string, images: PastedImage[], files: FileAttachment[]) => boolean | Promise<boolean>;
  onFork?: (prompt: string, images: PastedImage[]) => boolean | Promise<boolean>;
  canFork?: boolean;
  forkTargetLabel?: string;
  queuedPrompt: { id: string; preview: string; images?: PastedImage[]; imagesCount?: number; files?: FileAttachment[]; filesCount?: number } | null;
  queuedPrompts?: { id: string; preview: string; images?: PastedImage[]; imagesCount?: number; files?: FileAttachment[]; filesCount?: number }[];
  onPromoteQueued: (queuedId?: string) => void;
  onPromoteQueuedMulti?: (queuedIds: string[]) => void;
  onSteerQueued?: (queuedId?: string) => void;
  onCancelQueued?: (queuedId?: string) => void;
  onQueuedTextEdit?: (text: string, queuedId?: string) => void;
  onQueuedEditStart?: (queuedId?: string) => void;
  onQueuedEditFinish?: (queuedId?: string) => void;
  onReviewLastWork?: () => void;
  sendTarget?: "worker" | "supervisor";
  onSendTargetChange?: (target: "worker" | "supervisor") => void;
  /** Current session ID — changing it triggers auto-focus. */
  sessionId?: string;
  /** Schedule the draft as a future prompt instead of sending now.
   * Returns true once the backend acknowledges the created schedule. */
  onSchedule?: (payload: ScheduleSendPayload) => Promise<boolean> | boolean;
  /** Persisted attachments restored from the backend on session switch. */
  draftImages?: PastedImage[];
  /** Persist image changes to the backend. */
  onImagesChange?: (images: PastedImage[], text: string) => void;
  /** Whether supervisor split view is active. */
  supervisorEnabled?: boolean;
  /** Flip the supervisor toggle. */
  onToggleSupervisor?: (enabled: boolean) => void;
  /** Reopen the supervisor prompt modal to edit the custom prompt
   *  while supervisor is already enabled. */
  onEditSupervisorPrompt?: () => void;
  /** Graduate the supervisor's claude session into a new native BC root
   *  and re-back the supervisor on this session as a fork of the
   *  graduated session. Only meaningful when `supervisorEnabled` and
   *  the session is idle. */
  onSeparateSupervisor?: () => void;
  /** Save the current draft as a note for later. */
  onAddNote?: (text: string) => void;
  /** Attach selected capabilities to the next sent prompt only. */
  onAddCapabilityToNextTurn?: () => void;
  nextTurnCapabilities?: CapabilityContext[];
  onRemoveNextTurnCapability?: (sourceId: string) => void;
  /** Move the queued prompt to notes instead. */
  onQueuedToNote?: (text: string, queuedId: string) => void;
  /** Interrupt the active streaming turn. */
  onStop?: () => void;
  /** Stop request was sent but not yet acknowledged. */
  isStopping?: boolean;
  /** Fires when the textarea gains or loses focus. */
  onFocusChange?: (focused: boolean) => void;
  /** Projects available for @mention. */
  projects?: Project[];
  /** Sessions available for @mention. */
  sessions?: Session[];
  /** Node the user is currently on. Items from other nodes show a badge. */
  currentNodeId?: string;
  /** Machine snapshots for resolving node_id → display name. */
  machines?: NodeSnapshot[];
  headerNode?: ReactNode;
  overflowPanelNode?: ReactNode;
}

export function InputArea({
  onSend,
  onSteer,
  onInterrupt,
  canSteer = false,
  isStreaming: _isStreaming,
  disabled,
  tagCount = 0,
  draft,
  onDraftChange,
  onEngineer,
  onSendToNewSession,
  onFork,
  canFork = false,
  forkTargetLabel,
  queuedPrompt,
  queuedPrompts,
  onPromoteQueued,
  onPromoteQueuedMulti,
  onSteerQueued,
  onCancelQueued,
  onQueuedTextEdit,
  onQueuedEditStart,
  onQueuedEditFinish,
  onReviewLastWork,
  sendTarget,
  onSendTargetChange,
  sessionId,
  onSchedule,
  draftImages,
  onImagesChange,
  supervisorEnabled,
  onToggleSupervisor,
  onEditSupervisorPrompt,
  onSeparateSupervisor,
  onAddNote,
  onAddCapabilityToNextTurn,
  nextTurnCapabilities = [],
  onRemoveNextTurnCapability,
  onQueuedToNote,
  onStop,
  isStopping,
  onFocusChange,
  projects = [],
  sessions = [],
  currentNodeId = "primary",
  machines = [],
  headerNode,
  overflowPanelNode,
}: Props) {
  const { t } = useTranslation();
  const overflowMenuModules = useExtensionFrontendModules("input-overflow-menu");
  const composerActionModules = useExtensionFrontendModules("composer-actions");
  const viewport = useViewport();
  // On touch-class viewports the soft keyboard's Enter typically
  // means newline. Sending requires the explicit Send button so the
  // user can compose multi-line prompts without surprise submits.
  const enterIsNewline = viewport.mode !== "desktop";
  const compactActionMenus = viewport.mode === "mobile";
  const visibleQueuedPrompts = queuedPrompts ?? (queuedPrompt ? [queuedPrompt] : []);
  // Persisted display preference: collapse the whole queued-prompts list to a
  // one-line "n queued prompts" summary strip. Defaults collapsed on mobile
  // where vertical space is scarce; a stored preference always wins.
  const [queueCollapsed, setQueueCollapsed] = useLocalStorage(
    "better-agent-queued-list-collapsed",
    viewport.mode === "mobile",
  );
  // Multi-select for bulk queue actions (cancel/interrupt). Not persisted —
  // resets on remount; pruned below whenever the underlying queue changes so
  // stale ids (already sent/cancelled elsewhere) never linger in a selection.
  const [selectedQueuedIds, setSelectedQueuedIds] = useState<Set<string>>(new Set());
  useEffect(() => {
    setSelectedQueuedIds((prev) => {
      if (prev.size === 0) return prev;
      const liveIds = new Set(visibleQueuedPrompts.map((item) => item.id));
      const next = new Set([...prev].filter((id) => liveIds.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [visibleQueuedPrompts]);
  const toggleQueuedSelect = useCallback((id: string) => {
    setSelectedQueuedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);
  const [images, setImagesLocal] = useState<PastedImage[]>([]);
  const [files, setFiles] = useState<FileAttachment[]>([]);
  const [menuOpen, setMenuOpen] = useState(false);
  const [scheduleOpen, setScheduleOpen] = useState(false);
  const [focusModalOpen, setFocusModalOpen] = useState(false);
  const overflowTriggerRef = useRef<HTMLButtonElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const focusTextareaRef = useRef<HTMLTextAreaElement>(null);
  const highlightRef = useRef<HTMLDivElement>(null);
  const attachmentInputRef = useRef<HTMLInputElement>(null);
  const submitInFlightRef = useRef(false);

  // Local draft state for instant keystroke feedback. The textarea is
  // driven by this local state; parent state updates are debounced in
  // App.tsx (applySessionMetadata only fires every ~300 ms).
  const [localDraft, setLocalDraft] = useState(draft);
  const [prevDraft, setPrevDraft] = useState(draft);
  const lastSyncedRef = useRef(draft);
  // Counts local keystrokes not yet reflected in the parent draft prop.
  // Used by the sync logic to avoid overwriting mid-composition.
  const pendingLocalSeq = useRef(0);
  const localDraftRef = useRef(draft);
  // "Ignore one stale echo" guard. After an optimistic local clear (send /
  // fork), the parent's debounced onDraftChange — queued before the send
  // fired — can still land during the in-flight onSend with the value the
  // user just sent. Without this guard, the gDSFP sync below would
  // resurrect the sent text into the textarea. The guard is cleared by
  // any other parent draft value or by the next user keystroke.
  //
  // STATE (not ref): the gDSFP block reads & clears it during render;
  // React's StrictMode double-renders would consume a ref-based guard on
  // the throwaway first render and leave the real render unguarded.
  // State setters are rolled back with discarded renders, so the guard
  // survives until the render actually commits.
  const [ignoreNextDraft, setIgnoreNextDraft] = useState<string | null>(null);

  // Sync external draft changes into local state. Uses the
  // getDerivedStateFromProps pattern (synchronous during render) so
  // the textarea updates in the same render cycle as the prop change.
  if (draft !== prevDraft) {
    setPrevDraft(draft);
    if (ignoreNextDraft !== null && draft === ignoreNextDraft) {
      // Stale debounced echo of just-sent text — drop, don't resurrect.
      setIgnoreNextDraft(null);
      lastSyncedRef.current = draft;
    } else {
      // Any other parent value invalidates the guard.
      if (ignoreNextDraft !== null) setIgnoreNextDraft(null);
      if (pendingLocalSeq.current > 0) {
        // Parent caught up to a previous value, but user has typed more since.
        pendingLocalSeq.current = 0;
        lastSyncedRef.current = draft;
      } else {
        setLocalDraft(draft);
        lastSyncedRef.current = draft;
      }
    }
  }

  // Keep the ref in sync for callbacks that need the latest text (e.g.
  // onImagesChange) without re-creating the callback on every keystroke.
  useEffect(() => { localDraftRef.current = localDraft; }, [localDraft]);

  const updateDraftText = useCallback((next: string) => {
    setLocalDraft(next);
    lastSyncedRef.current = next;
    pendingLocalSeq.current++;
    if (ignoreNextDraft !== null) setIgnoreNextDraft(null);
    onDraftChange(next);
  }, [ignoreNextDraft, onDraftChange]);

  // Wraps setImages to also persist to the backend. Pass `persist: false`
  // when restoring from backend (avoids echo) or clearing on send (parent
  // handles the clear via handleDraftClearImmediate).
  const setImages = useCallback((update: PastedImage[] | ((prev: PastedImage[]) => PastedImage[]), persist = true) => {
    setImagesLocal((prev) => {
      const next = typeof update === "function" ? update(prev) : update;
      if (persist) onImagesChange?.(next, localDraftRef.current);
      return next;
    });
  }, [onImagesChange]);

  // Resize the textarea to fit content whenever it changes.
  useLayoutEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    const style = window.getComputedStyle(el);
    const minHeight = Number.parseFloat(style.minHeight) || 40;
    const maxHeight = Number.parseFloat(style.maxHeight) || 200;
    el.style.height = "0px";
    const contentHeight = el.scrollHeight;
    el.style.height = `${Math.min(Math.max(contentHeight, minHeight), maxHeight)}px`;
    el.style.overflowY = contentHeight > maxHeight ? "auto" : "hidden";
  }, [localDraft, sessionId, disabled]);

  // Auto-focus the prompt input whenever the session changes or the
  // component first mounts with a valid session. Skips when the user
  // is already focused on another input (modal, dialog, etc.).
  //
  // Desktop-only: on mobile/tablet a programmatic focus() pops the
  // soft keyboard, which yanks the viewport, triggers visualViewport
  // reflow, and prevents the user from reading the chat they just
  // switched into. The touch user types via the Send button anyway,
  // so the auto-focus has no upside there.
  useEffect(() => {
    if (!sessionId) return;
    if (viewport.mode !== "desktop") return;
    if (focusModalOpen) return;
    const el = textareaRef.current;
    if (!el || el.disabled) return;
    const active = document.activeElement;
    if (active && active !== el && (active.tagName === "INPUT" || active.tagName === "TEXTAREA" || active.tagName === "SELECT")) return;
    el.focus();
  }, [sessionId, viewport.mode, disabled, focusModalOpen]);

  useEffect(() => {
    if (!focusModalOpen) return;
    requestAnimationFrame(() => {
      const el = focusTextareaRef.current;
      if (!el) return;
      el.focus();
      el.setSelectionRange(el.value.length, el.value.length);
    });
  }, [focusModalOpen]);


  // Close the overflow menu on outside clicks.
  useEffect(() => {
    if (!menuOpen) return;
    const handler = (e: MouseEvent) => {
      const wrapper = (e.target as HTMLElement).closest(".input-overflow-wrapper");
      if (!wrapper) setMenuOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [menuOpen]);

  // Restore persisted images when switching sessions, and drop any stale
  // "ignore one stale echo" guard left over from a send on the previous
  // session — otherwise a new session whose draft happens to equal the
  // just-sent text would have its draft silently dropped.
  useEffect(() => {
    if (draftImages !== undefined) setImages(draftImages, false);
    else setImages([], false);
    setFiles([]);
    setIgnoreNextDraft(null);
  }, [sessionId]); // eslint-disable-line react-hooks/exhaustive-deps

  // Reconcile draft_images injected WHILE this session is already mounted
  // (e.g. the OS share sheet attaching a screenshot to the open session).
  // The restore effect above only runs on session switch, so without this
  // an external inject would never surface. Additive only: appends prop
  // entries not already present, so it neither clobbers in-progress local
  // composition nor fights the send-clear path. Writes straight to local
  // state (no onImagesChange) since the parent already owns these values.
  const draftImagesKey = useMemo(
    () => draftImages?.map((i) => i.base64).join("\n") ?? "",
    [draftImages],
  );
  useEffect(() => {
    if (!draftImages || draftImages.length === 0) return;
    setImagesLocal((prev) => mergeIncomingImages(prev, draftImages));
  }, [draftImagesKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const canSend = (localDraft.trim() || images.length > 0 || files.length > 0 || tagCount > 0) && !disabled;
  const promptMentionItems = useMemo(
    () => buildMentionItems(projects, sessions),
    [projects, sessions],
  );
  const mentionParts = useMemo(
    () => splitPromptMentionParts(localDraft, promptMentionItems),
    [localDraft, promptMentionItems],
  );
  // @mention state
  const [mentionState, setMentionState] = useState<{
    triggerStart: number;
    query: string;
  } | null>(null);
  const [mentionAnchorRect, setMentionAnchorRect] = useState<DOMRect | undefined>();

  const closeMention = useCallback(() => setMentionState(null), []);

  // Close @mention on outside clicks (delayed to allow item click)
  useEffect(() => {
    if (!mentionState) return;
    const handler = (e: MouseEvent) => {
      const dropdown = (e.target as HTMLElement).closest(".at-mention-dropdown");
      if (!dropdown) setMentionState(null);
    };
    const timer = setTimeout(() => {
      document.addEventListener("mousedown", handler);
    }, 0);
    return () => {
      clearTimeout(timer);
      document.removeEventListener("mousedown", handler);
    };
  }, [mentionState]);

  const handleMentionSelect = useCallback(
    (item: MentionItem, triggerStart: number, triggerEnd: number) => {
      const insertion = formatMentionInsert(item);
      const before = localDraft.slice(0, triggerStart);
      const after = localDraft.slice(triggerEnd);
      const next = before + insertion + after;
      updateDraftText(next);
      setMentionState(null);
      // Restore focus + position cursor after insertion
      requestAnimationFrame(() => {
        const el = textareaRef.current;
        if (el) {
          el.focus();
          const pos = before.length + insertion.length;
          el.setSelectionRange(pos, pos);
        }
      });
    },
    [localDraft, updateDraftText],
  );

  // Insert text into the draft at the caret (or over the selection),
  // reusing the same setLocalDraft + pendingLocalSeq + onDraftChange path
  // as typing so the gDSFP draft-resync above never clobbers it.
  const insertDraftText = useCallback(
    (text: string) => {
      if (!text) return;
      const el = textareaRef.current;
      const start = el?.selectionStart ?? localDraft.length;
      const end = el?.selectionEnd ?? localDraft.length;
      const before = localDraft.slice(0, start);
      const after = localDraft.slice(end);
      const next = before + text + after;
      updateDraftText(next);
      requestAnimationFrame(() => {
        const node = textareaRef.current;
        if (node) {
          node.focus();
          const pos = before.length + text.length;
          node.setSelectionRange(pos, pos);
        }
      });
    },
    [localDraft, updateDraftText],
  );

  const replaceDraftText = useCallback(
    (text: string) => {
      updateDraftText(text);
      setMentionState(null);
      requestAnimationFrame(() => {
        const node = textareaRef.current;
        if (!node) return;
        node.focus();
        node.setSelectionRange(text.length, text.length);
      });
    },
    [updateDraftText],
  );

  const detectMention = useCallback(
    (text: string, cursorPos: number) => {
      // Scan backwards from cursor for an unbroken @ trigger
      let i = cursorPos - 1;
      while (i >= 0 && text[i] !== "@" && text[i] !== " " && text[i] !== "\n") {
        i--;
      }
      if (i < 0 || text[i] !== "@") {
        setMentionState(null);
        return;
      }
      const query = text.slice(i + 1, cursorPos);
      setMentionState({ triggerStart: i, query });
    },
    [],
  );

  const submitDraft = useCallback(async (
    submit: (prompt: string, images: PastedImage[], files: FileAttachment[]) => boolean | Promise<boolean>,
  ) => {
    if (submitInFlightRef.current) return;
    const trimmed = localDraft.trim();
    if ((!trimmed && images.length === 0 && files.length === 0 && tagCount === 0) || disabled) return;
    submitInFlightRef.current = true;
    // Optimistically clear — the text is committed to the message.
    // The parent's handleDraftClearImmediate will also clear the prop,
    // but that happens inside the async onSend chain which may not
    // resolve within the same act() tick in tests. Arm the stale-echo
    // guard so a parent debounce that was queued BEFORE this send
    // (carrying `trimmed`) doesn't land mid-await and resurrect the text.
    setLocalDraft("");
    lastSyncedRef.current = "";
    pendingLocalSeq.current = 0;
    setIgnoreNextDraft(trimmed);
    try {
      const sent = await submit(trimmed, images, files);
      if (sent) {
        setImages([], false);
        setFiles([]);
      } else {
        // Send failed — restore the draft so the user doesn't lose it.
        setLocalDraft(trimmed);
        lastSyncedRef.current = trimmed;
        setIgnoreNextDraft(null);
      }
    } finally {
      submitInFlightRef.current = false;
    }
  }, [localDraft, images, files, disabled, tagCount]);

  const handleSend = useCallback(() => {
    void submitDraft(onSend);
  }, [submitDraft, onSend]);

  const handleSteer = useCallback(() => {
    if (!onSteer || !canSteer) return;
    void submitDraft(onSteer);
  }, [submitDraft, onSteer, canSteer]);

  const handleInterrupt = useCallback(() => {
    if (!onInterrupt) return;
    void submitDraft(onInterrupt);
  }, [submitDraft, onInterrupt]);

  const handleFork = useCallback(async () => {
    if (!onFork) return;
    const trimmed = localDraft.trim();
    if (!trimmed || disabled || !canFork) return;
    setLocalDraft("");
    lastSyncedRef.current = "";
    pendingLocalSeq.current = 0;
    setIgnoreNextDraft(trimmed);
    const sent = await onFork(trimmed, images);
    if (sent) {
      setImages([], false);
      onDraftChange("");
    } else {
      setLocalDraft(trimmed);
      lastSyncedRef.current = trimmed;
      setIgnoreNextDraft(null);
    }
  }, [localDraft, images, disabled, onFork, canFork, onDraftChange]);

  const handleSendToNewSession = useCallback(() => {
    if (!onSendToNewSession) return;
    void submitDraft(onSendToNewSession);
  }, [onSendToNewSession, submitDraft]);

  const handleScheduleSubmit = useCallback(
    async (payload: ScheduleSendPayload): Promise<boolean> => {
      if (!onSchedule || !sessionId) return false;
      const ok = await onSchedule(payload);
      if (!ok) return false;
      // Mirror handleSend/handleFork: clear the draft once the backend
      // owns the schedule. Schedules are text-only; attachments stay.
      setLocalDraft("");
      lastSyncedRef.current = "";
      pendingLocalSeq.current = 0;
      setIgnoreNextDraft(payload.prompt);
      onDraftChange("");
      return true;
    },
    [onSchedule, sessionId, onDraftChange],
  );

  const handleKeyDown = (e: React.KeyboardEvent) => {
    // Desktop: Enter sends, Shift+Enter inserts a newline.
    // Mobile/tablet: Enter always inserts a newline so the soft
    // keyboard's return key doesn't surprise-submit; the user sends
    // via the explicit Send button.
    if (enterIsNewline) return;
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      const trimmed = localDraft.trim();
      if (!trimmed && images.length === 0 && files.length === 0 && tagCount === 0 && queuedPrompt) {
        if (canSteer && onSteerQueued && _isStreaming) onSteerQueued();
        else onPromoteQueued();
      } else {
        handleSend();
      }
    }
  };

  const handleInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const v = e.target.value;
    const cursorPos = e.target.selectionStart ?? v.length;
    updateDraftText(v);
    const el = e.target;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 200) + "px";
    detectMention(v, cursorPos);
    if (mentionAnchorRect === undefined && textareaRef.current) {
      setMentionAnchorRect(textareaRef.current.getBoundingClientRect());
    }
  };

  const handleTextareaScroll = (e: React.UIEvent<HTMLTextAreaElement>) => {
    const highlight = highlightRef.current;
    if (!highlight) return;
    highlight.scrollTop = e.currentTarget.scrollTop;
    highlight.scrollLeft = e.currentTarget.scrollLeft;
  };

  const addImageFile = useCallback((file: File) => {
    fileToPastedImage(file).then((image) => {
      setImages((prev) => [...prev, image]);
    });
  }, [setImages]);

  const handlePaste = useCallback(
    (e: React.ClipboardEvent) => {
      const files = imageFilesFromClipboard(e.clipboardData);
      if (files.length === 0) return;
      e.preventDefault();
      files.forEach(addImageFile);
    },
    [addImageFile]
  );

  const removeImage = useCallback((index: number) => {
    setImages((prev) => prev.filter((_, i) => i !== index));
  }, [setImages]);

  const removeFile = useCallback((index: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== index));
  }, []);

  const handleAttachmentChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const selected = Array.from(e.target.files || []);
      selected.forEach((f) => {
        if (f.type.startsWith("image/")) {
          fileToPastedImage(f).then((image) => {
            setImages((prev) => [...prev, image]);
          });
        } else {
          fileToAttachment(f).then((att) => {
            setFiles((prev) => [...prev, att]);
          });
        }
      });
      e.target.value = "";
    },
    [setImages]
  );

  // When nothing is streaming, the button is a plain "Send" — queue
  // is irrelevant. Only expose that distinction while a turn is active.
  const somethingRunning = _isStreaming;
  const steerIsPrimary = somethingRunning && canSteer && !!onSteer;
  const handlePrimarySend = steerIsPrimary ? handleSteer : handleSend;
  const primarySendLabel = steerIsPrimary
    ? t("input.steerButton")
    : somethingRunning
      ? t("input.queueSendButton")
      : t("input.sendButton");
  const primarySendTitle = steerIsPrimary ? t("input.steerTitle") : undefined;
  const handleFocusModalSend = useCallback(() => {
    setFocusModalOpen(false);
    handlePrimarySend();
  }, [handlePrimarySend]);
  const showMobileSteerActions = compactActionMenus && steerIsPrimary;
  const stopButton = somethingRunning && onStop ? (
    <button
      className={`stop-btn${isStopping ? " stopping" : ""}`}
      data-testid="stop-btn"
      onClick={isStopping ? undefined : onStop}
      disabled={!!isStopping}
      title={t("message.stopButton")}
      aria-label={t("message.stopButton")}
    >
      {isStopping ? (
        <span className="stop-btn-spinner" />
      ) : (
        <>
          <Icon name="x-circle" size={14} />
          <span className="stop-btn-label">{t("message.stopButton")}</span>
        </>
      )}
    </button>
  ) : null;

  return (
    <div className="input-area" data-testid="input-area">
      {headerNode}
      {images.length > 0 && (
        <div className="image-previews">
          {images.map((img, i) => (
            <div key={`img-${i}`} className="image-preview-item">
              <img src={img.dataUrl} alt={`Pasted image ${i + 1}`} />
              <button
                className="image-remove-btn"
                onClick={() => removeImage(i)}
                title={t("input.removeImageTitle")}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}
      {files.length > 0 && (
        <div className="file-previews">
          {files.map((f, i) => (
            <div key={`file-${i}`} className="file-preview-item">
              <span className="file-preview-name">{f.name}</span>
              <span className="file-preview-size">{formatFileSize(f.size)}</span>
              <button
                className="file-remove-btn"
                onClick={() => removeFile(i)}
                title={t("input.removeImageTitle")}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}
      {visibleQueuedPrompts.length > 0 && (
        <div
          className={`queued-list-header${queueCollapsed ? " is-collapsed" : ""}`}
          data-testid="queued-list-header"
        >
          <button
            className="queued-minimize-btn"
            type="button"
            data-testid="queued-list-toggle"
            title={queueCollapsed ? t("input.queuedListExpand") : t("input.queuedListCollapse")}
            aria-label={queueCollapsed ? t("input.queuedListExpand") : t("input.queuedListCollapse")}
            aria-expanded={!queueCollapsed}
            onClick={() => setQueueCollapsed(!queueCollapsed)}
          >
            <Icon name={queueCollapsed ? "chevron-right" : "chevron-down"} size={14} />
          </button>
          <span
            className="queued-list-summary"
            data-testid="queued-list-summary"
            role="button"
            tabIndex={0}
            onClick={() => setQueueCollapsed(!queueCollapsed)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") setQueueCollapsed(!queueCollapsed);
            }}
          >
            {t(
              visibleQueuedPrompts.length === 1
                ? "input.queuedListSummary_1"
                : "input.queuedListSummary_other",
              { count: visibleQueuedPrompts.length },
            )}
          </span>
          {visibleQueuedPrompts.length > 1 && (
            <div className="queued-list-bulk-actions" data-testid="queued-list-bulk-actions">
              {onCancelQueued && (
                <button
                  className="queued-cancel-btn"
                  type="button"
                  data-testid="queued-bulk-cancel"
                  onClick={() => {
                    if (selectedQueuedIds.size > 0) {
                      selectedQueuedIds.forEach((id) => onCancelQueued(id));
                      setSelectedQueuedIds(new Set());
                    } else {
                      onCancelQueued();
                    }
                  }}
                >
                  {selectedQueuedIds.size > 0
                    ? t("input.queuedCancelSelected", { count: selectedQueuedIds.size })
                    : t("input.queuedCancelAll")}
                </button>
              )}
              {onPromoteQueuedMulti && (
                <button
                  className="promote-btn interrupt"
                  type="button"
                  data-testid="queued-bulk-interrupt"
                  onClick={() => {
                    const ids = selectedQueuedIds.size > 0
                      ? [...selectedQueuedIds]
                      : visibleQueuedPrompts.map((item) => item.id);
                    onPromoteQueuedMulti(ids);
                    setSelectedQueuedIds(new Set());
                  }}
                >
                  {selectedQueuedIds.size > 0
                    ? t("input.queuedInterruptSelected", { count: selectedQueuedIds.size })
                    : t("input.queuedInterruptAll")}
                </button>
              )}
            </div>
          )}
        </div>
      )}
      {!queueCollapsed && visibleQueuedPrompts.map((item) => (
        <QueuedPromptBanner
          key={item.id}
          preview={item.preview}
          images={item.images}
          imagesCount={item.imagesCount}
          files={item.files}
          filesCount={item.filesCount}
          selectable={visibleQueuedPrompts.length > 1}
          selected={selectedQueuedIds.has(item.id)}
          onToggleSelect={() => toggleQueuedSelect(item.id)}
          onPromote={() => onPromoteQueued(item.id)}
          onSteer={canSteer && _isStreaming && onSteerQueued ? () => onSteerQueued(item.id) : undefined}
          onCancel={onCancelQueued ? () => onCancelQueued(item.id) : undefined}
          onEdit={onQueuedTextEdit ? (text) => onQueuedTextEdit(text, item.id) : undefined}
          onEditStart={onQueuedEditStart ? () => onQueuedEditStart(item.id) : undefined}
          onEditFinish={onQueuedEditFinish ? () => onQueuedEditFinish(item.id) : undefined}
          onSaveToNote={onQueuedToNote ? (text) => onQueuedToNote(text, item.id) : undefined}
          steerLabel={t("input.steerButton")}
          steerTitle={t("input.steerTitle")}
          interruptLabel={t("input.interruptButton")}
          interruptTitle={t("input.interruptTitle")}
          cancelLabel={t("app.cancel")}
          confirmLabel={t("app.confirm")}
          queuedLabel={t("input.queuedLabel")}
          editLabel={t("input.queuedEdit")}
          editTitle={t("input.queuedEditTitle")}
          moreActionsLabel={t("input.queuedMoreActions")}
          saveToNoteLabel={t("input.queuedSaveToNote")}
          minimizeLabel={t("input.queuedMinimize")}
          expandLabel={t("input.queuedExpand")}
          selectLabel={t("input.queuedSelect")}
          compactActions={compactActionMenus}
        />
      ))}
      {forkTargetLabel && (
        <div
          className="input-fork-target"
          title={t("input.forkTargetTitle")}
          data-testid="input-fork-target"
        >
          → <strong>{forkTargetLabel}</strong>
        </div>
      )}
      {showMobileSteerActions && (
        <div className="mobile-steer-actions" data-testid="mobile-steer-actions">
          {stopButton}
          <div className="mobile-steer-action-pair">
            <button
              onClick={handleSteer}
              disabled={!canSend}
              className="send-btn steer"
              data-testid="send-btn"
              title={primarySendTitle}
            >
              {t("input.steerButton")}
            </button>
            <button
              onClick={handleSend}
              disabled={!canSend}
              className="send-btn queue"
              data-testid="queue-btn"
            >
              {t("input.queueSendButton")}
            </button>
          </div>
        </div>
      )}
      <div className="input-row" style={{ position: "relative" }}>
        <div className="input-textarea-shell">
          {localDraft && (
            <div
              ref={highlightRef}
              className="input-prompt-highlight"
              aria-hidden="true"
              data-testid="input-mention-highlight"
            >
              {mentionParts.map((part, index) =>
                part.kind === "mention" ? (
                  <span
                    key={`${index}-${part.text}`}
                    className={`input-prompt-mention kind-${part.mentionKind}`}
                  >
                    {part.text}
                  </span>
                ) : (
                  <span key={`${index}-${part.text}`}>{part.text}</span>
                ),
              )}
              {localDraft.endsWith("\n") ? "\n" : null}
            </div>
          )}
          <textarea
            ref={textareaRef}
            data-testid="input-textarea"
            className={localDraft ? "input-prompt has-highlight" : "input-prompt"}
            value={localDraft}
            onChange={handleInput}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            onScroll={handleTextareaScroll}
            onFocus={() => {
              onFocusChange?.(true);
              if (textareaRef.current) {
                setMentionAnchorRect(textareaRef.current.getBoundingClientRect());
              }
            }}
            onBlur={() => onFocusChange?.(false)}
            placeholder={
              disabled
                ? t("input.placeholderDisabled")
                : t("input.placeholderActive")
            }
            disabled={disabled}
            rows={1}
          />
        </div>
        {mentionState && (
          <AtMentionDropdown
            query={mentionState.query}
            triggerStart={mentionState.triggerStart}
            projects={projects}
            sessions={sessions}
            onSelect={handleMentionSelect}
            onClose={closeMention}
            anchorRect={mentionAnchorRect}
            currentNodeId={currentNodeId}
            machines={machines}
          />
        )}
        <input
          ref={attachmentInputRef}
          type="file"
          multiple
          style={{ display: "none" }}
          onChange={handleAttachmentChange}
        />
        <button
          type="button"
          className="composer-focus-btn"
          data-testid="composer-focus-btn"
          onClick={() => setFocusModalOpen(true)}
          disabled={disabled}
          title={t("input.focusModeOpen")}
          aria-label={t("input.focusModeOpen")}
        >
          <Icon name="expand" size={16} />
        </button>
        {/* Composer-action modules (e.g. composer-fill) render inline on
            desktop; on mobile they move into the ⋯ overflow menu below. */}
        {!compactActionMenus && composerActionModules.map((module) => (
          <ExtensionModuleSlot
            key={`${module.extension_id}:${module.id}`}
            module={module}
            className="extension-module-slot--composer-actions"
            context={{
              sessionId,
              draft: localDraft,
              onInsertText: insertDraftText,
              onReplaceText: replaceDraftText,
              disabled,
              isStreaming: _isStreaming,
            }}
          />
        ))}
        {!showMobileSteerActions && (
          <button
            onClick={handlePrimarySend}
            disabled={!canSend}
            className={`send-btn${steerIsPrimary ? " steer" : somethingRunning ? " queue" : ""}`}
            data-testid="send-btn"
            title={primarySendTitle}
          >
            {primarySendLabel}
          </button>
        )}
        {steerIsPrimary && !compactActionMenus && (
          <button
            onClick={handleSend}
            disabled={!canSend}
            className="send-btn queue"
            data-testid="queue-btn"
          >
            {t("input.queueSendButton")}
          </button>
        )}
        {somethingRunning && onInterrupt && !compactActionMenus && (
          <button
            onClick={handleInterrupt}
            disabled={!canSend}
            className="send-btn interrupt"
            data-testid="interrupt-btn"
            title={t("input.interruptTitle")}
          >
            {t("input.interruptSendButton")}
          </button>
        )}
        {!showMobileSteerActions && stopButton}
        <div className="input-overflow-wrapper">
          <button
            ref={overflowTriggerRef}
            className="input-overflow-trigger"
            onClick={() => setMenuOpen((o) => !o)}
            title={t("app.moreActions")}
            aria-label={t("app.moreActions")}
          >
            ⋯
          </button>
          {menuOpen && (
            <div className="input-overflow-menu">
              {overflowPanelNode ? (
                <div className="input-overflow-panel">
                  {overflowPanelNode}
                </div>
              ) : null}
              {/* On mobile, composer-action modules (e.g. composer-fill) live
                  here rather than inline. Same context as the inline slot plus
                  closeMenu so a picked suggestion can also dismiss the menu. */}
              {compactActionMenus && composerActionModules.map((module) => (
                <ExtensionModuleSlot
                  key={`${module.extension_id}:${module.id}`}
                  module={module}
                  className="extension-module-slot--composer-actions"
                  context={{
                    sessionId,
                    draft: localDraft,
                    onInsertText: insertDraftText,
                    onReplaceText: replaceDraftText,
                    disabled,
                    isStreaming: _isStreaming,
                    closeMenu: () => setMenuOpen(false),
                  }}
                />
              ))}
              {compactActionMenus && steerIsPrimary && !showMobileSteerActions && (
                <button
                  className="overflow-menu-item"
                  data-testid="queue-btn"
                  onClick={() => { setMenuOpen(false); handleSend(); }}
                  disabled={!canSend}
                >
                  {t("input.queueSendButton")}
                </button>
              )}
              {compactActionMenus && somethingRunning && onInterrupt && (
                <button
                  className="overflow-menu-item"
                  data-testid="interrupt-btn"
                  onClick={() => { setMenuOpen(false); handleInterrupt(); }}
                  disabled={!canSend}
                  title={t("input.interruptTitle")}
                >
                  {t("input.interruptSendButton")}
                </button>
              )}
              <button
                className="overflow-menu-item"
                data-testid="composer-focus-menu-btn"
                onClick={() => {
                  setMenuOpen(false);
                  setFocusModalOpen(true);
                }}
                disabled={disabled}
              >
                <Icon name="expand" size={14} /> {t("input.focusModeOpen")}
              </button>
              <button
                className="overflow-menu-item"
                onClick={() => { attachmentInputRef.current?.click(); setMenuOpen(false); }}
                disabled={disabled}
              >
                <Icon name="paperclip" size={14} /> {t("input.attachTitle")}
              </button>
              {onAddNote && (
                <button
                  className="overflow-menu-item"
                  onClick={() => {
                    setMenuOpen(false);
                    if (!localDraft.trim()) return;
                    onAddNote(localDraft.trim());
                  }}
                  disabled={!localDraft.trim()}
                  data-testid="add-note-btn"
                >
                  <Icon name="memo" size={14} /> {t("input.addNote", "Save as note")}
                </button>
              )}
              {onSchedule && sessionId && (
                <button
                  className="overflow-menu-item"
                  data-testid="schedule-btn"
                  onClick={() => {
                    setMenuOpen(false);
                    setScheduleOpen(true);
                  }}
                  disabled={disabled || !localDraft.trim()}
                  title={t("schedule.scheduleSend")}
                >
                  <Icon name="clock" size={14} /> {t("schedule.scheduleSend")}
                </button>
              )}
              {onSendToNewSession && (
                <button
                  className="overflow-menu-item"
                  data-testid="send-to-new-session-btn"
                  onClick={() => {
                    setMenuOpen(false);
                    handleSendToNewSession();
                  }}
                  disabled={!canSend}
                >
                  <Icon name="folder-plus" size={14} /> {t("input.sendToNewSession")}
                </button>
              )}
              {onAddCapabilityToNextTurn && (
                <button
                  className="overflow-menu-item"
                  onClick={() => {
                    setMenuOpen(false);
                    onAddCapabilityToNextTurn();
                  }}
                  disabled={disabled}
                  data-testid="add-turn-capability-btn"
                >
                  <Icon name="assistant-start" size={14} /> Add capability to next turn
                </button>
              )}
              {onEngineer && (
                <button
                  className="overflow-menu-item"
                  data-testid="engineer-btn"
                  onClick={() => {
                    setMenuOpen(false);
                    if (disabled) return;
                    onEngineer(localDraft.trim());
                  }}
                  disabled={disabled}
                >
                  {t("input.engineerButton")}
                </button>
              )}
              {overflowMenuModules.map((module) => (
                <ExtensionModuleSlot
                  key={`${module.extension_id}:${module.id}`}
                  module={module}
                  className="extension-module-slot--overflow-menu"
                  context={{
                    disabled,
                    isStreaming: _isStreaming,
                    supervisorEnabled,
                    sendTarget,
                    onReviewLastWork,
                    onSendTargetChange,
                    onToggleSupervisor,
                    onEditPrompt: onEditSupervisorPrompt,
                    onSeparateSupervisor,
                    closeMenu: () => setMenuOpen(false),
                  }}
                />
              ))}
              {onFork && (
                <button
                  className="overflow-menu-item"
                  onClick={() => { setMenuOpen(false); handleFork(); }}
                  disabled={!localDraft.trim() || disabled || !canFork}
                  data-testid="fork-btn"
                >
                  {t("input.forkButton")}
                </button>
              )}
            </div>
          )}
          {scheduleOpen && onSchedule && sessionId && (
            <ScheduleSendPopover
              prompt={localDraft}
              anchorRef={overflowTriggerRef}
              onClose={() => setScheduleOpen(false)}
              onSchedule={handleScheduleSubmit}
            />
          )}
        </div>
      </div>
      {nextTurnCapabilities.length > 0 && (
        <div className="capability-context-list input-capability-context-list">
          {nextTurnCapabilities.map((capability) => (
            <span key={capability.source_id} className="capability-context-chip">
              {capability.name}
              {onRemoveNextTurnCapability && (
                <button
                  type="button"
                  onClick={() => onRemoveNextTurnCapability(capability.source_id)}
                  aria-label={`Remove ${capability.name}`}
                >
                  ×
                </button>
              )}
            </span>
          ))}
        </div>
      )}
      {focusModalOpen && (
        <div
          className="modal-overlay composer-focus-overlay"
          onClick={() => setFocusModalOpen(false)}
        >
          <div
            className="modal-content composer-focus-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="composer-focus-title"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal-header composer-focus-header">
              <h2 id="composer-focus-title">{t("input.focusModeTitle")}</h2>
              <button
                type="button"
                className="modal-close"
                onClick={() => setFocusModalOpen(false)}
                aria-label={t("common.close")}
              >
                ×
              </button>
            </div>
            <div className="modal-body composer-focus-body">
              <textarea
                ref={focusTextareaRef}
                className="composer-focus-textarea"
                data-testid="composer-focus-textarea"
                value={localDraft}
                onChange={(e) => updateDraftText(e.target.value)}
                onPaste={handlePaste}
                placeholder={
                  disabled
                    ? t("input.placeholderDisabled")
                    : t("input.placeholderActive")
                }
                disabled={disabled}
              />
            </div>
            <div className="modal-footer composer-focus-footer">
              <button
                type="button"
                className="secondary-btn"
                onClick={() => setFocusModalOpen(false)}
              >
                {t("common.close")}
              </button>
              <button
                type="button"
                className={`send-btn${steerIsPrimary ? " steer" : somethingRunning ? " queue" : ""}`}
                data-testid="composer-focus-send-btn"
                onClick={handleFocusModalSend}
                disabled={!canSend}
                title={primarySendTitle}
              >
                {primarySendLabel}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function QueuedTagCards({
  comments,
  collapsed = false,
}: {
  comments: ReturnType<typeof splitPreview>["comments"];
  collapsed?: boolean;
}) {
  if (comments.length === 0) return null;
  return (
    <div className={`queued-tags-cards${collapsed ? " is-collapsed" : ""}`}>
      {comments.map((c, i) => {
        const fileHeader =
          c.file && c.range ? `${c.file}:${c.range}` :
          c.file ? c.file : null;
        return (
          <div key={i} className="comment-card inline-tags-card queued-tag-card">
            {fileHeader && (
              <div className="inline-tags-card-anchor">{fileHeader}</div>
            )}
            {c.selected && (
              <div className="inline-tags-card-selected">{c.selected}</div>
            )}
            <div className="comment-card-comment">{c.comment}</div>
          </div>
        );
      })}
    </div>
  );
}

/** Editable banner showing a queued prompt. Click to expand an inline editor.
 *  When the preview contains an `<inline-tags>` envelope, renders the
 *  comments as stacked cards (same style as InlineTagsCards) and the
 *  remaining user text separately — so the user sees readable comment
 *  cards instead of raw XML. */
function QueuedPromptBanner({
  preview,
  images,
  imagesCount,
  files,
  filesCount,
  selectable = false,
  selected = false,
  onToggleSelect,
  onPromote,
  onSteer,
  onCancel,
  onEdit,
  onEditStart,
  onEditFinish,
  onSaveToNote,
  steerLabel,
  steerTitle,
  interruptLabel,
  interruptTitle,
  cancelLabel,
  confirmLabel,
  queuedLabel,
  editLabel,
  editTitle,
  moreActionsLabel,
  saveToNoteLabel,
  minimizeLabel,
  expandLabel,
  selectLabel,
  compactActions = false,
}: {
  preview: string;
  images?: PastedImage[];
  imagesCount?: number;
  files?: FileAttachment[];
  filesCount?: number;
  selectable?: boolean;
  selected?: boolean;
  onToggleSelect?: () => void;
  onPromote?: () => void;
  onSteer?: () => void;
  onCancel?: () => void;
  onEdit?: (text: string) => void;
  onEditStart?: () => void;
  onEditFinish?: () => void;
  onSaveToNote?: (text: string) => void;
  steerLabel: string;
  steerTitle: string;
  interruptLabel: string;
  interruptTitle: string;
  cancelLabel: string;
  confirmLabel: string;
  queuedLabel: string;
  editLabel: string;
  editTitle: string;
  moreActionsLabel: string;
  saveToNoteLabel: string;
  minimizeLabel: string;
  expandLabel: string;
  selectLabel?: string;
  compactActions?: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [actionsOpen, setActionsOpen] = useState(false);
  // Persisted display preference: once minimized, future queued prompts
  // honor the same choice (mirrors the sidebar-minimize pattern) so the
  // banner stays out of the way until the user expands it again.
  const [minimized, setMinimized] = useLocalStorage(
    "better-agent-queued-prompt-minimized",
    false,
  );
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const { comments, userText } = useMemo(() => splitPreview(preview), [preview]);
  const hasComments = comments.length > 0;
  // The editor operates on the same text the banner displays: when an
  // inline-tags envelope is present we show/edit only the user text and
  // re-attach the envelope on commit; otherwise the raw preview.
  const displayText = hasComments ? userText : preview;
  const [editText, setEditText] = useState(displayText);
  const onEditFinishRef = useRef(onEditFinish);

  useEffect(() => {
    onEditFinishRef.current = onEditFinish;
  }, [onEditFinish]);

  useEffect(() => {
    if (editing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [editing]);

  // Sync external preview changes into edit state when not actively editing
  useEffect(() => {
    if (!editing) setEditText(displayText);
  }, [displayText, editing]);

  useEffect(() => {
    if (!actionsOpen) return;
    const handler = (e: MouseEvent) => {
      const wrapper = (e.target as HTMLElement).closest(".queued-overflow-wrapper");
      if (!wrapper) setActionsOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [actionsOpen]);

  useEffect(() => {
    if (!editing) return;
    return () => onEditFinishRef.current?.();
  }, [editing]);

  const startEditing = useCallback(() => {
    setEditText(displayText);
    onEditStart?.();
    setEditing(true);
  }, [displayText, onEditStart]);

  const commitEdit = useCallback(() => {
    const trimmed = editText.trim();
    const next = hasComments ? applyQueuedEdit(preview, trimmed) : trimmed;
    if (trimmed && next !== preview && onEdit) {
      onEdit(next);
    }
    setEditing(false);
  }, [editText, hasComments, preview, onEdit]);

  const cancelEdit = useCallback(() => {
    setEditText(displayText);
    setEditing(false);
  }, [displayText]);

  const imageCount = images && images.length > 0 ? images.length : (imagesCount ?? 0);
  const fileCount = files && files.length > 0 ? files.length : (filesCount ?? 0);

  // Minimized view: a single-line strip that keeps the user aware a
  // prompt is queued (label + truncated preview + a count of any hidden
  // attachments/comments) and leaves the primary Interrupt action and an
  // expand toggle reachable, while hiding the bulky tag cards, attachment
  // thumbnails and inline editor. Takes priority over the editing view so
  // collapsing always wins.
  if (minimized) {
    const summaryBits: string[] = [];
    if (imageCount > 0) {
      summaryBits.push(`${imageCount} image${imageCount !== 1 ? "s" : ""}`);
    }
    if (fileCount > 0) {
      summaryBits.push(`${fileCount} file${fileCount !== 1 ? "s" : ""}`);
    }
    return (
      <div
        className={`queued-prompt-banner is-minimized${hasComments ? " has-tags" : ""}`}
        data-testid="queued-prompt-banner"
        data-minimized="true"
      >
        <div className="queued-prompt-header">
          {selectable && onToggleSelect && (
            <input
              type="checkbox"
              className="queued-select-checkbox"
              data-testid="queued-select-checkbox"
              checked={selected}
              onChange={onToggleSelect}
              aria-label={selectLabel}
              title={selectLabel}
            />
          )}
          <button
            className="queued-minimize-btn"
            type="button"
            data-testid="queued-expand-btn"
            title={expandLabel}
            aria-label={expandLabel}
            aria-expanded={false}
            onClick={() => setMinimized(false)}
          >
            <Icon name="chevron-right" size={14} />
          </button>
          <span className="queued-prompt-label">{queuedLabel}</span>
        </div>
        <QueuedTagCards comments={comments} collapsed />
        <span
          className="queued-prompt-preview"
          onClick={() => setMinimized(false)}
          title={expandLabel}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") setMinimized(false);
          }}
        >
          {linkifyFilePaths(displayText)}
        </span>
        {summaryBits.length > 0 && (
          <span className="queued-attachments-count" data-testid="queued-minimized-summary">
            {summaryBits.join(" · ")}
          </span>
        )}
        <div className="queued-prompt-actions">
          {compactActions ? null : (
            <>
              {onCancel && (
                <button className="queued-cancel-btn" onClick={onCancel}>
                  {cancelLabel}
                </button>
              )}
              {onSaveToNote && (
                <button
                  className="queued-note-btn"
                  onClick={() => onSaveToNote(preview)}
                  title={saveToNoteLabel}
                >
                  <Icon name="memo" size={15} />
                </button>
              )}
            </>
          )}
          {onSteer && (
            <button
              className="promote-btn"
              data-testid="queued-steer-btn"
              onClick={onSteer}
              title={steerTitle}
            >
              {steerLabel}
            </button>
          )}
          {onPromote && (
            <button
              className="promote-btn interrupt"
              data-testid="queued-interrupt-btn"
              onClick={onPromote}
              title={interruptTitle}
            >
              {interruptLabel}
            </button>
          )}
          {compactActions && (onCancel || onSaveToNote) && (
            <QueuedPromptOverflowMenu
              open={actionsOpen}
              setOpen={setActionsOpen}
              onCancel={onCancel}
              cancelLabel={cancelLabel}
              onSaveToNote={onSaveToNote ? () => onSaveToNote(preview) : undefined}
              moreActionsLabel={moreActionsLabel}
              saveToNoteLabel={saveToNoteLabel}
            />
          )}
        </div>
      </div>
    );
  }

  const hasImages = (images?.length ?? 0) > 0 || (imagesCount ?? 0) > 0;
  const hasFiles = (files?.length ?? 0) > 0 || (filesCount ?? 0) > 0;
  const editModal = editing ? (
    <div className="queued-edit-backdrop" role="presentation" onMouseDown={cancelEdit}>
      <div
        className="queued-edit-modal"
        role="dialog"
        aria-modal="true"
        aria-label={editLabel}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="queued-edit-modal-header">
          <div className="queued-edit-modal-title">{editLabel}</div>
          <button
            className="queued-minimize-btn"
            type="button"
            aria-label={cancelLabel}
            onClick={cancelEdit}
          >
            <Icon name="x" size={16} />
          </button>
        </div>
        <textarea
          ref={inputRef}
          className="queued-prompt-edit-input"
          value={editText}
          rows={8}
          onChange={(e) => setEditText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              commitEdit();
            } else if (e.key === "Escape") {
              cancelEdit();
            }
          }}
        />
        <div className="queued-prompt-actions queued-edit-modal-actions">
          <button className="queued-cancel-btn" type="button" onClick={cancelEdit}>
            {cancelLabel}
          </button>
          <button className="promote-btn" type="button" onClick={commitEdit}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  ) : null;

  return (
    <>
    <div className={`queued-prompt-banner${hasComments ? " has-tags" : ""}${hasImages || hasFiles ? " has-attachments" : ""}`} data-testid="queued-prompt-banner">
      <div className="queued-prompt-header">
        {selectable && onToggleSelect && (
          <input
            type="checkbox"
            className="queued-select-checkbox"
            data-testid="queued-select-checkbox"
            checked={selected}
            onChange={onToggleSelect}
            aria-label={selectLabel}
            title={selectLabel}
          />
        )}
        <span className="queued-prompt-label">{queuedLabel}</span>
        <button
          className="queued-minimize-btn"
          type="button"
          data-testid="queued-minimize-btn"
          title={minimizeLabel}
          aria-label={minimizeLabel}
          aria-expanded={true}
          onClick={() => setMinimized(true)}
        >
          <Icon name="chevron-down" size={14} />
        </button>
      </div>
      {(hasImages || hasFiles) && (
        <div className="queued-attachments">
          {images?.map((img, i) => (
            <div key={`q-img-${i}`} className="queued-image-thumb">
              <img src={img.dataUrl} alt={`Queued image ${i + 1}`} />
            </div>
          ))}
          {!images?.length && imagesCount && imagesCount > 0 && (
            <span className="queued-attachments-count">{imagesCount} image{imagesCount !== 1 ? "s" : ""}</span>
          )}
          {files?.map((f, i) => (
            <div key={`q-file-${i}`} className="queued-file-badge">
              <span className="queued-file-name">{f.name}</span>
              <span className="queued-file-size">{formatFileSize(f.size)}</span>
            </div>
          ))}
          {!files?.length && filesCount && filesCount > 0 && (
            <span className="queued-attachments-count">{filesCount} file{filesCount !== 1 ? "s" : ""}</span>
          )}
        </div>
      )}
      <QueuedTagCards comments={comments} />
      <span
        className="queued-prompt-preview"
        onClick={startEditing}
        title={editTitle}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") startEditing();
        }}
      >
        {linkifyFilePaths((hasComments ? userText : preview) || editLabel)}
      </span>
      <div className="queued-prompt-actions">
        {compactActions ? null : (
          <>
            {onCancel && (
              <button
                className="queued-cancel-btn"
                onClick={onCancel}
              >
                {cancelLabel}
              </button>
            )}
            {onSaveToNote && (
              <button
                className="queued-note-btn"
                onClick={() => onSaveToNote(preview)}
                title={saveToNoteLabel}
              >
                <Icon name="memo" size={15} />
              </button>
            )}
          </>
        )}
        {onSteer && (
          <button
            className="promote-btn"
            data-testid="queued-steer-btn"
            onClick={onSteer}
            title={steerTitle}
          >
            {steerLabel}
          </button>
        )}
        {onPromote && (
          <button
            className="promote-btn interrupt"
            data-testid="queued-interrupt-btn"
            onClick={onPromote}
            title={interruptTitle}
          >
            {interruptLabel}
          </button>
        )}
        {compactActions && (onCancel || onSaveToNote) && (
          <QueuedPromptOverflowMenu
            open={actionsOpen}
            setOpen={setActionsOpen}
            onCancel={onCancel}
            cancelLabel={cancelLabel}
            onSaveToNote={onSaveToNote ? () => onSaveToNote(preview) : undefined}
            moreActionsLabel={moreActionsLabel}
            saveToNoteLabel={saveToNoteLabel}
          />
        )}
      </div>
    </div>
    {editModal}
    </>
  );
}

function QueuedPromptOverflowMenu({
  open,
  setOpen,
  onCancel,
  cancelLabel,
  onSaveToNote,
  moreActionsLabel,
  saveToNoteLabel,
}: {
  open: boolean;
  setOpen: (open: boolean | ((open: boolean) => boolean)) => void;
  onCancel?: () => void;
  cancelLabel: string;
  onSaveToNote?: () => void;
  moreActionsLabel: string;
  saveToNoteLabel: string;
}) {
  return (
    <div className="queued-overflow-wrapper">
      <button
        className="queued-overflow-trigger"
        type="button"
        title={moreActionsLabel}
        aria-label={moreActionsLabel}
        onClick={() => setOpen((value) => !value)}
      >
        ⋯
      </button>
      {open && (
        <div className="queued-overflow-menu">
          {onCancel && (
            <button
              className="overflow-menu-item"
              onClick={() => {
                setOpen(false);
                onCancel();
              }}
            >
              {cancelLabel}
            </button>
          )}
          {onSaveToNote && (
            <button
              className="overflow-menu-item"
              onClick={() => {
                setOpen(false);
                onSaveToNote();
              }}
            >
              <Icon name="memo" size={14} /> {saveToNoteLabel}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
