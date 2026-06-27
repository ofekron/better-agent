import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import Icon from "./Icon";

export interface SelectOption<T extends string = string> {
  value: T;
  label: ReactNode;
  disabled?: boolean;
}

interface SelectProps<T extends string = string> {
  value: T;
  options: SelectOption<T>[];
  onChange: (value: T) => void;
  disabled?: boolean;
  className?: string;
  /** Placeholder shown when no option matches the current value. */
  placeholder?: ReactNode;
  id?: string;
  title?: string;
  "aria-label"?: string;
  "data-testid"?: string;
}

interface MenuRect {
  left: number;
  top: number;
  width: number;
  /** When true the menu is rendered above the trigger. */
  above: boolean;
}

const MENU_MAX_HEIGHT = 280;

/** Styled replacement for a native <select>: a button trigger plus a
 * portal-rendered listbox. Keyboard-navigable, closes on outside click /
 * Escape / scroll, and matches the app's dark theme. */
export function Select<T extends string = string>({
  value,
  options,
  onChange,
  disabled,
  className,
  placeholder,
  id,
  title,
  "aria-label": ariaLabel,
  "data-testid": testId,
}: SelectProps<T>) {
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const [rect, setRect] = useState<MenuRect | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  const selected = options.find((o) => o.value === value);

  const computeRect = useCallback(() => {
    const el = triggerRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const below = window.innerHeight - r.bottom;
    const above = r.top;
    const openAbove = below < Math.min(MENU_MAX_HEIGHT, 200) && above > below;
    setRect({
      left: r.left,
      top: openAbove ? r.top : r.bottom,
      width: r.width,
      above: openAbove,
    });
  }, []);

  const close = useCallback(() => {
    setOpen(false);
    setActiveIndex(-1);
  }, []);

  const openMenu = useCallback(() => {
    if (disabled) return;
    computeRect();
    const idx = options.findIndex((o) => o.value === value);
    setActiveIndex(idx >= 0 ? idx : 0);
    setOpen(true);
  }, [disabled, computeRect, options, value]);

  useLayoutEffect(() => {
    if (open) computeRect();
  }, [open, computeRect]);

  useEffect(() => {
    if (!open) return;
    const onScroll = () => computeRect();
    const onResize = () => computeRect();
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (triggerRef.current?.contains(target)) return;
      if (menuRef.current?.contains(target)) return;
      close();
    };
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onResize);
    document.addEventListener("mousedown", onDown);
    return () => {
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onResize);
      document.removeEventListener("mousedown", onDown);
    };
  }, [open, computeRect, close]);

  const pick = (idx: number) => {
    const opt = options[idx];
    if (!opt || opt.disabled) return;
    onChange(opt.value);
    close();
    triggerRef.current?.focus();
  };

  const moveActive = (dir: 1 | -1) => {
    setActiveIndex((cur) => {
      const n = options.length;
      let next = cur;
      for (let i = 0; i < n; i++) {
        next = (next + dir + n) % n;
        if (!options[next]?.disabled) return next;
      }
      return cur;
    });
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (disabled) return;
    if (!open) {
      if (e.key === "Enter" || e.key === " " || e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        openMenu();
      }
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      close();
      triggerRef.current?.focus();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      moveActive(1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      moveActive(-1);
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      pick(activeIndex);
    } else if (e.key === "Home") {
      e.preventDefault();
      setActiveIndex(options.findIndex((o) => !o.disabled));
    } else if (e.key === "End") {
      e.preventDefault();
      for (let i = options.length - 1; i >= 0; i--) {
        if (!options[i].disabled) {
          setActiveIndex(i);
          break;
        }
      }
    }
  };

  return (
    <>
      <button
        type="button"
        id={id}
        ref={triggerRef}
        className={`bc-select-trigger${open ? " open" : ""}${className ? ` ${className}` : ""}`}
        disabled={disabled}
        title={title}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={ariaLabel}
        data-testid={testId}
        onClick={() => (open ? close() : openMenu())}
        onKeyDown={onKeyDown}
      >
        <span className="bc-select-value">
          {selected ? selected.label : <span className="bc-select-placeholder">{placeholder}</span>}
        </span>
        <Icon name="chevron-down" size={16} className="bc-select-caret" />
      </button>
      {open && rect
        ? createPortal(
            <div
              ref={menuRef}
              className={`bc-select-menu${rect.above ? " above" : ""}`}
              role="listbox"
              style={{
                position: "fixed",
                left: rect.left,
                width: rect.width,
                maxHeight: MENU_MAX_HEIGHT,
                ...(rect.above
                  ? { bottom: window.innerHeight - rect.top }
                  : { top: rect.top }),
              }}
            >
              {options.map((opt, idx) => (
                <button
                  type="button"
                  key={opt.value}
                  role="option"
                  aria-selected={opt.value === value}
                  className={
                    "bc-select-option" +
                    (opt.value === value ? " selected" : "") +
                    (idx === activeIndex ? " active" : "") +
                    (opt.disabled ? " disabled" : "")
                  }
                  disabled={opt.disabled}
                  onMouseEnter={() => setActiveIndex(idx)}
                  onClick={() => pick(idx)}
                >
                  <span className="bc-select-option-label">{opt.label}</span>
                  {opt.value === value && <Icon name="check" size={14} />}
                </button>
              ))}
            </div>,
            document.body,
          )
        : null}
    </>
  );
}

export default Select;
