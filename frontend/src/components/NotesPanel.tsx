import { useState, useRef, useCallback, useEffect } from "react";
import Icon from "./Icon";
import { useTranslation } from "react-i18next";
import type { Note } from "../types";

interface Props {
  notes: Note[];
  onRemove: (noteId: string) => void;
  onEdit: (noteId: string, text: string) => void;
  onSendToPrompt: (noteId: string, text: string) => void;
}

export function NotesPanel({ notes, onRemove, onEdit, onSendToPrompt }: Props) {
  const { t } = useTranslation();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState("");
  // Track whether an action button caused the blur so commitEdit can skip
  const actionBtnPressed = useRef(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const [stickToBottom, setStickToBottom] = useState(true);

  // Auto-scroll to bottom when notes change and stick is active
  useEffect(() => {
    if (!stickToBottom) return;
    const el = containerRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [notes, stickToBottom]);

  const handleScroll = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    const isAtBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
    setStickToBottom(isAtBottom);
  }, []);

  // Hooks must be called before any early returns
  const commitEdit = useCallback(() => {
    if (actionBtnPressed.current) {
      actionBtnPressed.current = false;
      return;
    }
    if (editingId && editText.trim()) {
      onEdit(editingId, editText.trim());
    }
    setEditingId(null);
    setEditText("");
  }, [editingId, editText, onEdit]);

  const startEdit = useCallback((note: Note) => {
    setEditingId(note.id);
    setEditText(note.text);
  }, []);

  const handleAction = useCallback((fn: () => void) => {
    actionBtnPressed.current = true;
    setEditingId(null);
    setEditText("");
    fn();
  }, []);

  if (notes.length === 0) {
    return (
      <div className="notes-panel-empty">
        <p>{t("notes.empty", "No notes yet")}</p>
        <p className="notes-panel-hint">
          {t("notes.hint", "Use the note button in the input area to save text as a note")}
        </p>
      </div>
    );
  }

  return (
    <div className="notes-panel" ref={containerRef} onScroll={handleScroll}>
      {notes.map((note) => (
        <div key={note.id} className="note-card">
          {editingId === note.id ? (
            <textarea
              className="note-edit-textarea"
              value={editText}
              onChange={(e) => setEditText(e.target.value)}
              onBlur={commitEdit}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  actionBtnPressed.current = true;
                  commitEdit();
                } else if (e.key === "Escape") {
                  setEditingId(null);
                  setEditText("");
                }
              }}
              autoFocus
              rows={3}
            />
          ) : (
            <p className="note-text">{note.text}</p>
          )}
          <div className="note-actions">
            <button
              className="note-action-btn note-send-btn"
              onMouseDown={(e) => { e.preventDefault(); handleAction(() => onSendToPrompt(note.id, note.text)); }}
              title={t("notes.sendToPrompt", "Send to prompt")}
            >
              ↑
            </button>
            <button
              className="note-action-btn note-edit-btn"
              onMouseDown={(e) => { e.preventDefault(); handleAction(() => startEdit(note)); }}
              title={t("notes.edit", "Edit")}
            >
              <Icon name="edit" size={14} />
            </button>
            <button
              className="note-action-btn note-remove-btn"
              onMouseDown={(e) => { e.preventDefault(); handleAction(() => onRemove(note.id)); }}
              title={t("notes.remove", "Remove")}
            >
              ×
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
