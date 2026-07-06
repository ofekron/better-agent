export const DEFAULT_BACKEND_PORT = "18765";
export const NATIVE_SERVER_URL_STORAGE_KEY = "better_agent_server_url";

export function normalizeServerUrl(value: string): string {
  const raw = value.trim().replace(/\/+$/, "");
  if (!raw) {
    throw new Error("required");
  }
  const withScheme = /^https?:\/\//i.test(raw) ? raw : `http://${raw}`;
  const parsed = new URL(withScheme);
  if (!parsed.port && parsed.protocol === "http:") {
    parsed.port = DEFAULT_BACKEND_PORT;
  }
  return `${parsed.protocol}//${parsed.host}`;
}

export function readNativeServerUrl(): string {
  try {
    return localStorage.getItem(NATIVE_SERVER_URL_STORAGE_KEY)?.replace(/\/+$/, "") ?? "";
  } catch {
    return "";
  }
}

export function writeNativeServerUrl(url: string): void {
  localStorage.setItem(NATIVE_SERVER_URL_STORAGE_KEY, normalizeServerUrl(url));
}

export function hasNativeServerUrl(): boolean {
  return readNativeServerUrl() !== "";
}

export function clearNativeServerUrl(): void {
  try {
    localStorage.removeItem(NATIVE_SERVER_URL_STORAGE_KEY);
  } catch {
    /* empty */
  }
}
