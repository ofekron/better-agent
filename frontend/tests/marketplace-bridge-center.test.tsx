import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MarketplaceBridgeCenter } from "../src/components/MarketplaceBridgeCenter";
import { eventBus } from "../src/lib/eventBus";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const emptySnapshot = {
  revision: 0,
  connection_state: "unpaired",
  revocation_pending: false,
  paired_devices: [],
  intents: [],
};
const pairedDevice = {
  device_id: `badvc_${"1".repeat(32)}`,
  label: "Ofek's Mac",
  public_key: "A".repeat(43),
};
const pairIntentId = `pair_${"1".repeat(32)}`;
const installIntentId = `baact_${"2".repeat(32)}`;
const updateIntentId = `baact_${"3".repeat(32)}`;

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
      ...emptySnapshot,
      revision: 1,
      connection_state: "connecting",
      intents: [{
        intent_id: pairIntentId,
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
      ...emptySnapshot,
      revision: 1,
      connection_state: "connected",
      intents: [{
        intent_id: installIntentId,
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
      ...awaiting,
      revision: 2,
      intents: [{ ...awaiting.intents[0], status: "succeeded" }],
    };
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const path = String(input);
      if (path.endsWith(`/${installIntentId}/approve`)) {
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

  it("requires confirmation before revoking the paired Marketplace device", async () => {
    const paired = {
      ...emptySnapshot,
      revision: 1,
      connection_state: "connected",
      paired_devices: [pairedDevice],
    };
    vi.mocked(fetch).mockResolvedValue(jsonResponse(paired));

    render(<MarketplaceBridgeCenter />);
    await waitFor(() => expect(screen.getByText("Ofek's Mac")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: "marketplaceBridge.revoke" }));
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(
      vi.mocked(fetch).mock.calls.every(([, init]) => init?.method !== "DELETE"),
    ).toBe(true);
  });

  it("revokes through the exact device endpoint and keeps remote cleanup visible", async () => {
    const paired = {
      ...emptySnapshot,
      revision: 1,
      connection_state: "connected",
      paired_devices: [pairedDevice],
    };
    const revoking = {
      ...emptySnapshot,
      revision: 2,
      revocation_pending: true,
    };
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      if (init?.method === "DELETE") return jsonResponse(revoking);
      return jsonResponse(paired);
    });

    render(<MarketplaceBridgeCenter />);
    await waitFor(() => expect(screen.getByText("Ofek's Mac")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "marketplaceBridge.revoke" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole(
      "button",
      { name: "marketplaceBridge.revoke" },
    ));

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringMatching(
        new RegExp(`/api/marketplace-bridge/devices/${pairedDevice.device_id}$`),
      ),
      { method: "DELETE" },
    ));
    expect(screen.getByText("marketplaceBridge.revokingTitle")).toBeTruthy();
    expect(screen.queryByText("Ofek's Mac")).toBeNull();
  });

  it("retains the authoritative device and allows closing after a malformed revoke response", async () => {
    const paired = {
      ...emptySnapshot,
      revision: 1,
      connection_state: "connected",
      paired_devices: [pairedDevice],
    };
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse(paired))
      .mockResolvedValueOnce(jsonResponse({}))
      .mockResolvedValueOnce(jsonResponse(paired));

    render(<MarketplaceBridgeCenter />);
    await waitFor(() => expect(screen.getByText("Ofek's Mac")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "marketplaceBridge.revoke" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole(
      "button",
      { name: "marketplaceBridge.revoke" },
    ));

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3));
    expect(screen.getByRole("alert").textContent).toContain("invalid marketplace snapshot");
    expect(screen.getAllByText("Ofek's Mac").length).toBeGreaterThan(0);
    for (const button of within(screen.getByRole("dialog")).getAllByRole(
      "button",
      { name: "app.cancel" },
    )) {
      expect(button).toHaveProperty("disabled", false);
    }
  });

  it("never lets a delayed revoke response overwrite a newer WS snapshot", async () => {
    let resolveRevoke!: (response: Response) => void;
    const paired = {
      ...emptySnapshot,
      revision: 1,
      connection_state: "connected",
      paired_devices: [pairedDevice],
    };
    const revoking = {
      ...emptySnapshot,
      revision: 2,
      revocation_pending: true,
    };
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse(paired))
      .mockImplementationOnce(() => new Promise((resolve) => {
        resolveRevoke = resolve;
      }))
      .mockResolvedValueOnce(jsonResponse(revoking));

    render(<MarketplaceBridgeCenter />);
    await waitFor(() => expect(screen.getByText("Ofek's Mac")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "marketplaceBridge.revoke" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole(
      "button",
      { name: "marketplaceBridge.revoke" },
    ));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));

    act(() => {
      eventBus.publish("marketplace_bridge_changed", { revision: 2 });
    });
    await waitFor(() => expect(screen.getByText("marketplaceBridge.revokingTitle")).toBeTruthy());

    await act(async () => {
      resolveRevoke(jsonResponse(paired));
    });
    expect(screen.getByText("marketplaceBridge.revokingTitle")).toBeTruthy();
    expect(screen.queryByText("Ofek's Mac")).toBeNull();
  });

  it.each([
    [
      "reserved characters in the device id",
      [{ ...pairedDevice, device_id: "badvc_%2F" }],
    ],
    [
      "more devices than the backend can own",
      [pairedDevice, { ...pairedDevice, device_id: `badvc_${"2".repeat(32)}` }],
    ],
    [
      "a non-base64url public key",
      [{ ...pairedDevice, public_key: "/".repeat(43) }],
    ],
    [
      "a label over 500 Unicode code points",
      [{ ...pairedDevice, label: "😀".repeat(501) }],
    ],
  ])("rejects %s before rendering device controls", async (_label, paired_devices) => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({
      ...emptySnapshot,
      revision: 1,
      connection_state: "connected",
      paired_devices,
    }));

    render(<MarketplaceBridgeCenter />);

    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    expect(screen.queryByRole("button", { name: "marketplaceBridge.revoke" })).toBeNull();
  });

  it("counts device labels by Unicode code point like the backend", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({
      ...emptySnapshot,
      revision: 1,
      connection_state: "connected",
      paired_devices: [{ ...pairedDevice, label: "😀".repeat(500) }],
    }));

    render(<MarketplaceBridgeCenter />);

    await waitFor(() => expect(
      screen.getByRole("button", { name: "marketplaceBridge.revoke" }),
    ).toBeTruthy());
  });

  it("accepts a redeemed pair alongside its authoritative paired device", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({
      ...emptySnapshot,
      revision: 2,
      connection_state: "connected",
      paired_devices: [pairedDevice],
      intents: [{
        intent_id: pairIntentId,
        action: "pair",
        status: "redeemed",
        device_label: pairedDevice.label,
        site_label: "Singular Marketplace",
        account_label: "ofek@example.test",
      }],
    }));

    render(<MarketplaceBridgeCenter />);

    await waitFor(() => expect(
      screen.getByText("marketplaceBridge.status.redeemed"),
    ).toBeTruthy());
    expect(screen.getByRole("button", { name: "marketplaceBridge.revoke" })).toBeTruthy();
  });

  it.each(["pending", "expired"])(
    "accepts a %s pair projection before confirmation labels exist",
    async (status) => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({
        ...emptySnapshot,
        revision: 1,
        connection_state: status === "pending" ? "connecting" : "unpaired",
        intents: [{
          intent_id: pairIntentId,
          action: "pair",
          status,
          device_label: pairedDevice.label,
        }],
      }));

      render(<MarketplaceBridgeCenter />);

      await waitFor(() => expect(
        screen.getByText(`marketplaceBridge.status.${status}`),
      ).toBeTruthy());
      expect(screen.queryByRole("alert")).toBeNull();
    },
  );

  it("requires all pair labels once confirmation is requested", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({
      ...emptySnapshot,
      revision: 1,
      connection_state: "connecting",
      intents: [{
        intent_id: pairIntentId,
        action: "pair",
        status: "awaiting_confirmation",
        device_label: pairedDevice.label,
      }],
    }));

    render(<MarketplaceBridgeCenter />);

    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it.each([
    ["pair", "succeeded"],
    ["install", "redeemed"],
  ])("rejects the %s action with cross-wired %s state", async (action, status) => {
    const intent_id = action === "pair" ? pairIntentId : installIntentId;
    vi.mocked(fetch).mockResolvedValue(jsonResponse({
      ...emptySnapshot,
      revision: 1,
      connection_state: "connected",
      intents: [{ intent_id, action, status }],
    }));

    render(<MarketplaceBridgeCenter />);

    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    expect(screen.queryByText(`marketplaceBridge.status.${status}`)).toBeNull();
  });

  it("explains a failed refresh inside an open revoke confirmation", async () => {
    const paired = {
      ...emptySnapshot,
      revision: 1,
      connection_state: "connected",
      paired_devices: [pairedDevice],
    };
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse(paired))
      .mockRejectedValueOnce(new Error("offline"));

    render(<MarketplaceBridgeCenter />);
    await waitFor(() => expect(screen.getByText("Ofek's Mac")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "marketplaceBridge.revoke" }));

    act(() => {
      eventBus.publish("marketplace_bridge_changed", { revision: 2 });
    });

    const dialog = screen.getByRole("dialog");
    await waitFor(() => expect(within(dialog).getByRole("alert").textContent).toBe("offline"));
    expect(within(dialog).getByRole("button", { name: "marketplaceBridge.revoke" }))
      .toHaveProperty("disabled", true);
    for (const button of within(dialog).getAllByRole("button", { name: "app.cancel" })) {
      expect(button).toHaveProperty("disabled", false);
    }
  });

  it("keeps nonterminal work visible and only dismisses terminal results", async () => {
    const running = {
      ...emptySnapshot,
      revision: 1,
      connection_state: "connected",
      intents: [{
        intent_id: updateIntentId,
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
      ...emptySnapshot,
      revision: 1,
      connection_state: "connected",
      intents: [{
        intent_id: installIntentId,
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
      ...emptySnapshot,
      revision: 1,
      connection_state: "connected",
      intents: [{
        intent_id: installIntentId,
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
        ...emptySnapshot,
        revision: 2,
        connection_state: "connected",
        intents: [{
          intent_id: updateIntentId,
          action: "update",
          status: "succeeded",
          extension: { id: "ofek-dev.adv", name: "Adversarial Review" },
        }],
      }));
    });
    await waitFor(() => expect(screen.getByText("marketplaceBridge.status.succeeded")).toBeTruthy());

    await act(async () => {
      resolveOlder(jsonResponse({
        ...emptySnapshot,
        revision: 1,
        connection_state: "connecting",
      }));
    });
    expect(screen.getByText("marketplaceBridge.status.succeeded")).toBeTruthy();
  });
});
