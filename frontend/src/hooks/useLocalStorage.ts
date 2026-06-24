import { useState, useEffect, useCallback } from "react";

/**
 * Persist a value to localStorage as JSON. Returns `[value, setValue]`.
 * `defaultValue` applies when the key is absent. On every change the new
 * value is written back so a tab refresh picks it up.
 */
export function useLocalStorage<T>(
  key: string,
  defaultValue: T,
): [T, (v: T | ((prev: T) => T)) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const stored = localStorage.getItem(key);
      if (stored === null) return defaultValue;
      return JSON.parse(stored) as T;
    } catch {
      return defaultValue;
    }
  });

  useEffect(() => {
    localStorage.setItem(key, JSON.stringify(value));
  }, [key, value]);

  const setter = useCallback(
    (v: T | ((prev: T) => T)) => {
      setValue(v);
    },
    [],
  );

  return [value, setter];
}
