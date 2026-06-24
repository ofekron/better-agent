import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { OpenFilePanel } from "../types";
import { FileViewer, type FileEditorHandle, type FileTagAnchor } from "./FileViewer";
import { useViewport } from "../hooks/useViewport";

interface Props {
  /** Backend-owned ordered list of open file panels for the session.
   * The container reflects this 1:1 — it never holds its own copy. */
  panels: OpenFilePanel[];
  /** Multi-machine: which node the session's files live on. Threaded
   * into each FileViewer so reads/writes hit the right machine. */
  nodeId?: string;
  /** Ask the backend to close a panel (App does the optimistic
   * applySessionMetadata + DELETE round-trip, same as inline tags). */
  onClosePanel: (id: string) => void;
  /** Register/deregister a panel's live editor handle so prompt-send
   * can snapshot each open file's viewport + selection. Keyed by
   * path (the stable panel identity). */
  registerEditor: (path: string, handle: FileEditorHandle | null) => void;
  onAddFileTag?: (anchor: FileTagAnchor) => Promise<void>;
  onStartDiscussion?: (filePath: string, line: number) => Promise<unknown>;
  pendingTagCountFor?: (path: string) => number;
}

/** Tabbed / split container for the session's open file panels.
 *
 * Pure projection of backend `open_file_panels`. Only the active tab
 * and the split toggle are local transient UI (allowed). In tab mode
 * only the active panel's FileViewer is mounted; in split mode every
 * panel is mounted side-by-side. */
export function FilePanels({
  panels,
  nodeId = "primary",
  onClosePanel,
  registerEditor,
  onAddFileTag,
  onStartDiscussion,
  pendingTagCountFor,
}: Props) {
  const { t } = useTranslation();
  const viewport = useViewport();
  const [activeId, setActiveId] = useState<string | null>(null);
  // Split mode is desktop-only. On mobile/tablet the side-by-side
  // layout doesn't fit; the toggle is hidden in the toolbar below
  // and `split` is forced to false.
  const [splitDesktop, setSplitDesktop] = useState(false);
  const split = viewport.mode === "desktop" && splitDesktop;
  const setSplit = setSplitDesktop;

  // Keep activeId valid against the backend-owned list. When the
  // active panel disappears (closed in another tab) or a new panel is
  // appended (agent / user just opened one), focus the last panel —
  // newly opened files are appended, so this makes "open" == "focus".
  useEffect(() => {
    if (panels.length === 0) {
      if (activeId !== null) setActiveId(null);
      return;
    }
    if (!panels.some((p) => p.id === activeId)) {
      setActiveId(panels[panels.length - 1].id);
    }
  }, [panels, activeId]);

  const cycle = useCallback(
    (dir: 1 | -1) => {
      if (panels.length < 2) return;
      const idx = panels.findIndex((p) => p.id === activeId);
      const base = idx < 0 ? 0 : idx;
      const next = (base + dir + panels.length) % panels.length;
      setActiveId(panels[next].id);
    },
    [panels, activeId],
  );

  // Best-effort Ctrl/Cmd+Tab cycling. Ctrl+Tab is browser-reserved in
  // some environments; the visible ‹ › buttons are the guaranteed
  // affordance. Shift reverses.
  useEffect(() => {
    if (panels.length < 2 || split) return;
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Tab") {
        e.preventDefault();
        cycle(e.shiftKey ? -1 : 1);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [panels.length, split, cycle]);

  if (panels.length === 0) return null;

  const active =
    panels.find((p) => p.id === activeId) ?? panels[panels.length - 1];

  const renderViewer = (panel: OpenFilePanel) => (
    <FileViewer
      key={panel.id}
      filePath={panel.path}
      nodeId={nodeId}
      focus={panel.focus ?? panel.selection ?? undefined}
      select={panel.selection ?? null}
      onClose={() => onClosePanel(panel.id)}
      onAddFileTag={onAddFileTag}
      onStartDiscussion={onStartDiscussion}
      pendingTagCount={pendingTagCountFor?.(panel.path) ?? 0}
      onEditorReady={(h) => registerEditor(panel.path, h)}
    />
  );

  return (
    <div className="file-panels">
      <div className="file-panels-tabs">
        {panels.length > 1 && (
          <button
            className="file-panels-cycle"
            onClick={() => cycle(-1)}
            title={t("filePanels.prevTab")}
          >
            ‹
          </button>
        )}
        <div className="file-panels-tablist">
          {panels.map((p) => {
            const name = p.path.split("/").pop() || p.path;
            const isActive = !split && p.id === active.id;
            return (
              <div
                key={p.id}
                className={`file-panels-tab${isActive ? " active" : ""}`}
                title={p.path}
                onClick={() => {
                  if (split) setSplit(false);
                  setActiveId(p.id);
                }}
              >
                <span className="file-panels-tab-name">{name}</span>
                <button
                  className="file-panels-tab-close"
                  title={t("filePanels.closeTab")}
                  onClick={(e) => {
                    e.stopPropagation();
                    onClosePanel(p.id);
                  }}
                >
                  ×
                </button>
              </div>
            );
          })}
        </div>
        {panels.length > 1 && (
          <button
            className="file-panels-cycle"
            onClick={() => cycle(1)}
            title={t("filePanels.nextTab")}
          >
            ›
          </button>
        )}
        {panels.length > 1 && viewport.mode === "desktop" && (
          <button
            className={`file-panels-split${split ? " active" : ""}`}
            onClick={() => setSplit((s) => !s)}
            title={split ? t("filePanels.unsplit") : t("filePanels.split")}
          >
            {split ? t("filePanels.unsplit") : t("filePanels.split")}
          </button>
        )}
      </div>

      <div className={`file-panels-body${split ? " split" : ""}`}>
        {split ? (
          panels.map((p) => (
            <div key={p.id} className="file-panels-pane">
              {renderViewer(p)}
            </div>
          ))
        ) : (
          <div className="file-panels-pane">{renderViewer(active)}</div>
        )}
      </div>
    </div>
  );
}
