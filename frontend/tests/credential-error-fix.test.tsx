import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";
import { CredentialErrorFix } from "../src/components/MessageBubble";

function response(body: unknown) {
  return Promise.resolve({
    ok: true,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(""),
  } as Response);
}

const META = {
  kind: "provider_credential",
  provider_id: "prov-1",
  credential_status: "blocked",
};

describe("in-chat credential error fix", () => {
  afterEach(() => vi.restoreAllMocks());

  it("renders the credential notice with a fix action", () => {
    render(<CredentialErrorFix meta={META} />);
    expect(screen.getByTestId("credential-error-fix")).toBeTruthy();
    expect(screen.getByText(/API key couldn't be accessed/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Fix credential access" })).toBeTruthy();
  });

  it("fires the provider retry endpoint and reports still-blocked truthfully", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      expect(String(input)).toContain("/api/providers/prov-1/credential/retry");
      return response({ credential_status: "blocked", has_api_key: false });
    });
    render(<CredentialErrorFix meta={META} />);
    fireEvent.click(screen.getByRole("button", { name: "Fix credential access" }));
    await waitFor(() => expect(screen.getByText(/still blocked/i)).toBeTruthy());
    expect(fetchMock).toHaveBeenCalledTimes(1);
    // The fix action stays available after a still-blocked result.
    expect(screen.getByRole("button", { name: "Fix credential access" })).toBeTruthy();
  });

  it("reports restored access after an available result", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(() =>
      response({ credential_status: "available", has_api_key: true }),
    );
    render(<CredentialErrorFix meta={META} />);
    fireEvent.click(screen.getByRole("button", { name: "Fix credential access" }));
    await waitFor(() =>
      expect(screen.getByText(/Credential access restored/)).toBeTruthy(),
    );
    expect(screen.queryByRole("button", { name: "Fix credential access" })).toBeNull();
  });
});
