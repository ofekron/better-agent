import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MarketplaceBridgeCenter } from "../src/components/MarketplaceBridgeCenter";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const emptySnapshot = {
  connection_state: "unpaired",
  intents: [],
};

function jsonResponse(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("MarketplaceBridgeCenter", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(emptySnapshot)));
  });

  it("refreshes the backend-owned pair confirmation after a desktop activation", async () => {
    const awaiting = {
      connection_state: "connecting",
      intents: [{
        intent_id: "intent-1",
        action: "pair",
        status: "awaiting_confirmation",
        site_label: "Singular Marketplace",
        account_label: "ofek@example.test",
        device_label: "Ofek's Mac",
      }],
    };
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse(emptySnapshot))
      .mockResolvedValue(jsonResponse(awaiting));

    render(<MarketplaceBridgeCenter />);
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    act(() => {
      window.dispatchEvent(new CustomEvent("better-agent:deep-link"));
    });

    await waitFor(() => expect(screen.getByRole("dialog")).toBeTruthy());
    expect(screen.getByText("Singular Marketplace")).toBeTruthy();
    expect(screen.getByText("ofek@example.test")).toBeTruthy();
    expect(vi.mocked(fetch).mock.calls.every(([, init]) => init?.method !== "POST")).toBe(true);
  });

  it("approves only through the intent-bound endpoint and reflects backend result", async () => {
    const awaiting = {
      connection_state: "connected",
      intents: [{
        intent_id: "intent-install",
        action: "install",
        status: "awaiting_confirmation",
        extension: {
          id: "ofek-dev.adv",
          name: "Adversarial Review",
          version: "1.2.3",
          publisher: "Singular Labs",
          permission_delta: ["filesystem"],
        },
      }],
    };
    const succeeded = {
      connection_state: "connected",
      intents: [{ ...awaiting.intents[0], status: "succeeded" }],
    };
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const path = String(input);
      if (path.endsWith("/intent-install/approve")) {
        expect(init?.method).toBe("POST");
        return jsonResponse(succeeded);
      }
      return jsonResponse(awaiting);
    });

    render(<MarketplaceBridgeCenter />);
    await waitFor(() => expect(screen.getByText("Adversarial Review")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "marketplaceBridge.approve" }));

    await waitFor(() => expect(screen.getByText("marketplaceBridge.status.succeeded")).toBeTruthy());
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("keeps nonterminal work visible and only dismisses terminal results", async () => {
    const running = {
      connection_state: "connected",
      intents: [{
        intent_id: "intent-update",
        action: "update",
        status: "committing",
        extension: { id: "ofek-dev.adv", name: "Adversarial Review" },
      }],
    };
    vi.mocked(fetch).mockResolvedValue(jsonResponse(running));
    render(<MarketplaceBridgeCenter />);

    await waitFor(() => expect(screen.getByText("marketplaceBridge.status.committing")).toBeTruthy());
    expect(screen.queryByRole("button", { name: "userRequest.dismiss" })).toBeNull();
  });

  it("rejects malformed action projections before rendering controls", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({
      connection_state: "connected",
      intents: [{
        intent_id: "intent-install",
        action: "install",
        status: "awaiting_confirmation",
        extension: { id: "", name: "Untrusted extension" },
      }],
    }));

    render(<MarketplaceBridgeCenter />);

    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByText("marketplaceBridge.connectionFailed")).toBeTruthy();
  });

  it("disables confirmation after a refresh makes backend state stale", async () => {
    const awaiting = {
      connection_state: "connected",
      intents: [{
        intent_id: "intent-install",
        action: "install",
        status: "awaiting_confirmation",
        extension: { id: "ofek-dev.adv", name: "Adversarial Review" },
      }],
    };
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse(awaiting))
      .mockRejectedValueOnce(new Error("offline"));

    render(<MarketplaceBridgeCenter />);
    await waitFor(() => expect(screen.getByRole("dialog")).toBeTruthy());

    act(() => {
      window.dispatchEvent(new CustomEvent("better-agent:deep-link"));
    });

    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    expect(screen.getByRole("button", { name: "marketplaceBridge.approve" }))
      .toHaveProperty("disabled", true);
    for (const button of screen.getAllByRole("button", { name: "marketplaceBridge.reject" })) {
      expect(button).toHaveProperty("disabled", true);
    }
  });

  it("never lets an older refresh overwrite a newer backend snapshot", async () => {
    let resolveOlder!: (response: Response) => void;
    let resolveNewer!: (response: Response) => void;
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse(emptySnapshot))
      .mockImplementationOnce(() => new Promise((resolve) => {
        resolveOlder = resolve;
      }))
      .mockImplementationOnce(() => new Promise((resolve) => {
        resolveNewer = resolve;
      }));

    render(<MarketplaceBridgeCenter />);
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    act(() => {
      window.dispatchEvent(new CustomEvent("better-agent:deep-link"));
      window.dispatchEvent(new CustomEvent("better-agent:deep-link"));
    });
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3));

    await act(async () => {
      resolveNewer(jsonResponse({
        connection_state: "connected",
        intents: [{
          intent_id: "intent-update",
          action: "update",
          status: "succeeded",
          extension: { id: "ofek-dev.adv", name: "Adversarial Review" },
        }],
      }));
    });
    await waitFor(() => expect(screen.getByText("marketplaceBridge.status.succeeded")).toBeTruthy());

    await act(async () => {
      resolveOlder(jsonResponse({
        connection_state: "connecting",
        intents: [],
      }));
    });
    expect(screen.getByText("marketplaceBridge.status.succeeded")).toBeTruthy();
  });
});
