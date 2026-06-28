import { useState, useRef, useCallback, useEffect, useMemo, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useViewport } from "../hooks/useViewport";
import Icon from "./Icon";
import { ExtensionModuleSlot, useExtensionFrontendModules } from "./ExtensionSlots";
import type { CapabilityContext, PastedImage, FileAttachment } from "../types";
import { fileToPastedImage, imageFilesFromClipboard } from "../utils/imageAttach";
import { mergeIncomingImages } from "../utils/shareAttach";
import { fileToAttachment } from "../utils/fileAttach";
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
  onFork?: (prompt: string, images: PastedImage[]) => boolean | Promise<boolean>;
  canFork?: boolean;
  forkTargetLabel?: string;
  queuedPrompt: { id: string; preview: string; images?: PastedImage[]; imagesCount?: number; files?: FileAttachment[]; filesCount?: number } | null;
  onPromoteQueued: () => void;
  onCancelQueued?: () => void;
  onQueuedTextEdit?: (text: string) => void;
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
  onQueuedToNote?: (text: string) => void;
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
  onFork,
  canFork = false,
  forkTargetLabel,
  queuedPrompt,
  onPromoteQueued,
  onCancelQueued,
  onQueuedTextEdit,
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
  const [images, setImagesLocal] = useState<PastedImage[]>([]);
  const [files, setFiles] = useState<FileAttachment[]>([]);
  const [menuOpen, setMenuOpen] = useState(false);
  const [scheduleOpen, setScheduleOpen] = useState(false);
  const overflowTriggerRef = useRef<HTMLButtonElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
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
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 200) + "px";
  }, [localDraft]);

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
    const el = textareaRef.current;
    if (!el || el.disabled) return;
    const active = document.activeElement;
    if (active && active !== el && (active.tagName === "INPUT" || active.tagName === "TEXTAREA" || active.tagName === "SELECT")) return;
    el.focus();
  }, [sessionId, viewport.mode, disabled]);


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
      setLocalDraft(next);
      lastSyncedRef.current = next;
      pendingLocalSeq.current++;
      onDraftChange(next);
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
    [localDraft, onDraftChange],
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
      setLocalDraft(next);
      lastSyncedRef.current = next;
      pendingLocalSeq.current++;
      onDraftChange(next);
      requestAnimationFrame(() => {
        const node = textareaRef.current;
        if (node) {
          node.focus();
          const pos = before.length + text.length;
          node.setSelectionRange(pos, pos);
        }
      });
    },
    [localDraft, onDraftChange],
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
        onPromoteQueued();
      } else {
        if (canSteer && onSteer && _isStreaming) handleSteer();
        else handleSend();
      }
    }
  };

  const handleInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const v = e.target.value;
    const cursorPos = e.target.selectionStart ?? v.length;
    // Instant local update so the textarea never lags.
    setLocalDraft(v);
    lastSyncedRef.current = v;
    pendingLocalSeq.current++;
    // User typed — any in-flight stale-echo guard is moot.
    if (ignoreNextDraft !== null) setIgnoreNextDraft(null);
    // Notify parent immediately — App.tsx debounces applySessionMetadata.
    onDraftChange(v);
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
  const canShowSteerAction = somethingRunning && canSteer && !!onSteer;
  const primarySendLabel = somethingRunning
    ? t("input.queueSendButton")
    : t("input.sendButton");

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
      {queuedPrompt && (
        <QueuedPromptBanner
          preview={queuedPrompt.preview}
          images={queuedPrompt.images}
          imagesCount={queuedPrompt.imagesCount}
          files={queuedPrompt.files}
          filesCount={queuedPrompt.filesCount}
          onPromote={onPromoteQueued}
          onCancel={onCancelQueued!}
          onEdit={onQueuedTextEdit}
          onSaveToNote={onQueuedToNote ?? undefined}
          interruptLabel={t("input.interruptButton")}
          interruptTitle={t("input.interruptTitle")}
          cancelLabel={t("app.cancel")}
          queuedLabel={t("input.queuedLabel")}
          compactActions={compactActionMenus}
        />
      )}
      {forkTargetLabel && (
        <div
          className="input-fork-target"
          title={t("input.forkTargetTitle")}
          data-testid="input-fork-target"
        >
          → <strong>{forkTargetLabel}</strong>
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
        {composerActionModules.map((module) => (
          <ExtensionModuleSlot
            key={`${module.extension_id}:${module.id}`}
            module={module}
            className="extension-module-slot--composer-actions"
            context={{
              sessionId,
              draft: localDraft,
              onInsertText: insertDraftText,
              disabled,
              isStreaming: _isStreaming,
            }}
          />
        ))}
        <button
          onClick={handleSend}
          disabled={!canSend}
          className={`send-btn${somethingRunning ? " queue" : ""}`}
          data-testid="send-btn"
        >
          {primarySendLabel}
        </button>
        {canShowSteerAction && !compactActionMenus && (
          <button
            onClick={handleSteer}
            disabled={!canSend}
            className="send-btn steer"
            data-testid="steer-btn"
            title={t("input.steerTitle")}
          >
            {t("input.steerButton")}
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
        {somethingRunning && onStop && !compactActionMenus && (
          <button
            className={`stop-btn${isStopping ? " stopping" : ""}`}
            data-testid="stop-btn"
            onClick={isStopping ? undefined : onStop}
            disabled={!!isStopping}
            title={t("message.stopButton")}
          >
            {isStopping ? <span className="stop-btn-spinner" /> : t("message.stopButton")}
          </button>
        )}
        <div className="input-overflow-wrapper">
          <button
            ref={overflowTriggerRef}
            className="input-overflow-trigger"
            onClick={() => setMenuOpen((o) => !o)}
            title="More actions"
            aria-label="More actions"
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
              {compactActionMenus && canShowSteerAction && (
                <button
                  className="overflow-menu-item"
                  data-testid="steer-btn"
                  onClick={() => { setMenuOpen(false); handleSteer(); }}
                  disabled={!canSend}
                  title={t("input.steerTitle")}
                >
                  {t("input.steerButton")}
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
              {compactActionMenus && somethingRunning && onStop && (
                <button
                  className="overflow-menu-item"
                  data-testid="stop-btn"
                  onClick={() => {
                    setMenuOpen(false);
                    if (!isStopping) onStop();
                  }}
                  disabled={!!isStopping}
                  title={t("message.stopButton")}
                >
                  {isStopping ? t("message.stopButton") : t("message.stopButton")}
                </button>
              )}
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
                  <Icon name="sparkles" size={14} /> Add capability to next turn
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
    </div>
  );
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
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
  onPromote,
  onCancel,
  onEdit,
  onSaveToNote,
  interruptLabel,
  interruptTitle,
  cancelLabel,
  queuedLabel,
  compactActions = false,
}: {
  preview: string;
  images?: PastedImage[];
  imagesCount?: number;
  files?: FileAttachment[];
  filesCount?: number;
  onPromote: () => void;
  onCancel: () => void;
  onEdit?: (text: string) => void;
  onSaveToNote?: (text: string) => void;
  interruptLabel: string;
  interruptTitle: string;
  cancelLabel: string;
  queuedLabel: string;
  compactActions?: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [actionsOpen, setActionsOpen] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const { comments, userText } = useMemo(() => splitPreview(preview), [preview]);
  const hasComments = comments.length > 0;
  // The editor operates on the same text the banner displays: when an
  // inline-tags envelope is present we show/edit only the user text and
  // re-attach the envelope on commit; otherwise the raw preview.
  const displayText = hasComments ? userText : preview;
  const [editText, setEditText] = useState(displayText);

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

  const startEditing = useCallback(() => {
    setEditText(displayText);
    setEditing(true);
  }, [displayText]);

  const commitEdit = useCallback(() => {
    const trimmed = editText.trim();
    const next = hasComments ? applyQueuedEdit(preview, trimmed) : trimmed;
    if (trimmed && next !== preview && onEdit) {
      onEdit(next);
    }
    setEditing(false);
  }, [editText, hasComments, preview, onEdit]);

  if (editing) {
    return (
      <div className="queued-prompt-banner" data-testid="queued-prompt-banner">
        <span className="queued-prompt-label">{queuedLabel}</span>
        <textarea
          ref={inputRef}
          className="queued-prompt-edit-input"
          value={editText}
          rows={3}
          onChange={(e) => setEditText(e.target.value)}
          onBlur={commitEdit}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              commitEdit();
            } else if (e.key === "Escape") {
              setEditText(preview);
              setEditing(false);
            }
          }}
        />
        {compactActions ? null : (
          <>
            <button
              className="queued-cancel-btn"
              onMouseDown={(e) => {
                e.preventDefault();
                onCancel();
              }}
            >
              {cancelLabel}
            </button>
            {onSaveToNote && (
              <button
                className="queued-note-btn"
                onMouseDown={(e) => {
                  e.preventDefault();
                  onSaveToNote(editText.trim() || preview);
                }}
              >
                <Icon name="memo" size={15} />
              </button>
            )}
          </>
        )}
        <button
          className="promote-btn interrupt"
          data-testid="queued-interrupt-btn"
          onMouseDown={(e) => {
            e.preventDefault();
            onPromote();
          }}
          title={interruptTitle}
        >
          {interruptLabel}
        </button>
        {compactActions && (
          <QueuedPromptOverflowMenu
            open={actionsOpen}
            setOpen={setActionsOpen}
            onCancel={onCancel}
            cancelLabel={cancelLabel}
            onSaveToNote={
              onSaveToNote ? () => onSaveToNote(editText.trim() || preview) : undefined
            }
          />
        )}
      </div>
    );
  }

  const hasImages = (images?.length ?? 0) > 0 || (imagesCount ?? 0) > 0;
  const hasFiles = (files?.length ?? 0) > 0 || (filesCount ?? 0) > 0;

  return (
    <div className={`queued-prompt-banner${hasComments ? " has-tags" : ""}${hasImages || hasFiles ? " has-attachments" : ""}`} data-testid="queued-prompt-banner">
      <span className="queued-prompt-label">{queuedLabel}</span>
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
      {hasComments && (
        <div className="queued-tags-cards">
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
      )}
      <span
        className="queued-prompt-preview"
        onClick={startEditing}
        title="Click to edit"
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") startEditing();
        }}
      >
        {hasComments ? userText : preview}
      </span>
      <div className="queued-prompt-actions">
        {compactActions ? null : (
          <>
            <button
              className="queued-cancel-btn"
              onClick={onCancel}
            >
              {cancelLabel}
            </button>
            {onSaveToNote && (
              <button
                className="queued-note-btn"
                onClick={() => onSaveToNote(preview)}
                title="Save to notes"
              >
                <Icon name="memo" size={15} />
              </button>
            )}
          </>
        )}
        <button
          className="promote-btn interrupt"
          data-testid="queued-interrupt-btn"
          onClick={onPromote}
          title={interruptTitle}
        >
          {interruptLabel}
        </button>
        {compactActions && (
          <QueuedPromptOverflowMenu
            open={actionsOpen}
            setOpen={setActionsOpen}
            onCancel={onCancel}
            cancelLabel={cancelLabel}
            onSaveToNote={onSaveToNote ? () => onSaveToNote(preview) : undefined}
          />
        )}
      </div>
    </div>
  );
}

function QueuedPromptOverflowMenu({
  open,
  setOpen,
  onCancel,
  cancelLabel,
  onSaveToNote,
}: {
  open: boolean;
  setOpen: (open: boolean | ((open: boolean) => boolean)) => void;
  onCancel: () => void;
  cancelLabel: string;
  onSaveToNote?: () => void;
}) {
  return (
    <div className="queued-overflow-wrapper">
      <button
        className="queued-overflow-trigger"
        type="button"
        title="More queued actions"
        aria-label="More queued actions"
        onClick={() => setOpen((value) => !value)}
      >
        ⋯
      </button>
      {open && (
        <div className="queued-overflow-menu">
          <button
            className="overflow-menu-item"
            onClick={() => {
              setOpen(false);
              onCancel();
            }}
          >
            {cancelLabel}
          </button>
          {onSaveToNote && (
            <button
              className="overflow-menu-item"
              onClick={() => {
                setOpen(false);
                onSaveToNote();
              }}
            >
              <Icon name="memo" size={14} /> Save to notes
            </button>
          )}
        </div>
      )}
    </div>
  );
}
