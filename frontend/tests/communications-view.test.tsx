import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import React from "react";
import "../src/i18n";
import { CommunicationsView } from "../src/components/CommunicationsView";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function mockFetch() {
  vi.spyOn(globalThis, "fetch").mockResolvedValue({
    ok: true,
    json: async () => ({
      items: [{
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
      }],
      count: 1,
      total: 1,
    }),
  } as Response);
}

describe("CommunicationsView", () => {
  it("renders communication rows collapsed by default and expands on click", async () => {
    mockFetch();
    render(<CommunicationsView mode="page" />);

    const row = await screen.findByRole("button", { name: /mssg/i });
    expect(screen.queryByText("second line")).toBeNull();

    fireEvent.click(row);

    expect(document.querySelector(".communication-body pre")?.textContent).toContain("second line");
    expect(screen.getByRole("link", { name: "Sender Session · send" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Target Session · targ" })).toBeTruthy();
  });

  it("requests a session-filtered log for panel mode", async () => {
    mockFetch();
    render(<CommunicationsView mode="panel" sessionId="target-session-5678" />);

    await screen.findByText("Communications");
    expect(globalThis.fetch).toHaveBeenCalledWith(
      "/api/communications?limit=100&session_id=target-session-5678",
      { credentials: "include" },
    );
  });
});
