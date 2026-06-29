import { useCallback, useState, useEffect, useMemo, useRef } from "react";
import { useTranslation } from "react-i18next";
import Editor from "@monaco-editor/react";
import { DiffEditor } from "@monaco-editor/react";
import type { editor } from "monaco-editor";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import Papa from "papaparse";
import type { FileFocus } from "../types";
import "highlight.js/styles/github-dark.css";
import { markdownLinkifyComponents } from "../utils/linkifyFilePaths";
import {
  FileCommentBar,
  type CommentSelection,
  type SubmittedComment,
} from "./FileCommentBar";
import { ProgressButton } from "../progress/ProgressButton";
import { trackedFetch, useOpProgress } from "../progress/store";
import { useScaledMonacoFontSize } from "../utils/typography";

import { API } from "../api";

export interface FileTagAnchor {
  filePath: string;
  comment: string;
  selectedText: string;
  startLine?: number;
  endLine?: number;
  startCol?: number;
  endCol?: number;
}

/** Live, transient view of the user's position in a mounted editor.
 * Read at prompt-send time to build the "files the user has open"
 * preamble. Never persisted (per the state-ownership rule). */
export interface FileEditorHandle {
  path: string;
  getVisibleRange: () => FileFocus | null;
  getCaretPosition: () => { line: number; column: number } | null;
  getSelection: () => FileFocus | null;
}

interface Props {
  filePath: string | null;
  diffBefore?: string;
  diffAfter?: string;
  focus?: FileFocus;
  /** Agent-/user-requested selection. Applied once after the file
   * loads (real Monaco selection); does not re-fire on user scroll. */
  select?: FileFocus | null;
  onClose: () => void;
  /** Multi-machine: which node's filesystem `filePath` lives on. The
   * GET/POST /api/file calls carry this as `node_id` so file reads
   * and writes hit the correct backend. Defaults to "primary" (the
   * local sentinel) when omitted. */
  nodeId?: string;
  /** Persist a file-anchored comment as an InlineTag on the current
   * session. When undefined the comment bar is hidden — caller has
   * opted out of commenting. */
  onAddFileTag?: (anchor: FileTagAnchor) => Promise<void>;
  onStartDiscussion?: (filePath: string, line: number) => Promise<unknown>;
  /** Count of file-anchored tags already queued for THIS file path,
   * surfaced in the comment bar's empty-state hint. */
  pendingTagCount?: number;
  /** Called with a live handle once the Monaco editor is mounted for
   * `filePath`, and with null on unmount / file switch. The parent
   * (FilePanels) registers these so prompt-send can snapshot each
   * open panel's current viewport + selection. */
  onEditorReady?: (handle: FileEditorHandle | null) => void;
}

// File-type category drives which viewer component renders the file.
type ViewerKind = "markdown" | "csv" | "tsv" | "json" | "pdf" | "video" | "code";
type FileIdentity = { mtime_ns?: number; size?: number };

const VIDEO_EXTS = new Set(["mp4", "webm", "mov", "avi", "mkv", "m4v", "ogv", "3gp"]);

function categorize(filePath: string, language: string): ViewerKind {
  const name = filePath.toLowerCase();
  if (name.endsWith(".md") || name.endsWith(".markdown")) return "markdown";
  if (name.endsWith(".csv")) return "csv";
  if (name.endsWith(".tsv")) return "tsv";
  if (name.endsWith(".pdf")) return "pdf";
  const ext = name.split(".").pop() ?? "";
  if (VIDEO_EXTS.has(ext)) return "video";
  if (language === "json") return "json";
  return "code";
}

// Map languages to nice Monaco themes.  "json" gets a softer palette than
// raw source files so it visually stands apart from general code.
function monacoThemeFor(kind: ViewerKind): string {
  if (kind === "json") return "vs-dark";
  return "vs-dark";
}

function identityFromPayload(payload: unknown): FileIdentity | null {
  if (!payload || typeof payload !== "object") return null;
  const data = payload as Record<string, unknown>;
  const mtime = typeof data.mtime_ns === "number" ? data.mtime_ns : undefined;
  const size = typeof data.size === "number" ? data.size : undefined;
  if (mtime === undefined || size === undefined) return null;
  return { mtime_ns: mtime, size };
}

function identitiesDiffer(
  loaded: FileIdentity | null,
  current: FileIdentity | null,
): boolean {
  if (!loaded || !current) return false;
  return loaded.mtime_ns !== current.mtime_ns || loaded.size !== current.size;
}

async function fetchFileIdentity(
  path: string,
  nodeId: string,
): Promise<FileIdentity | null> {
  const response = await fetch(
    `${API}/api/file/metadata?path=${encodeURIComponent(path)}&node_id=${encodeURIComponent(nodeId)}`,
  );
  return identityFromPayload(await response.json());
}

export function FileViewer({
  filePath,
  diffBefore,
  diffAfter,
  focus,
  select,
  onClose,
  nodeId = "primary",
  onAddFileTag,
  onStartDiscussion,
  pendingTagCount = 0,
  onEditorReady,
}: Props) {
  const { t } = useTranslation();
  const monacoFontSize = useScaledMonacoFontSize(13);
  const [content, setContent] = useState("");
  const [language, setLanguage] = useState("plaintext");
  const [dirty, setDirty] = useState(false);
  const [loadedIdentity, setLoadedIdentity] = useState<FileIdentity | null>(null);
  const [currentIdentity, setCurrentIdentity] = useState<FileIdentity | null>(null);
  const [rawVersion, setRawVersion] = useState(0);
  const saveOpId = filePath ? `file:save:${filePath}` : "file:save:none";
  const loadOpId = filePath ? `file:load:${filePath}` : "file:load:none";
  const { inflight: saving } = useOpProgress(saveOpId);
  const isDiffMode = diffBefore !== undefined && diffAfter !== undefined;
  const editorRef = useRef<editor.IStandaloneCodeEditor | null>(null);
  const decorationsRef = useRef<editor.IEditorDecorationsCollection | null>(null);
  const contextMenuLineRef = useRef<number | null>(null);
  // Editor readiness is a React state (not just a ref) so mounting the
  // Monaco editor re-triggers the decoration effect below — otherwise, on
  // the very first click that opens the right panel, the effect runs once
  // with editorRef=null and never again because onMount doesn't re-render.
  const [editorReady, setEditorReady] = useState(false);
  const [pendingSelection, setPendingSelection] =
    useState<CommentSelection | null>(null);
  // Container ref used for DOM-selection capture in markdown / CSV /
  // TSV views (those don't go through Monaco).
  const renderedContainerRef = useRef<HTMLDivElement | null>(null);
  // Md edit-view: when the user double-clicks the rendered markdown,
  // we switch into a raw Monaco editor. Edits auto-save after 1s of idle.
  const [mdEditing, setMdEditing] = useState(false);
  const saveDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const contentRef = useRef(content);
  contentRef.current = content;
  // Mirror `dirty` into a ref so the load-effect cleanup (whose closure
  // captures stale state) can decide whether an unmount/path-swap
  // should flush. INVARIANT: the cleanup flush must fire ONLY when the
  // user has genuine unsaved edits. Flushing unconditionally writes
  // `contentRef.current` — which is "" for a viewer that unmounted
  // before its fetch resolved (rapid tab switch / programmatic panel
  // churn) — clobbering the real file on disk with an empty string.
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;

  // Save the current `content` state to disk. Reads from contentRef so a
  // debounced timer firing post-mdEditing-flip still has the right value
  // (Monaco's value/ref is gone by then). Best-effort; overlapping saves
  // are tolerated (last-write-wins server-side).
  const flushSaveAt = useCallback(async (path: string) => {
    try {
      await trackedFetch(
        `file:save:${path}`,
        `${API}/api/file`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            path,
            content: contentRef.current,
            node_id: nodeId,
          }),
        },
        { silent: true },
      );
      setDirty(false);
      const identity = await fetchFileIdentity(path, nodeId);
      setLoadedIdentity(identity);
      setCurrentIdentity(identity);
    } catch {
      // best effort
    }
  }, [nodeId]);

  useEffect(() => {
    if (!filePath) return;
    // Clear stale content first — otherwise, on rapid file switches, the
    // decoration effect briefly runs with the previous file's content and
    // clamps the range against the wrong line count.
    setContent("");
    setDirty(false);
    setLoadedIdentity(null);
    setCurrentIdentity(null);
    setMdEditing(false);
    // Media files are served via /api/file/raw — no text fetch needed.
    const currentKind = filePath ? categorize(filePath, "plaintext") : "code";
    if (currentKind === "pdf" || currentKind === "video") {
      let cancelled = false;
      fetchFileIdentity(filePath, nodeId)
        .then((identity) => {
          if (cancelled) return;
          setLoadedIdentity(identity);
          setCurrentIdentity(identity);
        })
        .catch(() => {
          if (cancelled) return;
          setLoadedIdentity(null);
          setCurrentIdentity(null);
        });
      return () => {
        cancelled = true;
      };
    }
    let cancelled = false;
    trackedFetch(
      loadOpId,
      `${API}/api/file?path=${encodeURIComponent(filePath)}&node_id=${encodeURIComponent(nodeId)}`,
    )
      .then((r) => r.json())
      .then((d) => {
        if (cancelled) return;
        setContent(d.content || "");
        setLanguage(d.language || "plaintext");
        const identity = identityFromPayload(d);
        setLoadedIdentity(identity);
        setCurrentIdentity(identity);
      })
      .catch(() => {
        if (!cancelled) setContent(t("fileViewer.failedToLoad"));
      });
    // Cleanup runs on filePath change (and unmount). Flush any pending
    // save for the OLD path BEFORE the next effect run swaps content,
    // so mid-edit path swaps don't lose unsaved typing.
    const oldPath = filePath;
    return () => {
      cancelled = true;
      if (saveDebounceRef.current) {
        clearTimeout(saveDebounceRef.current);
        saveDebounceRef.current = null;
      }
      // Only persist on teardown when there are actual unsaved edits.
      // A clean / still-loading viewer must NEVER write back (its
      // buffer is "" pre-fetch → would empty the file on disk).
      if (dirtyRef.current) void flushSaveAt(oldPath);
    };
  }, [filePath, nodeId, t, flushSaveAt, loadOpId]);

  const save = useCallback(async () => {
    const ed = editorRef.current;
    if (!ed || !filePath || saving) return;
    const value = ed.getValue();
    await trackedFetch(saveOpId, `${API}/api/file`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: filePath, content: value, node_id: nodeId }),
    });
    setContent(value);
    setDirty(false);
    const identity = await fetchFileIdentity(filePath, nodeId);
    setLoadedIdentity(identity);
    setCurrentIdentity(identity);
  }, [filePath, saving, saveOpId, nodeId]);

  const saveRef = useRef(save);
  useEffect(() => { saveRef.current = save; }, [save]);

  const loadLatestFromDisk = useCallback(async () => {
    if (!filePath || dirtyRef.current || isDiffMode) return;
    const currentKind = categorize(filePath, language);
    if (currentKind === "pdf" || currentKind === "video") {
      const identity = await fetchFileIdentity(filePath, nodeId);
      setLoadedIdentity(identity);
      setCurrentIdentity(identity);
      setRawVersion((v) => v + 1);
      return;
    }
    const response = await trackedFetch(
      loadOpId,
      `${API}/api/file?path=${encodeURIComponent(filePath)}&node_id=${encodeURIComponent(nodeId)}`,
    );
    const data = await response.json();
    setContent(data.content || "");
    setLanguage(data.language || "plaintext");
    setDirty(false);
    const identity = identityFromPayload(data);
    setLoadedIdentity(identity);
    setCurrentIdentity(identity);
    setMdEditing(false);
  }, [filePath, isDiffMode, language, loadOpId, nodeId]);

  const returnToFormattedView = useCallback(async () => {
    if (saveDebounceRef.current) {
      clearTimeout(saveDebounceRef.current);
      saveDebounceRef.current = null;
    }
    if (filePath && dirtyRef.current) await flushSaveAt(filePath);
    setMdEditing(false);
  }, [filePath, flushSaveAt]);

  // Apply / clear the focus highlight whenever the target range, the loaded
  // content, or the mounted editor changes. Monaco can't decorate lines that
  // don't yet exist, so this only runs once content is in the model — and we
  // defer to the next animation frame so Monaco's model swap + panel resize
  // have settled before we scroll (otherwise automaticLayout fires *after*
  // our reveal and snaps the viewport back to the top).
  //
  // Dependencies are *primitive* startLine/endLine, not the focus object, so
  // an unrelated parent re-render that hands us a new object with the same
  // values doesn't retrigger the scroll.
  const startLine = focus?.startLine;
  const endLine = focus?.endLine;
  const selStart = select?.startLine;
  const selEnd = select?.endLine;
  useEffect(() => {
    const ed = editorRef.current;
    if (!ed) return;

    // Clear any previous decoration synchronously — safe regardless of state.
    if (decorationsRef.current) {
      decorationsRef.current.clear();
      decorationsRef.current = null;
    }
    if (!content || startLine === undefined || endLine === undefined) return;

    let cancelled = false;

    const apply = () => {
      if (cancelled) return;
      const model = ed.getModel();
      if (!model) return;
      const maxLine = model.getLineCount();
      // Monaco swaps the model text in its own effect, which commits on the
      // same frame as our `content` state update. If we somehow arrived
      // before that swap (maxLine == 1 but content has more than one line),
      // wait one more frame so line numbers are real.
      if (maxLine <= 1 && content.includes("\n")) {
        requestAnimationFrame(apply);
        return;
      }
      const start = Math.max(1, Math.min(startLine, maxLine));
      const end = Math.max(start, Math.min(endLine, maxLine));

      // Decoration is safe to apply even when the editor has zero size —
      // it just isn't visible yet. Scrolling into a zero-size viewport, on
      // the other hand, is a silent no-op, so defer reveal until layout.
      if (decorationsRef.current) {
        decorationsRef.current.clear();
      }
      decorationsRef.current = ed.createDecorationsCollection([
        {
          range: {
            startLineNumber: start,
            startColumn: 1,
            endLineNumber: end,
            endColumn: model.getLineMaxColumn(end),
          },
          options: {
            isWholeLine: true,
            className: "file-viewer-focus-line",
            linesDecorationsClassName: "file-viewer-focus-gutter",
            overviewRuler: {
              color: "#7b68ee",
              position: 7, // OverviewRulerLane.Full
            },
            minimap: {
              color: "#7b68ee",
              position: 2, // MinimapPosition.Gutter
            },
          },
        },
      ]);

      // Agent-/user-requested real Monaco selection (separate from the
      // focus highlight decoration). Applied once when the file loads;
      // not re-applied on scroll (deps are primitive selStart/selEnd).
      if (selStart !== undefined && selEnd !== undefined) {
        const ss = Math.max(1, Math.min(selStart, maxLine));
        const se = Math.max(ss, Math.min(selEnd, maxLine));
        ed.setSelection({
          startLineNumber: ss,
          startColumn: 1,
          endLineNumber: se,
          endColumn: model.getLineMaxColumn(se),
        });
      }

      const reveal = () => {
        if (cancelled) return;
        ed.revealLinesInCenter(start, end, 1 /* ScrollType.Immediate */);
      };

      // Force Monaco to recompute its layout against the current
      // container size before reading it — automaticLayout polls
      // asynchronously and can lag a fresh mount, leaving the editor
      // pinned at 5x5 so revealLinesInCenter never actually scrolls.
      // Drive the layout ourselves and retry on subsequent frames
      // until the container has real dimensions.
      const tryRevealNow = () => {
        if (cancelled) return false;
        const cont = ed.getDomNode()?.parentElement;
        if (!cont || cont.offsetHeight < 80 || cont.offsetWidth < 80) return false;
        ed.layout({ width: cont.offsetWidth, height: cont.offsetHeight });
        const li = ed.getLayoutInfo();
        if (li.height < 80 || li.width < 80) return false;
        reveal();
        return true;
      };
      if (!tryRevealNow()) {
        let attempts = 0;
        const tick = () => {
          if (cancelled) return;
          if (tryRevealNow()) return;
          if (++attempts < 60) requestAnimationFrame(tick);
        };
        requestAnimationFrame(tick);
      }
    };

    const rafId = requestAnimationFrame(apply);
    return () => {
      cancelled = true;
      cancelAnimationFrame(rafId);
    };
  }, [startLine, endLine, selStart, selEnd, content, editorReady]);

  // Expose a live handle to the parent (FilePanels) so prompt-send can
  // snapshot this panel's current viewport + selection. Re-registers on
  // file switch; deregisters on unmount. Pure read-through — the handle
  // reads Monaco on demand, nothing is persisted.
  useEffect(() => {
    if (!onEditorReady) return;
    const ed = editorRef.current;
    if (!ed || !editorReady || !filePath) {
      onEditorReady(null);
      return;
    }
    const path = filePath;
    onEditorReady({
      path,
      getVisibleRange: () => {
        const vr = ed.getVisibleRanges?.()[0];
        return vr
          ? { startLine: vr.startLineNumber, endLine: vr.endLineNumber }
          : null;
      },
      getCaretPosition: () => {
        const p = ed.getPosition?.();
        return p ? { line: p.lineNumber, column: p.column } : null;
      },
      getSelection: () => {
        const s = ed.getSelection?.();
        if (
          !s ||
          (s.startLineNumber === s.endLineNumber &&
            s.startColumn === s.endColumn)
        ) {
          return null;
        }
        return {
          startLine: s.startLineNumber,
          startColumn: s.startColumn,
          endLine: s.endLineNumber,
          endColumn: s.endColumn,
        };
      },
    });
    return () => onEditorReady(null);
  }, [onEditorReady, editorReady, filePath]);

  const kind: ViewerKind = useMemo(
    () => (filePath ? categorize(filePath, language) : "code"),
    [filePath, language],
  );

  // Reset any pending selection whenever the user navigates to a
  // different file — line numbers and rendered-DOM identities aren't
  // portable across files.
  useEffect(() => {
    setPendingSelection(null);
  }, [filePath, isDiffMode]);

  // Monaco-side selection capture: latch on mouseup / shift-keyup so we
  // don't fire mid-drag (which would steal focus into the comment
  // textarea and kill the drag). Same approach as FileEditor.
  // Collapsed selections are ignored — once the user has a real range,
  // a stray click shouldn't wipe their pending range; Cancel/Submit
  // are the only way out.
  useEffect(() => {
    if (!onAddFileTag) return;
    const ed = editorRef.current;
    if (!ed) return;

    const capture = () => {
      const sel = ed.getSelection();
      if (!sel) return;
      if (
        sel.startLineNumber === sel.endLineNumber &&
        sel.startColumn === sel.endColumn
      ) {
        return;
      }
      setPendingSelection({
        kind: "monaco",
        startLine: sel.startLineNumber,
        endLine: sel.endLineNumber,
        startCol: sel.startColumn,
        endCol: sel.endColumn,
      });
    };

    const mouseUp = ed.onMouseUp(capture);
    const keyUp = ed.onKeyUp((e) => {
      if (e.shiftKey) capture();
    });
    return () => {
      mouseUp.dispose();
      keyUp.dispose();
    };
  }, [editorReady, onAddFileTag]);

  useEffect(() => {
    const ed = editorRef.current;
    if (!ed || !editorReady || !filePath || !onStartDiscussion) return;
    const contextDisposable = ed.onContextMenu((event) => {
      const browserEvent = event.event.browserEvent;
      const fallbackTarget = ed.getTargetAtClientPoint(
        browserEvent.clientX,
        browserEvent.clientY,
      );
      contextMenuLineRef.current =
        event.target.position?.lineNumber ??
        fallbackTarget?.position?.lineNumber ??
        null;
    });
    const actionDisposable = ed.addAction({
      id: "better-agent.start-file-discussion",
      label: "Start discussion",
      contextMenuGroupId: "navigation",
      contextMenuOrder: 1,
      run: (active) => {
        const line = contextMenuLineRef.current ?? active.getPosition()?.lineNumber;
        if (line) void onStartDiscussion(filePath, line);
      },
    });
    return () => {
      contextDisposable.dispose();
      actionDisposable.dispose();
    };
  }, [editorReady, filePath, onStartDiscussion]);

  // DOM-side selection capture for non-Monaco views (markdown HTML,
  // CSV/TSV table). Read window.getSelection() on mouseup inside the
  // rendered container; capture the text iff the range is fully
  // contained in our container.
  useEffect(() => {
    if (!onAddFileTag) return;
    const el = renderedContainerRef.current;
    if (!el) return;

    const handler = () => {
      const winSel = window.getSelection();
      if (!winSel || winSel.isCollapsed || winSel.rangeCount === 0) return;
      const range = winSel.getRangeAt(0);
      // Both endpoints must live inside our container — otherwise the
      // user is dragging across boundaries we can't anchor against.
      if (!el.contains(range.startContainer) || !el.contains(range.endContainer)) {
        return;
      }
      const text = winSel.toString();
      if (!text.trim()) return;
      setPendingSelection({ kind: "text", selectedText: text });
    };

    el.addEventListener("mouseup", handler);
    el.addEventListener("keyup", handler);
    return () => {
      el.removeEventListener("mouseup", handler);
      el.removeEventListener("keyup", handler);
    };
    // Re-bind whenever the renderer kind changes (the ref points at a
    // different DOM node) or the file path changes.
  }, [onAddFileTag, kind, filePath]);

  const handleSubmitComment = useCallback(
    async ({ selection, comment }: SubmittedComment) => {
      if (!onAddFileTag || !filePath) return;
      const anchor: FileTagAnchor =
        selection.kind === "monaco"
          ? {
              filePath,
              comment,
              selectedText: "",
              startLine: selection.startLine,
              endLine: selection.endLine,
              startCol: selection.startCol,
              endCol: selection.endCol,
            }
          : {
              filePath,
              comment,
              selectedText: selection.selectedText,
            };
      await onAddFileTag(anchor);
      setPendingSelection(null);
      // For Monaco views, also collapse the editor's selection so the
      // bar doesn't re-arm on the same range when the user clicks back
      // in. (No-op for DOM views — browser clears its own selection on
      // textarea focus already.)
      if (selection.kind === "monaco") {
        const ed = editorRef.current;
        if (ed) {
          const pos = ed.getPosition();
          if (pos) {
            ed.setSelection({
              startLineNumber: pos.lineNumber,
              startColumn: pos.column,
              endLineNumber: pos.lineNumber,
              endColumn: pos.column,
            });
          }
        }
      }
    },
    [onAddFileTag, filePath],
  );

  const stale = identitiesDiffer(loadedIdentity, currentIdentity);

  useEffect(() => {
    if (!filePath || isDiffMode || !loadedIdentity) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const identity = await fetchFileIdentity(filePath, nodeId);
        if (!cancelled) setCurrentIdentity(identity);
      } catch {
        if (!cancelled) setCurrentIdentity(null);
      }
    };
    void poll();
    const interval = window.setInterval(() => { void poll(); }, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [filePath, nodeId, isDiffMode, loadedIdentity]);

  if (!filePath) return null;

  const fileName = filePath.split("/").pop() || filePath;
  // Diff mode always stays in Monaco — side-by-side diff is only meaningful for source.
  const showMonaco = isDiffMode || kind === "code" || kind === "json";
  const rawUrl = `${API}/api/file/raw?path=${encodeURIComponent(filePath)}&node_id=${encodeURIComponent(nodeId)}&_v=${rawVersion}`;

  return (
    <div className="file-viewer">
      <div className="file-viewer-header">
        <div className="file-viewer-title">
          <span className="file-viewer-path">
            {dirty && <span className="file-viewer-dirty" title={t("fileViewer.unsavedChangesTitle")}>●</span>}
            {filePath}
          </span>
          <span className={`file-viewer-kind kind-${kind}`}>{kind}</span>
          {stale && (
            <span className="file-viewer-stale" title={t("fileViewer.changedSinceLoadedTitle")}>
              {t("fileViewer.changedSinceLoaded")}
            </span>
          )}
          {isDiffMode && <span className="file-viewer-diff-badge">{t("fileViewer.beforeAfter")}</span>}
        </div>
        <div className="file-viewer-actions">
          {stale && !dirty && !isDiffMode && (
            <ProgressButton
              opId={loadOpId}
              className="btn-small"
              onClick={() => void loadLatestFromDisk()}
              loadingChildren={t("fileViewer.loadingLatest")}
              title={t("fileViewer.updateToLatestTitle")}
            >
              {t("fileViewer.updateToLatest")}
            </ProgressButton>
          )}
          {!isDiffMode && showMonaco && (
            <ProgressButton
              opId={saveOpId}
              className="btn-small"
              onClick={save}
              extraDisabled={!dirty}
              loadingChildren={t("fileViewer.saving")}
              title="Save (Cmd+S)"
            >
              {t("fileViewer.save")}
            </ProgressButton>
          )}
          {!isDiffMode && kind === "markdown" && mdEditing && (
            <button
              type="button"
              className="btn-small"
              onClick={() => void returnToFormattedView()}
              data-testid="file-viewer-md-view"
            >
              View
            </button>
          )}
          <button className="btn-small" onClick={onClose}>
            {t("fileViewer.close")}
          </button>
        </div>
      </div>

      {isDiffMode ? (
        <DiffEditor
          height="100%"
          language={language}
          original={diffBefore}
          modified={diffAfter}
          theme={monacoThemeFor(kind)}
          onMount={(diffEd) => {
            const modified = diffEd.getModifiedEditor();
            editorRef.current = modified;
            setEditorReady(true);
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
      ) : kind === "markdown" ? (
        mdEditing ? (
          <div className="file-viewer-md-edit" data-testid="file-viewer-md-monaco">
            <Editor
              height="100%"
              language="markdown"
              value={content}
              theme={monacoThemeFor(kind)}
              onMount={(ed) => {
                editorRef.current = ed;
                setEditorReady(true);
                ed.focus();
              }}
              onChange={(v) => {
                const next = v ?? "";
                setContent(next);
                setDirty(true);
                if (saveDebounceRef.current) clearTimeout(saveDebounceRef.current);
                saveDebounceRef.current = setTimeout(() => {
                  saveDebounceRef.current = null;
                  if (filePath) void flushSaveAt(filePath);
                }, 1000);
              }}
              options={{
                readOnly: false,
                minimap: { enabled: false },
                fontSize: monacoFontSize,
                lineNumbers: "on",
                scrollBeyondLastLine: false,
                wordWrap: "on",
                automaticLayout: true,
              }}
            />
          </div>
        ) : (
          <div
            className="file-viewer-markdown"
            ref={renderedContainerRef}
            onDoubleClick={() => {
              setPendingSelection(null);
              setMdEditing(true);
            }}
            data-testid="file-viewer-md-formatted"
          >
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              rehypePlugins={[rehypeHighlight]}
              components={markdownLinkifyComponents()}
            >
              {content}
            </ReactMarkdown>
          </div>
        )
      ) : kind === "csv" || kind === "tsv" ? (
        <div className="file-viewer-rendered-wrap" ref={renderedContainerRef}>
          <CsvTable content={content} delimiter={kind === "tsv" ? "\t" : ","} />
        </div>
      ) : kind === "pdf" ? (
        <div className="file-viewer-pdf">
          <iframe
            src={rawUrl}
            title={fileName}
            className="file-viewer-pdf-iframe"
          />
        </div>
      ) : kind === "video" ? (
        <div className="file-viewer-video">
          <video
            src={rawUrl}
            controls
            preload="metadata"
            className="file-viewer-video-player"
          >
            {t("fileViewer.videoNotSupported")}
          </video>
        </div>
      ) : showMonaco ? (
        <Editor
          height="100%"
          language={language}
          value={content}
          theme={monacoThemeFor(kind)}
          onMount={(ed, monaco) => {
            editorRef.current = ed;
            setEditorReady(true);
            ed.addCommand(
              monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS,
              () => { void saveRef.current(); },
            );
          }}
          onChange={(v) => setDirty(v !== content)}
          options={{
            readOnly: false,
            minimap: { enabled: false },
            fontSize: monacoFontSize,
            lineNumbers: "on",
            scrollBeyondLastLine: false,
            wordWrap: "on",
            automaticLayout: true,
          }}
        />
      ) : null}

      {onAddFileTag && (
        <FileCommentBar
          selection={pendingSelection}
          onSubmit={handleSubmitComment}
          onCancel={() => setPendingSelection(null)}
          pendingTagCount={pendingTagCount}
          draftKey={filePath}
        />
      )}
    </div>
  );
}

// Lazy CSV → HTML table. Papaparse handles quoted fields, escapes, BOM, etc.
// We cap rendered rows to avoid freezing the browser on huge files.
const MAX_ROWS = 2000;

function CsvTable({ content, delimiter }: { content: string; delimiter: string }) {
  const { t } = useTranslation();
  const { rows, truncated, error } = useMemo(() => {
    if (!content) return { rows: [] as string[][], truncated: false, error: null as string | null };
    const parsed = Papa.parse<string[]>(content, {
      delimiter,
      skipEmptyLines: true,
    });
    const allRows = (parsed.data || []) as string[][];
    const err = parsed.errors?.[0]?.message || null;
    return {
      rows: allRows.slice(0, MAX_ROWS),
      truncated: allRows.length > MAX_ROWS,
      error: err,
    };
  }, [content, delimiter]);

  if (error && rows.length === 0) {
    return <div className="file-viewer-error">{t("fileViewer.csvParseError")}{error}</div>;
  }

  const [header, ...body] = rows;
  return (
    <div className="file-viewer-table-wrap">
      <table className="file-viewer-table">
        {header && (
          <thead>
            <tr>
              {header.map((cell, i) => (
                <th key={i}>{cell}</th>
              ))}
            </tr>
          </thead>
        )}
        <tbody>
          {body.map((row, r) => (
            <tr key={r}>
              {row.map((cell, c) => (
                <td key={c}>{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {truncated && (
        <div className="file-viewer-truncated">
          {t("fileViewer.truncatedRows", { max: MAX_ROWS })}
        </div>
      )}
    </div>
  );
}
