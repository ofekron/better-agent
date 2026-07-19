import { useEffect, useId, useRef, useState } from "react";

import { ProgressButton } from "../progress/ProgressButton";
import type { NewSessionCreationAction } from "./NewSessionModal";
import Icon from "./Icon";

const ACTIONS: NewSessionCreationAction[] = ["create", "send", "send-and-open"];

interface Props {
  selectedAction: NewSessionCreationAction;
  labels: Record<NewSessionCreationAction, string>;
  loadingLabel: string;
  disabled: boolean;
  creating: boolean;
  onAction: (action: NewSessionCreationAction) => void;
}

export function NewSessionCreateButton({
  selectedAction,
  labels,
  loadingLabel,
  disabled,
  creating,
  onAction,
}: Props) {
  const [menuOpen, setMenuOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const firstItemRef = useRef<HTMLButtonElement>(null);
  const focusMenuOnOpenRef = useRef(false);
  const menuId = useId();
  const interactionDisabled = disabled || creating;
  const visibleMenuOpen = menuOpen && !interactionDisabled;

  useEffect(() => {
    if (!visibleMenuOpen) return;

    if (focusMenuOnOpenRef.current) {
      firstItemRef.current?.focus();
      focusMenuOnOpenRef.current = false;
    }

    const handlePointerDown = (event: PointerEvent) => {
      if (containerRef.current?.contains(event.target as Node)) return;
      setMenuOpen(false);
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setMenuOpen(false);
      toggleRef.current?.focus();
    };

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [visibleMenuOpen]);

  const runAction = (action: NewSessionCreationAction) => {
    setMenuOpen(false);
    onAction(action);
  };

  return (
    <div className="ns-create-split" ref={containerRef}>
      <ProgressButton
        className="btn-primary ns-create-primary"
        opId="session:create"
        progress={{ inflight: creating, error: null }}
        onClick={() => runAction(selectedAction)}
        extraDisabled={disabled}
        loadingChildren={loadingLabel}
      >
        {labels[selectedAction]}
      </ProgressButton>
      <button
        ref={toggleRef}
        type="button"
        className="btn-primary ns-create-toggle"
        aria-label={`${labels[selectedAction]} — ${labels.create}`}
        aria-haspopup="menu"
        aria-expanded={visibleMenuOpen}
        aria-controls={menuId}
        disabled={interactionDisabled}
        onClick={() => setMenuOpen((open) => !open)}
        onKeyDown={(event) => {
          if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
          event.preventDefault();
          focusMenuOnOpenRef.current = true;
          setMenuOpen(true);
        }}
      >
        <Icon name={visibleMenuOpen ? "chevron-up" : "chevron-down"} size={14} />
      </button>
      {visibleMenuOpen && (
        <div id={menuId} className="ns-create-menu" role="menu">
          {ACTIONS.map((action, index) => (
            <button
              key={action}
              ref={index === 0 ? firstItemRef : undefined}
              type="button"
              role="menuitem"
              className={action === selectedAction ? "selected" : undefined}
              onClick={() => runAction(action)}
            >
              <span>{labels[action]}</span>
              {action === selectedAction && <Icon name="check" size={14} />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
