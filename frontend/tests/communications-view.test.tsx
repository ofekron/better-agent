import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CommunicationsView } from "../src/components/CommunicationsView";

const fetchCommunications = vi.fn();

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

    await waitFor(() => expect(screen.getByText("review · thread-1")).toBeTruthy());
    fireEvent.click(container.querySelector(".communication-chevron") as HTMLButtonElement);
    expect(screen.getByText("sender-session, resolved-worker")).toBeTruthy();
  });
});
