import { useCallback, useState, useEffect, useMemo, useRef } from "react";
import { useTranslation } from "react-i18next";
import { DiffEditor, Editor } from "@monaco-editor/react";
import type { editor } from "monaco-editor";
import Papa from "papaparse";
import type { FileFocus } from "../types";
import "highlight.js/styles/github-dark.css";
import {
  FileCommentBar,
  type CommentSelection,
  type SubmittedComment,
} from "./FileCommentBar";
import { MarkdownFileEditor } from "./FileEditorPrimitives";
import { useMonacoSelectionCapture } from "./useMonacoSelectionCapture";
import Icon from "./Icon";
import { ProgressButton } from "../progress/ProgressButton";
import { trackedFetch, useOpProgress } from "../progress/store";
import { useScaledMonacoFontSize } from "../utils/typography";
import { useSaveShortcut } from "../hooks/useSaveShortcut";
import { useViewport } from "../hooks/useViewport";

import { API } from "../api";
import { rawFileUrl } from "../utils/rawFileUrl";
import { copyToClipboard } from "../utils/clipboard";

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
export type FileIdentity = { mtime_ns?: number; size?: number };
export type LoadedTextFile = {
  path: string;
  content: string;
  language: string;
  identity: FileIdentity | null;
};
type LoadedViewFile = LoadedTextFile & {
  diskContent: string;
  diskIdentity: FileIdentity | null;
  hasDraft: boolean;
};

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

export function identitiesDiffer(
  loaded: FileIdentity | null,
  current: FileIdentity | null,
): boolean {
  if (!loaded || !current) return false;
  return loaded.mtime_ns !== current.mtime_ns || loaded.size !== current.size;
}

export async function fetchFileIdentity(
  path: string,
  nodeId: string,
): Promise<FileIdentity | null> {
  const response = await fetch(
    `${API}/api/file/metadata?path=${encodeURIComponent(path)}&node_id=${encodeURIComponent(nodeId)}`,
  );
  return identityFromPayload(await response.json());
}

export async function fetchTextFile(
  path: string,
  nodeId: string,
  opId: string,
): Promise<LoadedTextFile> {
  const response = await trackedFetch(
    opId,
    `${API}/api/file?path=${encodeURIComponent(path)}&node_id=${encodeURIComponent(nodeId)}`,
  );
  const data = await response.json();
  return {
    path,
    content: data.content || "",
    language: data.language || "plaintext",
    identity: identityFromPayload(data),
  };
}

async function fetchDraft(path: string, nodeId: string): Promise<{
  exists: boolean;
  content?: string;
  base_identity?: FileIdentity | null;
}> {
  const response = await fetch(
    `${API}/api/file/draft?path=${encodeURIComponent(path)}&node_id=${encodeURIComponent(nodeId)}`,
  );
  return response.json();
}

async function fetchViewedTextFile(
  path: string,
  nodeId: string,
  opId: string,
): Promise<LoadedViewFile> {
  const disk = await fetchTextFile(path, nodeId, opId);
  const draft = await fetchDraft(path, nodeId);
  if (!draft.exists || typeof draft.content !== "string") {
    return {
      ...disk,
      diskContent: disk.content,
      diskIdentity: disk.identity,
      hasDraft: false,
    };
  }
  return {
    path,
    content: draft.content,
    language: disk.language,
    identity: identityFromPayload(draft.base_identity) ?? disk.identity,
    diskContent: disk.content,
    diskIdentity: disk.identity,
    hasDraft: true,
  };
}

const CLIPBOARD_STYLE_PROPS = [
  "background-color",
  "border",
  "border-collapse",
  "color",
  "font-family",
  "font-size",
  "font-style",
  "font-weight",
  "line-height",
  "padding",
  "text-decoration",
  "white-space",
];

function selectionHtmlWithInlineStyles(container: HTMLElement, range: Range): string {
  const wrapper = document.createElement("div");
  wrapper.appendChild(range.cloneContents());
  const staging = document.createElement("div");
  staging.className = container.className;
  staging.style.position = "fixed";
  staging.style.left = "-10000px";
  staging.style.top = "0";
  staging.appendChild(wrapper);
  document.body.appendChild(staging);
  try {
    wrapper.querySelectorAll<HTMLElement>("*").forEach((el) => {
      const computed = window.getComputedStyle(el);
      CLIPBOARD_STYLE_PROPS.forEach((prop) => {
        const value = computed.getPropertyValue(prop);
        if (value) el.style.setProperty(prop, value);
      });
    });
    return wrapper.innerHTML;
  } finally {
    document.body.removeChild(staging);
  }
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
  const [latestPreview, setLatestPreview] = useState<LoadedTextFile | null>(null);
  const [hasDraft, setHasDraft] = useState(false);
  const [copiedOriginal, setCopiedOriginal] = useState(false);
  const viewport = useViewport();
  const isTouchLayout = viewport.mode !== "desktop";
  // Live text of the Monaco selection — drives the mobile "Copy selection"
  // pill. On touch, Monaco owns its selection model and never surfaces the
  // OS copy sheet, so we copy the selected range ourselves.
  const [selectionText, setSelectionText] = useState("");
  const [copiedSelection, setCopiedSelection] = useState(false);
  const copySelectionResetRef = useRef<number | null>(null);
  const saveOpId = filePath ? `file:save:${filePath}` : "file:save:none";
  const loadOpId = filePath ? `file:load:${filePath}` : "file:load:none";
  const latestDiffOpId = filePath ? `file:latest-diff:${filePath}` : "file:latest-diff:none";
  const { inflight: saving } = useOpProgress(saveOpId);
  const isDiffMode = diffBefore !== undefined && diffAfter !== undefined;
  const editorRef = useRef<editor.IStandaloneCodeEditor | null>(null);
  const [activeEditor, setActiveEditor] =
    useState<editor.IStandaloneCodeEditor | null>(null);
  const decorationsRef = useRef<editor.IEditorDecorationsCollection | null>(null);
  const appliedRevealKeyRef = useRef<string | null>(null);
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
  const viewerRef = useRef<HTMLDivElement | null>(null);
  // Md edit-view: when the user double-clicks the rendered markdown,
  // we switch into a raw Monaco editor. Edits auto-save after 1s of idle.
  const [mdEditing, setMdEditing] = useState(false);
  const saveDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const copyResetRef = useRef<number | null>(null);
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
  const loadedIdentityRef = useRef<FileIdentity | null>(loadedIdentity);
  loadedIdentityRef.current = loadedIdentity;

  useEffect(() => {
    return () => {
      if (copyResetRef.current) window.clearTimeout(copyResetRef.current);
    };
  }, []);

  const flushDraftAt = useCallback(async (path: string) => {
    try {
      await trackedFetch(
        `file:draft:${path}`,
        `${API}/api/file/draft`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            path,
            content: contentRef.current,
            node_id: nodeId,
            base_identity: loadedIdentityRef.current,
          }),
        },
        { silent: true },
      );
      setDirty(false);
      setHasDraft(true);
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
    setLatestPreview(null);
    setHasDraft(false);
    setMdEditing(false);
    setActiveEditor(null);
    setEditorReady(false);
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
    fetchViewedTextFile(filePath, nodeId, loadOpId)
      .then((loaded) => {
        if (cancelled) return;
        setContent(loaded.content);
        setLanguage(loaded.language);
        setLoadedIdentity(loaded.identity);
        setCurrentIdentity(loaded.diskIdentity);
        setHasDraft(loaded.hasDraft);
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
      if (dirtyRef.current) void flushDraftAt(oldPath);
    };
  }, [filePath, nodeId, t, flushDraftAt, loadOpId]);

  const save = useCallback(async () => {
    if (!filePath || saving) return;
    const value = contentRef.current;
    await trackedFetch(saveOpId, `${API}/api/file`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: filePath, content: value, node_id: nodeId }),
    });
    await fetch(
      `${API}/api/file/draft?path=${encodeURIComponent(filePath)}&node_id=${encodeURIComponent(nodeId)}`,
      { method: "DELETE" },
    );
    setContent(value);
    setDirty(false);
    setHasDraft(false);
    const identity = await fetchFileIdentity(filePath, nodeId);
    setLoadedIdentity(identity);
    setCurrentIdentity(identity);
  }, [filePath, saving, saveOpId, nodeId]);

  const saveRef = useRef(save);
  useEffect(() => { saveRef.current = save; }, [save]);
  useSaveShortcut({
    enabled: Boolean(filePath && dirty && !saving),
    targetRef: viewerRef,
    onSave: () => {
      void saveRef.current();
    },
  });

  const applyLoadedTextFile = useCallback((loaded: LoadedTextFile) => {
    setContent(loaded.content);
    setLanguage(loaded.language);
    setDirty(false);
    setLoadedIdentity(loaded.identity);
    setCurrentIdentity(loaded.identity);
    setLatestPreview(null);
    setHasDraft(false);
    setMdEditing(false);
  }, []);

  const loadLatestFromDisk = useCallback(async () => {
    if (!filePath || dirtyRef.current || isDiffMode) return;
    const currentKind = categorize(filePath, language);
    if (currentKind === "pdf" || currentKind === "video") {
      const identity = await fetchFileIdentity(filePath, nodeId);
      setLoadedIdentity(identity);
      setCurrentIdentity(identity);
      setHasDraft(false);
      setRawVersion((v) => v + 1);
      return;
    }
    await fetch(
      `${API}/api/file/draft?path=${encodeURIComponent(filePath)}&node_id=${encodeURIComponent(nodeId)}`,
      { method: "DELETE" },
    );
    if (latestPreview?.path === filePath) {
      applyLoadedTextFile(latestPreview);
      return;
    }
    applyLoadedTextFile(await fetchTextFile(filePath, nodeId, loadOpId));
  }, [applyLoadedTextFile, filePath, isDiffMode, language, latestPreview, loadOpId, nodeId]);

  const previewLatestDiff = useCallback(async () => {
    if (!filePath || dirtyRef.current || isDiffMode) return;
    const currentKind = categorize(filePath, language);
    if (currentKind === "pdf" || currentKind === "video") return;
    setLatestPreview(await fetchTextFile(filePath, nodeId, latestDiffOpId));
  }, [filePath, isDiffMode, language, latestDiffOpId, nodeId]);

  const returnToFormattedView = useCallback(async () => {
    if (saveDebounceRef.current) {
      clearTimeout(saveDebounceRef.current);
      saveDebounceRef.current = null;
    }
    if (filePath && dirtyRef.current) await flushDraftAt(filePath);
    setMdEditing(false);
  }, [filePath, flushDraftAt]);

  const copyOriginalContent = useCallback(async () => {
    await copyToClipboard(contentRef.current);
    setCopiedOriginal(true);
    if (copyResetRef.current) window.clearTimeout(copyResetRef.current);
    copyResetRef.current = window.setTimeout(() => {
      setCopiedOriginal(false);
      copyResetRef.current = null;
    }, 1200);
  }, []);

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
  const selStartColumn = select?.startColumn;
  const selEndColumn = select?.endColumn;
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
      const revealKey = [
        filePath ?? "",
        startLine,
        endLine,
        selStart ?? "",
        selStartColumn ?? "",
        selEnd ?? "",
        selEndColumn ?? "",
      ].join(":");
      const shouldApplyReveal = appliedRevealKeyRef.current !== revealKey;

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
      if (shouldApplyReveal && selStart !== undefined && selEnd !== undefined) {
        const ss = Math.max(1, Math.min(selStart, maxLine));
        const se = Math.max(ss, Math.min(selEnd, maxLine));
        const ssMaxColumn = model.getLineMaxColumn(ss);
        const seMaxColumn = model.getLineMaxColumn(se);
        ed.setSelection({
          startLineNumber: ss,
          startColumn: Math.max(1, Math.min(selStartColumn ?? 1, ssMaxColumn)),
          endLineNumber: se,
          endColumn: Math.max(1, Math.min(selEndColumn ?? seMaxColumn, seMaxColumn)),
        });
      }

      if (!shouldApplyReveal) return;
      appliedRevealKeyRef.current = revealKey;

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
  }, [filePath, startLine, endLine, selStart, selEnd, selStartColumn, selEndColumn, content, editorReady]);

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

  useMonacoSelectionCapture({
    editor: activeEditor,
    enabled: editorReady && Boolean(onAddFileTag),
    onCapture: useCallback((selection) => {
      setPendingSelection({
        kind: "monaco",
        startLine: selection.startLine,
        endLine: selection.endLine,
        startCol: selection.startCol,
        endCol: selection.endCol,
      });
    }, []),
  });

  // Track the Monaco selection's text live so a touch user can copy it.
  // activeEditor is non-null only while a Monaco editor is mounted, so this
  // safely no-ops for markdown / csv / media views.
  useEffect(() => {
    if (!isTouchLayout || !activeEditor) {
      setSelectionText("");
      return;
    }
    const ed = activeEditor;
    const read = () => {
      const sel = ed.getSelection();
      const model = ed.getModel();
      if (!sel || !model || sel.isEmpty()) {
        setSelectionText("");
        return;
      }
      setSelectionText(model.getValueInRange(sel));
    };
    read();
    const sub = ed.onDidChangeCursorSelection(read);
    return () => {
      sub.dispose();
      setSelectionText("");
    };
  }, [isTouchLayout, activeEditor]);

  const copySelection = useCallback(async () => {
    if (!selectionText) return;
    await copyToClipboard(selectionText);
    setCopiedSelection(true);
    if (copySelectionResetRef.current) {
      window.clearTimeout(copySelectionResetRef.current);
    }
    copySelectionResetRef.current = window.setTimeout(() => {
      setCopiedSelection(false);
      copySelectionResetRef.current = null;
    }, 1200);
  }, [selectionText]);

  useEffect(() => {
    return () => {
      if (copySelectionResetRef.current) {
        window.clearTimeout(copySelectionResetRef.current);
      }
    };
  }, []);

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

  useEffect(() => {
    const el = renderedContainerRef.current;
    if (!el) return;

    const handler = (event: ClipboardEvent) => {
      const selection = window.getSelection();
      if (!selection || selection.isCollapsed || selection.rangeCount === 0) return;
      const range = selection.getRangeAt(0);
      if (!el.contains(range.startContainer) || !el.contains(range.endContainer)) return;
      const text = selection.toString();
      if (!text.trim()) return;
      event.preventDefault();
      event.clipboardData?.setData("text/plain", text);
      event.clipboardData?.setData("text/html", selectionHtmlWithInlineStyles(el, range));
    };

    document.addEventListener("copy", handler, true);
    return () => document.removeEventListener("copy", handler, true);
  }, [kind, filePath, content, mdEditing, isDiffMode]);

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
  const canPreviewLatestDiff = stale && !dirty && !isDiffMode && kind !== "pdf" && kind !== "video";
  const showLatestDiff = canPreviewLatestDiff && latestPreview !== null;

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
  const canSaveViewedContent = !isDiffMode && !showLatestDiff && (showMonaco || kind === "markdown");
  const canCopyOriginalContent = !isDiffMode && !showLatestDiff && kind !== "pdf" && kind !== "video";
  const canCommentOnFile = !!onAddFileTag && !isDiffMode && !showLatestDiff;
  const hasUnsavedOriginalChanges = dirty || hasDraft;
  const synced = !stale && !hasUnsavedOriginalChanges && !showLatestDiff;
  const rawUrl = rawFileUrl(API, filePath, nodeId, rawVersion);

  return (
    <div className="file-viewer" ref={viewerRef}>
      <div className="file-viewer-header">
        <div className="file-viewer-title">
          <span className="file-viewer-path">
            {hasUnsavedOriginalChanges && <span className="file-viewer-dirty" title={t("fileViewer.unsavedChangesTitle")}>●</span>}
            {filePath}
          </span>
          <span className={`file-viewer-kind kind-${kind}`}>{kind}</span>
          {synced && (
            <span className="file-viewer-sync-state state-synced" title={t("fileViewer.syncedTitle")}>
              {t("fileViewer.synced")}
            </span>
          )}
          {hasUnsavedOriginalChanges && (
            <span className="file-viewer-sync-state state-draft" title={t("fileViewer.draftSavedTitle")}>
              {t("fileViewer.draftSaved")}
            </span>
          )}
          {stale && (
            <span className="file-viewer-stale" title={t("fileViewer.changedSinceLoadedTitle")}>
              {t("fileViewer.changedSinceLoaded")}
            </span>
          )}
          {isDiffMode && <span className="file-viewer-diff-badge">{t("fileViewer.beforeAfter")}</span>}
          {showLatestDiff && <span className="file-viewer-diff-badge">{t("fileViewer.loadedToLatest")}</span>}
        </div>
        <div className="file-viewer-actions">
          {showLatestDiff && (
            <button
              type="button"
              className="btn-small"
              onClick={() => setLatestPreview(null)}
            >
              {t("fileViewer.backToFile")}
            </button>
          )}
          {canPreviewLatestDiff && !showLatestDiff && (
            <ProgressButton
              opId={latestDiffOpId}
              className="btn-small"
              onClick={() => void previewLatestDiff()}
              loadingChildren={t("fileViewer.loadingLatest")}
              title={t("fileViewer.viewLatestDiffTitle")}
            >
              {t("fileViewer.viewLatestDiff")}
            </ProgressButton>
          )}
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
          {canSaveViewedContent && (
            <ProgressButton
              opId={saveOpId}
              className="btn-small"
              onClick={save}
              extraDisabled={!hasUnsavedOriginalChanges}
              loadingChildren={t("fileViewer.saving")}
              title="Save (Cmd+S)"
            >
              {t("fileViewer.save")}
            </ProgressButton>
          )}
          {canCopyOriginalContent && (
            <button
              type="button"
              className="btn-small"
              onClick={() => void copyOriginalContent()}
              title={t("fileViewer.copyOriginalTitle")}
            >
              <Icon name="clipboard" size={13} />
              {copiedOriginal ? t("fileViewer.copied") : t("fileViewer.copyOriginal")}
            </button>
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

      {showLatestDiff && latestPreview ? (
        <div className="file-viewer-latest-diff" data-testid="file-viewer-latest-diff">
          <DiffEditor
            height="100%"
            language={latestPreview.language || language}
            original={content}
            modified={latestPreview.content}
            theme={monacoThemeFor(kind)}
            onMount={(diffEd) => {
              const modified = diffEd.getModifiedEditor();
              editorRef.current = modified;
              setActiveEditor(modified);
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
        </div>
      ) : isDiffMode ? (
        <DiffEditor
          height="100%"
          language={language}
          original={diffBefore}
          modified={diffAfter}
          theme={monacoThemeFor(kind)}
          onMount={(diffEd) => {
            const modified = diffEd.getModifiedEditor();
            editorRef.current = modified;
            setActiveEditor(modified);
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
        <MarkdownFileEditor
          value={content}
          editing={mdEditing}
          readOnly={false}
          fontSize={monacoFontSize}
          theme={monacoThemeFor(kind)}
          editClassName="file-viewer-md-edit"
          formattedClassName="file-viewer-markdown"
          editTestId="file-viewer-md-monaco"
          formattedTestId="file-viewer-md-formatted"
          renderedRef={renderedContainerRef}
          autoFocus
          onRequestEdit={() => {
            setPendingSelection(null);
            setMdEditing(true);
          }}
          onMount={(ed) => {
            editorRef.current = ed;
            setActiveEditor(ed);
            setEditorReady(true);
          }}
          onChange={(next) => {
            setContent(next);
            setDirty(true);
            if (saveDebounceRef.current) clearTimeout(saveDebounceRef.current);
            saveDebounceRef.current = setTimeout(() => {
              saveDebounceRef.current = null;
              if (filePath) void flushDraftAt(filePath);
            }, 1000);
          }}
        />
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
            setActiveEditor(ed);
            setEditorReady(true);
            ed.addCommand(
              monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS,
              () => { void saveRef.current(); },
            );
          }}
          onChange={(v) => {
            const next = v ?? "";
            setContent(next);
            setDirty(true);
            if (saveDebounceRef.current) clearTimeout(saveDebounceRef.current);
            saveDebounceRef.current = setTimeout(() => {
              saveDebounceRef.current = null;
              if (filePath) void flushDraftAt(filePath);
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
      ) : null}

      {canCommentOnFile && (
        <FileCommentBar
          selection={pendingSelection}
          onSubmit={handleSubmitComment}
          onCancel={() => setPendingSelection(null)}
          pendingTagCount={pendingTagCount}
          draftKey={filePath}
        />
      )}
      {isTouchLayout && selectionText && (
        <button
          type="button"
          className="file-viewer-copy-selection"
          onClick={() => void copySelection()}
          title={t("fileViewer.copySelectionTitle")}
        >
          <Icon name="clipboard" size={13} />
          {copiedSelection ? t("fileViewer.copied") : t("fileViewer.copySelection")}
        </button>
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
