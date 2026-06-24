import { describe, expect, it, vi } from "vitest";
import { markFirstRunWizardSeen } from "../src/lib/firstRunWizard";

describe("first-run wizard", () => {
  it("persists that the wizard was shown without requiring a finish click", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ first_run_wizard_done: true }),
    } as Response);

    await markFirstRunWizardSeen();

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/user-prefs",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ first_run_wizard_done: true }),
      }),
    );
  });
});
