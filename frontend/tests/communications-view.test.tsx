import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CommunicationsView } from "../src/components/CommunicationsView";

const fetchCommunications = vi.fn();
const here = dirname(fileURLToPath(import.meta.url));

vi.mock("../src/api", () => ({
  fetchCommunications: (...args: unknown[]) => fetchCommunications(...args),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, options?: { defaultValue?: string; count?: number; total?: number }) => {
      if (options?.defaultValue) {
        return options.defaultValue
          .replace("{{count}}", String(options.count ?? ""))
          .replace("{{total}}", String(options.total ?? ""));
      }
      return key;
    },
  }),
}));

afterEach(() => {
  cleanup();
  fetchCommunications.mockReset();
});

describe("CommunicationsView", () => {
  function communication(overrides = {}) {
    return {
      id: "delivered:target:msg-1",
      kind: "mssg",
      status: "delivered",
      created_at: "2026-01-01T00:00:00+00:00",
      from_session_id: "sender-session-1234",
      from_name: "Sender Session",
      to_session_id: "target-session-5678",
      to_name: "Target Session",
      chat_id: null,
      chat_name: "",
      body: "first line\nsecond line",
      ...overrides,
    };
  }

  it("renders communication rows collapsed by default and expands on click", async () => {
    fetchCommunications.mockResolvedValueOnce({
      items: [communication()],
      count: 1,
      total: 1,
    });

    const { container } = render(<CommunicationsView mode="page" />);

    await waitFor(() => expect(screen.getByText("mssg")).toBeTruthy());
    expect(screen.queryByText("second line")).toBeNull();

    fireEvent.click(container.querySelector(".communication-chevron") as HTMLButtonElement);

    expect(document.querySelector(".communication-body pre")?.textContent).toContain("second line");
    expect(screen.getByRole("link", { name: "Sender Session · send" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Target Session · targ" })).toBeTruthy();
  });

  it("requests a session-filtered log for panel mode", async () => {
    fetchCommunications.mockResolvedValueOnce({
      items: [communication()],
      count: 1,
      total: 1,
    });

    render(<CommunicationsView mode="panel" sessionId="target-session-5678" />);

    await waitFor(() => expect(fetchCommunications).toHaveBeenCalledWith("target-session-5678", 100));
  });

  it("renders addressed target and participant ids when names are missing", async () => {
    fetchCommunications.mockResolvedValueOnce({
      items: [communication({
        id: "queued:target:message",
        status: "queued",
        from_session_id: "sender-session",
        from_name: "Sender",
        to_session_id: "resolved-worker",
        to_name: "Resolved Worker",
        participants: [
          { session_id: "sender-session", name: "" },
          { session_id: "resolved-worker", name: "" },
        ],
        addressed_target: {
          kind: "pool",
          value: "review",
          pool_affinity_key: "thread-1",
        },
        body: "please review",
      })],
      count: 1,
      total: 1,
    });

    const { container } = render(<CommunicationsView mode="panel" sessionId="sender-session" />);

    await waitFor(() => expect(screen.getByRole("link", { name: "Resolved Worker · reso" })).toBeTruthy());
    expect(screen.queryByText("review · thread-1")).toBeNull();
    fireEvent.click(container.querySelector(".communication-chevron") as HTMLButtonElement);
    expect(screen.getByText("review · thread-1")).toBeTruthy();
    expect(screen.getByText("sender-session, resolved-worker")).toBeTruthy();
  });

  it.each([
    ["mssg"],
    ["team_ask"],
    ["delegate_task"],
  ])("keeps the receiver visible for %s rows with addressed routing metadata", async (kind) => {
    fetchCommunications.mockResolvedValueOnce({
      items: [communication({
        id: `${kind}:target:message`,
        kind,
        status: "queued",
        from_session_id: "sender-session",
        from_name: "Sender",
        to_session_id: "resolved-worker",
        to_name: "",
        addressed_target: {
          kind: "pool",
          value: "review",
          pool_affinity_key: "thread-1",
        },
        body: "please review",
      })],
      count: 1,
      total: 1,
    });

    const { container } = render(<CommunicationsView mode="page" />);

    await waitFor(() => expect(screen.getByRole("link", { name: "resolved-worker · reso" })).toBeTruthy());
    expect(screen.queryByText("review · thread-1")).toBeNull();
    fireEvent.click(container.querySelector(".communication-chevron") as HTMLButtonElement);
    expect(screen.getByText("review · thread-1")).toBeTruthy();
  });

  it("renders chats in a separate section from direct messages", async () => {
    fetchCommunications.mockResolvedValueOnce({
      items: [communication({ body: "direct message" })],
      chats: [communication({
        id: "chat:room:1",
        kind: "chat",
        status: "posted",
        from_session_id: "sender-session",
        from_name: "Sender",
        to_session_id: null,
        to_name: "Team Room",
        chat_id: "room",
        chat_name: "Team Room",
        participants: [
          { session_id: "sender-session", name: "Sender" },
          { session_id: "receiver-session", name: "Receiver" },
        ],
        body: "second room message",
        messages: [
          {
            id: "chat:room:1",
            seq: 1,
            created_at: "2026-01-01T00:00:00+00:00",
            from_session_id: "sender-session",
            from_name: "Sender",
            body: "first room message",
          },
          {
            id: "chat:room:2",
            seq: 2,
            created_at: "2026-01-01T00:01:00+00:00",
            from_session_id: "receiver-session",
            from_name: "Receiver",
            body: "second room message",
          },
        ],
      })],
      count: 1,
      total: 2,
      chat_count: 1,
    });

    const { container } = render(<CommunicationsView mode="page" />);

    await waitFor(() => expect(screen.getByText("communications.chats")).toBeTruthy());
    expect(screen.getByText("communications.chats")).toBeTruthy();
    expect(screen.getByText("communications.directMessages")).toBeTruthy();
    expect(container.querySelectorAll(".communication-card-chat")).toHaveLength(1);
    expect(container.querySelectorAll(".communications-list .communication-card-chat")).toHaveLength(0);
    expect(screen.getByText("Sender, Receiver")).toBeTruthy();
    expect(screen.getByText("second room message")).toBeTruthy();
    expect(screen.queryByText("first room message")).toBeNull();

    fireEvent.click(container.querySelector(".communication-chat-card-header") as HTMLButtonElement);

    expect(screen.getByText("first room message")).toBeTruthy();
    expect(screen.getAllByText("second room message").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("direct message")).toBeTruthy();
  });

  it("reserves row layout space for sender and receiver before preview text", () => {
    const css = readFileSync(resolve(here, "../src/styles/globals.css"), "utf8");

    expect(css).toContain("grid-template-columns: auto minmax(420px, 1.35fr) minmax(220px, 1fr) auto auto;");
    expect(css).toContain("grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);");
    expect(css).toContain(".communication-flow a,\n.communication-flow > span:not(.communication-arrow)");
    expect(css).toContain(".communications-section-chats");
    expect(css).toContain(".communication-chat-card-header");
  });
});
