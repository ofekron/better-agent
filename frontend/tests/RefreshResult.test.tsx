import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { RefreshResult } from "../src/components/RefreshResult";
import "../src/i18n";

const STORAGE_KEY = "bc_refresh_context";

function seedContext(requestId: string) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({
    requestId,
    previousHash: "same-hash",
    refreshTime: Date.now(),
  }));
}

function mockBuildInfo(refreshResult: {
  request_id: string;
  status: "succeeded" | "failed";
  completed_at: string;
  error: string | null;
}) {
  globalThis.fetch = vi.fn(async () => ({
    ok: true,
    json: async () => ({
      git_hash: "same-hash",
      refresh_result: refreshResult,
    }),
  })) as unknown as typeof fetch;
}

beforeEach(() => {
  vi.stubGlobal("__BUILD_HASH__", "same-hash");
  vi.stubGlobal("__BUILD_TIME__", "2026-06-06T12:00:00Z");
});

describe("RefreshResult", () => {
  it("distinguishes a successful build with an unchanged Git hash", async () => {
    seedContext("refresh-ok");
    mockBuildInfo({
      request_id: "refresh-ok",
      status: "succeeded",
      completed_at: "2026-06-06T12:00:01Z",
      error: null,
    });

    render(<RefreshResult />);

    expect(await screen.findByText(
      "Refresh succeeded (Git version same-hash unchanged)",
    )).toBeTruthy();
    fireEvent.click(screen.getByText("Details"));
    expect(screen.getByText("Build succeeded")).toBeTruthy();
    expect(screen.getByText(/local uncommitted changes may still be included/)).toBeTruthy();
  });

  it("shows an explicit failure and the retained-build error details", async () => {
    seedContext("refresh-failed");
    mockBuildInfo({
      request_id: "refresh-failed",
      status: "failed",
      completed_at: "2026-06-06T12:00:01Z",
      error: "TypeScript compilation failed",
    });

    render(<RefreshResult />);

    expect(await screen.findByText(
      "Backend restarted; frontend build failed — using previous build (same-hash)",
    )).toBeTruthy();
    fireEvent.click(screen.getByText("Details"));
    await waitFor(() => {
      expect(screen.getByText("Backend restarted; previous frontend restored")).toBeTruthy();
      expect(screen.getByText("TypeScript compilation failed")).toBeTruthy();
    });
  });
});
