/** Fetch with automatic retry for transient errors (network failures, 5xx).
 *
 * Retries up to `maxAttempts` times with exponential backoff.
 * Non-transient errors (4xx) are not retried. */

const DEFAULT_MAX_ATTEMPTS = 3;
const BASE_DELAY_MS = 1000;
const MAX_DELAY_MS = 8000;

function isRetryableStatus(status: number): boolean {
  return status >= 500 || status === 429;
}

function delay(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

export async function fetchWithRetry(
  input: RequestInfo | URL,
  init?: RequestInit,
  opts?: { maxAttempts?: number },
): Promise<Response> {
  const max = opts?.maxAttempts ?? DEFAULT_MAX_ATTEMPTS;
  let lastError: Error | null = null;

  for (let attempt = 0; attempt < max; attempt++) {
    try {
      const r = await fetch(input, init);
      if (!r.ok && isRetryableStatus(r.status) && attempt < max - 1) {
        const d = Math.min(BASE_DELAY_MS * 2 ** attempt, MAX_DELAY_MS);
        await delay(d);
        continue;
      }
      return r;
    } catch (e) {
      lastError = e instanceof Error ? e : new Error(String(e));
      if (attempt < max - 1) {
        const d = Math.min(BASE_DELAY_MS * 2 ** attempt, MAX_DELAY_MS);
        await delay(d);
      }
    }
  }

  throw lastError ?? new Error("fetchWithRetry: all attempts failed");
}
