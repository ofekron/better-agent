export const DEFAULT_OFFLINE_REQUEST_TIMEOUT_MS = 8000;

export class HttpStatusError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message || `HTTP ${status}`);
    this.name = "HttpStatusError";
    this.status = status;
  }
}

export function isRetryableOfflineError(error: unknown): boolean {
  if (error instanceof DOMException && error.name === "AbortError") return true;
  if (error instanceof TypeError) return true;
  if (error instanceof HttpStatusError) {
    return error.status === 408 || error.status === 425 || error.status === 429 || error.status >= 500;
  }
  return false;
}

export async function fetchWithTimeout(
  input: RequestInfo | URL,
  init: RequestInit = {},
  timeoutMs: number = DEFAULT_OFFLINE_REQUEST_TIMEOUT_MS,
): Promise<Response> {
  const controller = new AbortController();
  const upstreamSignal = init.signal;
  if (upstreamSignal?.aborted) {
    controller.abort(upstreamSignal.reason);
  } else if (upstreamSignal) {
    upstreamSignal.addEventListener("abort", () => controller.abort(upstreamSignal.reason), {
      once: true,
    });
  }
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } finally {
    window.clearTimeout(timer);
  }
}

export async function responseError(res: Response): Promise<HttpStatusError> {
  let detail = `HTTP ${res.status}`;
  try {
    const body = await res.json();
    if (body && typeof body.detail === "string") detail = body.detail;
  } catch {
    try {
      const text = await res.text();
      if (text) detail = text;
    } catch {
      detail = `HTTP ${res.status}`;
    }
  }
  return new HttpStatusError(res.status, detail);
}
