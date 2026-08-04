import { useState, useEffect, useRef, useCallback } from "react";
import { copyToClipboard } from "../utils/clipboard";
import { useMobileActionSheet, isTouchInteractionViewport } from "./MobileActionSheet";
import type { ActionItem } from "./MobileActionSheet";
import Icon from "./Icon";

interface Props {
  onAdd: (text: string, comment: string, messageId: string) => void;
}

interface PopupState {
  text: string;
  messageId: string;
  x: number;
  y: number;
}

export function SelectionPopup({ onAdd }: Props) {
  const [popup, setPopup] = useState<PopupState | null>(null);
  const popupRef = useRef<HTMLDivElement>(null);
  // Mirrors the open popup's text synchronously so the document-level
  // contextmenu handler can tell whether the popup is open without waiting
  // for React to re-render (avoids a one-frame race).
  const popupTextRef = useRef<string | null>(null);
  // Stable refs for props so the mouseup/touchend effect deps don't
  // change when parent re-renders (which would tear down listeners).
  const onAddRef = useRef(onAdd);
  onAddRef.current = onAdd;
  const { show: showSheet } = useMobileActionSheet();

  const closePopup = useCallback(() => {
    popupTextRef.current = null;
    setPopup(null);
  }, []);

  const dismiss = useCallback(() => {
    // Only tear down the native selection when the popup was actually open.
    // On mobile (Android WebView), long-pressing text fires contextmenu
    // before the selection settles; unconditionally calling removeAllRanges()
    // kills the browser-created selection and prevents text selection entirely.
    if (popupTextRef.current) {
      const sel = window.getSelection();
      if (sel && !sel.isCollapsed) sel.removeAllRanges();
    }
    closePopup();
  }, [closePopup]);

  // Show the mobile action sheet for a text selection.
  const showMobileSheet = useCallback(
    (text: string, messageId: string) => {
      const items: ActionItem[] = [
        {
          id: "copy",
          label: "Copy",
          icon: <Icon name="clipboard" size={14} />,
          onClick: async () => {
            await copyToClipboard(text);
          },
        },
        {
          id: "comment",
          label: "Comment",
          icon: <Icon name="chat" size={14} />,
          onClick: () => onAddRef.current(text, "", messageId),
        },
      ];

      showSheet(items, text.length > 40 ? text.slice(0, 40) + "…" : text);
    },
    [showSheet],
  );

  useEffect(() => {
    let touchTimeout: ReturnType<typeof setTimeout> | null = null;
    let lastPointerType: "mouse" | "touch" | null = null;

    // Shared: given an active selection, resolve it to a message and
    // show the popup. Touch selections use the native mobile handles;
    // mouse selections keep the native range available for browser copy.
    const showPopupForSelection = (isTouch: boolean) => {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed || !sel.toString().trim()) return;

      const text = sel.toString();

      // Walk up from the selection anchor to find [data-message-id]. A
      // non-empty live selection always carries an attached anchor node.
      const anchor = sel.anchorNode!;
      const el: HTMLElement = anchor instanceof HTMLElement
        ? anchor
        : anchor.parentElement!;

      const messageEl = el.closest("[data-message-id]") as HTMLElement | null;
      if (!messageEl) return;

      const messageId = messageEl.getAttribute("data-message-id");
      if (!messageId) return;

      // On mobile, use the action sheet instead of the floating popup.
      if (isTouch && isTouchInteractionViewport()) {
        showMobileSheet(text, messageId);
        return;
      }

      const range = sel.getRangeAt(0);
      const rect = range.getBoundingClientRect();

      setPopup({
        text,
        messageId,
        x: rect.left + rect.width / 2,
        y: rect.bottom + 8,
      });
      popupTextRef.current = text;
    };

    const handleMouseUp = (e: MouseEvent) => {
      if (popupRef.current?.contains(e.target as Node)) return;
      // On mobile, the browser synthesizes a mouseup after touchend.
      // If a touchend recently fired and the 400ms touch timeout is still
      // pending, this mouseup is that echo — skip it so the touch path
      // handles the selection and preserves the native selection handles
      // the user needs to drag and extend the range.
      if (lastPointerType === "touch" && touchTimeout) return;
      lastPointerType = "mouse";

      const sel = window.getSelection();
      if (!sel || sel.isCollapsed || !sel.toString().trim()) {
        // Click with no selection — dismiss if popup is open
        // Use setTimeout so the click-on-Add button registers first
        setTimeout(() => {
          const currentSel = window.getSelection();
          if (!currentSel || currentSel.isCollapsed) {
            dismiss();
          }
        }, 0);
        return;
      }

      showPopupForSelection(false);
    };

    // On touch devices, selection finalizes after the user lifts their
    // finger and the OS settles the selection handles. A short delay
    // avoids reading a stale/empty selection.
    const handleTouchEnd = (e: TouchEvent) => {
      if (popupRef.current?.contains(e.target as Node)) return;
      lastPointerType = "touch";
      if (touchTimeout) clearTimeout(touchTimeout);
      touchTimeout = setTimeout(() => {
        touchTimeout = null;
        showPopupForSelection(true);
      }, 400);
    };

    const handleContextMenu = () => {
      if (popupTextRef.current) return;
      dismiss();
    };

    document.addEventListener("mouseup", handleMouseUp);
    document.addEventListener("touchend", handleTouchEnd, { passive: true });
    document.addEventListener("contextmenu", handleContextMenu, true);
    return () => {
      document.removeEventListener("mouseup", handleMouseUp);
      document.removeEventListener("touchend", handleTouchEnd);
      document.removeEventListener("contextmenu", handleContextMenu, true);
      if (touchTimeout) clearTimeout(touchTimeout);
    };
  }, [dismiss, showMobileSheet]);

  if (!popup) return null;

  return (
    <div
      ref={popupRef}
      className="selection-popup"
      style={{
        left: popup.x,
        top: popup.y,
        transform: "translateX(-50%)",
      }}
    >
      <div className="selection-popup-actions">
        <button
          className="selection-popup-action-btn"
          onClick={async () => {
            if (await copyToClipboard(popup.text)) closePopup();
          }}
        >
          Copy
        </button>
        <button
          className="selection-popup-action-btn"
          onClick={() => {
            window.getSelection()?.removeAllRanges();
            onAddRef.current(popup.text, "", popup.messageId);
            closePopup();
          }}
        >
          Comment
        </button>
      </div>
    </div>
  );
}
