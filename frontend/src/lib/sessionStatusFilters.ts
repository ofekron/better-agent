import { SESSION_STATUS_KEYS, type SessionStatusKey } from "./sessionRegistry";

export type SessionStatusFilterMode = "include" | "exclude";
export type SessionStatusFilters = Partial<Record<SessionStatusKey, SessionStatusFilterMode>>;

/** Tri-state cycle for one status chip: neutral → include → exclude →
 * neutral. Pure so the cycle is unit-testable without a DOM. */
export function nextStatusFilters(
  current: SessionStatusFilters,
  key: SessionStatusKey,
): SessionStatusFilters {
  const next = { ...current };
  if (current[key] === "include") next[key] = "exclude";
  else if (current[key] === "exclude") delete next[key];
  else next[key] = "include";
  return next;
}

/** Only the entries whose bucket the backend still knows about. */
export function sanitizeStatusFilters(value: unknown): SessionStatusFilters {
  if (!value || typeof value !== "object") return {};
  const out: SessionStatusFilters = {};
  for (const key of SESSION_STATUS_KEYS) {
    const mode = (value as Record<string, unknown>)[key];
    if (mode === "include" || mode === "exclude") out[key] = mode;
  }
  return out;
}

export function statusKeysWithMode(
  value: SessionStatusFilters,
  mode: SessionStatusFilterMode,
): SessionStatusKey[] {
  return SESSION_STATUS_KEYS.filter((key) => value[key] === mode);
}
