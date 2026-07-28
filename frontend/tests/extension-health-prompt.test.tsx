import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import "../src/i18n";
import {
  ExtensionHealthPrompt,
  type ActiveHealthDecision,
} from "../src/components/ExtensionHealthPrompt";
import { useExtensionHealthDecision } from "../src/hooks/useExtensionHealthDecision";
import { eventBus } from "../src/lib/eventBus";

const decision: ActiveHealthDecision = {
  extensionId: "ofek-dev.flaky",
  extensionName: "Flaky",
  pending: {
    id: "dec-1",
    reason: "Timed out 3× in 10 minutes.",
    at: 1,
    elapsed_seconds: 42,
    cohort: [
      { extension_id: "ofek-dev.flaky", name: "Flaky" },
      { extension_id: "ofek-dev.dependent", name: "Dependent" },
    ],
  },
};

function renderPrompt(submit: (action: "disable" | "keep_enabled") => Promise<boolean>) {
  return render(<ExtensionHealthPrompt decision={decision} onSubmit={submit} />);
}

describe("ExtensionHealthPrompt", () => {
  it("renders the extension name, reason, and cohort", () => {
    renderPrompt(() => Promise.resolve(true));
    expect(screen.getByTestId("extension-health-prompt")).toBeTruthy();
    expect(screen.getByText(/Extension needs attention: Flaky/)).toBeTruthy();
    expect(screen.getByText(/Timed out 3× in 10 minutes\./)).toBeTruthy();
    expect(screen.getByText(/Flaky, Dependent/)).toBeTruthy();
  });

  it("shows in-progress state on the chosen action while submitting", async () => {
    let resolveSubmit!: (ok: boolean) => void;
    const submit = vi.fn(
      () => new Promise<boolean>((resolve) => { resolveSubmit = resolve; }),
    );
    renderPrompt(submit);

    await act(async () => {
      fireEvent.click(screen.getByTestId("extension-health-prompt").querySelector('[data-action="disable"]')!);
    });
    expect(submit).toHaveBeenCalledWith("disable");
    // Spinner state: the chosen button shows the working label and both actions disable.
    expect(screen.getByText("Working…")).toBeTruthy();
    expect(
      (screen.getByTestId("extension-health-prompt").querySelector('[data-action="keep-enabled"]') as HTMLButtonElement).disabled,
    ).toBe(true);

    await act(async () => { resolveSubmit(true); });
  });

  it("stays visible with an error when the backend rejects the decision", async () => {
    const submit = vi.fn(() => Promise.resolve(false));
    renderPrompt(submit);

    await act(async () => {
      fireEvent.click(screen.getByTestId("extension-health-prompt").querySelector('[data-action="keep-enabled"]')!);
    });
    expect(submit).toHaveBeenCalledWith("keep_enabled");

    // Failure keeps the prompt mounted and surfaces a retriable error; the
    // actions become actionable again so the user can retry.
    expect(await screen.findByText(/Couldn't submit your decision/i)).toBeTruthy();
    expect(
      (screen.getByTestId("extension-health-prompt").querySelector('[data-action="disable"]') as HTMLButtonElement).disabled,
    ).toBe(false);
  });
});

function HealthProbe() {
  const { decision } = useExtensionHealthDecision();
  return <div data-testid="health-probe">{decision ? decision.pending.id : "none"}</div>;
}

describe("useExtensionHealthDecision (extension.catalog push)", () => {
  it("surfaces a pending decision, then clears it when extension.catalog fires after the backend clears it", async () => {
    const pendingPayload = {
      extensions: [
        {
          manifest: { id: "ofek-dev.flaky", name: "Flaky" },
          pending_health_decision: {
            id: "dec-1", reason: "timed out", at: 1, elapsed_seconds: 1, cohort: [],
          },
        },
      ],
    };
    let cleared = false;
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async () => ({
      ok: true,
      json: async () => (cleared ? { extensions: [] } : pendingPayload),
    }) as unknown as Response);
    vi.stubGlobal("fetch", fetchMock);

    const { unmount } = render(<HealthProbe />);
    // Initial fetch surfaces the pending decision.
    expect(await screen.findByText("dec-1")).toBeTruthy();

    // Backend clears the decision; the catalog push triggers a refetch that
    // removes the prompt — no user action or polling required.
    cleared = true;
    await act(async () => {
      eventBus.publish("extension.catalog", {});
    });
    expect(await screen.findByText("none")).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/extensions"),
      expect.objectContaining({ credentials: "include" }),
    );

    unmount();
    vi.unstubAllGlobals();
  });
});
