import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

// --- hoisted control plane --------------------------------------------------

const native = vi.hoisted(() => ({
  normalizeServerUrl: vi.fn((v: string) => v),
  writeNativeServerUrl: vi.fn(),
}));
const candidates = vi.hoisted(() => ({
  list: ["https://host.example", "http://1.2.3.4:18765"] as string[],
}));
const fetchFn = vi.hoisted(() => ({ current: vi.fn() }));

vi.mock("react-i18next", () => {
  // Return the key verbatim; the component only renders these strings, it does
  // not branch on their interpolated content.
  const t = (k: string) => k;
  return { useTranslation: () => ({ t }) };
});

vi.mock("../src/nativeServerConfig", () => ({
  DEFAULT_BACKEND_PORT: "18765",
  normalizeServerUrl: (v: string) => native.normalizeServerUrl(v),
  writeNativeServerUrl: (v: string) => native.writeNativeServerUrl(v),
}));

vi.mock("../src/serverCandidates.generated", () => ({
  get SERVER_CANDIDATES() {
    return candidates.list;
  },
}));

vi.mock("../src/components/OpenRecoveryAppButton", () => ({
  OpenRecoveryAppButton: () => <div data-testid="recovery" />,
}));

import { ServerSetup } from "../src/components/ServerSetup";

// --- fetch / AbortSignal.timeout stubs --------------------------------------

const realFetch = globalThis.fetch;

beforeEach(() => {
  native.normalizeServerUrl.mockImplementation((v: string) => v);
  native.normalizeServerUrl.mockClear();
  native.writeNativeServerUrl.mockClear();
  fetchFn.current.mockReset();
  globalThis.fetch = ((...args: unknown[]) => fetchFn.current(...args)) as unknown as typeof fetch;
  // happy-dom has no AbortSignal.timeout; the component only needs a signal
  // object to pass through to fetch, which we stub anyway.
  if (!AbortSignal.timeout) {
    (AbortSignal as unknown as { timeout: unknown }).timeout = () =>
      ({ aborted: false }) as AbortSignal;
  }
});

afterEach(() => {
  globalThis.fetch = realFetch;
});

function renderSetup(props: { onConfigured?: () => void; initialUrl?: string } = {}) {
  const onConfigured = props.onConfigured ?? vi.fn();
  const utils = render(<ServerSetup onConfigured={onConfigured} initialUrl={props.initialUrl} />);
  return { ...utils, onConfigured };
}

// --- initial prefill ---------------------------------------------------------

describe("ServerSetup — initial prefill", () => {
  it("pre-fills with the top server candidate when no initialUrl is given", () => {
    candidates.list = ["https://top.example", "http://1.2.3.4:18765"];
    renderSetup();
    expect((screen.getByDisplayValue("https://top.example") as HTMLInputElement).value).toBe(
      "https://top.example",
    );
  });

  it("pre-fills with initialUrl when provided", () => {
    candidates.list = ["https://top.example"];
    renderSetup({ initialUrl: "http://custom:9999" });
    expect((screen.getByDisplayValue("http://custom:9999") as HTMLInputElement).value).toBe(
      "http://custom:9999",
    );
  });

  it("falls back to an empty value when there are no candidates and no initialUrl", () => {
    candidates.list = [];
    renderSetup();
    expect((screen.getByPlaceholderText("192.168.1.100") as HTMLInputElement).value).toBe("");
  });
});

// --- alternates chips --------------------------------------------------------

describe("ServerSetup — alternates chips", () => {
  it("renders a chip for each non-selected candidate with the scheme stripped", () => {
    candidates.list = ["https://selected.example", "http://1.2.3.4:18765", "https://other.example"];
    const { container } = renderSetup();
    const chips = container.querySelectorAll(".server-setup-chip");
    expect(chips.length).toBe(2);
    expect(chips[0].textContent).toBe("1.2.3.4:18765");
    expect(chips[1].textContent).toBe("other.example");
  });

  it("hides the chips block when there are no alternates", () => {
    candidates.list = ["https://only.example"];
    const { container } = renderSetup();
    expect(container.querySelector(".server-setup-chips")).toBeNull();
  });

  it("clicking a chip sets the url and clears any error", async () => {
    candidates.list = ["https://first.example", "http://second.example"];
    const { container } = renderSetup();
    // Force an error first.
    native.normalizeServerUrl.mockImplementation(() => {
      throw new Error("required");
    });
    await act(async () => {
      fireEvent.click(screen.getByPlaceholderText("192.168.1.100")?.closest("form")!.querySelector("button.login-submit")!);
    });
    expect(container.querySelector(".login-error")?.textContent).toBe("serverSetup.urlRequired");
    // Now click a chip — error clears and url updates.
    native.normalizeServerUrl.mockImplementation((v: string) => v);
    await act(async () => {
      fireEvent.click(container.querySelector(".server-setup-chip")!);
    });
    expect(container.querySelector(".login-error")).toBeNull();
    expect((screen.getByDisplayValue("http://second.example") as HTMLInputElement).value).toBe(
      "http://second.example",
    );
  });
});

// --- validation errors -------------------------------------------------------

describe("ServerSetup — validation", () => {
  it("shows the required error when normalize throws 'required'", async () => {
    candidates.list = ["https://top.example"];
    native.normalizeServerUrl.mockImplementation(() => {
      throw new Error("required");
    });
    const { container } = renderSetup();
    await act(async () => {
      fireEvent.submit(container.querySelector("form")!);
    });
    expect(container.querySelector(".login-error")?.textContent).toBe("serverSetup.urlRequired");
    expect(fetchFn.current).not.toHaveBeenCalled();
  });

  it("shows the invalid error for any other Error", async () => {
    candidates.list = ["https://top.example"];
    native.normalizeServerUrl.mockImplementation(() => {
      throw new Error("bad");
    });
    const { container } = renderSetup();
    await act(async () => {
      fireEvent.submit(container.querySelector("form")!);
    });
    expect(container.querySelector(".login-error")?.textContent).toBe("serverSetup.urlInvalid");
  });

  it("shows the invalid error for a non-Error throw", async () => {
    candidates.list = ["https://top.example"];
    native.normalizeServerUrl.mockImplementation(() => {
      throw "string-thrown";
    });
    const { container } = renderSetup();
    await act(async () => {
      fireEvent.submit(container.querySelector("form")!);
    });
    expect(container.querySelector(".login-error")?.textContent).toBe("serverSetup.urlInvalid");
  });
});

// --- connectivity test + save -----------------------------------------------

describe("ServerSetup — connectivity & save", () => {
  it("tests connectivity, writes the url, and calls onConfigured on success", async () => {
    candidates.list = ["https://top.example"];
    native.normalizeServerUrl.mockImplementation((v: string) => `clean:${v}`);
    fetchFn.current.mockResolvedValue({ ok: true, status: 200 });
    const onConfigured = vi.fn();
    const { container } = renderSetup({ onConfigured });
    await act(async () => {
      fireEvent.submit(container.querySelector("form")!);
    });
    await waitFor(() => expect(onConfigured).toHaveBeenCalled());
    expect(fetchFn.current).toHaveBeenCalledWith(
      "clean:https://top.example/api/auth/needs_setup",
      expect.objectContaining({ signal: expect.anything() }),
    );
    expect(native.writeNativeServerUrl).toHaveBeenCalledWith("clean:https://top.example");
  });

  it("shows a connection-failed error when the response is not ok", async () => {
    candidates.list = ["https://top.example"];
    native.normalizeServerUrl.mockImplementation((v: string) => v);
    fetchFn.current.mockResolvedValue({ ok: false, status: 500 });
    const { container } = renderSetup();
    await act(async () => {
      fireEvent.submit(container.querySelector("form")!);
    });
    await waitFor(() =>
      expect(container.querySelector(".login-error")?.textContent).toBe("serverSetup.connectionFailed"),
    );
    expect(native.writeNativeServerUrl).not.toHaveBeenCalled();
  });

  it("shows a connection-failed error when fetch rejects", async () => {
    candidates.list = ["https://top.example"];
    native.normalizeServerUrl.mockImplementation((v: string) => v);
    fetchFn.current.mockRejectedValue(new Error("network down"));
    const { container } = renderSetup();
    await act(async () => {
      fireEvent.submit(container.querySelector("form")!);
    });
    await waitFor(() =>
      expect(container.querySelector(".login-error")?.textContent).toBe("serverSetup.connectionFailed"),
    );
  });

  it("shows the testing label and disables submit while the request is in flight", async () => {
    candidates.list = ["https://top.example"];
    native.normalizeServerUrl.mockImplementation((v: string) => v);
    let resolveFetch!: (v: unknown) => void;
    fetchFn.current.mockImplementation(() => new Promise((r) => (resolveFetch = r)));
    const { container } = renderSetup();
    await act(async () => {
      fireEvent.submit(container.querySelector("form")!);
    });
    const submit = container.querySelector("button.login-submit") as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    expect(submit.textContent).toBe("serverSetup.testing");
    await act(async () => {
      resolveFetch({ ok: true, status: 200 });
      await new Promise((r) => setTimeout(r, 0));
    });
    await waitFor(() => expect((container.querySelector("button.login-submit") as HTMLButtonElement).disabled).toBe(false));
  });
});

// --- input change -----------------------------------------------------------

describe("ServerSetup — input change", () => {
  it("updates the url and clears the error on change", async () => {
    candidates.list = ["https://top.example"];
    const { container } = renderSetup();
    native.normalizeServerUrl.mockImplementation(() => {
      throw new Error("required");
    });
    await act(async () => {
      fireEvent.submit(container.querySelector("form")!);
    });
    expect(container.querySelector(".login-error")).toBeTruthy();
    native.normalizeServerUrl.mockImplementation((v: string) => v);
    const input = screen.getByPlaceholderText("192.168.1.100") as HTMLInputElement;
    await act(async () => {
      fireEvent.change(input, { target: { value: "http://edited.example" } });
    });
    expect(input.value).toBe("http://edited.example");
    expect(container.querySelector(".login-error")).toBeNull();
  });
});
