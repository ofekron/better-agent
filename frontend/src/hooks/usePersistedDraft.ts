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

/**
 * Same auto-saving/restoring contract as `usePersistedDraft`, generalized to
 * any JSON-serializable value (e.g. draft attachments). `empty` is both the
 * initial value and what `clear`/an empty-equivalent `set` resets to.
 */
export function usePersistedJSONDraft<T>(
  key: string | null,
  empty: T,
): [T, (v: T | ((prev: T) => T)) => void, () => void] {
  // Callers must pass a stable (module-level) `empty` value — it's a real
  // dependency here, not a fresh literal per render.
  const read = useCallback(
    (k: string | null): T => {
      if (!k) return empty;
      try {
        const raw = localStorage.getItem(k);
        return raw ? (JSON.parse(raw) as T) : empty;
      } catch {
        return empty;
      }
    },
    [empty],
  );

  const [value, setValue] = useState<T>(() => read(key));
  const keyRef = useRef(key);

  useEffect(() => {
    if (keyRef.current === key) return;
    keyRef.current = key;
    setValue(read(key));
  }, [key, read]);

  const isEmpty = useCallback((v: T) => Array.isArray(v) ? v.length === 0 : !v, []);

  const set = useCallback(
    (v: T | ((prev: T) => T)) => {
      setValue((prev) => {
        const next = typeof v === "function" ? (v as (prev: T) => T)(prev) : v;
        if (key) {
          try {
            if (!isEmpty(next)) localStorage.setItem(key, JSON.stringify(next));
            else localStorage.removeItem(key);
          } catch {
            /* quota / private mode — draft stays in memory */
          }
        }
        return next;
      });
    },
    [key, isEmpty],
  );

  const clear = useCallback(() => {
    setValue(empty);
    if (!key) return;
    try {
      localStorage.removeItem(key);
    } catch {
      /* ignore */
    }
  }, [key, empty]);

  return [value, set, clear];
}
