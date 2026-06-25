import { API } from "../api";

type FrontendLogLevel = "debug" | "info" | "warn" | "error";

let installed = false;

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
    const errArg = args.find((a) => a instanceof Error) as Error | undefined;
    postFrontendLog(
      "error",
      "console.error",
      args.map(stringifyArg).join(" "),
      errArg?.stack || "",
    );
  };
}
