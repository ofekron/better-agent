// @vitest-environment happy-dom

import { act, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

beforeEach(() => {
  vi.resetModules();
  vi.stubGlobal("crypto", { randomUUID: vi.fn(() => "indicator-correlation") });
});

it("shows a compact pending count until acknowledgement", async () => {
  await import("../../src/i18n");
  const { beginThreeStateSync } = await import("../../src/progress/store");
  const { SyncStatusIndicator } = await import("../../src/components/SyncStatusIndicator");
  render(<SyncStatusIndicator />);

  let confirm = () => {};
  act(() => {
    confirm = beginThreeStateSync<never>({
      operationId: "settings:save:1",
      action: "Save settings",
      reconcile: vi.fn(),
    }).confirmAcknowledgement;
  });

  expect(screen.getByRole("status").textContent).toContain("Saving changes (1)");
  act(() => confirm());
  expect(screen.queryByRole("status")).toBeNull();
});
