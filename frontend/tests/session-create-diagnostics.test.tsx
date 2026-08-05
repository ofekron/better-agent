// @vitest-environment happy-dom

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const progressMocks = vi.hoisted(() => ({
  startOp: vi.fn(),
  completeOp: vi.fn(),
  failOp: vi.fn(),
}));

const requestMocks = vi.hoisted(() => ({
  responseError: vi.fn(),
}));

vi.mock("../src/progress/store", () => progressMocks);
vi.mock("src/utils/offlineRequest", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/utils/offlineRequest")>()),
  responseError: requestMocks.responseError,
}));

import { useSession } from "../src/hooks/useSession";
import { HttpStatusError } from "../src/utils/offlineRequest";

const CREATE_OPTIONS = {
  name: "diagnostic-test",
  model: "gpt-5.4-mini",
  cwd: "/tmp",
};

type CreateOutcome = Response | Error;

function installFetch(createOutcome: CreateOutcome) {
  const logPayloads: Array<Record<string, unknown>> = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.includes("/api/logs/frontend")) {
      logPayloads.push(JSON.parse(String(init?.body)));
      return new Response("{}", { status: 200 });
    }
    if (url.includes("/api/sessions?") && (!init?.method || init.method === "GET")) {
      return new Response(JSON.stringify({
        sessions: [],
        has_more: false,
        snapshot_complete: true,
      }), { status: 200, headers: { "content-type": "application/json" } });
    }
    if (url.endsWith("/api/sessions") && init?.method === "POST") {
      if (createOutcome instanceof Error) throw createOutcome;
      return createOutcome;
    }
    if (url.endsWith("/healthz")) return new Response("{}", { status: 200 });
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { logPayloads };
}

async function flushDurableLog(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => window.setTimeout(resolve, 0));
  });
}

beforeEach(() => {
  progressMocks.startOp.mockReset();
  progressMocks.completeOp.mockReset();
  progressMocks.failOp.mockReset();
  requestMocks.responseError.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("fresh-session creation diagnostics", () => {
  it("logs a bounded redacted HTTP detail, fails progress, and rethrows the original error", async () => {
    const { logPayloads } = installFetch(new Response("{}", { status: 422 }));
    const original = new HttpStatusError(
      422,
      `token=provider-secret ${"x".repeat(900)}`,
    );
    requestMocks.responseError.mockResolvedValue(original);
    const { result } = renderHook(() => useSession());

    let thrown: unknown;
    await act(async () => {
      try {
        await result.current.createSession(CREATE_OPTIONS);
      } catch (error) {
        thrown = error;
      }
    });
    await flushDurableLog();

    expect(thrown).toBe(original);
    expect(progressMocks.failOp).toHaveBeenCalledWith(
      "session:create",
      expect.stringContaining("[REDACTED]"),
    );
    expect(progressMocks.completeOp).not.toHaveBeenCalledWith("session:create");
    expect(logPayloads).toHaveLength(1);
    const message = String(logPayloads[0].message);
    expect(message).not.toContain("provider-secret");
    const diagnostic = JSON.parse(message.slice(message.indexOf("{")));
    expect(diagnostic.code).toBe("http_error");
    expect(diagnostic.status).toBe(422);
    expect(diagnostic.detail).toContain("[REDACTED]");
    expect(diagnostic.detail.length).toBeLessThanOrEqual(512);
  });

  it.each([
    ["network", new TypeError("token=network-secret internal host")],
    ["abort", new DOMException("token=abort-secret internal host", "AbortError")],
  ])("logs only a fixed safe code for %s failures", async (_kind, original) => {
    const { logPayloads } = installFetch(original);
    const { result } = renderHook(() => useSession());

    let thrown: unknown;
    await act(async () => {
      try {
        await result.current.createSession(CREATE_OPTIONS);
      } catch (error) {
        thrown = error;
      }
    });
    await flushDurableLog();

    expect(thrown).toBe(original);
    const safeCode = _kind === "abort" ? "request_aborted" : "network_error";
    expect(progressMocks.failOp).toHaveBeenCalledWith("session:create", safeCode);
    expect(progressMocks.completeOp).not.toHaveBeenCalledWith("session:create");
    expect(logPayloads).toHaveLength(1);
    const serialized = JSON.stringify(logPayloads[0]);
    expect(serialized).toContain(safeCode);
    expect(serialized).not.toContain("internal host");
    expect(serialized).not.toContain(`${_kind}-secret`);
  });

  it("completes progress without a failure log on success", async () => {
    const session = {
      id: "created-session",
      name: "diagnostic-test",
      model: "gpt-5.4-mini",
      cwd: "/tmp",
      orchestration_mode: "native",
    };
    const { logPayloads } = installFetch(new Response(JSON.stringify(session), {
      status: 200,
      headers: { "content-type": "application/json" },
    }));
    const { result } = renderHook(() => useSession());

    let created: unknown;
    await act(async () => {
      created = await result.current.createSession(CREATE_OPTIONS);
    });
    await flushDurableLog();

    expect(created).toEqual(session);
    expect(progressMocks.completeOp).toHaveBeenCalledWith("session:create");
    expect(progressMocks.failOp).not.toHaveBeenCalledWith(
      "session:create",
      expect.anything(),
    );
    expect(logPayloads).toHaveLength(0);
  });
});
