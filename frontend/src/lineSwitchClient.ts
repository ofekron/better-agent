export const LINE_SWITCH_PORT = 18768;
export const LINE_SWITCH_STORAGE_KEY = "better_agent_line_switch";

export interface LineSwitchConnection {
  baseUrl: string;
  token: string;
}

export interface LineSwitchState {
  active_line: string;
  lines: Record<string, string>;
  line_targets?: Record<string, { backend_port?: number; backend_url?: string }>;
  incompatible: Record<string, string[]>;
  switchable: boolean;
  request?: { target?: string; status?: string; error?: string };
}

function storage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

export function parseLineSwitchAccessUrl(value: string): LineSwitchConnection {
  const raw = value.trim();
  const url = new URL(/^https?:\/\//i.test(raw) ? raw : `http://${raw}`);
  const token = url.hash.slice(1);
  if (!token || token.length < 32 || url.username || url.password) throw new Error("invalid");
  if (url.protocol !== "http:" && url.protocol !== "https:") throw new Error("invalid");
  url.hash = "";
  url.search = "";
  url.pathname = url.pathname.replace(/\/+$/, "");
  return { baseUrl: url.toString().replace(/\/$/, ""), token };
}

export function readLineSwitchConnection(): LineSwitchConnection | null {
  const raw = storage()?.getItem(LINE_SWITCH_STORAGE_KEY);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as LineSwitchConnection;
    return parseLineSwitchAccessUrl(`${parsed.baseUrl}/#${parsed.token}`);
  } catch {
    return null;
  }
}

export function writeLineSwitchConnection(connection: LineSwitchConnection): void {
  storage()?.setItem(LINE_SWITCH_STORAGE_KEY, JSON.stringify(connection));
}

export function clearLineSwitchConnection(): void {
  storage()?.removeItem(LINE_SWITCH_STORAGE_KEY);
}

async function request<T>(connection: LineSwitchConnection, path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${connection.baseUrl}${path}`, {
    ...init,
    cache: "no-store",
    headers: {
      Authorization: `Bearer ${connection.token}`,
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload as T;
}

export function fetchLineSwitchState(connection: LineSwitchConnection): Promise<LineSwitchState> {
  return request(connection, "/api/state");
}

export function requestLineSwitch(
  connection: LineSwitchConnection,
  target: string,
): Promise<{ status?: string; target_url?: string }> {
  return request(connection, "/api/switch", {
    method: "POST",
    body: JSON.stringify({ target }),
  });
}

export function targetServerUrl(
  state: LineSwitchState,
  target: string,
  connection: LineSwitchConnection,
  responseUrl = "",
): string {
  const configured = state.line_targets?.[target];
  const port = configured?.backend_port;
  const candidate = responseUrl || configured?.backend_url || "";
  const controller = new URL(connection.baseUrl);
  if (candidate) {
    const parsed = new URL(candidate);
    if (["127.0.0.1", "localhost", "::1"].includes(parsed.hostname)) {
      parsed.hostname = controller.hostname;
    }
    return parsed.toString().replace(/\/$/, "");
  }
  if (!Number.isInteger(port) || Number(port) < 1 || Number(port) > 65535) return "";
  controller.port = String(port);
  controller.pathname = "";
  return controller.toString().replace(/\/$/, "");
}
