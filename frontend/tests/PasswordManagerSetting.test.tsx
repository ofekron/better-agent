import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// Keep the slice isolated: extBackendBase returns a stable URL and trackPromise
// is a passthrough so the real progress store (WS extender, retry) doesn't run.
vi.mock("../src/extensionIds", async (importActual) => {
  const actual = await importActual<typeof import("../src/extensionIds")>();
  return { ...actual, extBackendBase: () => "http://test/cb" };
});

vi.mock("../src/progress/store", () => ({
  trackPromise: <T,>(_op: string, fn: () => Promise<T>) => ({ promise: fn() }),
}));

import { PasswordManagerSetting } from "../src/components/PasswordManagerSetting";

type Resp = {
  ok: boolean;
  status: number;
  text: () => Promise<string>;
  json: () => Promise<unknown>;
};

function resp(opts: { ok?: boolean; status?: number; text?: string; json?: unknown } = {}): Resp {
  const ok = opts.ok ?? true;
  return {
    ok,
    status: opts.status ?? (ok ? 200 : 400),
    text: async () => opts.text ?? "",
    json: async () => opts.json ?? {},
  };
}

function deferred<T = unknown>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const LIST_URL = "http://test/cb/settings/password-manager";
const STORE_URL = "http://test/cb/settings/password-manager/store";

interface FetchCall {
  url: string;
  init?: RequestInit;
}

let fetchMock: ReturnType<typeof vi.fn>;

/** vitest records every call on fetchMock.mock.calls regardless of the active
 *  impl — safer than a manual log that a mockImplementation override bypasses. */
function methodCalls(method: string): FetchCall[] {
  return fetchMock.mock.calls
    .map(([url, init]) => ({ url: url as string, init: init as RequestInit | undefined }))
    .filter((c) => c.init?.method === method);
}

beforeEach(() => {
  fetchMock = vi.fn(async () => resp({ json: { items: [] } }));
  globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;
  // happy-dom ships no window.confirm; provide a default-accepting spy.
  window.confirm = vi.fn().mockReturnValue(true);
});

afterEach(() => {
  vi.restoreAllMocks();
  delete (window as { confirm?: unknown }).confirm;
});

/** Inputs are ordered service, account, password inside the form. */
function inputs(container: HTMLElement): HTMLInputElement[] {
  return Array.from(container.querySelectorAll("form input")) as HTMLInputElement[];
}

function setFields(container: HTMLElement, svc: string, acct: string, pw: string): void {
  const [s, a, p] = inputs(container);
  fireEvent.change(s, { target: { value: svc } });
  fireEvent.change(a, { target: { value: acct } });
  fireEvent.change(p, { target: { value: pw } });
}

describe("PasswordManagerSetting — load", () => {
  it("shows a loading state, then renders loaded items", async () => {
    const list = deferred<Resp>();
    fetchMock.mockImplementation(async () => list.promise);
    const { container } = render(<PasswordManagerSetting />);
    await waitFor(() => expect(screen.getByText("settings.passwordManagerLoading")).toBeTruthy());
    list.resolve(resp({ json: { items: [{ service: "svc", account: "acct" }] } }));
    await waitFor(() => expect(screen.getByText("svc")).toBeTruthy());
    expect(screen.getByText("acct")).toBeTruthy();
    // both item action buttons render
    expect(screen.getByText("settings.passwordManagerEdit")).toBeTruthy();
    expect(screen.getByText("settings.passwordManagerDelete")).toBeTruthy();
    // no error/status rendered
    expect(container.querySelector(".password-manager-status")).toBeNull();
    expect(container.querySelector(".setup-error")).toBeNull();
  });

  it("shows the empty state when no items and tolerates a missing items field", async () => {
    fetchMock.mockImplementation(async () => resp({ json: {} })); // items undefined
    const { container } = render(<PasswordManagerSetting />);
    await waitFor(() =>
      expect(screen.getByText("settings.passwordManagerEmpty")).toBeTruthy(),
    );
    expect(container.querySelectorAll(".password-manager-item")).toHaveLength(0);
  });

  it("surfaces a list-load error when the response is not ok", async () => {
    fetchMock.mockImplementation(async () => resp({ ok: false, text: "boom-list" }));
    const { container } = render(<PasswordManagerSetting />);
    await waitFor(() => expect(container.querySelector(".setup-error")!.textContent).toBe("boom-list"));
  });

  it("surfaces a fallback message when fetch rejects with a non-Error", async () => {
    fetchMock.mockImplementation(async () => {
      throw "string failure";
    });
    const { container } = render(<PasswordManagerSetting />);
    await waitFor(() =>
      expect(container.querySelector(".setup-error")!.textContent).toBe(
        "settings.passwordManagerListFailed",
      ),
    );
  });
});

describe("PasswordManagerSetting — save", () => {
  it("ignores submit while fields are incomplete, then saves once complete", async () => {
    let postCalled = false;
    const listItems = { items: [{ service: "svc", account: "acct" }] };
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        postCalled = true;
        return resp({ json: {} });
      }
      return resp({ json: listItems });
    });

    const { container } = render(<PasswordManagerSetting />);
    await waitFor(() => expect(screen.getByText("svc")).toBeTruthy());

    const form = container.querySelector("form")!;
    // Submit with empty fields — save() must bail (canSave false).
    fireEvent.submit(form);
    await waitFor(() => expect(postCalled).toBe(false));

    // Incremental entry exercises each canSave short-circuit.
    const [s, a, p] = inputs(container);
    fireEvent.change(s, { target: { value: "svc2" } }); // service set, account empty
    fireEvent.submit(form);
    await waitFor(() => expect(postCalled).toBe(false));
    fireEvent.change(a, { target: { value: "acct2" } }); // account set, password empty
    fireEvent.submit(form);
    await waitFor(() => expect(postCalled).toBe(false));
    fireEvent.change(p, { target: { value: "pw2" } }); // all set → canSave true

    fireEvent.submit(form);
    await waitFor(() => expect(postCalled).toBe(true));

    // success status rendered, password cleared, body carried the fields
    await waitFor(() =>
      expect(container.querySelector(".password-manager-status")!.textContent).toBe(
        "settings.passwordManagerSaved",
      ),
    );
    expect(p.value).toBe("");
    const postCall = methodCalls("POST")[0]!;
    expect(postCall.url).toBe(STORE_URL);
    expect(JSON.parse(postCall.init!.body as string)).toEqual({
      service: "svc2",
      account: "acct2",
      password: "pw2",
    });
  });

  it("disables the submit button and flips its label while saving", async () => {
    const store = deferred<Resp>();
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") return store.promise;
      return resp({ json: { items: [] } });
    });
    const { container } = render(<PasswordManagerSetting />);
    await waitFor(() => expect(screen.getByText("settings.passwordManagerEmpty")).toBeTruthy());

    setFields(container, "s", "a", "p");
    const submit = screen.getByRole("button", { name: "settings.passwordManagerStore" });
    fireEvent.click(submit);

    await waitFor(() => {
      const btn = screen.getByRole("button", { name: "settings.passwordManagerSaving" });
      expect((btn as HTMLButtonElement).disabled).toBe(true);
    });
    store.resolve(resp({ json: {} }));
    await waitFor(() =>
      expect(container.querySelector(".password-manager-status")!.textContent).toBe(
        "settings.passwordManagerSaved",
      ),
    );
  });

  it("surfaces a save error when the store response is not ok", async () => {
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") return resp({ ok: false, text: "boom-store" });
      return resp({ json: { items: [] } });
    });
    const { container } = render(<PasswordManagerSetting />);
    await waitFor(() => expect(screen.getByText("settings.passwordManagerEmpty")).toBeTruthy());
    setFields(container, "s", "a", "p");
    fireEvent.submit(container.querySelector("form")!);
    await waitFor(() =>
      expect(container.querySelector(".setup-error")!.textContent).toBe("boom-store"),
    );
  });

  it("surfaces a fallback save message when fetch rejects with a non-Error", async () => {
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") throw "nope";
      return resp({ json: { items: [] } });
    });
    const { container } = render(<PasswordManagerSetting />);
    await waitFor(() => expect(screen.getByText("settings.passwordManagerEmpty")).toBeTruthy());
    setFields(container, "s", "a", "p");
    fireEvent.submit(container.querySelector("form")!);
    await waitFor(() =>
      expect(container.querySelector(".setup-error")!.textContent).toBe(
        "settings.passwordManagerFailed",
      ),
    );
  });
});

describe("PasswordManagerSetting — edit & delete", () => {
  it("edit populates the form fields and clears the password", async () => {
    fetchMock.mockImplementation(async () =>
      resp({ json: { items: [{ service: "svc", account: "acct" }] } }),
    );
    const { container } = render(<PasswordManagerSetting />);
    await waitFor(() => expect(screen.getByText("svc")).toBeTruthy());
    const [s, a, p] = inputs(container);
    expect(s.value).toBe("");
    fireEvent.click(screen.getByText("settings.passwordManagerEdit"));
    expect(s.value).toBe("svc");
    expect(a.value).toBe("acct");
    expect(p.value).toBe("");
    await waitFor(() =>
      expect(container.querySelector(".password-manager-status")!.textContent).toBe(
        "settings.passwordManagerEditing",
      ),
    );
  });

  it("aborts delete when the user cancels the confirm dialog", async () => {
    vi.mocked(window.confirm).mockReturnValue(false);
    fetchMock.mockImplementation(async () =>
      resp({ json: { items: [{ service: "svc", account: "acct" }] } }),
    );
    render(<PasswordManagerSetting />);
    await waitFor(() => expect(screen.getByText("svc")).toBeTruthy());
    fireEvent.click(screen.getByText("settings.passwordManagerDelete"));
    await waitFor(() => expect(window.confirm).toHaveBeenCalled());
    expect(methodCalls("DELETE")).toHaveLength(0);
  });

  it("clears the form when deleting the item currently being edited", async () => {
    fetchMock.mockImplementation(async () =>
      resp({ json: { items: [{ service: "svc", account: "acct" }] } }),
    );
    const { container } = render(<PasswordManagerSetting />);
    await waitFor(() => expect(screen.getByText("svc")).toBeTruthy());
    // Load the item into the form, then delete it — matching clear path.
    fireEvent.click(screen.getByText("settings.passwordManagerEdit"));
    const [s, a] = inputs(container);
    expect(s.value).toBe("svc");
    fireEvent.click(screen.getByText("settings.passwordManagerDelete"));
    await waitFor(() =>
      expect(container.querySelector(".password-manager-status")!.textContent).toBe(
        "settings.passwordManagerDeleted",
      ),
    );
    expect(s.value).toBe("");
    expect(a.value).toBe("");
    const del = methodCalls("DELETE")[0]!;
    expect(del.url).toBe(LIST_URL);
    expect(JSON.parse(del.init!.body as string)).toEqual({ service: "svc", account: "acct" });
  });

  it("does not clear unrelated form fields when deleting a different item", async () => {
    fetchMock.mockImplementation(async () =>
      resp({ json: { items: [{ service: "svc", account: "acct" }] } }),
    );
    const { container } = render(<PasswordManagerSetting />);
    await waitFor(() => expect(screen.getByText("svc")).toBeTruthy());
    // Form holds a different service/account than the listed item.
    setFields(container, "other", "other-acct", "pw");
    fireEvent.click(screen.getByText("settings.passwordManagerDelete"));
    await waitFor(() =>
      expect(container.querySelector(".password-manager-status")!.textContent).toBe(
        "settings.passwordManagerDeleted",
      ),
    );
    const [s, a] = inputs(container);
    expect(s.value).toBe("other");
    expect(a.value).toBe("other-acct");
  });

  it("disables the delete button and flips its label while deleting", async () => {
    const del = deferred<Resp>();
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "DELETE") return del.promise;
      return resp({ json: { items: [{ service: "svc", account: "acct" }] } });
    });
    render(<PasswordManagerSetting />);
    await waitFor(() => expect(screen.getByText("svc")).toBeTruthy());
    fireEvent.click(screen.getByText("settings.passwordManagerDelete"));
    const btn = await screen.findByRole("button", { name: "settings.passwordManagerDeleting" });
    expect((btn as HTMLButtonElement).disabled).toBe(true);
    del.resolve(resp({ json: {} }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "settings.passwordManagerDelete" })).toBeTruthy(),
    );
  });

  it("surfaces a delete error when the delete response is not ok", async () => {
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "DELETE") return resp({ ok: false, text: "boom-del" });
      return resp({ json: { items: [{ service: "svc", account: "acct" }] } });
    });
    const { container } = render(<PasswordManagerSetting />);
    await waitFor(() => expect(screen.getByText("svc")).toBeTruthy());
    fireEvent.click(screen.getByText("settings.passwordManagerDelete"));
    await waitFor(() =>
      expect(container.querySelector(".setup-error")!.textContent).toBe("boom-del"),
    );
  });

  it("surfaces a fallback delete message when fetch rejects with a non-Error", async () => {
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "DELETE") throw "nope";
      return resp({ json: { items: [{ service: "svc", account: "acct" }] } });
    });
    const { container } = render(<PasswordManagerSetting />);
    await waitFor(() => expect(screen.getByText("svc")).toBeTruthy());
    fireEvent.click(screen.getByText("settings.passwordManagerDelete"));
    await waitFor(() =>
      expect(container.querySelector(".setup-error")!.textContent).toBe(
        "settings.passwordManagerDeleteFailed",
      ),
    );
  });
});
