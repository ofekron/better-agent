import { useCallback, useRef, useEffect, useState } from "react";
import Icon from "./Icon";
import { useTranslation } from "react-i18next";
import type { InlineTag } from "../types/inlineTag";
import { isSaveShortcutEvent } from "../hooks/useSaveShortcut";

interface Props {
  tags: InlineTag[];
  onRemove: (id: string) => void;
  onUpdate: (id: string, updates: { comment?: string }) => void;
  focusedCommentId: string | null;
  onFocusComment: (id: string | null) => void;
  /** Tag ID that should auto-enter edit mode (newly created with empty comment). */
  autoEditId?: string | null;
  /** Called once after autoEditId is consumed (edit started). */
  onAutoEditConsumed?: () => void;
}

export function CommentsPanel({
  tags,
  onRemove,
  onUpdate,
  focusedCommentId,
  onFocusComment,
  autoEditId,
  onAutoEditConsumed,
}: Props) {
  const { t } = useTranslation();
  const innerRef = useRef<HTMLDivElement>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState("");

  // Auto-start editing for newly created tags with empty comments.
  useEffect(() => {
    if (!autoEditId) return;
    const tag = tags.find((t) => t.id === autoEditId);
    if (!tag) return;
    setEditingId(autoEditId);
    setEditText(tag.comment);
    onAutoEditConsumed?.();
  }, [autoEditId, tags, onAutoEditConsumed]);

  // Scroll the focused card into view within the panel.
  useEffect(() => {
    if (!focusedCommentId || !innerRef.current) return;
    const card = innerRef.current.querySelector(
      `[data-comment-id="${focusedCommentId}"]`,
    ) as HTMLElement | null;
    card?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [focusedCommentId]);

  const handleClick = useCallback(
    (id: string) => {
      if (editingId === id) return;
      onFocusComment(focusedCommentId === id ? null : id);
    },
    [focusedCommentId, onFocusComment, editingId],
  );

  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Save the comment text. Never auto-remove — removal is explicit via the ×
  // button only, so clicking outside a just-started comment keeps it.
  const persist = useCallback(
    (id: string, text: string) => {
      onUpdate(id, { comment: text });
    },
    [onUpdate],
  );

  const startEdit = useCallback((id: string, currentComment: string) => {
    setEditingId(id);
    setEditText(currentComment);
  }, []);

  // Always-save: persist every keystroke (debounced) — no Save/Cancel.
  const handleEditChange = useCallback(
    (id: string, text: string) => {
      setEditText(text);
      if (saveTimer.current) clearTimeout(saveTimer.current);
      saveTimer.current = setTimeout(() => persist(id, text), 300);
    },
    [persist],
  );

  // Flush the pending save and leave edit mode (blur / Enter / Escape).
  const finishEdit = useCallback(() => {
    if (saveTimer.current) {
      clearTimeout(saveTimer.current);
      saveTimer.current = null;
    }
    if (editingId) persist(editingId, editText);
    setEditingId(null);
  }, [editingId, editText, persist]);

  useEffect(
    () => () => {
      if (saveTimer.current) clearTimeout(saveTimer.current);
    },
    [],
  );

  if (tags.length === 0) {
    return (
      <div className="comments-panel-empty">
        {t("comments.empty", "No comments yet. Select text in the chat to add one.")}
      </div>
    );
  }

  return (
    <div className="comments-panel-content" ref={innerRef}>
      {tags.map((tag) => {
        const isFocused = focusedCommentId === tag.id;
        const isHovered = hoveredId === tag.id;
        const isEditing = editingId === tag.id;
        const fileHeader = tag.fileAnchor
          ? tag.fileAnchor.startLine !== undefined
            ? `${tag.fileAnchor.filePath}:${tag.fileAnchor.startLine}${tag.fileAnchor.endLine !== tag.fileAnchor.startLine ? `-${tag.fileAnchor.endLine}` : ""}`
            : tag.fileAnchor.filePath
          : null;
        return (
          <div
            key={tag.id}
            data-comment-id={tag.id}
            className={`comments-panel-card${isFocused ? " focused" : ""}${isHovered ? " hovered" : ""}`}
            onClick={() => handleClick(tag.id)}
            onMouseEnter={() => setHoveredId(tag.id)}
            onMouseLeave={() => setHoveredId(null)}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => {
              const el = (e.target as HTMLElement).tagName;
              if (el === "TEXTAREA" || el === "INPUT") return;
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                handleClick(tag.id);
              }
            }}
          >
            <div className="comments-panel-card-header">
              {tag.displayNumber !== undefined && (
                <span className="comments-panel-card-number">
                  {tag.displayNumber}
                </span>
              )}
              {fileHeader && (
                <div className="inline-tags-card-anchor">{fileHeader}</div>
              )}
              <div className="comments-panel-card-actions">
                <button
                  className="comments-panel-card-edit"
                  onClick={(e) => {
                    e.stopPropagation();
                    startEdit(tag.id, tag.comment);
                  }}
                  aria-label={t("comments.edit", "Edit comment")}
                  title={t("comments.edit", "Edit comment")}
                >
                  <Icon name="edit" size={14} />
                </button>
                <button
                  className="comments-panel-card-remove"
                  onClick={(e) => {
                    e.stopPropagation();
                    onRemove(tag.id);
                  }}
                  aria-label={t("comments.remove", "Remove comment")}
                  title={t("comments.remove", "Remove comment")}
                >
                  ×
                </button>
              </div>
            </div>
            {tag.selectedText && (
              <div className="comments-panel-card-text">{tag.selectedText}</div>
            )}
            {isEditing ? (
              <div className="comments-panel-card-edit-area">
                <textarea
                  className="comments-panel-card-textarea"
                  value={editText}
                  onChange={(e) => handleEditChange(tag.id, e.target.value)}
                  onBlur={finishEdit}
                  autoFocus
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      finishEdit();
                    }
                    if (e.key === "Escape") {
                      e.stopPropagation();
                      finishEdit();
                    }
                    if (isSaveShortcutEvent(e)) {
                      e.preventDefault();
                      finishEdit();
                    }
                  }}
                />
              </div>
            ) : (
              <div className="comments-panel-card-comment">{tag.comment}</div>
            )}
          </div>
        );
      })}
    </div>
  );
}
