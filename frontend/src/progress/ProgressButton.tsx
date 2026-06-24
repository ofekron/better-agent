import { type ButtonHTMLAttributes, type ReactNode } from "react";
import { useOpProgress } from "./store";

interface Props extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "disabled"> {
  opId: string | readonly string[];
  /** Business-rule disabled (e.g. invalid form). OR-combined with
   * inflight, not replaced. */
  extraDisabled?: boolean;
  /** Optional content shown while inflight. Defaults to `children`. */
  loadingChildren?: ReactNode;
  children: ReactNode;
}

export function ProgressButton({
  opId,
  extraDisabled,
  loadingChildren,
  children,
  className,
  ...rest
}: Props) {
  const { inflight, error } = useOpProgress(opId);
  const disabled = inflight || !!extraDisabled;
  const cls = [
    className,
    inflight ? "progress-inflight" : null,
    error ? "progress-error" : null,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <button
      {...rest}
      disabled={disabled}
      className={cls || undefined}
      data-progress-inflight={inflight ? "1" : undefined}
      data-progress-error={error ?? undefined}
      title={error ? error : rest.title}
    >
      {inflight && loadingChildren !== undefined ? loadingChildren : children}
    </button>
  );
}
