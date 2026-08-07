import { Capacitor } from "@capacitor/core";
import { App as CapApp } from "@capacitor/app";
import { API } from "../api";

type FrontendLogLevel = "debug" | "info" | "warn" | "error";

let installed = false;

// One id per page load (i.e. per WebView renderer process boot). Android
// LMK/OOM kills terminate the process with no chance to log the kill itself
// (see installLifecycleLogger below), so the only way to tell "this boot
// silently died" apart from "this boot backgrounded normally, still same
// tab" in post-hoc frontend.log analysis is to stamp every lifecycle event
// with a value that's stable within a boot and changes across boots. Bare
// URLs (e.g. the generic Capacitor shell "http://localhost/") collapse
// unrelated boots into the same key otherwise.
const BOOT_ID = crypto.randomUUID();
const EXTENSION_PERFORMANCE_EVENT = "better-agent:extension-performance";
const DEFAULT_SLOW_TIMING_MS = 250;
const MAIN_THREAD_BLOCKED_MS = 80;
const SECRET_PATTERNS: Array<[RegExp, string]> = [
  [/([?&](?:token|access_token|refresh_token|ticket)=)[^&#\s]+/gi, "$1[REDACTED]"],
  [/(\bBearer\s+)[A-Za-z0-9._~+/=-]+/gi, "$1[REDACTED]"],
  [/(\b(?:token|access_token|refresh_token|ticket)\s*[:=]\s*)[^\s,;]+/gi, "$1[REDACTED]"],
];
const benignConsoleErrorMessages = new Set([
  "No processing needed",
  "No processing needed.",
]);

function stringifyArg(arg: unknown): string {
  if (arg instanceof Error) return arg.stack || arg.message;
  if (typeof arg === "string") return arg;
  try {
    return JSON.stringify(arg);
  } catch {
    return String(arg);
  }
}

function redactSecrets(value: string): string {
  return SECRET_PATTERNS.reduce(
    (redacted, [pattern, replacement]) => redacted.replace(pattern, replacement),
    value,
  );
}

export function sanitizeDiagnosticText(value: string, maxChars: number): string {
  return redactSecrets(value).slice(0, maxChars);
}

function postFrontendLog(
  level: FrontendLogLevel,
  source: string,
  message: string,
  stack = "",
  immediate = false,
): void {
  if ((level === "debug" || level === "info") && message.startsWith("TESTAPE_SDK custom_state ")) {
    return;
  }
  const payload = {
    level,
    source,
    message: redactSecrets(message),
    stack: redactSecrets(stack),
    url: redactSecrets(window.location.href),
    user_agent: navigator.userAgent,
  };
  const send = () => {
    try {
      fetch(`${API}/api/logs/frontend`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        keepalive: true,
      }).catch(() => {});
    } catch {
      // Logging must never affect the UI path being observed.
    }
  };
  if (immediate) {
    send();
    return;
  }
  window.setTimeout(send, 0);
}

/** Durable diagnostic channel: ships a structured line to the backend
 * `frontend.log` regardless of console wiring. Use for transient frontend
 * state transitions that must survive a backend restart for post-hoc
 * forensics (queue banner / offline backlog / reconnect). Never pass prompt
 * content — ids and lengths only. */
export function logDurable(source: string, stage: string, data: Record<string, unknown>): void {
  let message: string;
  try {
    message = `${stage} ${JSON.stringify(data)}`;
  } catch {
    message = `${stage} <unserializable>`;
  }
  postFrontendLog("warn", source, message);
}

export function logDurableImmediate(
  source: string,
  stage: string,
  data: Record<string, unknown>,
): void {
  let message: string;
  try {
    message = `${stage} ${JSON.stringify(data)}`;
  } catch {
    message = `${stage} <unserializable>`;
  }
  postFrontendLog("warn", source, message, "", true);
}

export function logTiming(
  source: string,
  stage: string,
  startedAt: number,
  data: Record<string, unknown> = {},
  thresholdMs = DEFAULT_SLOW_TIMING_MS,
): void {
  const durationMs = Math.round(performance.now() - startedAt);
  if (durationMs < thresholdMs) return;
  logDurable(source, stage, { ...data, duration_ms: durationMs });
}

export function logFailure(
  source: string,
  stage: string,
  error: unknown,
  data: Record<string, unknown> = {},
): void {
  const err = error instanceof Error ? error : new Error(String(error));
  postFrontendLog(
    "error",
    source,
    `${stage} ${redactSecrets(JSON.stringify({ ...data, error: err.message }))}`,
    err.stack || "",
  );
}

export function timeAsync<T>(
  source: string,
  stage: string,
  fn: () => Promise<T>,
  data: Record<string, unknown> = {},
  thresholdMs = DEFAULT_SLOW_TIMING_MS,
): Promise<T> {
  const startedAt = performance.now();
  return fn().then(
    (value) => {
      logTiming(source, stage, startedAt, data, thresholdMs);
      return value;
    },
    (error) => {
      logFailure(source, `${stage}_failed`, error, {
        ...data,
        duration_ms: Math.round(performance.now() - startedAt),
      });
      throw error;
    },
  );
}

export function installFrontendLogger(): void {
  if (installed) return;
  installed = true;

  window.addEventListener("error", (event) => {
    postFrontendLog(
      "error",
      "window.error",
      event.message || "Uncaught error",
      event.error instanceof Error ? event.error.stack || "" : "",
    );
  });

  window.addEventListener("unhandledrejection", (event) => {
    const reason = event.reason;
    postFrontendLog(
      "error",
      "unhandledrejection",
      stringifyArg(reason),
      reason instanceof Error ? reason.stack || "" : "",
    );
  });

  window.addEventListener(EXTENSION_PERFORMANCE_EVENT, (event) => {
    const detail = event instanceof CustomEvent ? event.detail : null;
    if (!isExtensionPerformanceDetail(detail)) return;
    logDurable(`extension-perf.${detail.extension}`, detail.stage, detail.metrics);
  });

  installMainThreadBlockLogger();
  installLifecycleLogger();

  const nativeConsoleError = console.error.bind(console);
  console.error = (...args: unknown[]) => {
    nativeConsoleError(...args);
    if (isBenignConsoleError(args)) return;
    // React's error boundaries pass an ErrorInfo object carrying
    // `componentStack` (the "at <Component>" tree) as a separate arg.
    // That tree is the most useful clue for debugging a crash, so surface
    // it in the dedicated stack field instead of letting it get buried as
    // escaped JSON inside the message.
    const componentStack = extractComponentStack(args);
    let stack = extractConsoleStack(args);
    if (componentStack) {
      stack = stack
        ? `${stack}\nReact component stack:${componentStack}`
        : `React component stack:${componentStack}`;
    }
    // Drop the React ErrorInfo object from the message — its
    // componentStack is now in the stack field; keeping it would
    // duplicate the whole tree as escaped JSON bloat.
    const msgArgs = args.filter(
      (a) => !isReactErrorInfo(a),
    );
    postFrontendLog(
      "error",
      "console.error",
      msgArgs.map(stringifyArg).join(" "),
      stack,
    );
  };
}

// Android WebView LMK/OOM kills reclaim the process by total RSS (native +
// graphics + DOM + bitmaps included), not by the V8 JS heap alone. A low
// used_js_heap_bytes/js_heap_limit_bytes ratio does NOT rule out a memory
// kill — it only rules out JS-heap pressure specifically. Read these
// alongside the kill, never as a standalone exoneration.
interface ChromeMemoryInfo {
  usedJSHeapSize: number;
  totalJSHeapSize: number;
  jsHeapSizeLimit: number;
}

/** Chromium-only (`performance.memory`), present on Android WebView; absent
 *  elsewhere. Best-effort snapshot for post-hoc crash-loop forensics — never
 *  gate behavior on it. */
export function memorySnapshot(): Record<string, number> {
  const memory = (performance as Performance & { memory?: ChromeMemoryInfo }).memory;
  if (!memory) return {};
  return {
    used_js_heap_bytes: memory.usedJSHeapSize,
    total_js_heap_bytes: memory.totalJSHeapSize,
    js_heap_limit_bytes: memory.jsHeapSizeLimit,
  };
}

// Temporary diagnostic for the Android crash-loop investigation: a renderer
// process kill (the leading hypothesis) fires none of visibilitychange /
// pagehide / appStateChange — the process is gone before any handler can
// run. Observed cycles are alive only ~5s, shorter than the existing 15s
// websocket-traffic summary cadence, so without a tighter heartbeat a kill
// leaves zero memory samples. Remove once the crash-loop cause is found.
//
// Bounded to HEARTBEAT_MAX_TICKS: an unbounded interval would be a
// permanent per-boot network+allocation beacon, which risks perturbing the
// very memory/resource pressure this is trying to measure. Every observed
// crash-loop cycle reboots the app (and this heartbeat with it), so a
// per-boot cap loses no coverage of the loop itself.
const HEARTBEAT_INTERVAL_MS = 2_000;
// 60 ticks, NOT "2 minutes wall-clock": a background/frozen page pauses the
// interval (observed ~41 ticks logged across a 28-minute freeze-and-resume
// in practice), so this caps foreground activity, not elapsed time since
// boot — a single boot that's repeatedly backgrounded can log for far
// longer than 2 minutes of wall-clock before hitting the cap.
const HEARTBEAT_MAX_TICKS = 60;

function installMemoryHeartbeat(): void {
  if (!Capacitor.isNativePlatform()) return;
  if (!("memory" in performance)) return;
  let ticks = 0;
  const intervalId = window.setInterval(() => {
    logDurable("lifecycle", "memory_heartbeat", { boot_id: BOOT_ID, ...memorySnapshot() });
    ticks += 1;
    if (ticks >= HEARTBEAT_MAX_TICKS) window.clearInterval(intervalId);
  }, HEARTBEAT_INTERVAL_MS);
}

/** Diagnostics for the Android boot-crash-loop pattern: repeated fast
 *  connect/disconnect cycles with no in-app error or reload-trigger code to
 *  explain them (suspected native WebView process kill outside this app's
 *  visibility). None of these events can themselves prevent the crash —
 *  they only ensure the NEXT occurrence leaves a trail in frontend.log. */
function installLifecycleLogger(): void {
  document.addEventListener("visibilitychange", () => {
    logDurableImmediate("lifecycle", "visibility_change", {
      boot_id: BOOT_ID,
      visibility_state: document.visibilityState,
      ...memorySnapshot(),
    });
  });

  // Fires synchronously right before the page is torn down (nav, tab
  // close, OS reclaim) — the last chance to get a log out before state
  // is gone, hence the immediate (non-debounced) send.
  window.addEventListener("pagehide", (event) => {
    logDurableImmediate("lifecycle", "pagehide", {
      boot_id: BOOT_ID,
      persisted: event.persisted,
      ...memorySnapshot(),
    });
  });

  if (Capacitor.isNativePlatform()) {
    CapApp.addListener("appStateChange", (state) => {
      logDurableImmediate("lifecycle", "app_state_change", {
        boot_id: BOOT_ID,
        is_active: state.isActive,
        ...memorySnapshot(),
      });
    }).catch(() => {
      // Logging must never affect the UI path being observed.
    });
  }

  installMemoryHeartbeat();
}

function installMainThreadBlockLogger(): void {
  const PerformanceObserverCtor = window.PerformanceObserver;
  if (!PerformanceObserverCtor) return;
  try {
    const observer = new PerformanceObserverCtor((list) => {
      for (const entry of list.getEntries()) {
        const durationMs = Math.round(entry.duration);
        if (durationMs < MAIN_THREAD_BLOCKED_MS) continue;
        logDurable("main-thread", "blocked", {
          duration_ms: durationMs,
          entry_type: entry.entryType,
          name: entry.name,
        });
      }
    });
    observer.observe({ entryTypes: ["longtask"] });
  } catch {
    // Browser support varies; absence of longtask support should not affect boot.
  }
}

function isExtensionPerformanceDetail(value: unknown): value is {
  extension: string;
  stage: string;
  metrics: Record<string, unknown>;
} {
  if (!value || typeof value !== "object") return false;
  const detail = value as Record<string, unknown>;
  if (typeof detail.extension !== "string" || !/^[a-z0-9.-]{1,80}$/.test(detail.extension)) return false;
  if (typeof detail.stage !== "string" || !/^[a-z0-9._-]{1,80}$/.test(detail.stage)) return false;
  if (!detail.metrics || typeof detail.metrics !== "object" || Array.isArray(detail.metrics)) return false;
  const numericKeys = new Set([
    "generation", "authority_generation", "subscribers", "max_subscribers", "attempts",
    "completions", "failures", "suppressed", "coalesced", "outstanding",
    "max_outstanding", "duration_ms",
  ]);
  const categoricalValues: Record<string, Set<string>> = {
    trigger: new Set(["initial", "manual", "online", "visibility"]),
    reason: new Set(["hidden", "cadence", "auth_scope", "idle", "version_replaced"]),
  };
  for (const [key, metric] of Object.entries(detail.metrics as Record<string, unknown>)) {
    if (numericKeys.has(key)) {
      if (typeof metric !== "number" || !Number.isFinite(metric) || metric < 0) return false;
      continue;
    }
    if (key === "in_flight" || key === "accepted") {
      if (typeof metric !== "boolean") return false;
      continue;
    }
    if (!categoricalValues[key]?.has(String(metric))) return false;
  }
  return true;
}

function isBenignConsoleError(args: unknown[]): boolean {
  if (args.length !== 1) return false;
  const arg = args[0];
  if (!arg || typeof arg !== "object" || arg instanceof Error || Array.isArray(arg)) {
    return false;
  }
  const keys = Object.keys(arg);
  if (keys.length !== 1 || keys[0] !== "message") return false;
  const message = (arg as { message?: unknown }).message;
  return typeof message === "string" && benignConsoleErrorMessages.has(message);
}

function extractConsoleStack(args: unknown[]): string {
  const error = args.find((arg) => arg instanceof Error);
  if (error instanceof Error) return error.stack || "";
  for (const arg of args) {
    if (!arg || typeof arg !== "object" || Array.isArray(arg)) continue;
    const stack = (arg as { stack?: unknown }).stack;
    if (typeof stack === "string" && stack) return stack;
  }
  return "";
}

/** Pull React's `componentStack` out of a console.error arg list (the
 * `ErrorInfo` object a boundary passes next to the Error), if present. */
function extractComponentStack(args: unknown[]): string {
  for (const a of args) {
    if (isReactErrorInfo(a)) {
      return (a as { componentStack: string }).componentStack;
    }
  }
  return "";
}

function isReactErrorInfo(a: unknown): boolean {
  return (
    !!a &&
    typeof a === "object" &&
    typeof (a as { componentStack?: unknown }).componentStack === "string"
  );
}
