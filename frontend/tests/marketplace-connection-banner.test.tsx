import fs from "node:fs";
import path from "node:path";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MarketplaceBridgeCenter } from "../src/components/MarketplaceBridgeCenter";
import { eventBus } from "../src/lib/eventBus";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const css = fs.readFileSync(
  path.resolve(__dirname, "../src/styles/globals.css"),
  "utf8",
);

function ruleBody(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = css.match(new RegExp(`${escaped}(?![\\w-])[^{}]*\\{([^}]*)\\}`));
  expect(match, `missing CSS rule for ${selector}`).not.toBeNull();
  return match![1];
}

function zIndexOf(selector: string): number {
  const found = ruleBody(selector).match(/z-index:\s*(\d+)/);
  expect(found, `missing z-index for ${selector}`).not.toBeNull();
  return Number(found![1]);
}

const snapshotWithDevice = {
  revision: 1,
  connection_state: "connected",
  revocation_pending: false,
  paired_devices: [{
    device_id: `badvc_${"1".repeat(32)}`,
    label: "Ofek's Mac",
    public_key: "A".repeat(43),
  }],
  intents: [],
};

function jsonResponse(value: unknown) {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function dismissButtons() {
  return screen.queryAllByRole("button", { name: "userRequest.dismiss" });
}

function refresh() {
  act(() => {
    eventBus.publish("marketplace_bridge_changed", {});
  });
}

describe("marketplace connection-failure banner", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    }));
  });

  it("can be dismissed by the user", async () => {
    render(<MarketplaceBridgeCenter />);

    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    expect(screen.getByText("marketplaceBridge.connectionFailed")).toBeTruthy();
    expect(screen.getByText("Failed to fetch")).toBeTruthy();

    fireEvent.click(dismissButtons()[0]);

    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    expect(screen.queryByText("marketplaceBridge.connectionFailed")).toBeNull();
  });

  it("leaves nothing undismissable behind when a device was paired before the outage", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(snapshotWithDevice));
    render(<MarketplaceBridgeCenter />);
    await waitFor(() => expect(screen.getByText("Ofek's Mac")).toBeTruthy());

    refresh();
    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    // The last-known device is stale while the backend is unreachable; showing
    // it as a live "connected device" would be an untruthful state claim.
    expect(screen.queryByText("Ofek's Mac")).toBeNull();

    fireEvent.click(dismissButtons()[0]);

    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    expect(document.querySelector(".marketplace-bridge-status")).toBeNull();
  });

  it("re-surfaces only when a fresh attempt actually fails again", async () => {
    render(<MarketplaceBridgeCenter />);
    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    fireEvent.click(dismissButtons()[0]);
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());

    refresh();

    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
  });

  it("does not flash the stale error back while a refresh is still in flight", async () => {
    render(<MarketplaceBridgeCenter />);
    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    fireEvent.click(dismissButtons()[0]);
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());

    let settle: (value: Response) => void = () => {};
    vi.mocked(fetch).mockImplementationOnce(() => new Promise((resolve) => {
      settle = resolve;
    }));
    refresh();
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));

    // The request has not resolved: no new error exists, so the dismissed one
    // must stay dismissed rather than reappearing for the request's duration.
    expect(screen.queryByRole("alert")).toBeNull();

    await act(async () => {
      settle(jsonResponse({ ...snapshotWithDevice, paired_devices: [] }));
    });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("lays the banner out on the shared status grid instead of the pulse column", async () => {
    render(<MarketplaceBridgeCenter />);
    const alert = await screen.findByRole("alert");

    // grid-template-columns is `10px minmax(0,1fr) auto`: the title must sit
    // inside the text wrapper, not as a direct child squeezed into the 10px
    // pulse column.
    expect(alert.querySelector(".marketplace-bridge-status__pulse")).not.toBeNull();
    const wrapper = alert.querySelector(":scope > div");
    expect(wrapper).not.toBeNull();
    expect(wrapper!.querySelector("strong")!.textContent)
      .toBe("marketplaceBridge.connectionFailed");
  });

  it("paints the passive status stack below modal overlays", () => {
    // The reported bug: this stack covered the New Session modal.
    expect(zIndexOf(".marketplace-bridge-statuses"))
      .toBeLessThan(zIndexOf(".modal-overlay"));
  });

  it("keeps the blocking approval toast stack above modal overlays", () => {
    // .chat-toast-stack hosts approval / input requests that must stay
    // actionable while a modal is open — it must NOT be demoted alongside the
    // passive marketplace stack.
    expect(zIndexOf(".chat-toast-stack"))
      .toBeGreaterThan(zIndexOf(".modal-overlay"));
  });

  it("gives the dismiss button a real touch target without restyling siblings", () => {
    const button = ruleBody(".marketplace-bridge-status__dismiss");
    expect(button).toContain("min-width: 32px");
    expect(button).toContain("min-height: 32px");
    expect(ruleBody(".marketplace-bridge-status__dismiss:focus-visible"))
      .toContain("outline");
    // A `.marketplace-bridge-status > button` rule outranks the revoke
    // button's own class and would strip its border/background/margin.
    expect(css).not.toMatch(/\.marketplace-bridge-status\s*>\s*button/);
  });

  it("marks every dismiss control with the scoped class", async () => {
    render(<MarketplaceBridgeCenter />);
    const alert = await screen.findByRole("alert");
    expect(alert.querySelector(".marketplace-bridge-status__dismiss")).not.toBeNull();
  });

  it("colors the failed pulse dot with the error background, not text color", () => {
    const failed = ruleBody(".marketplace-bridge-status--failed .marketplace-bridge-status__pulse");
    expect(failed).toContain("background: var(--error)");
    expect(failed).toContain("animation: none");
  });
});
