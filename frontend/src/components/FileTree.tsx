import { useState, useEffect, useCallback, useRef } from "react";
import Icon from "./Icon";
import { useTranslation } from "react-i18next";
import type { FileNode, FileSearchResult, SearchMethod } from "../types";
import { SearchMethods } from "./SearchMethods";
import { API } from "../api";
import { fetchWithRetry } from "../utils/fetchRetry";
import { joinPickerPath } from "src/utils/pathJoin";

interface Props {
  cwd: string;
  /** Multi-machine: which node's filesystem to browse. Defaults to
   * "primary" (the local sentinel) when omitted. */
  nodeId?: string;
  onFileClick: (path: string) => void;
  onEngineerFile?: (path: string) => void;
  collapsed?: boolean;
  onToggleCollapsed?: () => void;
  allowCreate?: boolean;
}

function TreeNode({
  node,
  depth,
  onFileClick,
  onEngineerFile,
  forceExpanded,
}: {
  node: FileNode;
  depth: number;
  onFileClick: (path: string) => void;
  onEngineerFile?: (path: string) => void;
  forceExpanded?: boolean;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(depth < 1);
  const [hovered, setHovered] = useState(false);
  const isOpen = forceExpanded || expanded;

  if (node.type === "file") {
    return (
      <div
        className="tree-node tree-file"
        style={{ paddingInlineStart: depth * 16 }}
        onClick={() => onFileClick(node.path)}
        onMouseEnter={() => setHovered(true)}
        onMouseLeave={() => setHovered(false)}
      >
        {node.name}
        {hovered && onEngineerFile && (
          <button
            className="tree-file-edit-btn"
            onClick={(e) => {
              e.stopPropagation();
              onEngineerFile(node.path);
            }}
            title={t("files.editWithAiTitle")}
          >
            <Icon name="edit" size={14} />
          </button>
        )}
      </div>
    );
  }

  return (
    <div>
      <div
        className="tree-node tree-dir"
        style={{ paddingInlineStart: depth * 16 }}
        onClick={() => !forceExpanded && setExpanded(!expanded)}
      >
        <span className="tree-arrow">{isOpen ? "v" : ">"}</span> {node.name}
      </div>
      {isOpen &&
        node.children?.map((child) => (
          <TreeNode
            key={child.path}
            node={child}
            depth={depth + 1}
            onFileClick={onFileClick}
            onEngineerFile={onEngineerFile}
            forceExpanded={forceExpanded}
          />
        ))}
    </div>
  );
}

export function FileTree({
  cwd,
  nodeId = "primary",
  onFileClick,
  onEngineerFile,
  collapsed = false,
  onToggleCollapsed,
  allowCreate = true,
}: Props) {
  const { t } = useTranslation();
  const [tree, setTree] = useState<FileNode | null>(null);
  const [search, setSearch] = useState("");
  const [searching, setSearching] = useState(false);
  const [result, setResult] = useState<FileSearchResult | null>(null);
  const [methods, setMethods] = useState<SearchMethod[]>(["path", "name"]);
  const [newName, setNewName] = useState("");
  const [createKind, setCreateKind] = useState<"file" | "directory">("file");
  const [createError, setCreateError] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);
  const methodsParam = methods.join(",");

  const refresh = useCallback(async () => {
    if (!cwd) return;
    try {
      const res = await fetchWithRetry(
        `${API}/api/files?path=${encodeURIComponent(cwd)}&node_id=${encodeURIComponent(nodeId)}`
      );
      const data = await res.json();
      setTree(data);
    } catch {
      // retried 3x, still failed
    }
  }, [cwd, nodeId]);

  useEffect(() => {
    if (!collapsed) refresh();
    else {
      setSearch("");
      setResult(null);
      setNewName("");
      setCreateError("");
    }
  }, [refresh, collapsed]);

  // Debounced backend search
  const query = search.trim();
  useEffect(() => {
    if (!cwd || !query) {
      setResult(null);
      setSearching(false);
      return;
    }
    setSearching(true);
    const id = setTimeout(() => {
      fetchWithRetry(
        `${API}/api/files/search?root=${encodeURIComponent(cwd)}&q=${encodeURIComponent(
          query,
        )}&kind=file&methods=${encodeURIComponent(methodsParam)}&node_id=${encodeURIComponent(nodeId)}`,
      )
        .then((r) => r.json())
        .then((d: FileSearchResult) => setResult(d))
        .catch(() => setResult(null))
        .finally(() => setSearching(false));
    }, 200);
    return () => clearTimeout(id);
  }, [cwd, query, methodsParam, nodeId]);

  const header = (
    <div className="file-tree-header">
      <button
        type="button"
        className="file-tree-toggle"
        onClick={onToggleCollapsed}
        aria-expanded={!collapsed}
        title={collapsed ? t("files.expandTitle") : t("files.collapseTitle")}
      >
        <span className="collapse-arrow">{collapsed ? <Icon name="chevron-right" size={12} /> : <Icon name="chevron-down" size={12} />}</span>
        <span>{t("files.header")}</span>
      </button>
      {!collapsed && (
        <button
          className="btn-small"
          onClick={(e) => {
            e.stopPropagation();
            refresh();
          }}
        >
          {t("files.refreshButton")}
        </button>
      )}
    </div>
  );

  if (collapsed) {
    return <div className="file-tree file-tree-collapsed">{header}</div>;
  }

  const createEntry = async () => {
    const name = newName.trim();
    if (!cwd || !name) return;
    setCreateError("");
    const path = joinPickerPath(cwd, name);
    try {
      const res = await fetchWithRetry(`${API}/api/files/create`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, kind: createKind, node_id: nodeId }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      const created = await res.json();
      setNewName("");
      await refresh();
      if (created.type === "file") onFileClick(created.path);
    } catch (e: unknown) {
      setCreateError(e instanceof Error ? e.message : "Unknown error");
    }
  };

  const renderBody = () => {
    if (query) {
      if (searching && !result)
        return <div className="file-tree-empty">{t("files.searching")}</div>;
      if (!result?.root)
        return <div className="file-tree-empty">{t("files.noResults")}</div>;
      return (
        <>
          {result.symbols_unavailable && (
            <div className="file-tree-notice">{t("files.symbolsUnavailable")}</div>
          )}
          {result.truncated && (
            <div className="file-tree-result-count">
              {t("files.truncated", { count: result.count })}
            </div>
          )}
          {!result.truncated && (
            <div className="file-tree-result-count">
              {t("files.resultCount", { count: result.count })}
            </div>
          )}
          {result.root.children?.map((child) => (
            <TreeNode
              key={child.path}
              node={child}
              depth={0}
              onFileClick={onFileClick}
              onEngineerFile={onEngineerFile}
              forceExpanded
            />
          ))}
        </>
      );
    }
    if (!tree?.children?.length)
      return <div className="file-tree-empty">{t("files.noWorkspace")}</div>;
    return tree.children.map((child) => (
      <TreeNode
        key={child.path}
        node={child}
        depth={0}
        onFileClick={onFileClick}
        onEngineerFile={onEngineerFile}
      />
    ));
  };

  return (
    <div className="file-tree">
      {header}
      <div className="file-tree-search">
        <input
          ref={searchRef}
          type="text"
          className="file-tree-search-input"
          placeholder={t("files.searchPlaceholder")}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              setSearch("");
              searchRef.current?.blur();
            }
          }}
        />
        {search && (
          <button
            className="file-tree-search-clear"
            onClick={() => setSearch("")}
            title={t("files.clearSearch")}
          >
            ×
          </button>
        )}
      </div>
      {query && (
        <div className="file-tree-methods">
          <SearchMethods
            available={["path", "name", "symbols"]}
            value={methods}
            onChange={setMethods}
          />
        </div>
      )}
      {allowCreate && !query && (
        <div className="picker-create-row file-tree-create-row">
          <select
            value={createKind}
            onChange={(e) => setCreateKind(e.target.value as "file" | "directory")}
            aria-label={t("filePicker.createKind")}
          >
            <option value="file">{t("filePicker.file")}</option>
            <option value="directory">{t("filePicker.folder")}</option>
          </select>
          <input
            type="text"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                createEntry();
              }
            }}
            placeholder={t("filePicker.newEntryPlaceholder")}
          />
          <button
            type="button"
            className="btn-small"
            onClick={createEntry}
            disabled={!newName.trim()}
          >
            {t("filePicker.create")}
          </button>
        </div>
      )}
      {createError && <div className="setup-error file-tree-create-error">{createError}</div>}
      <div className="file-tree-content">
        {renderBody()}
      </div>
    </div>
  );
}
