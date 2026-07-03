import { useState, useCallback, useEffect, useRef } from "react";
import { flushSync } from "react-dom";
import { useTranslation } from "react-i18next";
import type { PastedImage } from "./InputArea";
import { useMobileActionSheet, isMobileViewport } from "./MobileActionSheet";
import type { ActionItem } from "./MobileActionSheet";
import { getMobileHandlers } from "../contexts/MobileHandlersContext";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";
import Icon from "./Icon";

/** Long-press on a non-text target (image, video) → action sheet.
 *  Gated to media because firing on text would race with — and
 *  ultimately interrupt — Android's native text-selection drag /
 *  selection toolbar. Selection-derived actions (Copy, Comment,
 *  AdvSync) flow through SelectionPopup's touchend path instead. */
function isMediaTarget(target: EventTarget | null): boolean {
  return (
    target instanceof HTMLImageElement || target instanceof HTMLVideoElement
  );
}

function isNativeTextSelectionTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  if (FORM_TAG_NAMES.has(target.tagName)) return true;
  return Boolean(target.closest("p, pre, code, blockquote, li, h1, h2, h3, h4, h5, h6"));
}

function isMobileLongPressTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.closest("[data-mobile-context-owner]")) return false;
  if (isMediaTarget(target)) return true;
  return !isNativeTextSelectionTarget(target);
}

export interface InvestigationData {
  prompt: string;
  images: PastedImage[];
}

interface Props {
  onInvestigate: (data: InvestigationData) => void;
  activeSessionId?: string;
  activeSessionCwd?: string;
  children: React.ReactNode;
}

/** Form elements where right-click should open the native browser menu. */
const FORM_TAG_NAMES = new Set(["INPUT", "TEXTAREA", "SELECT"]);

/** Captures a real screenshot of the current tab via getDisplayMedia,
 *  then draws a red crosshair at the click position. Returns a base64
 *  data URL. Uses preferCurrentTab so Chrome shows a simple "share this
 *  tab?" prompt instead of the full screen picker. */
async function captureAnnotatedScreenshot(
  clickX: number,
  clickY: number,
): Promise<string> {
  const stream = await navigator.mediaDevices.getDisplayMedia({
    // @ts-expect-error — preferCurrentTab is a Chrome-specific hint
    // that skips the screen picker and offers only the current tab.
    preferCurrentTab: true,
  });

  const track = stream.getVideoTracks()[0];
  const video = document.createElement("video");
  video.srcObject = stream;
  video.muted = true;

  await new Promise<void>((resolve, reject) => {
    video.onloadeddata = () => resolve();
    video.onerror = () => reject(new Error("Failed to load capture stream"));
    video.play().catch(reject);
  });

  // Wait one animation frame so the video has actual pixel data.
  await new Promise<void>((r) => requestAnimationFrame(() => r()));

  const w = video.videoWidth;
  const h = video.videoHeight;
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d")!;
  ctx.drawImage(video, 0, 0, w, h);

  // Release the stream immediately — we only needed one frame.
  track.stop();
  stream.getTracks().forEach((t) => t.stop());

  // Scale click coordinates from CSS pixels to video pixels.
  const dpr = window.devicePixelRatio || 1;
  const cx = clickX * dpr;
  const cy = clickY * dpr;
  const ringRadius = 18 * dpr;

  ctx.strokeStyle = "red";
  ctx.lineWidth = 3 * dpr;
  ctx.beginPath();
  ctx.arc(cx, cy, ringRadius, 0, Math.PI * 2);
  ctx.stroke();

  ctx.fillStyle = "red";
  ctx.beginPath();
  ctx.arc(cx, cy, 4 * dpr, 0, Math.PI * 2);
  ctx.fill();

  ctx.strokeStyle = "red";
  ctx.lineWidth = 2 * dpr;
  const arm = ringRadius + 6 * dpr;
  ctx.beginPath();
  ctx.moveTo(cx - arm, cy);
  ctx.lineTo(cx + arm, cy);
  ctx.moveTo(cx, cy - arm);
  ctx.lineTo(cx, cy + arm);
  ctx.stroke();

  return canvas.toDataURL("image/png");
}

function dataUrlToBase64(dataUrl: string): string {
  const idx = dataUrl.indexOf(",");
  return idx >= 0 ? dataUrl.slice(idx + 1) : dataUrl;
}

function dataUrlToMediaType(dataUrl: string): string {
  const match = dataUrl.match(/^data:([^;]+);/);
  return match ? match[1] : "image/png";
}

/** Long-press duration in ms to trigger mobile action sheet. */
const LONG_PRESS_MS = 500;
/** Max finger movement in px before cancelling long-press. */
const MOVE_THRESHOLD = 10;
/** Estimated per-item height for viewport clamping. */
const MENU_ITEM_H = 36;
/** Estimated menu width for viewport clamping. */
const MENU_W = 200;
/** Extra padding for menu bottom. */
const MENU_PAD = 16;

export function InvestigateContextMenu({ onInvestigate, activeSessionId, activeSessionCwd, children }: Props) {
  const { t } = useTranslation();
  const [desktopItems, setDesktopItems] = useState<ActionItem[] | null>(null);
  const [menuPos, setMenuPos] = useState<{ x: number; y: number } | null>(null);
  const [capturing, setCapturing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const clickPosRef = useRef<{ x: number; y: number }>(null!);
  const activeSessionIdRef = useRef(activeSessionId);
  activeSessionIdRef.current = activeSessionId;
  const activeSessionCwdRef = useRef(activeSessionCwd);
  activeSessionCwdRef.current = activeSessionCwd;

  const { show: showSheet } = useMobileActionSheet();

  // Long-press state refs.
  const longPressTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const longPressFiredRef = useRef(false);
  const touchStartPosRef = useRef<{ x: number; y: number } | null>(null);

  // Close desktop menu on click outside or Escape.
  useEffect(() => {
    if (!desktopItems) return;
    const close = () => {
      setDesktopItems(null);
      setMenuPos(null);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    const timer = setTimeout(() => {
      document.addEventListener("click", close);
      document.addEventListener("keydown", onKey);
    }, 0);
    return () => {
      clearTimeout(timer);
      document.removeEventListener("click", close);
      document.removeEventListener("keydown", onKey);
    };
  }, [desktopItems]);

  const handleInvestigate = useCallback(async () => {
    const pos = clickPosRef.current;
    // Force synchronous removal of the context menu from the DOM before
    // capturing the screenshot, so the menu isn't in the image.
    flushSync(() => {
      setDesktopItems(null);
      setMenuPos(null);
    });
    setCapturing(true);
    setError(null);
    try {
      const dataUrl = await captureAnnotatedScreenshot(pos.x, pos.y);
      const base64 = dataUrlToBase64(dataUrl);
      const mediaType = dataUrlToMediaType(dataUrl);
      const sessionId = activeSessionIdRef.current ?? "unknown";

      const prompt = [
        "Investigate the following issue.",
        "",
        `**Source session:** ${sessionId}`,
        `**Page URL:** ${window.location.href}`,
        `**Click position:** (${pos.x}, ${pos.y})`,
        "",
        "The attached screenshot shows the UI state at the moment the user right-clicked. The red crosshair marks the exact click location.",
        "",
        "Analyze what's happening in the screenshot and investigate any visible issues, errors, or unexpected behavior. Focus on the area around the click marker.",
      ].join("\n");

      onInvestigate({ prompt, images: [{ dataUrl, base64, mediaType }] });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Screenshot capture failed");
    } finally {
      setCapturing(false);
    }
  }, [onInvestigate]);

  // Build the action list for any context-trigger (desktop right-click or
  // mobile long-press). Reads handlers at call time, not render time.
  const buildActions = useCallback(
    (target: HTMLElement, x: number, y: number): ActionItem[] => {
      const items: ActionItem[] = [];
      const handlers = getMobileHandlers();
      clickPosRef.current = { x, y };

      // Rewind — available on user messages.
      const userMsgEl = target.closest("[data-message-id].user-message-box") as HTMLElement | null;
      if (userMsgEl && handlers.rewind) {
        const messageId = userMsgEl.getAttribute("data-message-id")!;
        items.push({
          id: "rewind",
          label: "Rewind",
          icon: <Icon name="rewind" size={14} />,
          onClick: () => handlers.rewind!(messageId, { x, y }),
        });
      }

      // Copy message/session ids from any message.
      const msgEl = target.closest("[data-message-id]") as HTMLElement | null;
      if (msgEl) {
        const messageId = msgEl.getAttribute("data-message-id")!;
        const sessionId = activeSessionIdRef.current ?? "";
        const cwd = activeSessionCwdRef.current ?? "";
        const parts = [messageId];
        if (sessionId) parts.push(sessionId);
        if (cwd) parts.push(cwd);
        items.push({
          id: "copy-id",
          label: t("session.copyAction"),
          icon: <Icon name="clipboard" size={14} />,
          onClick: () => navigator.clipboard.writeText(parts.join(" ")).catch(() => {}),
        });
      }

      // Investigate — available unless inside a modal or form element.
      if (!target.closest(".modal-overlay") && !FORM_TAG_NAMES.has(target.tagName)) {
        items.push({
          id: "investigate",
          label: "Investigate",
          icon: <Icon name="search" size={14} />,
          onClick: handleInvestigate,
        });
      }

      // Text selection actions — if text is currently selected.
      const sel = window.getSelection();
      if (sel && !sel.isCollapsed && sel.toString().trim()) {
        const text = sel.toString().trim();
        const anchor = sel.anchorNode;
        const anchorEl =
          anchor instanceof HTMLElement ? anchor : anchor?.parentElement;
        const msgEl = anchorEl?.closest("[data-message-id]") as HTMLElement | null;
        const messageId = msgEl?.getAttribute("data-message-id");

        items.push({
          id: "copy",
          label: "Copy",
          icon: <Icon name="clipboard" size={14} />,
          onClick: () => {
            navigator.clipboard.writeText(text).catch(() => {});
            sel.removeAllRanges();
          },
        });

        if (messageId && handlers.addTag) {
          items.push({
            id: "comment",
            label: "Comment",
            icon: <Icon name="chat" size={14} />,
            onClick: () => handlers.addTag!(text, "", messageId),
          });
        }

        if (messageId && handlers.advSync) {
          items.push({
            id: "adv-sync",
            label: "Adversarial Sync",
            icon: <Icon name="swords" size={14} />,
            onClick: () => handlers.advSync!(text, messageId),
          });
        }
      }

      return items;
    },
    [handleInvestigate, t],
  );

  // Desktop right-click → show custom floating toolbar WITHOUT suppressing
  // the native context menu. Both menus coexist — native handles
  // Copy/Paste/Inspect, ours adds app-specific actions.
  const handleContextMenu = useCallback(
    (e: React.MouseEvent) => {
      const target = e.target as HTMLElement;
      if (FORM_TAG_NAMES.has(target.tagName)) return;
      if (target.closest(".modal-overlay")) return;

      // On mobile, let touch handlers manage this.
      if (isMobileViewport()) return;

      const x = e.clientX;
      const y = e.clientY;
      const items = buildActions(target, x, y);
      if (items.length === 0) return;

      // Position ABOVE the cursor so it doesn't overlap the native menu
      // (which appears at/below cursor position).
      const menuH = items.length * MENU_ITEM_H + MENU_PAD;
      const clamped = {
        x: Math.max(8, Math.min(x - 10, window.innerWidth - MENU_W - 8)),
        y: Math.max(8, y - menuH - 4),
      };
      setDesktopItems(items);
      setMenuPos(clamped);
    },
    [buildActions, showSheet],
  );

  // Mobile long-press detection via touch events.
  // Listeners are always attached; each handler gates on isMobileViewport()
  // at call time so viewport resizes are handled reactively without
  // re-mounting the effect.
  useEffect(() => {
    const handleTouchStart = (e: TouchEvent) => {
      if (!isMobileViewport()) return;
      if (!isMobileLongPressTarget(e.target)) return;

      const touch = e.touches[0];
      touchStartPosRef.current = { x: touch.clientX, y: touch.clientY };
      longPressFiredRef.current = false;

      longPressTimerRef.current = setTimeout(() => {
        longPressFiredRef.current = true;
        const target = e.target as HTMLElement;

        if (FORM_TAG_NAMES.has(target.tagName)) return;
        if (target.closest(".modal-overlay")) return;

        const pos = touchStartPosRef.current!;
        const items = buildActions(target, pos.x, pos.y);
        if (items.length === 0) return;

        showSheet(items);
      }, LONG_PRESS_MS);
    };

    const handleTouchMove = (e: TouchEvent) => {
      if (!touchStartPosRef.current) return;
      const touch = e.touches[0];
      const dx = touch.clientX - touchStartPosRef.current.x;
      const dy = touch.clientY - touchStartPosRef.current.y;
      if (Math.sqrt(dx * dx + dy * dy) > MOVE_THRESHOLD) {
        if (longPressTimerRef.current) {
          clearTimeout(longPressTimerRef.current);
          longPressTimerRef.current = null;
        }
      }
    };

    const handleTouchEnd = () => {
      if (longPressTimerRef.current) {
        clearTimeout(longPressTimerRef.current);
        longPressTimerRef.current = null;
      }
      longPressFiredRef.current = false;
      touchStartPosRef.current = null;
    };

    // Suppress native context menu only where the app owns long-press.
    const suppressNative = (e: Event) => {
      if (!isMobileViewport()) return;
      if (!isMobileLongPressTarget(e.target)) return;
      const target = e.target as HTMLElement;
      if (FORM_TAG_NAMES.has(target.tagName)) return;
      if (target.closest(".modal-overlay")) return;
      e.preventDefault();
    };

    document.addEventListener("touchstart", handleTouchStart, { passive: true });
    document.addEventListener("touchmove", handleTouchMove, { passive: true });
    document.addEventListener("touchend", handleTouchEnd, { passive: true });
    document.addEventListener("contextmenu", suppressNative, true);

    return () => {
      document.removeEventListener("touchstart", handleTouchStart);
      document.removeEventListener("touchmove", handleTouchMove);
      document.removeEventListener("touchend", handleTouchEnd);
      document.removeEventListener("contextmenu", suppressNative, true);
      if (longPressTimerRef.current) clearTimeout(longPressTimerRef.current);
    };
  }, [buildActions, showSheet]);

  const closeDesktopMenu = useCallback(() => {
    setDesktopItems(null);
    setMenuPos(null);
  }, []);
  useBackButtonDismiss(desktopItems !== null, closeDesktopMenu);

  return (
    <div onContextMenu={handleContextMenu} style={{ display: "contents" }}>
      {children}

      {/* Unified floating context menu (desktop). */}
      {desktopItems && menuPos && (
        <div
          className="ctx-menu"
          style={{ position: "fixed", left: menuPos.x, top: menuPos.y, zIndex: 10000 }}
          onClick={(e) => e.stopPropagation()}
        >
          {desktopItems.map((item) => (
            <button
              key={item.id}
              className={`ctx-menu-item${item.id === "investigate" && capturing ? " disabled" : ""}`}
              disabled={item.id === "investigate" && capturing}
              onClick={() => {
                if (item.id === "investigate") {
                  // Investigate manages its own close (needs flushSync before screenshot).
                  item.onClick();
                } else {
                  flushSync(() => closeDesktopMenu());
                  item.onClick();
                }
              }}
            >
              {item.icon && <span className="ctx-menu-icon">{item.icon}</span>}
              {item.id === "investigate" && capturing ? "Capturing…" : item.label}
            </button>
          ))}
          {error && <div className="ctx-menu-error">{error}</div>}
        </div>
      )}
    </div>
  );
}
