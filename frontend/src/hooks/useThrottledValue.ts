import { useEffect, useRef, useState } from "react";

/**
 * Returns `value` throttled to at most one update per `intervalMs`, always
 * emitting a trailing update so the final value is never dropped. With
 * `intervalMs <= 0` it passes the value through unchanged on the next tick.
 *
 * Used to coalesce rapid streaming-driven re-renders so chat layout
 * animations animate in chunks instead of firing on every token.
 */
export function useThrottledValue<T>(value: T, intervalMs: number): T {
  const [throttled, setThrottled] = useState(value);
  const lastEmit = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const latest = useRef(value);
  latest.current = value;

  useEffect(() => {
    if (intervalMs <= 0) {
      setThrottled(value);
      return;
    }
    const now = Date.now();
    const elapsed = now - lastEmit.current;
    if (elapsed >= intervalMs) {
      lastEmit.current = now;
      setThrottled(value);
      return;
    }
    // A trailing emit is already scheduled; it reads `latest` so it picks
    // up this newer value without resetting the timer.
    if (timer.current) return;
    timer.current = setTimeout(() => {
      lastEmit.current = Date.now();
      timer.current = null;
      setThrottled(latest.current);
    }, intervalMs - elapsed);
  }, [value, intervalMs]);

  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );

  return throttled;
}
