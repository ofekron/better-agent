import {
  createContext,
  useContext,
  useState,
  useCallback,
  useEffect,
  type ReactNode,
} from "react";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";

export interface ActionItem {
  id: string;
  label: string;
  icon?: ReactNode;
  onClick: () => void;
  danger?: boolean;
  disabled?: boolean;
}

interface MobileActionSheetContextValue {
  show: (items: ActionItem[], header?: string) => void;
  dismiss: () => void;
  visible: boolean;
}

const MobileActionSheetContext = createContext<MobileActionSheetContextValue>({
  show: () => {},
  dismiss: () => {},
  visible: false,
});

export function useMobileActionSheet() {
  return useContext(MobileActionSheetContext);
}

export function MobileActionSheetProvider({
  children,
}: {
  children: ReactNode;
}) {
  const [sheet, setSheet] = useState<{
    items: ActionItem[];
    header?: string;
  } | null>(null);

  const show = useCallback((items: ActionItem[], header?: string) => {
    setSheet({ items, header });
  }, []);

  const dismiss = useCallback(() => {
    setSheet(null);
  }, []);

  return (
    <MobileActionSheetContext.Provider
      value={{ show, dismiss, visible: !!sheet }}
    >
      {children}
      {sheet && (
        <ActionSheet
          items={sheet.items}
          header={sheet.header}
          onClose={dismiss}
        />
      )}
    </MobileActionSheetContext.Provider>
  );
}

function ActionSheet({
  items,
  header,
  onClose,
}: {
  items: ActionItem[];
  header?: string;
  onClose: () => void;
}) {
  const [visible, setVisible] = useState(false);

  // Animate in on mount.
  useEffect(() => {
    requestAnimationFrame(() => setVisible(true));
  }, []);

  const handleClose = useCallback(() => {
    setVisible(false);
    // Wait for fade-out animation before unmounting.
    setTimeout(onClose, 200);
  }, [onClose]);
  useBackButtonDismiss(true, handleClose);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") handleClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [handleClose]);

  return (
    <div
      className={`mobile-action-sheet-backdrop${visible ? " visible" : ""}`}
      onClick={handleClose}
    >
      <div
        className={`mobile-action-sheet${visible ? " visible" : ""}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mobile-action-sheet-handle" />
        {header && (
          <div className="mobile-action-sheet-header">{header}</div>
        )}
        <div className="mobile-action-sheet-items">
          {items.map((item) => (
            <button
              key={item.id}
              className={`mobile-action-sheet-item${item.danger ? " danger" : ""}`}
              onClick={() => {
                item.onClick();
                handleClose();
              }}
              disabled={item.disabled}
            >
              {item.icon && (
                <span className="mobile-action-sheet-icon">{item.icon}</span>
              )}
              {item.label}
            </button>
          ))}
        </div>
        <button className="mobile-action-sheet-cancel" onClick={handleClose}>
          Cancel
        </button>
      </div>
    </div>
  );
}

/** Check if the current viewport is mobile-width. Matches BP_MOBILE from
 *  useViewport — kept as a simple function so non-hook code paths (event
 *  handlers) can call it without a React context. */
export function isMobileViewport(): boolean {
  return window.innerWidth <= 480;
}

export function isTouchInteractionViewport(): boolean {
  if (isMobileViewport()) return true;
  if (navigator.maxTouchPoints > 0) return true;
  return (
    window.matchMedia?.("(pointer: coarse)").matches ||
    window.matchMedia?.("(hover: none)").matches ||
    false
  );
}
