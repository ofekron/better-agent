// Exercises the real src/api.ts fetch wrappers end-to-end against a mocked
// global `fetch`. Other tests in this suite mock `src/api` itself, which
// leaves the wrapper bodies uncovered; this file is the authoritative
// coverage for that single source of truth.

import { beforeEach, describe, expect, it, vi } from "vitest";

// Mutable state read by the mocked Capacitor + nativeServerConfig modules
// during a fresh dynamic import of src/api, so each load-time branch of
// _resolveApiBase can be exercised independently.
const state = vi.hoisted(() => ({
  native: false,
  storedUrl: "",
}));

vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform: () => state.native },
}));
vi.mock("../src/nativeServerConfig", () => ({
  readNativeServerUrl: () => state.storedUrl,
}));

type ApiModule = typeof import("../src/api");

async function loadApi(): Promise<ApiModule> {
  vi.resetModules();
  const api = await import("../src/api");
  const { setBuiltinExtensionIds } = await import("../src/extensionIds");
  setBuiltinExtensionIds({ scheduler: "ext-scheduler" });
  return api;
}

function jsonRes(body: unknown, status = 200, statusText = "OK"): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText,
    headers: new Headers(),
    json: async () => body,
    text: async () => (typeof body === "string" ? body : JSON.stringify(body)),
  } as unknown as Response;
}

const fetchMock = vi.fn();

beforeEach(() => {
  state.native = false;
  state.storedUrl = "";
  vi.stubEnv("VITE_API_URL", "");
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
});

describe("api — load-time base resolution", () => {
  it("web same-origin: empty API base, ws derived from location (http)", async () => {
    const api = await loadApi();
    expect(api.API).toBe("");
    expect(api.getWsUrl()).toBe(`ws://${window.location.host}/ws/chat`);
    expect(api.WS_URL).toBe(`ws://${window.location.host}/ws/chat`);
  });

  it("web same-origin over https: wss upgrade from location protocol", async () => {
    const prev = window.location.href;
    window.location.href = "https://test.local:9/";
    try {
      const api = await loadApi();
      expect(api.API).toBe("");
      expect(api.getWsUrl()).toBe("wss://test.local:9/ws/chat");
    } finally {
      window.location.href = prev;
    }
  });

  it("remote http origin: API carries origin, http→ws upgrade", async () => {
    vi.stubEnv("VITE_API_URL", "http://remote.example:8080");
    const api = await loadApi();
    expect(api.API).toBe("http://remote.example:8080");
    expect(api.getWsUrl()).toBe("ws://remote.example:8080/ws/chat");
  });

  it("remote https origin: wss upgrade on the ws base", async () => {
    vi.stubEnv("VITE_API_URL", "https://secure.example");
    const api = await loadApi();
    expect(api.API).toBe("https://secure.example");
    expect(api.getWsUrl()).toBe("wss://secure.example/ws/chat");
  });

  it("native: prefers the runtime-configured server URL", async () => {
    state.native = true;
    state.storedUrl = "http://native.host";
    const api = await loadApi();
    expect(api.API).toBe("http://native.host");
    expect(api.getWsUrl()).toBe("ws://native.host/ws/chat");
  });

  it("native with no stored URL falls back to the build-time override", async () => {
    state.native = true;
    state.storedUrl = "";
    vi.stubEnv("VITE_API_URL", "http://fallback.example");
    const api = await loadApi();
    expect(api.API).toBe("http://fallback.example");
  });
});

describe("api — _json error handling", () => {
  it("throws HTTP status with the response body when not ok", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(jsonRes("boom", 500, "ERR"));
    await expect(api.fetchRuntimeProfiles()).rejects.toThrow("HTTP 500: boom");
  });

  it("falls back to statusText when the body cannot be read", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue({
      ok: false,
      status: 502,
      statusText: "Bad Gateway",
      headers: new Headers(),
      text: async () => {
        throw new Error("unreadable");
      },
    } as unknown as Response);
    await expect(api.fetchRuntimeProfiles()).rejects.toThrow("HTTP 502: Bad Gateway");
  });
});

describe("api — runtime profiles", () => {
  it("fetchRuntimeProfiles GETs the snapshot", async () => {
    const api = await loadApi();
    const snap = { active: "p1", profiles: [] };
    fetchMock.mockResolvedValue(jsonRes(snap));
    await expect(api.fetchRuntimeProfiles()).resolves.toEqual(snap);
    expect(fetchMock).toHaveBeenCalledWith("/api/runtime-profiles", {
      credentials: "include",
    });
  });

  it("createRuntimeProfile POSTs the body", async () => {
    const api = await loadApi();
    const created = { id: "p1" };
    fetchMock.mockResolvedValue(jsonRes(created));
    const body = { provider_id: "claude", runner: "native", name: "n" };
    await expect(api.createRuntimeProfile(body)).resolves.toEqual(created);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/runtime-profiles");
    expect(init).toMatchObject({
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  });

  it("patchRuntimeProfile PATCHes the named profile (path-encoded)", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(jsonRes({ id: "p 1" }));
    await api.patchRuntimeProfile("p 1", { name: "x" });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/runtime-profiles/p%201");
    expect(init).toMatchObject({ method: "PATCH" });
  });

  it("deleteRuntimeProfile DELETEs and parses", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(jsonRes(null));
    await api.deleteRuntimeProfile("p1");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/runtime-profiles/p1");
    expect(init).toMatchObject({ method: "DELETE", credentials: "include" });
  });

  it("activateRuntimeProfile POSTs to /activate", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(jsonRes({ id: "p1" }));
    await api.activateRuntimeProfile("p1");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/runtime-profiles/p1/activate");
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: "POST" });
  });
});

describe("api — analytics query assembly", () => {
  async function call(api: ApiModule, args: Parameters<ApiModule["fetchAnalytics"]>) {
    fetchMock.mockResolvedValue(jsonRes({}));
    await api.fetchAnalytics(...args);
    return fetchMock.mock.calls[0][0] as string;
  }

  it("no args → bare endpoint", async () => {
    const api = await loadApi();
    expect(await call(api, [])).toBe("/api/analytics");
  });

  it("start only", async () => {
    const api = await loadApi();
    expect(await call(api, ["2026-01-01"])).toBe("/api/analytics?start=2026-01-01");
  });

  it("end only", async () => {
    const api = await loadApi();
    expect(await call(api, [undefined, "2026-02-01"])).toBe("/api/analytics?end=2026-02-01");
  });

  it("explicit non-auto granularity is set", async () => {
    const api = await loadApi();
    expect(await call(api, [undefined, undefined, "day"])).toBe(
      "/api/analytics?granularity=day",
    );
  });

  it("'auto' granularity is omitted", async () => {
    const api = await loadApi();
    expect(await call(api, [undefined, undefined, "auto"])).toBe("/api/analytics");
  });

  it("forwards the abort signal", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(jsonRes({}));
    const controller = new AbortController();
    await api.fetchAnalytics(undefined, undefined, undefined, controller.signal);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ signal: controller.signal });
  });
});

describe("api — communications + chat", () => {
  it("fetchCommunications default limit, no session filter", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(jsonRes({ items: [], count: 0, total: 0 }));
    await api.fetchCommunications();
    expect(fetchMock.mock.calls[0][0]).toBe("/api/communications?limit=200");
  });

  it("fetchCommunications with session filter", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(jsonRes({ items: [], count: 0, total: 0 }));
    await api.fetchCommunications("s 1", 5);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/communications?limit=5&session_id=s+1");
  });

  it("postChatMessage POSTs to the chat's messages endpoint", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(jsonRes({ ok: true }));
    await api.postChatMessage("c 1", "sender-1", "hello");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/chats/c%201/messages");
    expect(init).toMatchObject({
      method: "POST",
      body: JSON.stringify({ sender_session_id: "sender-1", message: "hello" }),
    });
  });
});

describe("api — schedules", () => {
  it("fetchSessionSchedules GETs the session schedules list", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(jsonRes({ schedules: [] }));
    await api.fetchSessionSchedules("s 1");
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/extensions/ext-scheduler/backend/sessions/s%201/schedules",
    );
  });

  it("createSessionSchedule POSTs the schedule body", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(jsonRes({ schedule: { id: "x" } }));
    const body = { prompt: "p", kind: "once" as const, fire_at: "2026-01-01T00:00:00" };
    await api.createSessionSchedule("s1", body);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/extensions/ext-scheduler/backend/sessions/s1/schedules");
    expect(init).toMatchObject({ method: "POST", body: JSON.stringify(body) });
  });

  it("fetchAllSchedules GETs the global list", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(jsonRes({ schedules: [] }));
    await api.fetchAllSchedules();
    expect(fetchMock.mock.calls[0][0]).toBe("/api/schedules");
  });

  it("deleteScheduleById resolves when ok", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(jsonRes("", 204, "No Content"));
    await expect(api.deleteScheduleById("sch1")).resolves.toBeUndefined();
    expect(fetchMock.mock.calls[0]).toEqual([
      "/api/schedules/sch1",
      { method: "DELETE", credentials: "include" },
    ]);
  });

  it("deleteScheduleById throws when not ok", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(jsonRes("nope", 500, "ERR"));
    await expect(api.deleteScheduleById("sch1")).rejects.toThrow("HTTP 500: nope");
  });

  it("deleteScheduleById falls back to statusText when the body is unreadable", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue({
      ok: false,
      status: 500,
      statusText: "ERR",
      headers: new Headers(),
      text: async () => {
        throw new Error("unreadable");
      },
    } as unknown as Response);
    await expect(api.deleteScheduleById("sch1")).rejects.toThrow("HTTP 500: ERR");
  });

  it("cancelSchedule resolves when ok", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(jsonRes("", 204, "No Content"));
    await expect(api.cancelSchedule("sch1", "s1")).resolves.toBeUndefined();
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/extensions/ext-scheduler/backend/schedules/sch1?app_session_id=s1",
    );
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: "DELETE" });
  });

  it("cancelSchedule throws when not ok", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(jsonRes("denied", 403, "Forbidden"));
    await expect(api.cancelSchedule("sch1", "s1")).rejects.toThrow("HTTP 403: denied");
  });

  it("cancelSchedule falls back to statusText when the body is unreadable", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue({
      ok: false,
      status: 403,
      statusText: "Forbidden",
      headers: new Headers(),
      text: async () => {
        throw new Error("unreadable");
      },
    } as unknown as Response);
    await expect(api.cancelSchedule("sch1", "s1")).rejects.toThrow("HTTP 403: Forbidden");
  });
});

describe("api — session organization", () => {
  it("fetchSessionOrganization without project", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(jsonRes({ folders: [], tags: [], assignments: {} }));
    await api.fetchSessionOrganization();
    expect(fetchMock.mock.calls[0][0]).toBe("/api/session-organization");
  });

  it("fetchSessionOrganization with project filter", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(jsonRes({ folders: [], tags: [], assignments: {} }));
    await api.fetchSessionOrganization("proj 1");
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/session-organization?project_id=proj%201",
    );
  });

  it("createSessionFolder unwraps .folder", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(jsonRes({ folder: { id: "f1", name: "n" } }));
    await expect(api.createSessionFolder("p1", "n")).resolves.toEqual({
      id: "f1",
      name: "n",
    });
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({ project_id: "p1", name: "n" }),
    });
  });

  it("deleteSessionFolder passes the mode", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(
      jsonRes({ deleted: true, folder_ids: [], session_ids: [], folder_count: 0, session_count: 0 }),
    );
    await api.deleteSessionFolder("f1", "delete_sessions");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/session-folders/f1?mode=delete_sessions");
    expect(init).toMatchObject({ method: "DELETE" });
  });

  it("createSessionTag with project unwraps .tag", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(jsonRes({ tag: { id: "t1", name: "n" } }));
    await expect(api.createSessionTag("n", "p1")).resolves.toEqual({
      id: "t1",
      name: "n",
    });
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      body: JSON.stringify({ name: "n", project_id: "p1" }),
    });
  });

  it("createSessionTag without project sends null", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(jsonRes({ tag: { id: "t1", name: "n" } }));
    await api.createSessionTag("n");
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      body: JSON.stringify({ name: "n", project_id: null }),
    });
  });

  it("updateSessionOrganization PATCHes the session organization", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(jsonRes({ session_id: "s 1", organization: {} }));
    const patch = { folder_id: "f1", tag_ids: ["t1"] };
    await api.updateSessionOrganization("s 1", patch);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/sessions/s%201/organization");
    expect(init).toMatchObject({ method: "PATCH", body: JSON.stringify(patch) });
  });
});

describe("api — push tokens + notification preferences", () => {
  it("registerPushToken POSTs the registration", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(jsonRes(null));
    await expect(
      api.registerPushToken("dev1", "tok", "ios", "s1"),
    ).resolves.toBeUndefined();
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({ device_id: "dev1", token: "tok", platform: "ios", session_id: "s1" }),
    });
  });

  it("unregisterPushToken DELETEs without parsing", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(jsonRes(null));
    await api.unregisterPushToken("dev1");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/push-tokens/dev1");
    expect(init).toMatchObject({ method: "DELETE" });
  });

  it("getMobileNotificationPreferences unwraps the preferences", async () => {
    const api = await loadApi();
    const prefs = { pending_approvals: true, pending_questions: false, completed_turns: true };
    fetchMock.mockResolvedValue(jsonRes({ notification_preferences: prefs }));
    await expect(api.getMobileNotificationPreferences("dev1")).resolves.toEqual(prefs);
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/push-tokens/dev1/notification-preferences",
    );
  });

  it("updateMobileNotificationPreferences PATCHes and unwraps", async () => {
    const api = await loadApi();
    const prefs = { pending_approvals: false, pending_questions: false, completed_turns: false };
    fetchMock.mockResolvedValue(jsonRes({ notification_preferences: prefs }));
    await expect(
      api.updateMobileNotificationPreferences("dev1", { completed_turns: false }),
    ).resolves.toEqual(prefs);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/push-tokens/dev1/notification-preferences");
    expect(init).toMatchObject({
      method: "PATCH",
      body: JSON.stringify({ notification_preferences: { completed_turns: false } }),
    });
  });
});
