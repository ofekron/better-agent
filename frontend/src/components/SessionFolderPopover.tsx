import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";
import { buildFolderPathMap } from "../sessionFolders";
import type { SessionFolder } from "../types";
import type { PopoverAnchor } from "./SessionTagPopover";
import { SearchInput } from "./SearchInput";

interface Props {
  anchor: PopoverAnchor;
  folders: SessionFolder[];
  assignedFolderId: string | null;
  onSelect: (folderId: string | null) => void;
  onCreateFolder?: (name: string) => void;
  onClose: () => void;
}

const GAP = 4;
const MARGIN = 8;

export function SessionFolderPopover({
  anchor,
  folders,
  assignedFolderId,
  onSelect,
  onCreateFolder,
  onClose,
}: Props) {
  const { t } = useTranslation();
  const ref = useRef<HTMLDivElement>(null);
  const [query, setQuery] = useState("");
  const [pos, setPos] = useState<React.CSSProperties>({
    position: "fixed",
    zIndex: 1000,
    top: anchor.bottom + GAP,
    left: anchor.left,
  });

  useBackButtonDismiss(true, onClose);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    let top: number | undefined = anchor.bottom + GAP;
    let bottom: number | undefined;
    if (top + r.height > vh - MARGIN) {
      bottom = vh - anchor.top + GAP;
      top = undefined;
    }
    let left = anchor.left;
    if (left + r.width > vw - MARGIN) left = vw - r.width - MARGIN;
    if (left < MARGIN) left = MARGIN;
    setPos({
      position: "fixed",
      zIndex: 1000,
      ...(bottom !== undefined ? { bottom } : { top }),
      left,
    });
  }, [anchor.top, anchor.bottom, anchor.left]);

  useEffect(() => {
    const onDocClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  const q = query.trim().toLowerCase();
  const paths = useMemo(() => buildFolderPathMap(folders), [folders]);
  const visible = useMemo(() => {
    const base = q
      ? folders.filter((folder) =>
          (paths.get(folder.id) ?? folder.name).toLowerCase().includes(q),
        )
      : folders;
    return [...base].sort((a, b) => {
      const ax = assignedFolderId === a.id ? 0 : 1;
      const bx = assignedFolderId === b.id ? 0 : 1;
      if (ax !== bx) return ax - bx;
      return (paths.get(a.id) ?? a.name).localeCompare(paths.get(b.id) ?? b.name);
    });
  }, [folders, paths, q, assignedFolderId]);

  const exactMatch = q
    ? folders.some((folder) => folder.name.toLowerCase() === q)
    : true;
  const canCreate = !!onCreateFolder && q.length > 0 && !exactMatch;

  const handleCreate = () => {
    if (!canCreate) return;
    onCreateFolder?.(query.trim());
    setQuery("");
  };

  return (
    <div
      ref={ref}
      className="session-tag-popover"
      role="dialog"
      aria-modal="false"
      aria-label={t("session.folder")}
      style={pos}
    >
      <div className="session-tag-popover-header">{t("session.folderPopoverTitle")}</div>
      <SearchInput
        type="text"
        className="session-tag-popover-search"
        placeholder={t("session.folderSearch")}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            if (canCreate) handleCreate();
          }
        }}
        autoFocus
      />
      <div className="session-tag-popover-list">
        {!q && (
          <button
            type="button"
            className={`session-tag-toggle ${assignedFolderId ? "" : "active"}`}
            aria-pressed={!assignedFolderId}
            onClick={() => onSelect(null)}
          >
            {t("session.unfiled")}
          </button>
        )}
        {visible.map((folder) => {
          const active = assignedFolderId === folder.id;
          return (
            <button
              key={folder.id}
              type="button"
              className={`session-tag-toggle ${active ? "active" : ""}`}
              aria-pressed={active}
              title={paths.get(folder.id) ?? folder.name}
              onClick={() => onSelect(folder.id)}
            >
              {paths.get(folder.id) ?? folder.name}
            </button>
          );
        })}
      </div>
      {visible.length === 0 && !canCreate && (
        <div className="session-tag-popover-empty">{t("session.noFolderMatch")}</div>
      )}
      {canCreate && (
        <button
          type="button"
          className="session-tag-popover-create"
          onClick={handleCreate}
        >
          {t("session.createFolder", { name: query.trim() })}
        </button>
      )}
      <button
        type="button"
        className="btn-small session-tag-popover-done"
        onClick={onClose}
      >
        {t("session.done")}
      </button>
    </div>
  );
}
