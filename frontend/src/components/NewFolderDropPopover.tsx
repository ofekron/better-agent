import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";
import type { PopoverAnchor } from "./SessionTagPopover";

interface Props {
  anchor: PopoverAnchor;
  onCreate: (name: string) => void;
  onClose: () => void;
}

const GAP = 4;
const MARGIN = 8;

/** Inline name prompt shown after dropping a session onto the "New folder"
 * drop target. Replaces window.prompt, which pywebview's OS webview does
 * not implement (returns null silently). */
export function NewFolderDropPopover({ anchor, onCreate, onClose }: Props) {
  const { t } = useTranslation();
  const ref = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [name, setName] = useState("");
  const [pos, setPos] = useState<React.CSSProperties>({
    position: "fixed",
    zIndex: 1000,
    top: anchor.bottom + GAP,
    left: anchor.left,
  });

  useBackButtonDismiss(true, onClose);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    let left = anchor.left;
    if (left + r.width > vw - MARGIN) left = vw - r.width - MARGIN;
    if (left < MARGIN) left = MARGIN;
    let top = anchor.bottom + GAP;
    if (top + r.height > vh - MARGIN) top = Math.max(MARGIN, anchor.top - r.height - GAP);
    setPos({ position: "fixed", zIndex: 1000, top, left });
  }, [anchor.top, anchor.bottom, anchor.left]);

  useEffect(() => {
    inputRef.current?.focus();
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

  const trimmed = name.trim();
  const submit = () => {
    if (!trimmed) return;
    onCreate(trimmed);
    onClose();
  };

  return (
    <div
      ref={ref}
      className="session-tag-popover"
      role="dialog"
      aria-modal="false"
      aria-label={t("session.newFolder")}
      style={pos}
    >
      <div className="session-tag-popover-header">{t("session.newFolder")}</div>
      <input
        ref={inputRef}
        type="text"
        className="session-tag-popover-search"
        placeholder={t("session.newFolderPrompt")}
        value={name}
        onChange={(e) => setName(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            submit();
          }
        }}
      />
      <button
        type="button"
        className="session-tag-popover-create"
        disabled={!trimmed}
        onClick={submit}
      >
        {t("session.createFolder", { name: trimmed || t("session.newFolderPrompt") })}
      </button>
    </div>
  );
}
