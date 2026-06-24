import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Auto-saving text draft backed by localStorage. Unlike `useLocalStorage`,
 * this re-reads the stored value whenever `key` changes (e.g. switching
 * files), so an in-progress draft is restored when the user returns and is
 * never lost on unmount. `clear` removes the entry (call after a successful
 * submit/cancel). When `key` is null the draft is in-memory only.
 */
export function usePersistedDraft(
  key: string | null,
): [string, (v: string) => void, () => void] {
  const read = useCallback((k: string | null): string => {
    if (!k) return "";
    try {
      return localStorage.getItem(k) ?? "";
    } catch {
      return "";
    }
  }, []);

  const [value, setValue] = useState<string>(() => read(key));
  const keyRef = useRef(key);

  // Re-hydrate from the new key when it changes.
  useEffect(() => {
    if (keyRef.current === key) return;
    keyRef.current = key;
    setValue(read(key));
  }, [key, read]);

  const set = useCallback(
    (v: string) => {
      setValue(v);
      if (!key) return;
      try {
        if (v) localStorage.setItem(key, v);
        else localStorage.removeItem(key);
      } catch {
        /* quota / private mode — draft stays in memory */
      }
    },
    [key],
  );

  const clear = useCallback(() => {
    setValue("");
    if (!key) return;
    try {
      localStorage.removeItem(key);
    } catch {
      /* ignore */
    }
  }, [key]);

  return [value, set, clear];
}
