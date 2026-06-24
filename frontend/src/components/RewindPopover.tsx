import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";

interface Props {
  x: number;
  y: number;
  enabled: boolean;
  onConfirm: () => void;
  onClose: () => void;
}

export function RewindPopover({ x, y, enabled, onConfirm, onClose }: Props) {
  const { t } = useTranslation();
  const [confirming, setConfirming] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useBackButtonDismiss(true, onClose);

  useEffect(() => {
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

  const handleClick = () => {
    if (!enabled) return;
    if (!confirming) {
      setConfirming(true);
      return;
    }
    onConfirm();
  };

  return (
    <div
      ref={ref}
      className="rewind-popover"
      style={{ position: "fixed", top: y, left: x, zIndex: 1000 }}
    >
      <button
        type="button"
        className="rewind-popover-button"
        disabled={!enabled}
        title={enabled ? undefined : t("rewind.noCheckpoint")}
        onClick={handleClick}
      >
        {confirming ? t("rewind.clickConfirm") : t("rewind.rewindWithFiles")}
      </button>
    </div>
  );
}
