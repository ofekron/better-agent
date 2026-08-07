// Generic collapse/expand chrome shared by every structural surface/
// renderer (ToolCard, native_subagent_turn block, Explanation's own
// collapse) — one place owning the disclosure control's accessible name,
// 44x44 CSS-pixel target (chat-panel.md "Responsive behavior"), and
// aria-expanded wiring, instead of each caller re-deriving it.

import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

interface CollapsibleBlockProps {
  header: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
  className?: string;
  testId?: string;
  /** Controlled mode: when both are provided, this component reflects
   * `open` and calls `onToggle` instead of owning its own boolean —
   * needed by callers (SubAgentTurnView) that must know the instant the
   * block opens, to gate a lazy fetch on that exact transition rather
   * than guessing from a separately-owned boolean. */
  open?: boolean;
  onToggle?: (open: boolean) => void;
}

export function CollapsibleBlock({
  header,
  children,
  defaultOpen = false,
  className,
  testId,
  open: controlledOpen,
  onToggle,
}: CollapsibleBlockProps) {
  const { t } = useTranslation();
  const [uncontrolledOpen, setUncontrolledOpen] = useState(defaultOpen);
  const isControlled = controlledOpen !== undefined && onToggle !== undefined;
  const open = isControlled ? controlledOpen : uncontrolledOpen;
  const setOpen = (next: boolean) => {
    if (isControlled) onToggle(next);
    else setUncontrolledOpen(next);
  };
  return (
    <div className={className ? `surface-collapsible ${className}` : "surface-collapsible"} data-testid={testId}>
      <button
        type="button"
        className="surface-collapsible-header"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        aria-label={open ? (t("message.collapseMessageAria") as string) : (t("message.expandMessageAria") as string)}
        style={{ minHeight: 44, minWidth: 44 }}
      >
        <span className="surface-collapsible-caret" aria-hidden="true">
          {open ? "▾" : "▸"}
        </span>
        {header}
      </button>
      {open && <div className="surface-collapsible-body">{children}</div>}
    </div>
  );
}

/** The grammar's `renderEllipsis` — a clickable `...` that exists ONLY
 * when the hidden subtree has at least one renderable descendant
 * (render invariant: "The clickable ... exists only when its hidden
 * subtree contains at least one renderable child"). Activating it is the
 * caller's job (`onExpand`) — this component is pure chrome. */
export function Ellipsis({ onExpand, count }: { onExpand: () => void; count?: number }) {
  return (
    <button
      type="button"
      className="surface-ellipsis"
      onClick={onExpand}
      aria-label="Expand hidden content"
      style={{
        background: "none",
        border: "none",
        cursor: "pointer",
        padding: "2px 6px",
        color: "inherit",
        opacity: 0.7,
        font: "inherit",
        minHeight: 44,
        minWidth: 44,
      }}
    >
      {count && count > 0 ? `• • • (${count})` : "• • •"}
    </button>
  );
}
