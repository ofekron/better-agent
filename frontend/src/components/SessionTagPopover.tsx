import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";
import type { SessionTag } from "../types";
import { SearchInput } from "./SearchInput";

/** Viewport-coord anchor of the trigger (from getBoundingClientRect). */
export interface PopoverAnchor {
  top: number;
  bottom: number;
  left: number;
}

interface Props {
  anchor: PopoverAnchor;
  /** Every project tag — toggling membership is the whole point. */
  tags: SessionTag[];
  assignedTagIds: Set<string>;
  onToggle: (tagId: string) => void;
  /** Create a new project tag AND assign it to this session. Omit to hide
   *  the inline "Create" affordance. */
  onCreateTag?: (name: string) => void;
  onClose: () => void;
}

const GAP = 4;
const MARGIN = 8;

/** Fixed-position tag editor anchored to its trigger. Uses viewport
 *  coords (position: fixed) so it is never clipped by the sidebar's
 *  overflow-y: auto container. Position is measured after mount (before
 *  paint) and flipped/clamped to stay on screen — works in LTR, RTL,
 *  and narrow mobile viewports. Modeled on RewindPopover. */
export function SessionTagPopover({
  anchor,
  tags,
  assignedTagIds,
  onToggle,
  onCreateTag,
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

  // Measure after mount (before paint): flip up if it would overflow the
  // bottom, and clamp horizontally so it never leaves the viewport.
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
  // Assigned tags first (so everything that applies is visible up top),
  // then alphabetical — within the current search filter.
  const visible = useMemo(() => {
    const base = q ? tags.filter((tg) => tg.name.toLowerCase().includes(q)) : tags;
    return [...base].sort((a, b) => {
      const ax = assignedTagIds.has(a.id) ? 0 : 1;
      const bx = assignedTagIds.has(b.id) ? 0 : 1;
      if (ax !== bx) return ax - bx;
      return a.name.localeCompare(b.name);
    });
  }, [tags, q, assignedTagIds]);

  const exactMatch = q
    ? tags.some((tg) => tg.name.toLowerCase() === q)
    : true;
  const canCreate = !!onCreateTag && q.length > 0 && !exactMatch;

  const handleCreate = () => {
    if (!canCreate) return;
    onCreateTag?.(query.trim());
    setQuery("");
  };

  return (
    <div
      ref={ref}
      className="session-tag-popover"
      role="dialog"
      aria-modal="false"
      aria-label={t("session.tags")}
      style={pos}
    >
      <div className="session-tag-popover-header">{t("session.tagPopoverTitle")}</div>
      <SearchInput
        type="text"
        className="session-tag-popover-search"
        placeholder={t("session.tagSearch")}
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
      {visible.length === 0 ? (
        <div className="session-tag-popover-empty">
          {tags.length === 0 ? t("session.noTagsYet") : t("session.noTagMatch")}
        </div>
      ) : (
        <div className="session-tag-popover-list">
          {visible.map((tag) => {
            const active = assignedTagIds.has(tag.id);
            return (
              <button
                key={tag.id}
                type="button"
                className={`session-tag-toggle ${active ? "active" : ""}`}
                aria-pressed={active}
                title={tag.name}
                onClick={() => onToggle(tag.id)}
              >
                {tag.color && (
                  <span
                    className="session-tag-color-dot"
                    style={{ background: tag.color }}
                    aria-hidden="true"
                  />
                )}
                {tag.name}
              </button>
            );
          })}
        </div>
      )}
      {canCreate && (
        <button
          type="button"
          className="session-tag-popover-create"
          onClick={handleCreate}
        >
          {t("session.createTag", { name: query.trim() })}
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
