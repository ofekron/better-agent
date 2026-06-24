import { useState, useEffect, useCallback } from "react";
import { useTranslation } from "react-i18next";
import type { BrowseResult, FileNode, FileSearchResult, SearchMethod } from "../types";
import { trackedFetch, useOpProgress } from "../progress/store";
import { PickerNode } from "./PickerNode";
import Icon from "./Icon";
import { SearchMethods } from "./SearchMethods";
import { useMachines } from "../hooks/useMachines";
import { useLocalNodeId } from "../hooks/useLocalNodeId";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";
import { joinPickerPath } from "src/utils/pathJoin";

import { API } from "../api";
const BROWSE_OP_ID = "dirPicker:browse";

interface Props {
  open: boolean;
  initialPath?: string;
  /** Multi-machine: which node's filesystem the picker starts browsing.
   * Defaults to the local node. The picker shows a machine selector at
   * the top when more than one machine is declared in topology. */
  initialNodeId?: string;
  onCancel: () => void;
  /** Callback receives the picked path AND the machine it belongs to. */
  onPick: (path: string, nodeId: string) => void;
  allowCreate?: boolean;
}

export function DirPickerModal({
  open, initialPath, initialNodeId, onCancel, onPick, allowCreate = true,
}: Props) {
  const { t } = useTranslation();
  useBackButtonDismiss(open, onCancel);
  const localNodeId = useLocalNodeId();
  const { machines } = useMachines();
  const [nodeId, setNodeId] = useState<string>(initialNodeId || localNodeId);
  const [data, setData] = useState<BrowseResult | null>(null);
  const [pathInput, setPathInput] = useState("");
  const [selected, setSelected] = useState("");
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [result, setResult] = useState<FileSearchResult | null>(null);
  const [searching, setSearching] = useState(false);
  const [methods, setMethods] = useState<SearchMethod[]>(["path"]);
  const [newFolderName, setNewFolderName] = useState("");
  const { inflight: loading } = useOpProgress(BROWSE_OP_ID);
  const methodsParam = methods.join(",");

  const browse = useCallback(async (path: string, nodeOverride?: string) => {
    setError("");
    const targetNode = nodeOverride ?? nodeId;
    try {
      const res = await trackedFetch(
        BROWSE_OP_ID,
        `${API}/api/browse?path=${encodeURIComponent(path)}&node_id=${encodeURIComponent(targetNode)}`,
      );
      const r: BrowseResult = await res.json();
      setData(r);
      setPathInput(r.path);
      setSelected(r.path);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Unknown error");
    }
  }, [nodeId]);

  useEffect(() => {
    if (!open) return;
    setSearch("");
    setNewFolderName("");
    setResult(null);
    // Reset to initialNodeId every time the modal opens (caller decides
    // which machine to start on; the picker doesn't persist the choice).
    const startNode = initialNodeId || localNodeId;
    setNodeId(startNode);
    browse(initialPath || "", startNode);
    // browse is stable on nodeId; we pass startNode explicitly to avoid
    // racing the setNodeId state update.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, initialPath, initialNodeId, localNodeId]);

  const root = data?.path || "";
  const query = search.trim();
  useEffect(() => {
    if (!root || !query) {
      setResult(null);
      setSearching(false);
      return;
    }
    setSearching(true);
    const id = setTimeout(() => {
      fetch(
        `${API}/api/files/search?root=${encodeURIComponent(root)}&q=${encodeURIComponent(
          query,
        )}&kind=dir&methods=${encodeURIComponent(methodsParam)}&node_id=${encodeURIComponent(nodeId)}`,
      )
        .then((r) => r.json())
        .then((d: FileSearchResult) => setResult(d))
        .catch(() => setResult(null))
        .finally(() => setSearching(false));
    }, 200);
    return () => clearTimeout(id);
  }, [root, query, methodsParam, nodeId]);

  if (!open) return null;

  const handleGo = (e: React.FormEvent) => {
    e.preventDefault();
    browse(pathInput);
  };

  const createDirectory = async (path: string, pickAfterCreate: boolean) => {
    setError("");
    try {
      const res = await fetch(`${API}/api/files/create`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, kind: "directory", node_id: nodeId }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      const created = await res.json();
      if (pickAfterCreate) {
        onPick(created.path, nodeId);
        return;
      }
      setNewFolderName("");
      await browse(created.path);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Unknown error");
    }
  };

  const createChildDirectory = () => {
    if (!data?.exists || !newFolderName.trim()) return;
    createDirectory(joinPickerPath(data.path, newFolderName), false);
  };

  const pickSelected = () => {
    if (!selected) return;
    if (allowCreate && data && !data.exists && !query) {
      createDirectory(selected, true);
      return;
    }
    onPick(selected, nodeId);
  };

  const selectNode = (node: FileNode) => {
    if (node.type === "directory") setSelected(node.path);
  };
  const pickNode = (node: FileNode) => {
    if (node.type === "directory") onPick(node.path, nodeId);
  };

  const renderResults = () => {
    if (searching && !result)
      return <div className="dir-picker-empty">{t("dirPicker.searching")}</div>;
    if (!result?.root)
      return <div className="dir-picker-empty">{t("dirPicker.searchNoMatches")}</div>;
    return (
      <>
        {result.truncated && (
          <div className="fp-truncated">
            {t("dirPicker.truncated", { count: result.count })}
          </div>
        )}
        {result.root.children?.map((child) => (
          <PickerNode
            key={child.path}
            node={child}
            depth={0}
            selected={selected}
            onSelect={selectNode}
            onActivate={pickNode}
            forceExpanded
          />
        ))}
      </>
    );
  };

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div
        className="modal-content dir-picker-content"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h2>{t("dirPicker.title")}</h2>
          <button className="modal-close" onClick={onCancel}>
            &times;
          </button>
        </div>

        <div className="modal-body">
          {machines.length > 1 && (
            <div className="dir-picker-machine-row">
              <label>{t("dirPicker.machineLabel")}</label>
              <select
                value={nodeId}
                onChange={(e) => {
                  const next = e.target.value;
                  setNodeId(next);
                  // Re-browse from the new node's home dir.
                  browse("", next);
                }}
              >
                {machines.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.id}
                    {m.id === localNodeId ? ` (${t("dirPicker.thisMachine")})` : ""}
                  </option>
                ))}
              </select>
            </div>
          )}
          <form className="dir-picker-address-bar" onSubmit={handleGo}>
            <button
              type="button"
              className="dir-picker-nav-btn"
              onClick={() => data?.parent && browse(data.parent)}
              disabled={!data?.parent}
              title="Parent directory"
            >
              <Icon name="arrow-up" size={16} />
            </button>
            <button
              type="button"
              className="dir-picker-nav-btn"
              onClick={() => browse("")}
              title={t("dirPicker.homeTitle")}
            >
              <Icon name="home" size={16} />
            </button>
            <input
              type="text"
              value={pathInput}
              onChange={(e) => setPathInput(e.target.value)}
              spellCheck={false}
              placeholder={t("dirPicker.pathPlaceholder")}
            />
            <button type="submit" className="dir-picker-nav-btn">
              {t("dirPicker.goButton")}
            </button>
          </form>

          <input
            type="text"
            className="dir-picker-search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t("dirPicker.searchPlaceholder")}
          />

          <SearchMethods
            available={["path", "name"]}
            value={methods}
            onChange={setMethods}
          />

          {error && <div className="setup-error">{error}</div>}

          {!query && allowCreate && data && !data.exists && (
            <div className="dir-picker-create-hint">
              {t("dirPicker.willCreate", { path: data.path })}
            </div>
          )}

          {!query && allowCreate && data?.exists && (
            <div className="picker-create-row">
              <input
                type="text"
                value={newFolderName}
                onChange={(e) => setNewFolderName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    createChildDirectory();
                  }
                }}
                placeholder={t("dirPicker.newFolderPlaceholder")}
              />
              <button
                type="button"
                className="dir-picker-nav-btn"
                onClick={createChildDirectory}
                disabled={!newFolderName.trim()}
                title={t("dirPicker.createFolder")}
              >
                <Icon name="folder" size={14} />
                {t("dirPicker.create")}
              </button>
            </div>
          )}

          <div className="dir-picker-list">
            {query ? (
              renderResults()
            ) : (
              <>
                {loading && (
                  <div className="dir-picker-empty">{t("dirPicker.loading")}</div>
                )}
                {!loading && data && data.exists && data.entries.length === 0 && (
                  <div className="dir-picker-empty">{t("dirPicker.noSubdirs")}</div>
                )}
                {!loading &&
                  data?.entries.map((entry) => (
                    <button
                      key={entry.path}
                      className="dir-picker-row"
                      onClick={() => browse(entry.path)}
                      onDoubleClick={() => onPick(entry.path, nodeId)}
                      title={entry.path}
                    >
                      <span className="dir-picker-icon"><Icon name="folder" size={14} /></span>
                      <span className="dir-picker-name">{entry.name}</span>
                    </button>
                  ))}
              </>
            )}
          </div>
        </div>

        <div className="modal-footer">
          <button className="setup-cancel-btn" onClick={onCancel}>
            {t("dirPicker.cancel")}
          </button>
          <button
            className="setup-save-btn"
            onClick={pickSelected}
            disabled={!selected}
            title={selected}
          >
            {allowCreate && data && !data.exists && !query
              ? t("dirPicker.createDirectory")
              : t("dirPicker.selectDirectory")}
          </button>
        </div>
      </div>
    </div>
  );
}
