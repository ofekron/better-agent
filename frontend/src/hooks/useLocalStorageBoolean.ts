import { useEffect, useState, type Dispatch, type SetStateAction } from "react";

// Persist a boolean state to localStorage as "1"/"0". `defaultValue`
// only applies when the key is absent (`getItem === null`); any
// non-"1" stored value (incl. "0" or junk) deserializes to false,
// matching the inlined `localStorage.getItem(K) === "1"` pattern this
// hook replaces. On every value change (and on mount) the new value
// is written back so a tab refresh picks it up.
// The setter is the full `Dispatch<SetStateAction<boolean>>` so callers
// can pass either a value or a functional updater (e.g. `set(v => !v)`).
export function useLocalStorageBoolean(
  key: string,
  defaultValue = false,
): [boolean, Dispatch<SetStateAction<boolean>>] {
  const [value, setValue] = useState(() => {
    const stored = localStorage.getItem(key);
    if (stored === null) return defaultValue;
    return stored === "1";
  });
  useEffect(() => {
    localStorage.setItem(key, value ? "1" : "0");
  }, [key, value]);
  return [value, setValue];
}
