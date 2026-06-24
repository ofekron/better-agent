import { useState, useEffect, useRef } from "react";
import { API } from "../api";
import { trackPromise } from "../progress/store";

const DEFAULTS = [
  "TLDR",
  "Didn't read, but I trust you go ahead",
  "/Adv",
  "Confirmed Go ahead",
];

export function ShortcutSettings() {
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
    setSaving(true);
    try {
      await trackPromise(
        "shortcuts:save",
        () => fetch(`${API}/api/user-prefs`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ shortcut_responses: updated }),
        }),
      ).promise;
    } catch {
      return;
    } finally {
      setSaving(false);
    }
    setShortcuts(updated);
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
