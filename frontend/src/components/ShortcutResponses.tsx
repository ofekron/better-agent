import { useState, useEffect, useCallback, useRef } from "react";
import { API } from "../api";
import { trackPromise } from "../progress/store";
import { useViewport } from "../hooks/useViewport";

const PROMPT_INPUT_SELECTOR = '[data-testid="input-textarea"]';

/**
 * True when a keyboard-relevant element OTHER than the prompt input holds
 * focus. On mobile we hide quick replies in that case so they don't crowd
 * the viewport while another field's keyboard is up. `document.body` focus
 * (just reading the chat) does not count as "something on focus".
 */
function otherFieldFocused(): boolean {
  const ae = document.activeElement as HTMLElement | null;
  if (!ae || ae.tagName === "BODY") return false;
  if (ae.matches?.(PROMPT_INPUT_SELECTOR)) return false;
  return (
    ae.tagName === "INPUT" ||
    ae.tagName === "TEXTAREA" ||
    ae.tagName === "SELECT" ||
    ae.isContentEditable
  );
}

interface Props {
  /** Called when user clicks a shortcut — sends it as their next prompt. */
  onSend: (prompt: string) => void;
  /** Whether the session is currently streaming (hide shortcuts while streaming). */
  isStreaming: boolean;
  /** Whether the chat is disabled. */
  disabled: boolean;
  /** The last assistant message text — used to pick relevant shortcuts. */
  lastAssistantText: string;
  /** Configured shortcut responses list. */
  shortcuts: string[];
}

export function ShortcutResponses({
  onSend,
  isStreaming,
  disabled,
  lastAssistantText,
  shortcuts,
}: Props) {
  // Start with all shortcuts shown immediately; filter when picker responds.
  const [filtered, setFiltered] = useState<string[]>([]);
  const pickSeq = useRef(0);
  const { mode } = useViewport();
  const isMobile = mode === "mobile";
  const [hiddenByFocus, setHiddenByFocus] = useState(false);

  // On mobile, hide quick replies whenever a field other than the prompt
  // input gains focus (e.g. a modal search box, settings input).
  useEffect(() => {
    if (!isMobile) {
      setHiddenByFocus(false);
      return;
    }
    const update = () => setHiddenByFocus(otherFieldFocused());
    update();
    document.addEventListener("focusin", update);
    document.addEventListener("focusout", update);
    return () => {
      document.removeEventListener("focusin", update);
      document.removeEventListener("focusout", update);
    };
  }, [isMobile]);

  const pickRelevant = useCallback(
    async (text: string, seq: number) => {
      if (!text || !shortcuts.length) return;
      try {
        const resp = await trackPromise(
          "shortcuts:pick",
          () =>
            fetch(`${API}/api/shortcuts/pick`, {
              method: "POST",
              headers: { "content-type": "application/json" },
              credentials: "include",
              body: JSON.stringify({ assistant_text: text.slice(0, 4000) }),
            }),
        ).promise;
        // Stale response — a newer turn ended while we were in flight.
        if (seq !== pickSeq.current) return;
        if (resp.ok) {
          const data = await resp.json();
          setFiltered(data.shortcuts ?? shortcuts);
        }
      } catch {
        // Keep showing all shortcuts on error — no filter.
      }
    },
    [shortcuts],
  );

  // When a turn ends (lastAssistantText changes + not streaming), show all
  // immediately, then kick off async filtering.
  useEffect(() => {
    if (isStreaming || disabled || !lastAssistantText || !shortcuts.length) {
      setFiltered([]);
      return;
    }
    // Immediately show all shortcuts.
    setFiltered(shortcuts);
    // Then ask the picker to filter.
    const seq = ++pickSeq.current;
    void pickRelevant(lastAssistantText, seq);
  }, [lastAssistantText, isStreaming, disabled, shortcuts, pickRelevant]);

  if (isStreaming || disabled || !filtered.length || hiddenByFocus) return null;

  return (
    <div className="shortcut-responses">
      {filtered.map((shortcut) => (
        <button
          key={shortcut}
          className="shortcut-btn"
          onClick={() => onSend(shortcut)}
          title={shortcut}
        >
          {shortcut}
        </button>
      ))}
    </div>
  );
}
