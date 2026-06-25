import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { OpenFilePanel } from "../types";
import { API } from "../api";
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

type PanelViewMode = "source" | "browser";

function isHtmlPanel(path: string): boolean {
  const lower = path.toLowerCase();
  return lower.endsWith(".html") || lower.endsWith(".htm");
}

/** Mint a signed preview URL from the backend. Path-based route, so
 * relative asset URLs inside the page resolve to sibling files; the
 * embedded signature is the credential (the sandboxed opaque-origin
 * iframe cannot send the session cookie for its subrequests). */
export async function fetchHtmlPreviewUrl(path: string, nodeId: string): Promise<string> {
  const res = await fetch(
    `${API}/api/file/preview-url?path=${encodeURIComponent(path)}&node_id=${encodeURIComponent(nodeId)}`,
  );
  if (!res.ok) throw new Error(`preview-url failed: ${res.status}`);
  const data = await res.json();
  return `${API}${data.url}`;
}

function BrowserFilePreview({ filePath, nodeId }: { filePath: string; nodeId: string }) {
  const { t } = useTranslation();
  const [src, setSrc] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setSrc(null);
    setLoading(true);
    setError(false);
    fetchHtmlPreviewUrl(filePath, nodeId)
      .then((url) => {
        if (!cancelled) setSrc(url);
      })
      .catch(() => {
        if (!cancelled) {
          setLoading(false);
          setError(true);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [filePath, nodeId]);

  return (
    <div className="file-browser-preview" data-testid="file-browser-preview">
      <div className="file-browser-preview-note">
        {t("filePanels.browserPreviewSandboxNote")}
      </div>
      {error && (
        <div className="file-browser-preview-state">
          {t("filePanels.browserPreviewFailed")}
        </div>
      )}
      {loading && !error && (
        <div className="file-browser-preview-state">
          {t("filePanels.browserPreviewLoading")}
        </div>
      )}
      {src && (
        <iframe
          title={filePath}
          className="file-browser-preview-frame"
          sandbox="allow-scripts allow-popups allow-downloads allow-forms allow-modals"
          src={src}
          onLoad={() => setLoading(false)}
        />
      )}
    </div>
  );
}

/** Tabbed / split container for the session's open file panels.
 *
 * Pure projection of backend `open_file_panels`. Only the active tab,
 * split toggle, and per-tab source/browser preview mode are local
 * transient UI (allowed). In tab mode only the active panel's viewer is
 * mounted; in split mode every panel is mounted side-by-side. */
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
  const previousPanelIdsRef = useRef<string[]>([]);
  const [viewModes, setViewModes] = useState<Record<string, PanelViewMode>>({});
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
    const panelIds = panels.map((p) => p.id);
    const previousPanelIds = previousPanelIdsRef.current;
    const lastPanelId = panelIds[panelIds.length - 1] ?? null;
    const previousLastPanelId = previousPanelIds[previousPanelIds.length - 1] ?? null;
    const openedOrReordered =
      panelIds.length > previousPanelIds.length ||
      (panelIds.length === previousPanelIds.length && lastPanelId !== previousLastPanelId);
    previousPanelIdsRef.current = panelIds;
    if (panels.length === 0) {
      if (activeId !== null) setActiveId(null);
      return;
    }
    if (openedOrReordered && activeId !== lastPanelId) {
      setActiveId(lastPanelId);
      return;
    }
    if (!panels.some((p) => p.id === activeId)) {
      setActiveId(panels[panels.length - 1].id);
    }
  }, [panels, activeId]);

  useEffect(() => {
    const ids = new Set(panels.map((p) => p.id));
    setViewModes((prev) => {
      let changed = false;
      const next: Record<string, PanelViewMode> = {};
      for (const [id, mode] of Object.entries(prev)) {
        if (!ids.has(id)) {
          changed = true;
          continue;
        }
        next[id] = mode;
      }
      return changed ? next : prev;
    });
  }, [panels]);

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

  // Open the window synchronously (popup blockers require a direct
  // user-gesture call), then navigate it once the signed URL arrives.
  // The preview page is CSP-sandboxed into an opaque origin; opener is
  // severed anyway as defense in depth.
  const openInBrowserTab = useCallback(
    (path: string) => {
      const win = window.open("about:blank", "_blank");
      fetchHtmlPreviewUrl(path, nodeId)
        .then((url) => {
          if (!win) return;
          win.opener = null;
          win.location.href = url;
        })
        .catch(() => {
          win?.close();
        });
    },
    [nodeId],
  );

  const setPanelMode = useCallback((id: string, mode: PanelViewMode) => {
    setViewModes((prev) => {
      if (prev[id] === mode || (mode === "source" && prev[id] === undefined)) return prev;
      if (mode === "source") {
        const { [id]: _removed, ...rest } = prev;
        void _removed;
        return rest;
      }
      return { ...prev, [id]: mode };
    });
  }, []);

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

  const currentIds = useMemo(() => new Set(panels.map((p) => p.id)), [panels]);

  if (panels.length === 0) return null;

  const active =
    panels.find((p) => p.id === activeId) ?? panels[panels.length - 1];

  const renderViewer = (panel: OpenFilePanel) => {
    const canBrowserRender = isHtmlPanel(panel.path);
    const mode = canBrowserRender ? viewModes[panel.id] ?? "source" : "source";

    return (
      <div className="file-panels-viewer-shell">
        {canBrowserRender && (
          <div className="file-panels-view-mode" role="group" aria-label={t("filePanels.viewModeLabel")}>
            <button
              type="button"
              className={`btn-small${mode === "source" ? " active" : ""}`}
              onClick={() => setPanelMode(panel.id, "source")}
              title={t("filePanels.viewSourceTitle")}
            >
              {t("filePanels.viewSource")}
            </button>
            <button
              type="button"
              className={`btn-small${mode === "browser" ? " active" : ""}`}
              onClick={() => setPanelMode(panel.id, "browser")}
              title={t("filePanels.renderBrowserTitle")}
            >
              {t("filePanels.renderBrowser")}
            </button>
            <button
              type="button"
              className="btn-small"
              onClick={() => openInBrowserTab(panel.path)}
              title={t("filePanels.openInBrowserTabTitle")}
            >
              {t("filePanels.openInBrowserTab")}
            </button>
          </div>
        )}
        {mode === "browser" ? (
          <BrowserFilePreview filePath={panel.path} nodeId={nodeId} />
        ) : (
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
            onEditorReady={(h) => {
              if (currentIds.has(panel.id)) registerEditor(panel.path, h);
            }}
          />
        )}
      </div>
    );
  };

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
