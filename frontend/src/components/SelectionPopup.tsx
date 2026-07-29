import { useState, useEffect, useRef, useCallback } from "react";
import { useTranslation } from "react-i18next";
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

type Phase = "actions" | "comment";

export function SelectionPopup({ onAdd }: Props) {
  const { t } = useTranslation();
  const [popup, setPopup] = useState<PopupState | null>(null);
  const [phase, setPhase] = useState<Phase>("actions");
  const [comment, setComment] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const popupRef = useRef<HTMLDivElement>(null);
  // Mirrors popup.text synchronously so the keydown handler can read it
  // without waiting for React to re-render (avoids one-frame race).
  const popupTextRef = useRef<string | null>(null);
  // Stable refs for props so the mouseup/touchend effect deps don't
  // change when parent re-renders (which would tear down listeners).
  const onAddRef = useRef(onAdd);
  onAddRef.current = onAdd;
  const { show: showSheet } = useMobileActionSheet();

  const closePopup = useCallback(() => {
    popupTextRef.current = null;
    setPopup(null);
    setComment("");
    setPhase("actions");
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

      // Walk up from the selection anchor to find [data-message-id]
      const anchor = sel.anchorNode;
      if (!anchor) return;
      const el =
        anchor instanceof HTMLElement
          ? anchor
          : anchor.parentElement;
      if (!el) return;

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
      setComment("");
      setPhase("actions");
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
      if (touchTimeout) {
        clearTimeout(touchTimeout);
        touchTimeout = null;
      }

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
        if (lastPointerType !== "touch") return;
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

  // Focus input when entering comment phase
  useEffect(() => {
    if (popup && phase === "comment") {
      setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [popup, phase]);

  const handleSubmit = useCallback(() => {
    if (!popup) return;
    window.getSelection()?.removeAllRanges();
    onAddRef.current(popup.text, comment.trim(), popup.messageId);
    closePopup();
  }, [popup, comment, closePopup]);

  const handleCopy = useCallback(async () => {
    if (!popup) return;
    if (await copyToClipboard(popup.text)) closePopup();
  }, [popup, closePopup]);

  const handleComment = useCallback(() => {
    if (!popup) return;
    window.getSelection()?.removeAllRanges();
    onAddRef.current(popup.text, "", popup.messageId);
    closePopup();
  }, [popup, closePopup]);

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
      {phase === "actions" ? (
        <div className="selection-popup-actions">
          <button className="selection-popup-action-btn" onClick={() => void handleCopy()}>
            Copy
          </button>
          <button className="selection-popup-action-btn" onClick={handleComment}>
            Comment
          </button>
        </div>
      ) : (
        <div className="selection-popup-row">
          <input
            ref={inputRef}
            className="selection-popup-input"
            type="text"
            placeholder={t("selection.placeholder")}
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") handleSubmit();
              if (e.key === "Escape") dismiss();
            }}
          />
          <button
            className="selection-popup-add"
            onClick={handleSubmit}
            disabled={!comment.trim()}
          >
            Add
          </button>
          <button className="selection-popup-close" onClick={dismiss}>
            &times;
          </button>
        </div>
      )}
    </div>
  );
}
