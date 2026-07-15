import { useState, useEffect, useRef } from "react";
import { API } from "../api";
import { runThreeStateSync, trackPromise } from "../progress/store";
import { useTranslation } from "react-i18next";

const DEFAULTS = [
  "TLDR",
  "Didn't read, but I trust you go ahead",
  "/Adv",
  "Confirmed Go ahead",
];

export function ShortcutSettings() {
  const { t } = useTranslation();
  const [shortcuts, setShortcuts] = useState<string[]>(DEFAULTS);
  const [newShortcut, setNewShortcut] = useState("");
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    trackPromise("shortcuts:load", () => fetch(`${API}/api/user-prefs`))
      .promise
      .then((r: Response) => r.json())
      .then((data: { shortcut_responses?: string[] }) => {
        if (data.shortcut_responses) setShortcuts(data.shortcut_responses);
      })
      .catch(() => {});
  }, []);

  const save = async (updated: string[]) => {
    const previous = shortcuts;
    setShortcuts(updated);
    setSaving(true);
    try {
      await runThreeStateSync({
        operationId: "shortcuts:save",
        action: t("settings.quickReplies", "Quick Replies"),
        reconcile: async () => {
          const response = await fetch(`${API}/api/user-prefs`);
          if (!response.ok) { setShortcuts(previous); return; }
          const prefs = await response.json() as { shortcut_responses?: string[] };
          setShortcuts(prefs.shortcut_responses || previous);
        },
        mutate: async () => {
          const response = await fetch(`${API}/api/user-prefs`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ shortcut_responses: updated }),
          });
          if (!response.ok) throw new Error(await response.text());
          return response;
        },
      });
    } catch {
      return;
    } finally {
      setSaving(false);
    }
    window.dispatchEvent(
      new CustomEvent("shortcut_responses_changed", { detail: updated }),
    );
  };

  const remove = (i: number) => {
    const updated = shortcuts.filter((_, idx) => idx !== i);
    void save(updated);
  };

  const add = () => {
    const trimmed = newShortcut.trim();
    if (!trimmed || shortcuts.includes(trimmed)) return;
    const updated = [...shortcuts, trimmed];
    setNewShortcut("");
    void save(updated);
    inputRef.current?.focus();
  };

  const resetDefaults = () => {
    void save([...DEFAULTS]);
  };

  return (
    <div className="shortcut-settings">
      <div className="shortcut-settings-header">
        <label>Quick Replies</label>
        <button
          type="button"
          className="btn-secondary"
          onClick={resetDefaults}
          disabled={saving}
        >
          Reset
        </button>
      </div>
      <div className="shortcut-settings-list">
        {shortcuts.map((s, i) => (
          <div key={i} className="shortcut-settings-item">
            <span className="shortcut-settings-text">{s}</span>
            <button
              type="button"
              className="shortcut-settings-remove"
              onClick={() => remove(i)}
              disabled={saving}
              title="Remove"
            >
              ×
            </button>
          </div>
        ))}
      </div>
      <div className="shortcut-settings-add">
        <input
          ref={inputRef}
          type="text"
          value={newShortcut}
          onChange={(e) => setNewShortcut(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") add();
          }}
          placeholder="Add shortcut…"
          disabled={saving}
        />
        <button
          type="button"
          className="btn-secondary"
          onClick={add}
          disabled={saving || !newShortcut.trim()}
        >
          +
        </button>
      </div>
    </div>
  );
}
