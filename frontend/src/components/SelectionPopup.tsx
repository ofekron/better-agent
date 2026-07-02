import { useState, useEffect, useRef, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { applyTagHighlights, PENDING_TAG_ID } from "../utils/tagHighlights";
import { useMobileActionSheet, isMobileViewport } from "./MobileActionSheet";
import type { ActionItem } from "./MobileActionSheet";
import Icon from "./Icon";

interface Props {
  onAdd: (text: string, comment: string, messageId: string) => void;
  /** Optional handler for the "Adversarial sync" action. When set,
   * the popup renders a third button. Invoked with the selection
   * verbatim + the messageId the selection was anchored to. The
   * caller (App) POSTs /api/sessions/{id}/adv_sync. */
  onAdvSync?: (text: string, messageId: string) => void;
}

interface PopupState {
  text: string;
  messageId: string;
  x: number;
  y: number;
}

type Phase = "actions" | "comment";

/** Copy text to clipboard with a textarea+execCommand fallback for
 *  insecure contexts (HTTP / non-localhost) where the modern
 *  clipboard API is unavailable. The textarea is kept in the viewport
 *  (top-left, 1×1px, transparent) and explicitly focused so that
 *  iOS Safari and Android WebView accept execCommand("copy"). */
async function copyToClipboard(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "0";
    ta.style.left = "0";
    ta.style.width = "1px";
    ta.style.height = "1px";
    ta.style.padding = "0";
    ta.style.border = "none";
    ta.style.outline = "none";
    ta.style.boxShadow = "none";
    ta.style.background = "transparent";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    try {
      document.execCommand("copy");
    } catch {
      // best effort
    }
    document.body.removeChild(ta);
  }
}

export function SelectionPopup({ onAdd, onAdvSync }: Props) {
  const { t } = useTranslation();
  const [popup, setPopup] = useState<PopupState | null>(null);
  const [phase, setPhase] = useState<Phase>("actions");
  const [comment, setComment] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const popupRef = useRef<HTMLDivElement>(null);
  // Cleanup function returned by `applyTagHighlights` for the live
  // preview highlight. Stored in a ref so dismiss/Add can tear it down
  // without depending on render cycles.
  const pendingCleanupRef = useRef<(() => void) | null>(null);
  // Mirrors popup.text synchronously so the keydown handler can read it
  // without waiting for React to re-render (avoids one-frame race).
  const popupTextRef = useRef<string | null>(null);
  // Stable refs for props so the mouseup/touchend effect deps don't
  // change when parent re-renders (which would tear down listeners).
  const onAddRef = useRef(onAdd);
  onAddRef.current = onAdd;
  const onAdvSyncRef = useRef(onAdvSync);
  onAdvSyncRef.current = onAdvSync;

  const { show: showSheet } = useMobileActionSheet();

  const clearPendingHighlight = useCallback(() => {
    pendingCleanupRef.current?.();
    pendingCleanupRef.current = null;
  }, []);

  const dismiss = useCallback(() => {
    // Only tear down the native selection when the popup was actually open.
    // On mobile (Android WebView), long-pressing text fires contextmenu
    // before the selection settles; unconditionally calling removeAllRanges()
    // kills the browser-created selection and prevents text selection entirely.
    const wasOpen = !!popupTextRef.current;
    clearPendingHighlight();
    popupTextRef.current = null;
    if (wasOpen) {
      const sel = window.getSelection();
      if (sel && !sel.isCollapsed) sel.removeAllRanges();
    }
    setPopup(null);
    setComment("");
    setPhase("actions");
  }, [clearPendingHighlight]);

  // Show the mobile action sheet for a text selection.
  const showMobileSheet = useCallback(
    (text: string, messageId: string) => {
      const items: ActionItem[] = [
        {
          id: "copy",
          label: "Copy",
          icon: <Icon name="clipboard" size={14} />,
          onClick: () => {
            copyToClipboard(text);
            window.getSelection()?.removeAllRanges();
          },
        },
        {
          id: "comment",
          label: "Comment",
          icon: <Icon name="chat" size={14} />,
          onClick: () => onAddRef.current(text, "", messageId),
        },
      ];

      if (onAdvSyncRef.current) {
        items.push({
          id: "adv-sync",
          label: "Adversarial Sync",
          icon: <Icon name="swords" size={14} />,
          onClick: () => onAdvSyncRef.current?.(text, messageId),
        });
      }

      showSheet(items, text.length > 40 ? text.slice(0, 40) + "…" : text);
    },
    [showSheet],
  );

  useEffect(() => {
    let touchTimeout: ReturnType<typeof setTimeout> | null = null;
    let lastPointerType: "mouse" | "touch" | null = null;

    // Shared: given an active selection, resolve it to a message and
    // show the popup. `isTouch` controls whether the native selection
    // is cleared (desktop: yes, to avoid highlight washout; mobile:
    // no, to keep native selection handles visible).
    const showPopupForSelection = (isTouch: boolean) => {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed || !sel.toString().trim()) return;

      const text = sel.toString().trim();
      if (!text) return;

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
      if (isTouch && isMobileViewport()) {
        showMobileSheet(text, messageId);
        return;
      }

      const range = sel.getRangeAt(0);
      const rect = range.getBoundingClientRect();

      if (!isTouch) {
        // Tear down any prior pending preview before applying a new one.
        // applyTagHighlights splits text nodes and wraps them in spans,
        // which destroys the native browser selection. We intentionally
        // do NOT re-select — re-selecting from the spans would hit the
        // wrong occurrence when the selected text appears multiple times
        // (applyTagHighlights uses indexOf, always finding the first match).
        // The backup Ctrl+C handler and Copy button both use copyToClipboard
        // which holds the correct text from the original selection.
        clearPendingHighlight();
        pendingCleanupRef.current = applyTagHighlights(messageEl, [
          {
            id: PENDING_TAG_ID,
            messageId,
            selectedText: text,
            comment: "",
            timestamp: "",
          },
        ]);
      } else {
        clearPendingHighlight();
      }

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

    // Dismiss selection popup on right-click — the unified context menu
    // takes over selection actions when it appears.
    const handleContextMenu = () => { dismiss(); };

    document.addEventListener("mouseup", handleMouseUp);
    document.addEventListener("touchend", handleTouchEnd, { passive: true });
    document.addEventListener("contextmenu", handleContextMenu, true);
    return () => {
      document.removeEventListener("mouseup", handleMouseUp);
      document.removeEventListener("touchend", handleTouchEnd);
      document.removeEventListener("contextmenu", handleContextMenu, true);
      if (touchTimeout) clearTimeout(touchTimeout);
    };
  }, [dismiss, clearPendingHighlight, showMobileSheet]);

  // Ctrl/Cmd+C copies the captured selection snapshot. Native copy can
  // truncate around rendered markdown nodes after the preview highlight
  // mutates the selected DOM.
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (!popupTextRef.current) return;
      if ((e.ctrlKey || e.metaKey) && e.key === "c") {
        if (document.activeElement === inputRef.current) return;
        e.preventDefault();
        copyToClipboard(popupTextRef.current);
        dismiss();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [dismiss]);

  // Focus input when entering comment phase
  useEffect(() => {
    if (popup && phase === "comment") {
      setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [popup, phase]);

  // Cleanup any lingering pending highlight on unmount.
  useEffect(() => {
    return () => clearPendingHighlight();
  }, [clearPendingHighlight]);

  const handleSubmit = useCallback(() => {
    if (!popup) return;
    clearPendingHighlight();
    window.getSelection()?.removeAllRanges();
    onAddRef.current(popup.text, comment.trim(), popup.messageId);
    setPopup(null);
    setComment("");
    setPhase("actions");
  }, [popup, comment, clearPendingHighlight]);

  const handleCopy = useCallback(() => {
    if (!popup) return;
    copyToClipboard(popup.text);
    dismiss();
  }, [popup, dismiss]);

  const handleComment = useCallback(() => {
    if (!popup) return;
    clearPendingHighlight();
    window.getSelection()?.removeAllRanges();
    onAddRef.current(popup.text, "", popup.messageId);
    setPopup(null);
    setComment("");
    setPhase("actions");
  }, [popup, clearPendingHighlight]);

  const handleAdvSync = useCallback(() => {
    if (!popup || !onAdvSyncRef.current) return;
    clearPendingHighlight();
    onAdvSyncRef.current(popup.text, popup.messageId);
    setPopup(null);
    setComment("");
    setPhase("actions");
  }, [popup, clearPendingHighlight]);

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
          <button className="selection-popup-action-btn" onClick={handleCopy}>
            Copy
          </button>
          <button className="selection-popup-action-btn" onClick={handleComment}>
            Comment
          </button>
          {onAdvSync && (
            <button
              className="selection-popup-action-btn"
              onClick={handleAdvSync}
              title="Run adversarial sync: spawn supportive + adversarial forks and ping-pong them to convergence"
            >
              Adversarial sync
            </button>
          )}
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
