import { type KeyboardEvent, type ReactNode, useEffect, useId, useRef } from "react";
import { useTranslation } from "react-i18next";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";

interface Props {
  open: boolean;
  title: string;
  eyebrow?: string;
  busy: boolean;
  disabled?: boolean;
  confirmDisabled?: boolean;
  error: string;
  confirmLabel?: string;
  cancelLabel?: string;
  children: ReactNode;
  onConfirm: () => void;
  onCancel: () => void;
}

export function MarketplaceConfirmationModal({
  open,
  title,
  eyebrow,
  busy,
  disabled = false,
  confirmDisabled = false,
  error,
  confirmLabel,
  cancelLabel,
  children,
  onConfirm,
  onCancel,
}: Props) {
  const { t } = useTranslation();
  const titleId = useId();
  const dialogRef = useRef<HTMLElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  useBackButtonDismiss(open, busy || disabled ? () => undefined : onCancel);
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    cancelRef.current?.focus();
    return () => previous?.focus();
  }, [open]);
  if (!open) return null;

  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === "Escape" && !busy && !disabled) {
      event.preventDefault();
      onCancel();
      return;
    }
    if (event.key !== "Tab") return;
    const controls = Array.from(
      dialogRef.current?.querySelectorAll<HTMLElement>(
        "button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])",
      ) ?? [],
    );
    if (controls.length === 0) return;
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div
      className="modal-overlay marketplace-intent-overlay"
      onClick={busy || disabled ? undefined : onCancel}
    >
      <section
        ref={dialogRef}
        className="modal-content marketplace-intent-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onKeyDown={onKeyDown}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-header">
          <div>
            {eyebrow ? <span className="marketplace-intent-eyebrow">{eyebrow}</span> : null}
            <h2 id={titleId}>{title}</h2>
          </div>
          <button
            className="modal-close"
            onClick={onCancel}
            disabled={busy || disabled}
            aria-label={cancelLabel ?? t("app.cancel")}
          >
            &times;
          </button>
        </div>
        <div className="modal-body marketplace-intent-body">
          {children}
          {error ? <div className="setup-error" role="alert">{error}</div> : null}
        </div>
        <div className="modal-footer">
          <button
            ref={cancelRef}
            type="button"
            className="btn-secondary"
            onClick={onCancel}
            disabled={busy || disabled}
            autoFocus
          >
            {cancelLabel ?? t("app.cancel")}
          </button>
          <button
            type="button"
            className="btn-primary"
            onClick={onConfirm}
            disabled={busy || disabled || confirmDisabled}
          >
            {confirmLabel ?? (busy ? t("settings.extensionsUpdating") : t("app.confirm"))}
          </button>
        </div>
      </section>
    </div>
  );
}
