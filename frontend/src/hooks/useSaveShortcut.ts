import { useEffect, useRef, type RefObject } from "react";

type SaveShortcutEvent = {
  key: string;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
  preventDefault: () => void;
};

export function isSaveShortcutEvent(event: SaveShortcutEvent): boolean {
  return (
    event.key.toLowerCase() === "s" &&
    (event.ctrlKey || event.metaKey) &&
    !event.altKey &&
    !event.shiftKey
  );
}

export function useSaveShortcut({
  enabled,
  onSave,
  targetRef,
}: {
  enabled: boolean;
  onSave: () => void;
  targetRef?: RefObject<HTMLElement | null>;
}) {
  const onSaveRef = useRef(onSave);
  onSaveRef.current = onSave;

  useEffect(() => {
    if (!enabled) return;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (!isSaveShortcutEvent(event)) return;
      const target = targetRef?.current;
      if (target && !target.contains(document.activeElement)) return;
      event.preventDefault();
      onSaveRef.current();
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [enabled, targetRef]);
}
