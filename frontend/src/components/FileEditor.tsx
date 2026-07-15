import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { useTranslation } from "react-i18next";
import { DiffEditor } from "@monaco-editor/react";
import type { editor } from "monaco-editor";
import "highlight.js/styles/github-dark.css";
import { FileCommentBar, type SubmittedComment } from "./FileCommentBar";
import { FileDiscussionPanel } from "./FileDiscussionPanel";
import { runThreeStateSync, trackedFetch } from "../progress/store";
import { useScaledMonacoFontSize } from "../utils/typography";
import type { ChatMessage, FileDiscussion } from "../types";
import { MarkdownFileEditor } from "./FileEditorPrimitives";
import { useMonacoSelectionCapture } from "./useMonacoSelectionCapture";
import { useSaveShortcut } from "../hooks/useSaveShortcut";

type ViewMode = "diff" | "file";

import { API } from "../api";
const POLL_MS = 1500;

// Stable default identities: inline `= []` defaults mint a new array
// every render, which retriggers identity-keyed effects and can spin
// an infinite render loop.
const EMPTY_FILE_DISCUSSIONS: FileDiscussion[] = [];
const EMPTY_SESSION_MESSAGES: ChatMessage[] = [];

export interface FileAnchorComment {
  filePath: string;
  startLine: number;
  endLine: number;
  startCol: number;
  endCol: number;
  comment: string;
}

interface Props {
  /** Absolute path to prompt.md in the eng session's temp dir. */
  tempFilePath: string;
  /** Diff baseline — the user's original draft. Stable for the
   * lifetime of the overlay. */
  originalContent: string;
  /** Fired when the user picks a selection + types a comment + clicks
   * "Add comment". Implementation in App.tsx queues this as a
   * file-anchored InlineTag on the eng session — the user can stack
   * multiple comments and Send merges them all into one prompt. */
  onSubmitComment: (anchor: FileAnchorComment) => Promise<void>;
  /** Count of file-anchored comments already queued on this session.
   * Surfaced as a chip in the comment bar so the user knows their
   * previous comment landed and can keep adding more before Send. */
  pendingTagCount?: number;
  /** Whether this viewer may write the file back to disk. True for
   * prompt-engineering (the user edits prompt.md here). MUST be false
   * for file-editing mode: those are real project files the AGENT
   * owns — the poll-fed `liveContent` lags disk by up to one poll, so
   * any save would race the agent and revert its edits. When false the
   * viewer is read-only to disk; the user changes the file by queuing
   * comments, not by typing. */
  diskWritable?: boolean;
  fileDiscussions?: FileDiscussion[];
  sessionMessages?: ChatMessage[];
  onStartDiscussion?: (filePath: string, line: number) => Promise<FileDiscussion>;
  onPatchDiscussion?: (discussionId: string, patch: Partial<FileDiscussion>) => Promise<void>;
  onSendDiscussionMessage?: (
    discussionId: string,
    prompt: string,
    clientId: string,
  ) => Promise<void>;
}

interface PendingSelection {
  startLine: number;
  endLine: number;
  startCol: number;
  endCol: number;
}

/** Live-diff prompt.md viewer that lets the user attach comments to
 * specific line ranges. Comments survive subsequent file edits because
 * they reference line:col rather than copied text — the backend message
 * embeds the anchor verbatim so Claude sees "Re prompt.md:5:1-7:80\n…".
 */
export function FileEditor({
  tempFilePath,
  originalContent,
  onSubmitComment,
  pendingTagCount = 0,
  diskWritable = true,
  fileDiscussions = EMPTY_FILE_DISCUSSIONS,
  sessionMessages = EMPTY_SESSION_MESSAGES,
  onStartDiscussion,
  onPatchDiscussion,
  onSendDiscussionMessage,
}: Props) {
  const { t } = useTranslation();
  const monacoFontSize = useScaledMonacoFontSize(13);
  const [liveContent, setLiveContent] = useState<string>(originalContent);
  const [reviewBaseline, setReviewBaseline] = useState<string>(originalContent);
  const [pendingSelection, setPendingSelection] =
    useState<PendingSelection | null>(null);
  const [viewMode, setViewMode] = useState<ViewMode>("file");
  /** The currently-mounted Monaco editor we attach the selection
   * listener to. In "diff" mode this is the modified-side editor of
   * the DiffEditor; in "file" mode it's the standalone Editor. The
   * effect below re-runs whenever this changes so the listener
   * follows the active view. */
  const [activeEditor, setActiveEditor] =
    useState<editor.IStandaloneCodeEditor | null>(null);
  const [pendingDiscussionMessages, setPendingDiscussionMessages] = useState<ChatMessage[]>([]);
  const contextMenuLineRef = useRef<number | null>(null);
  const editorRootRef = useRef<HTMLDivElement | null>(null);

  const cancelRef = useRef(false);

  // Md edit-view state. Markdown content renders ReactMarkdown by default;
  // dblclick switches to a raw Monaco editor. While editing, the file
  // poll is suspended so backend writes can't clobber user-typed content.
  const [mdEditing, setMdEditing] = useState(false);
  const saveDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const liveContentRef = useRef(liveContent);
  liveContentRef.current = liveContent;

  const writeFileContent = useCallback(async (content: string) => {
    try {
      await runThreeStateSync({
        operationId: `file-editor:save:${tempFilePath}`,
        action: t("fileViewer.save"),
        info: tempFilePath,
        reconcile: async () => {
          const response = await fetch(`${API}/api/file?path=${encodeURIComponent(tempFilePath)}`);
          if (!response.ok) throw new Error(await response.text());
          const data = await response.json() as { content?: string };
          if (typeof data.content === "string") setLiveContent(data.content);
        },
        mutate: async () => {
          const response = await trackedFetch(
            `file:save:${tempFilePath}`,
            `${API}/api/file`,
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ path: tempFilePath, content }),
            },
          );
          if (!response.ok) throw new Error(await response.text());
          return response;
        },
      });
    } catch {
      // The canonical controller reconciles and reports the failure.
    }
  }, [tempFilePath, t]);

  const flushSave = useCallback(async () => {
    await writeFileContent(liveContentRef.current);
  }, [writeFileContent]);
  useSaveShortcut({
    enabled: diskWritable && mdEditing,
    targetRef: editorRootRef,
    onSave: () => {
      if (saveDebounceRef.current) {
        clearTimeout(saveDebounceRef.current);
        saveDebounceRef.current = null;
      }
      void flushSave();
    },
  });

  // Poll the file. Replaces a WS-driven refresh later — see CLAUDE.md
  // state-ownership rule; live file content is backend-owned and the
  // frontend only reflects it. SUSPENDED while mdEditing so the poll
  // doesn't clobber user-typed content mid-edit. Re-creates after the
  // user explicitly returns to formatted view.
  useEffect(() => {
    if (mdEditing) return;
    cancelRef.current = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const tick = async () => {
      try {
        const r = await fetch(
          `${API}/api/file?path=${encodeURIComponent(tempFilePath)}`,
        );
        if (cancelRef.current) return;
        if (r.ok) {
          const d = (await r.json()) as { content?: string };
          if (!cancelRef.current && typeof d.content === "string") {
            setLiveContent(d.content);
          }
        }
      } catch {
        // best-effort
      }
      if (!cancelRef.current) {
        timer = setTimeout(tick, POLL_MS);
      }
    };
    tick();

    return () => {
      cancelRef.current = true;
      if (timer) clearTimeout(timer);
    };
  }, [tempFilePath, mdEditing]);

  // Reset md edit-view state on temp-file path change (a brand-new
  // session); also fire a final save best-effort for the OLD path
  // BEFORE the next effect run swaps tempFilePath.
  useEffect(() => {
    setMdEditing(false);
    setReviewBaseline(originalContent);
    return () => {
      if (saveDebounceRef.current) {
        clearTimeout(saveDebounceRef.current);
        saveDebounceRef.current = null;
      }
      if (diskWritable) void flushSave();
    };
  }, [tempFilePath, originalContent, flushSave, diskWritable]);

  const returnToFormattedView = useCallback(async () => {
    if (saveDebounceRef.current) {
      clearTimeout(saveDebounceRef.current);
      saveDebounceRef.current = null;
    }
    await flushSave();
    setMdEditing(false);
  }, [flushSave]);

  const hasServerChanges = liveContent !== reviewBaseline;

  const fileDiscussionIds = useMemo(
    () => new Set(fileDiscussions.map((discussion) => discussion.id)),
    [fileDiscussions],
  );

  useEffect(() => {
    setPendingDiscussionMessages((pending) => {
      const kept = pending.filter(
        (message) => {
          if (!message.file_discussion_id) return false;
          if (!fileDiscussionIds.has(message.file_discussion_id)) return false;
          return !sessionMessages.some(
            (sessionMessage) => sessionMessage.client_id === message.client_id,
          );
        },
      );
      // Keep the previous identity when nothing was pruned so this
      // effect can never feed a render→effect→setState loop.
      return kept.length === pending.length ? pending : kept;
    });
  }, [fileDiscussionIds, sessionMessages]);

  const acceptServerChanges = useCallback(() => {
    setReviewBaseline(liveContentRef.current);
  }, []);

  const revertServerChanges = useCallback(async () => {
    await writeFileContent(reviewBaseline);
    setLiveContent(reviewBaseline);
  }, [reviewBaseline, writeFileContent]);

  useMonacoSelectionCapture({
    editor: activeEditor,
    enabled: true,
    onCapture: useCallback((selection) => {
      setPendingSelection({
        startLine: selection.startLine,
        endLine: selection.endLine,
        startCol: selection.startCol,
        endCol: selection.endCol,
      });
    }, []),
  });

  useEffect(() => {
    if (!activeEditor || !onStartDiscussion) return;
    const contextDisposable = activeEditor.onContextMenu((event) => {
      const browserEvent = event.event.browserEvent;
      const fallbackTarget = activeEditor.getTargetAtClientPoint(
        browserEvent.clientX,
        browserEvent.clientY,
      );
      contextMenuLineRef.current =
        event.target.position?.lineNumber ??
        fallbackTarget?.position?.lineNumber ??
        null;
    });
    const actionDisposable = activeEditor.addAction({
      id: "better-agent.start-file-discussion",
      label: "Start Discussion",
      contextMenuGroupId: "navigation",
      contextMenuOrder: 1,
      run: (ed) => {
        const line = contextMenuLineRef.current ?? ed.getPosition()?.lineNumber;
        if (line) void onStartDiscussion(tempFilePath, line);
      },
    });
    return () => {
      contextDisposable.dispose();
      actionDisposable.dispose();
    };
  }, [activeEditor, onStartDiscussion, tempFilePath]);

  useEffect(() => {
    if (!activeEditor || fileDiscussions.length === 0) return;
    const roots: Root[] = [];
    const zoneIds: string[] = [];
    const observers: ResizeObserver[] = [];

    activeEditor.changeViewZones((accessor) => {
      for (const discussion of fileDiscussions) {
        const domNode = document.createElement("div");
        domNode.className = "file-discussion-zone";
        const zone: editor.IViewZone = {
          afterLineNumber: discussion.line,
          heightInPx: discussion.collapsed ? 34 : 170,
          domNode,
        };
        const zoneId = accessor.addZone(zone);
        zoneIds.push(zoneId);
        const relayout = () => {
          const nextHeight = Math.max(34, Math.ceil(domNode.scrollHeight));
          if (zone.heightInPx === nextHeight) return;
          zone.heightInPx = nextHeight;
          activeEditor.changeViewZones((layoutAccessor) => {
            layoutAccessor.layoutZone(zoneId);
          });
        };
        if (typeof ResizeObserver !== "undefined") {
          const resizeObserver = new ResizeObserver(relayout);
          resizeObserver.observe(domNode);
          observers.push(resizeObserver);
        } else {
          window.setTimeout(relayout, 0);
        }
        const root = createRoot(domNode);
        root.render(
          <FileDiscussionPanel
            discussion={discussion}
            messages={sessionMessages}
            pendingMessages={pendingDiscussionMessages}
            sessionId={undefined}
            onSend={async (discussionId, prompt, clientId) => {
              const pendingMessage: ChatMessage = {
                id: clientId,
                role: "user",
                content: prompt,
                events: [],
                timestamp: new Date().toISOString(),
                isStreaming: false,
                client_id: clientId,
                file_discussion_id: discussionId,
              };
              setPendingDiscussionMessages((pending) => [...pending, pendingMessage]);
              await onSendDiscussionMessage?.(discussionId, prompt, clientId);
            }}
            onToggleCollapsed={async (discussionId, collapsed) => {
              await onPatchDiscussion?.(discussionId, { collapsed });
            }}
          />,
        );
        roots.push(root);
      }
    });

    return () => {
      for (const observer of observers) observer.disconnect();
      activeEditor.changeViewZones((accessor) => {
        for (const zoneId of zoneIds) accessor.removeZone(zoneId);
      });
      for (const root of roots) root.unmount();
    };
  }, [
    activeEditor,
    fileDiscussions,
    onPatchDiscussion,
    onSendDiscussionMessage,
    pendingDiscussionMessages,
    sessionMessages,
  ]);

  // Detach the active editor reference when the view mode changes;
  // the next onMount (DiffEditor or Editor depending on the new mode)
  // will repopulate it.
  useEffect(() => {
    setActiveEditor(null);
    // Also clear any pending range selection when switching views —
    // line numbers are the same in both modes, but the user's mental
    // model of "I just selected this" tied to a visual editor that's
    // about to unmount is best reset.
    setPendingSelection(null);
  }, [viewMode]);

  const handleSubmit = async ({ selection, comment }: SubmittedComment) => {
    if (selection.kind !== "monaco") return;
    await onSubmitComment({
      filePath: tempFilePath,
      startLine: selection.startLine,
      endLine: selection.endLine,
      startCol: selection.startCol,
      endCol: selection.endCol,
      comment,
    });
    setPendingSelection(null);
    // Collapse the editor's selection too so the comment bar doesn't
    // re-arm on the same range when the user clicks back into Monaco.
    const ed = activeEditor;
    if (ed) {
      const pos = ed.getPosition();
      if (pos) ed.setSelection({
        startLineNumber: pos.lineNumber,
        startColumn: pos.column,
        endLineNumber: pos.lineNumber,
        endColumn: pos.column,
      });
    }
  };

  return (
    <div className="file-viewer eng-file-editor" ref={editorRootRef}>
      <div className="file-viewer-header">
        <div className="file-viewer-title">
          <span className="file-viewer-path">{tempFilePath}</span>
          <span className="file-viewer-kind kind-markdown">{t("engFile.promptMd")}</span>
          {!diskWritable && hasServerChanges && (
            <span className="file-viewer-stale" title={t("engFile.changedTitle")}>
              {t("engFile.changed")}
            </span>
          )}
          {viewMode === "diff" && (
            <span className="file-viewer-diff-badge">{t("engFile.baselineToCurrent")}</span>
          )}
        </div>
        <div className="eng-file-editor-header-actions">
          {!diskWritable && hasServerChanges && (
            <>
              <button
                type="button"
                className="btn-small"
                onClick={acceptServerChanges}
                data-testid="eng-accept-diff"
              >
                {t("engFile.acceptDiff")}
              </button>
              <button
                type="button"
                className="btn-small"
                onClick={() => void revertServerChanges()}
                data-testid="eng-revert-diff"
              >
                {t("engFile.revertDiff")}
              </button>
            </>
          )}
          <div
            className="eng-file-editor-mode-toggle"
            role="tablist"
            aria-label={t("engFile.viewMode")}
          >
            <button
              type="button"
              role="tab"
              aria-selected={viewMode === "diff"}
              className={`btn-small ${viewMode === "diff" ? "active" : ""}`}
              onClick={() => setViewMode("diff")}
              data-testid="eng-view-diff"
            >
              {t("engFile.diff")}
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={viewMode === "file"}
              className={`btn-small ${viewMode === "file" ? "active" : ""}`}
              onClick={() => setViewMode("file")}
              data-testid="eng-view-file"
            >
              {t("engFile.file")}
            </button>
            {mdEditing && (
              <button
                type="button"
                className="btn-small"
                onClick={() => void returnToFormattedView()}
                data-testid="eng-file-md-view"
              >
                {t("engFile.view")}
              </button>
            )}
          </div>
        </div>
      </div>

      <div className="eng-file-editor-body">
        {viewMode === "diff" ? (
          <DiffEditor
            height="100%"
            language="markdown"
            original={reviewBaseline}
            modified={liveContent}
            theme="vs-dark"
            onMount={(diffEd) => {
              setActiveEditor(diffEd.getModifiedEditor());
            }}
            options={{
              readOnly: true,
              minimap: { enabled: false },
              fontSize: monacoFontSize,
              scrollBeyondLastLine: false,
              wordWrap: "on",
              renderSideBySide: true,
            }}
          />
        ) : (
          <MarkdownFileEditor
            value={liveContent}
            editing={mdEditing || !diskWritable}
            readOnly={!diskWritable}
            fontSize={monacoFontSize}
            theme="vs-dark"
            editClassName="eng-file-editor-md-edit"
            formattedClassName="eng-file-editor-md-formatted file-viewer-markdown"
            editTestId="eng-file-md-monaco"
            formattedTestId="eng-file-md-formatted"
            autoFocus={diskWritable}
            onRequestEdit={
              diskWritable
                ? () => {
                    setPendingSelection(null);
                    setMdEditing(true);
                  }
                : undefined
            }
            onMount={setActiveEditor}
            onChange={(next) => {
              setLiveContent(next);
              if (saveDebounceRef.current) clearTimeout(saveDebounceRef.current);
              saveDebounceRef.current = setTimeout(() => {
                saveDebounceRef.current = null;
                void flushSave();
              }, 1000);
            }}
          />
        )}
      </div>

      <FileCommentBar
        selection={
          pendingSelection
            ? {
                kind: "monaco",
                startLine: pendingSelection.startLine,
                endLine: pendingSelection.endLine,
                startCol: pendingSelection.startCol,
                endCol: pendingSelection.endCol,
              }
            : null
        }
        onSubmit={handleSubmit}
        onCancel={() => setPendingSelection(null)}
        pendingTagCount={pendingTagCount}
        draftKey={tempFilePath}
      />
    </div>
  );
}
