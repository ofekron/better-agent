// Single source of truth for backend host/endpoints. Previously this
// const was copy-pasted into ~14 files; changing host/port now touches
// only this file.
//
// Three deployment modes:
//   1. Same-origin (default): Vite proxies /api+ws in dev; backend serves
//      the built frontend from the backend port in prod. API="", WS from location.host.
//   2. Capacitor native: runtime URL from localStorage, set via the
//      first-run ServerSetup screen. Falls back to VITE_API_URL if set
//      (debug builds). API carries the origin; WS upgrades to wss: when
//      the API is https.
//   3. Remote (VITE_API_URL): baked at build time for non-Capacitor
//      remote deployments.

import { Capacitor } from "@capacitor/core";
import { withTokenQuery } from "./bearerAuth";
import { extId } from "./extensionIds";
import { readNativeServerUrl } from "./nativeServerConfig";
import type {
  Schedule,
  SessionFolder,
  SessionOrganizationSnapshot,
  SessionTag,
} from "./types";

function _resolveApiBase(): string {
  // Capacitor native: prefer runtime-configured URL from localStorage
  if (Capacitor.isNativePlatform()) {
    const stored = readNativeServerUrl();
    if (stored) return stored;
  }
  // Build-time override (remote deploys, debug builds)
  return import.meta.env.VITE_API_URL || "";
}

const _apiBase = _resolveApiBase();

export const API = _apiBase;

const _wsBase = _apiBase
  ? _apiBase.replace(/^http/, "ws")
  : `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}`;

const _wsUrl = `${_wsBase}/ws/chat`;

/** Browsers can't set Authorization on the WS handshake — the backend
 * accepts ?token= as a fallback. Applied whenever a bearer token is
 * stored (native, or web contexts where the session cookie can't
 * travel, e.g. cross-site iframes); no-op otherwise. Resolved fresh at
 * every (re)connect so a token stored after module load (i.e.,
 * immediately after login) makes it into the URL. */
export function getWsUrl(): string {
  return withTokenQuery(_wsUrl);
}

// Kept as a stable identity so unchanged callers still type-check and
// the (more important) function above is what useWebSocket calls at
// each reconnect.
export const WS_URL = _wsUrl;

// ---------------------------------------------------------------------------
// Small typed helpers for backend-owned session surfaces (REST pull side of
// the pull+push contract; the push side is the matching WS frame).

async function _json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`HTTP ${res.status}: ${body || res.statusText}`);
  }
  return (await res.json()) as T;
}

/** Usage analytics over a time range. ``start``/``end`` are 'YYYY-MM-DD'.
 * Read-only snapshot; refetch when the user changes the range. */
export type AnalyticsGranularity = "auto" | "day" | "week" | "month";

export async function fetchAnalytics(
  start?: string,
  end?: string,
  granularity?: AnalyticsGranularity,
): Promise<AnalyticsReport> {
  const params = new URLSearchParams();
  if (start) params.set("start", start);
  if (end) params.set("end", end);
  if (granularity && granularity !== "auto") params.set("granularity", granularity);
  const qs = params.toString();
  const res = await fetch(
    `${API}/api/analytics${qs ? `?${qs}` : ""}`,
    { credentials: "include" },
  );
  return _json(res);
}

export interface CommunicationLogItem {
  id: string;
  kind: string;
  status: string;
  created_at: string;
  from_session_id: string;
  from_name: string;
  to_session_id?: string | null;
  to_name: string;
  chat_id?: string | null;
  chat_name: string;
  participants?: { session_id: string; name: string }[];
  addressed_target?: { kind: string; value: string; pool_affinity_key?: string } | null;
  body: string;
  messages?: {
    id: string;
    seq: number;
    created_at: string;
    from_session_id: string;
    from_name: string;
    body: string;
  }[];
}

export interface CommunicationLogResponse {
  items: CommunicationLogItem[];
  chats?: CommunicationLogItem[];
  count: number;
  total: number;
  chat_count?: number;
}

export async function fetchCommunications(
  sessionId?: string,
  limit = 200,
): Promise<CommunicationLogResponse> {
  const params = new URLSearchParams();
  params.set("limit", String(limit));
  if (sessionId) params.set("session_id", sessionId);
  const res = await fetch(`${API}/api/communications?${params.toString()}`, {
    credentials: "include",
  });
  return _json(res);
}

export async function postChatMessage(
  chatId: string,
  senderSessionId: string,
  message: string,
): Promise<unknown> {
  const res = await fetch(`${API}/api/chats/${encodeURIComponent(chatId)}/messages`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      sender_session_id: senderSessionId,
      message,
    }),
  });
  return _json(res);
}

export interface AnalyticsReport {
  range: { start: string; end: string; granularity: string };
  providers: { id: string; name: string; kind: string }[];
  sessions: {
    total: number;
    user_total: number;
    messages_total: number;
    series: { t: string; count: number; user_count: number }[];
    by_provider: { kind: string; name: string; count: number }[];
    by_model: { kind: string; model: string; count: number }[];
    by_orchestration: { mode: string; count: number }[];
  };
  turns: {
    total: number;
    series: { t: string; count: number; user_count: number; duration_ms: number }[];
    by_provider: { kind: string; name: string; turns: number }[];
    by_model: { kind: string; model: string; turns: number }[];
    duration_avg_ms: number;
    duration_p50_ms: number;
  };
  llm_calls: {
    total: number;
    token_usage: {
      input_tokens: number;
      output_tokens: number;
      cache_creation_input_tokens: number;
      cache_read_input_tokens: number;
      total_tokens: number;
      duration_ms?: number;
    };
    series: {
      t: string;
      count: number;
      input_tokens: number;
      output_tokens: number;
      cache_creation_input_tokens: number;
      cache_read_input_tokens: number;
      total_tokens: number;
    }[];
    by_provider: { provider_id: string; kind: string; name: string; calls: number; total_tokens: number }[];
    by_model: { kind: string; model: string; calls: number; total_tokens: number }[];
    by_source: { source: string; calls: number; total_tokens: number }[];
    by_reason: { reason: string; calls: number; total_tokens: number }[];
    recent: {
      id?: string;
      timestamp?: string;
      source: string;
      reason: string;
      provider_id: string;
      provider_kind: string;
      provider_name: string;
      model: string;
      reasoning_effort?: string | null;
      app_session_id?: string | null;
      provider_session_id?: string | null;
      prompt_preview: string;
      token_usage: {
        input_tokens: number;
        output_tokens: number;
        cache_creation_input_tokens: number;
        cache_read_input_tokens: number;
        total_tokens: number;
      };
      success?: boolean | null;
      error?: string | null;
    }[];
  };
}

/** Snapshot of the session's pending schedules. Push side:
 * `schedules_updated` WS frames (payload carries the full list). */
export async function fetchSessionSchedules(
  sessionId: string,
): Promise<{ schedules: Schedule[] }> {
  const res = await fetch(
    `${API}/api/extensions/${extId("scheduler")}/backend/sessions/${encodeURIComponent(sessionId)}/schedules`,
    { credentials: "include" },
  );
  return _json(res);
}

/** Schedule a prompt to fire into the session later. `fire_at` is a
 * naive local ISO datetime (the store rejects timezone-aware values). */
export async function createSessionSchedule(
  sessionId: string,
  body: {
    prompt: string;
    kind: "once" | "recurring";
    fire_at: string;
    interval_seconds?: number | null;
  },
): Promise<{ schedule: Schedule }> {
  const res = await fetch(
    `${API}/api/extensions/${extId("scheduler")}/backend/sessions/${encodeURIComponent(sessionId)}/schedules`,
    {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
  return _json(res);
}

/** Snapshot of every schedule across all sessions (Schedules page),
 * enriched with session_name/session_exists. Push side: the global
 * `schedules_changed` WS ping. */
export async function fetchAllSchedules(): Promise<{ schedules: Schedule[] }> {
  const res = await fetch(`${API}/api/schedules`, { credentials: "include" });
  return _json(res);
}

/** Cancel one schedule by id — works for orphans whose session was
 * deleted (no session gate server-side). */
export async function deleteScheduleById(scheduleId: string): Promise<void> {
  const res = await fetch(
    `${API}/api/schedules/${encodeURIComponent(scheduleId)}`,
    { method: "DELETE", credentials: "include" },
  );
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`HTTP ${res.status}: ${body || res.statusText}`);
  }
}

/** Cancel one schedule. */
export async function cancelSchedule(
  scheduleId: string,
  sessionId: string,
): Promise<void> {
  const params = new URLSearchParams({ app_session_id: sessionId });
  const res = await fetch(
    `${API}/api/extensions/${extId("scheduler")}/backend/schedules/${encodeURIComponent(scheduleId)}?${params.toString()}`,
    { method: "DELETE", credentials: "include" },
  );
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`HTTP ${res.status}: ${body || res.statusText}`);
  }
}

export async function fetchSessionOrganization(
  projectId?: string,
): Promise<SessionOrganizationSnapshot> {
  const qs = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
  const res = await fetch(`${API}/api/session-organization${qs}`, {
    credentials: "include",
  });
  return _json(res);
}

export async function createSessionFolder(
  projectId: string,
  name: string,
): Promise<SessionFolder> {
  const res = await fetch(`${API}/api/session-folders`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ project_id: projectId, name }),
  });
  return (await _json<{ folder: SessionFolder }>(res)).folder;
}

export async function createSessionTag(
  name: string,
  projectId?: string,
): Promise<SessionTag> {
  const res = await fetch(`${API}/api/session-tags`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ name, project_id: projectId || null }),
  });
  return (await _json<{ tag: SessionTag }>(res)).tag;
}

export async function updateSessionOrganization(
  sessionId: string,
  patch: {
    folder_id?: string | null;
    tag_ids?: string[];
    add_tag_ids?: string[];
    remove_tag_ids?: string[];
  },
): Promise<{
  session_id: string;
  organization: {
    folder_id?: string | null;
    tag_ids?: string[];
    tags?: SessionTag[];
  };
}> {
  const res = await fetch(
    `${API}/api/sessions/${encodeURIComponent(sessionId)}/organization`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify(patch),
    },
  );
  return _json(res);
}
