import { API } from "../api";

type FrontendLogLevel = "debug" | "info" | "warn" | "error";

let installed = false;
const EXTENSION_PERFORMANCE_EVENT = "better-agent:extension-performance";
const PERFORMANCE_INCIDENT_EVENT = "better-agent:performance-incident";
const DEFAULT_SLOW_TIMING_MS = 250;
const MAIN_THREAD_BLOCKED_MS = 80;
const MAX_LONG_FRAME_SCRIPTS = 5;
// Resolved at CALL time, not module load: capturing `fetch` once here
// would permanently pin whatever implementation was installed at first
// import, defeating any later `globalThis.fetch` reassignment (the
// bearerAuth interceptor in production, the test harness's mock fetch
// in tests).
function frontendLogFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> | null {
  return typeof fetch === "function" ? fetch(input, init) : null;
}
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

function redactedLocation(): string {
  const url = new URL(window.location.href);
  url.pathname = url.pathname.replace(/^\/s\/[^/]+/, "/s/[OPAQUE]");
  url.searchParams.delete("token");
  url.searchParams.delete("ticket");
  return url.toString();
}

function postFrontendLog(level: FrontendLogLevel, source: string, message: string, stack = ""): void {
  if ((level === "debug" || level === "info") && message.startsWith("TESTAPE_SDK custom_state ")) {
    return;
  }
  const payload = {
    level,
    source,
    message: redactSecrets(message),
    stack: redactSecrets(stack),
    url: redactSecrets(redactedLocation()),
    user_agent: navigator.userAgent,
  };
  const send = () => {
    try {
      frontendLogFetch(`${API}/api/logs/frontend`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        keepalive: true,
      })?.catch(() => {});
    } catch {
      // Logging must never affect the UI path being observed.
    }
  };
  window.setTimeout(send, 0);
}

export type MutationFailureDiagnostic = {
  actionKey: string;
  correlationId: string;
  failureKind: "network" | "rejected" | "unknown";
};

export function logMutationFailure(diagnostic: MutationFailureDiagnostic): void {
  const actionKey = diagnostic.actionKey.replace(/[^a-z0-9._-]/gi, "_").slice(0, 96);
  const correlationId = diagnostic.correlationId.match(
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
  )?.[0];
  if (!actionKey || !correlationId) return;
  window.setTimeout(() => {
    frontendLogFetch(`${API}/api/logs/frontend-mutation`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        event: "mutation_failed",
        action_key: actionKey,
        correlation_id: correlationId,
        failure_kind: diagnostic.failureKind,
      }),
      keepalive: true,
    })?.catch(() => {});
  }, 0);
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

  const nativeConsoleError = console.error.bind(console);
  console.error = (...args: unknown[]) => {
    nativeConsoleError(...args);
    if (isBenignConsoleError(args)) return;
    const errArg = args.find((a) => a instanceof Error) as Error | undefined;
    // React's error boundaries pass an ErrorInfo object carrying
    // `componentStack` (the "at <Component>" tree) as a separate arg.
    // That tree is the most useful clue for debugging a crash, so surface
    // it in the dedicated stack field instead of letting it get buried as
    // escaped JSON inside the message.
    const componentStack = extractComponentStack(args);
    let stack = errArg?.stack || "";
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

function installMainThreadBlockLogger(): void {
  const PerformanceObserverCtor = window.PerformanceObserver;
  if (!PerformanceObserverCtor) return;
  try {
    const observer = new PerformanceObserverCtor((list) => {
      for (const entry of list.getEntries()) {
        const durationMs = Math.round(entry.duration);
        if (durationMs < MAIN_THREAD_BLOCKED_MS) continue;
        window.dispatchEvent(new CustomEvent(PERFORMANCE_INCIDENT_EVENT, {
          detail: { start_time: entry.startTime, duration_ms: durationMs },
        }));
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
  installLongAnimationFrameLogger(PerformanceObserverCtor);
}

type LongFrameScript = {
  duration?: number;
  forcedStyleAndLayoutDuration?: number;
  sourceURL?: string;
  functionName?: string;
  invokerType?: string;
};

type LongFrameEntry = PerformanceEntry & {
  blockingDuration?: number;
  renderStart?: number;
  styleAndLayoutStart?: number;
  scripts?: LongFrameScript[];
};

function safeScriptSource(source: string | undefined): string {
  if (!source) return "unknown";
  try {
    const url = new URL(source, window.location.href);
    if (url.origin === window.location.origin) return url.pathname.slice(0, 240);
    return url.origin.slice(0, 160);
  } catch {
    return "unknown";
  }
}

function safeLabel(value: string | undefined): string {
  return String(value || "unknown").replace(/[^a-zA-Z0-9_$.:<> -]/g, "_").slice(0, 120);
}

function installLongAnimationFrameLogger(
  PerformanceObserverCtor: typeof PerformanceObserver,
): void {
  try {
    const observer = new PerformanceObserverCtor((list) => {
      for (const rawEntry of list.getEntries()) {
        const entry = rawEntry as LongFrameEntry;
        const durationMs = Math.round(entry.duration * 10) / 10;
        if (durationMs < MAIN_THREAD_BLOCKED_MS) continue;
        const scripts = [...(entry.scripts || [])]
          .sort((left, right) => (right.duration || 0) - (left.duration || 0))
          .slice(0, MAX_LONG_FRAME_SCRIPTS)
          .map((script) => ({
            duration_ms: Math.round((script.duration || 0) * 10) / 10,
            forced_style_layout_ms: Math.round((script.forcedStyleAndLayoutDuration || 0) * 10) / 10,
            source: safeScriptSource(script.sourceURL),
            function: safeLabel(script.functionName),
            invoker: safeLabel(script.invokerType),
          }));
        logDurable("main-thread", "long-animation-frame", {
          start_time: Math.round(entry.startTime * 10) / 10,
          duration_ms: durationMs,
          blocking_duration_ms: Math.round((entry.blockingDuration || 0) * 10) / 10,
          render_delay_ms: entry.renderStart
            ? Math.round((entry.renderStart - entry.startTime) * 10) / 10
            : 0,
          style_layout_delay_ms: entry.styleAndLayoutStart && entry.renderStart
            ? Math.round((entry.styleAndLayoutStart - entry.renderStart) * 10) / 10
            : 0,
          scripts,
        });
      }
    });
    observer.observe({ entryTypes: ["long-animation-frame"] });
  } catch {
    // Chromium versions without LoAF attribution keep the long-task diagnostic.
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
  try {
    for (const [key, metric] of Object.entries(detail.metrics as Record<string, unknown>)) {
      if (numericKeys.has(key)) {
        if (typeof metric !== "number" || !Number.isFinite(metric) || metric < 0) return false;
        continue;
      }
      if (key === "in_flight" || key === "accepted") {
        if (typeof metric !== "boolean") return false;
        continue;
      }
      if (typeof metric !== "string" || !categoricalValues[key]?.has(metric)) return false;
    }
  } catch {
    return false;
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
