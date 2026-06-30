import { API } from "../api";

type FrontendLogLevel = "debug" | "info" | "warn" | "error";

let installed = false;
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

function postFrontendLog(level: FrontendLogLevel, source: string, message: string, stack = ""): void {
  if ((level === "debug" || level === "info") && message.startsWith("TESTAPE_SDK custom_state ")) {
    return;
  }
  const payload = {
    level,
    source,
    message,
    stack,
    url: window.location.href,
    user_agent: navigator.userAgent,
  };
  fetch(`${API}/api/logs/frontend`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    keepalive: true,
  }).catch(() => {});
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
