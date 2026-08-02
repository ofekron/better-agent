import { SingleFlight } from "./singleFlight";

export type BackendRequestResult =
  | { kind: "http_response"; response: Response }
  | { kind: "browser_access_blocked" }
  | { kind: "unreachable"; error: unknown }
  | { kind: "aborted"; error: unknown };

export type BackendAccessError =
  | { kind: "browser_access_blocked" }
  | { kind: "unreachable" }
  | { kind: "http_error"; scope: "auth" | "setup" | "sessions"; status: number };

const healthProbeFlight = new SingleFlight<string>();
const HEALTH_PROBE_TIMEOUT_MS = 5_000;

function endpoint(api: string, path: string): string {
  return `${api.replace(/\/+$/, "")}${path}`;
}

function probeServerResponse(api: string): Promise<void> {
  const normalizedApi = api.replace(/\/+$/, "");
  return healthProbeFlight.run(normalizedApi, async () => {
    await fetch(endpoint(normalizedApi, "/healthz"), {
      cache: "no-store",
      credentials: "omit",
      method: "GET",
      mode: "no-cors",
      signal: AbortSignal.timeout(HEALTH_PROBE_TIMEOUT_MS),
    });
  });
}

export async function requestBackend(
  api: string,
  path: string,
  init: RequestInit = {},
  callerSignal?: AbortSignal,
): Promise<BackendRequestResult> {
  try {
    const response = await fetch(endpoint(api, path), {
      ...init,
      signal: callerSignal ?? init.signal,
    });
    return { kind: "http_response", response };
  } catch (error) {
    if (callerSignal?.aborted || init.signal?.aborted) {
      return { kind: "aborted", error };
    }
    if (!(error instanceof TypeError)) {
      return { kind: "unreachable", error };
    }
    try {
      await probeServerResponse(api);
    } catch {
      if (callerSignal?.aborted || init.signal?.aborted) {
        return { kind: "aborted", error };
      }
      return { kind: "unreachable", error };
    }
    if (callerSignal?.aborted || init.signal?.aborted) {
      return { kind: "aborted", error };
    }
    return { kind: "browser_access_blocked" };
  }
}

export async function requestBackendWithTimeout(
  api: string,
  path: string,
  init: RequestInit,
  timeoutMs: number,
): Promise<BackendRequestResult> {
  const controller = new AbortController();
  const abortFromCaller = () => controller.abort(init.signal?.reason);
  init.signal?.addEventListener("abort", abortFromCaller, { once: true });
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await requestBackend(api, path, init, controller.signal);
  } finally {
    window.clearTimeout(timeout);
    init.signal?.removeEventListener("abort", abortFromCaller);
  }
}
