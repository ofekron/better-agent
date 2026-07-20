import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { makeAssistantMsg, makeSession, makeUserMsg } from "./fixtures";
import { renderApp } from "./harness";
import { useSession } from "../src/hooks/useSession";
import type { Session } from "../src/types";
import {
  applyOlderMessagePage,
  parseOlderMessagePage,
  type OlderMessagePage,
} from "../src/lib/messagePagination";
import { SingleFlight } from "../src/lib/singleFlight";

function page(overrides: Partial<OlderMessagePage> = {}): OlderMessagePage {
  return {
    messages: [],
    has_older: false,
    oldest_loaded_seq: null,
    total_messages: 2,
    ...overrides,
  };
}

function paginatedSession() {
  return makeSession({
    messages: [
      makeUserMsg({ id: "u-10", seq: 10 }),
      makeAssistantMsg({ id: "a-11", seq: 11 }),
      makeUserMsg({ id: "optimistic", seq: undefined }),
    ],
    pagination: {
      total_messages: 6,
      oldest_loaded_seq: 10,
      has_older: true,
    },
  });
}

interface PaginationFetchGate {
  calls: number[];
  resolve(beforeSeq: number, body: OlderMessagePage): void;
  reject(beforeSeq: number, error: Error): void;
  restore(): void;
}

function installPaginationFetchGate(
  session = paginatedSession(),
  additionalSessions: Session[] = [],
): PaginationFetchGate {
  const realFetch = globalThis.fetch;
  const sessions = [session, ...additionalSessions];
  const calls: number[] = [];
  const pending = new Map<number, Array<{
    resolve: (response: Response) => void;
    reject: (error: Error) => void;
  }>>();
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL): Promise<Response> => {
    const raw = typeof input === "string"
      ? input
      : input instanceof URL
        ? input.toString()
        : input.url;
    const url = new URL(raw, "http://localhost");
    if (url.pathname.endsWith(`/${session.id}/messages`)) {
      const beforeSeq = Number.parseInt(url.searchParams.get("before_seq") ?? "", 10);
      calls.push(beforeSeq);
      return new Promise<Response>((resolve, reject) => {
        pending.set(beforeSeq, [...(pending.get(beforeSeq) ?? []), { resolve, reject }]);
      });
    }
    const selected = sessions.find((candidate) =>
      url.pathname === `/api/sessions/${candidate.id}`
    );
    const body = selected ?? { sessions };
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }) as typeof fetch;

  const take = (beforeSeq: number) => {
    const queue = pending.get(beforeSeq);
    const request = queue?.shift();
    if (!request) throw new Error(`No pending pagination request for ${beforeSeq}`);
    if (queue?.length === 0) pending.delete(beforeSeq);
    return request;
  };
  return {
    calls,
    resolve(beforeSeq, body) {
      take(beforeSeq).resolve(new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      }));
    },
    reject(beforeSeq, error) {
      take(beforeSeq).reject(error);
    },
    restore() {
      globalThis.fetch = realFetch;
    },
  };
}

let paginationGate: PaginationFetchGate | null = null;

afterEach(() => {
  paginationGate?.restore();
  paginationGate = null;
});

describe("message pagination", () => {
  it("removes the load-older control after an authoritative empty page", async () => {
    const messages = [
      makeUserMsg({ id: "u-10", seq: undefined }),
      makeAssistantMsg({ id: "a-11", seq: undefined }),
    ];
    const session = makeSession({
      messages,
      pagination: {
        total_messages: messages.length,
        oldest_loaded_seq: 10,
        has_older: true,
      },
    });
    const h = await renderApp({ seed: { sessions: [session] } });

    await h.selectSession(session.id);
    expect(h.$(".load-older-link")).not.toBeNull();
    const turnCount = h.$$(".message-row").length;
    await h.click(".load-older-link");

    expect(h.$(".load-older-link")).toBeNull();
    expect(h.$$(".message-row")).toHaveLength(turnCount);
    h.unmount();
  });

  it("applies an exact-boundary page without duplicates or reordering existing messages", () => {
    const session = paginatedSession();
    const updated = applyOlderMessagePage(session, 10, page({
      messages: [
        makeUserMsg({ id: "u-8", seq: 8 }),
        makeAssistantMsg({ id: "optimistic", seq: 9 }),
      ],
      has_older: true,
      oldest_loaded_seq: 8,
      total_messages: 8,
    }));

    expect(updated.messages?.map((message) => message.id)).toEqual([
      "u-8",
      "u-10",
      "a-11",
      "optimistic",
    ]);
    expect(updated.pagination).toEqual({
      total_messages: 8,
      oldest_loaded_seq: 8,
      has_older: true,
    });
  });

  it("discards noncontiguous empty and non-empty pages", () => {
    const session = paginatedSession();
    expect(applyOlderMessagePage(session, 8, page())).toBe(session);
    expect(applyOlderMessagePage(session, 8, page({
      messages: [makeUserMsg({ id: "u-6", seq: 6 })],
      oldest_loaded_seq: 6,
    }))).toBe(session);
  });

  it("does not derive a pagination cursor from rendered message sequences", () => {
    const withoutPagination = makeSession({
      messages: [
        makeUserMsg({ id: "u-10", seq: 10 }),
        makeAssistantMsg({ id: "a-11", seq: 11 }),
      ],
      pagination: undefined,
    });
    expect(applyOlderMessagePage(withoutPagination, 10, page())).toBe(withoutPagination);
  });

  it("rejects inconsistent page boundaries", () => {
    expect(() => parseOlderMessagePage({
      messages: [makeUserMsg({ id: "u-8", seq: 8 })],
      has_older: true,
      oldest_loaded_seq: null,
      total_messages: 8,
    })).toThrow("numeric oldest_loaded_seq");
    expect(() => parseOlderMessagePage({
      messages: [],
      has_older: false,
      oldest_loaded_seq: 8,
      total_messages: 8,
    })).toThrow("null oldest_loaded_seq");
    expect(() => parseOlderMessagePage({
      messages: [
        makeUserMsg({ id: "u-8", seq: 8 }),
        makeAssistantMsg({ id: "a-9", seq: 9 }),
      ],
      has_older: true,
      oldest_loaded_seq: 5,
      total_messages: 8,
    })).toThrow("match its first message");
  });

  it("coalesces identical work but keeps different cursors independent", async () => {
    const singleFlight = new SingleFlight<string>();
    let resolveFirst!: () => void;
    let resolveSecond!: () => void;
    const firstWork = vi.fn(() => new Promise<void>((resolve) => { resolveFirst = resolve; }));
    const secondWork = vi.fn(() => new Promise<void>((resolve) => { resolveSecond = resolve; }));

    const first = singleFlight.run("s:10", firstWork);
    const duplicate = singleFlight.run("s:10", firstWork);
    const second = singleFlight.run("s:8", secondWork);
    await Promise.resolve();
    expect(firstWork).toHaveBeenCalledTimes(1);
    expect(secondWork).toHaveBeenCalledTimes(1);

    resolveSecond();
    resolveFirst();
    await Promise.all([first, duplicate, second]);
  });

  it("clears rejected work so the same cursor can retry", async () => {
    const singleFlight = new SingleFlight<string>();
    const failed = vi.fn().mockRejectedValueOnce(new Error("offline"));
    await expect(singleFlight.run("s:10", failed)).rejects.toThrow("offline");

    const retry = vi.fn().mockResolvedValue(undefined);
    await expect(singleFlight.run("s:10", retry)).resolves.toBeUndefined();
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it("coalesces the hook's same cursor and discards a reversed jump-ahead response", async () => {
    paginationGate = installPaginationFetchGate();
    const { result } = renderHook(() => useSession());
    await waitFor(() => expect(result.current.sessions).toHaveLength(1));
    await act(async () => {
      await result.current.selectSession("sess-1");
    });

    let exact!: Promise<void>;
    let duplicate!: Promise<void>;
    let jump!: Promise<void>;
    await act(async () => {
      exact = result.current.loadOlderMessages("sess-1", 10);
      duplicate = result.current.loadOlderMessages("sess-1", 10);
      jump = result.current.loadOlderMessages("sess-1", 8);
      await Promise.resolve();
    });
    expect(paginationGate.calls).toEqual([10, 8]);

    paginationGate.resolve(8, page({
      messages: [makeUserMsg({ id: "u-6", seq: 6 })],
      oldest_loaded_seq: 6,
    }));
    await act(async () => { await jump; });
    expect(result.current.currentSession?.pagination?.oldest_loaded_seq).toBe(10);

    paginationGate.resolve(10, page({
      messages: [
        makeUserMsg({ id: "u-8", seq: 8 }),
        makeAssistantMsg({ id: "a-9", seq: 9 }),
      ],
      has_older: true,
      oldest_loaded_seq: 8,
      total_messages: 8,
    }));
    await act(async () => { await Promise.all([exact, duplicate]); });
    expect(result.current.currentSession?.pagination?.oldest_loaded_seq).toBe(8);
    expect(result.current.currentSession?.messages?.map((message) => message.id)).toEqual([
      "u-8",
      "a-9",
      "u-10",
      "a-11",
      "optimistic",
    ]);
  });

  it("clears the hook's rejected cursor so a retry can retire pagination", async () => {
    paginationGate = installPaginationFetchGate();
    const { result } = renderHook(() => useSession());
    await waitFor(() => expect(result.current.sessions).toHaveLength(1));
    await act(async () => {
      await result.current.selectSession("sess-1");
    });

    const failed = result.current.loadOlderMessages("sess-1", 10);
    await Promise.resolve();
    paginationGate.reject(10, new Error("offline"));
    await expect(failed).rejects.toThrow("offline");

    const retry = result.current.loadOlderMessages("sess-1", 10);
    await Promise.resolve();
    expect(paginationGate.calls).toEqual([10, 10]);
    paginationGate.resolve(10, page());
    await act(async () => { await retry; });
    expect(result.current.currentSession?.pagination?.has_older).toBe(false);
  });

  it("discards a delayed page after switching to another session", async () => {
    const other = makeSession({ id: "sess-2", title: "Other session" });
    paginationGate = installPaginationFetchGate(paginatedSession(), [other]);
    const { result } = renderHook(() => useSession());
    await waitFor(() => expect(result.current.sessions).toHaveLength(2));
    await act(async () => {
      await result.current.selectSession("sess-1");
    });

    const delayed = result.current.loadOlderMessages("sess-1", 10);
    await Promise.resolve();
    await act(async () => {
      await result.current.selectSession("sess-2");
    });
    paginationGate.resolve(10, page({
      messages: [makeUserMsg({ id: "u-8", seq: 8 })],
      oldest_loaded_seq: 8,
    }));
    await act(async () => { await delayed; });

    expect(result.current.currentSession?.id).toBe("sess-2");
    expect(result.current.currentSession?.messages).toEqual(other.messages);
  });

  it("discards a delayed page after pagination metadata is removed", () => {
    const withoutPagination = { ...paginatedSession(), pagination: undefined };
    expect(applyOlderMessagePage(withoutPagination, 10, page({
      messages: [makeUserMsg({ id: "u-8", seq: 8 })],
      oldest_loaded_seq: 8,
    }))).toBe(withoutPagination);
  });

  it("renders a retryable error when loading older messages fails", async () => {
    const session = paginatedSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    h.backend.failNextWithStatus(500, `/api/sessions/${session.id}/messages`);

    await h.click(".load-older-link");
    expect(h.$(".load-older-error")?.textContent).toContain("chat.loadOlderFailed");
    expect(h.$(".load-older-error .load-older-link")).not.toBeNull();

    await h.click(".load-older-error .load-older-link");
    expect(h.$(".load-older-error")).toBeNull();
    expect(h.$(".load-older-link")).toBeNull();
    h.unmount();
  });
});
