import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";
import { AmbientMcpSettings } from "../src/components/SettingsPage";

const capability = {
  id: "user:notes",
  name: "Notes",
  launcher: { command: "notes-mcp", args: ["--stdio"], env: {} },
  policy: { native_exposure: true },
  ownership: "user",
  available: true,
  unavailable_reason: null,
};

function response(body: unknown, ok = true) {
  return Promise.resolve({ ok, json: () => Promise.resolve(body) } as Response);
}

describe("ambient MCP settings", () => {
  afterEach(() => vi.restoreAllMocks());

  it("renders backend availability and preserves extension-owned controls", async () => {
    vi.spyOn(globalThis, "fetch").mockReturnValue(response({ capabilities: [
      capability,
      { ...capability, id: "extension:board", name: "Board", ownership: "extension" },
      { ...capability, id: "core:ui", name: "UI", ownership: "better-agent-core", available: false, unavailable_reason: "Session bound" },
    ] }));
    render(<AmbientMcpSettings />);
    expect(await screen.findByText("Session bound")).toBeTruthy();
    expect(screen.getByText("Managed by extension settings below")).toBeTruthy();
    expect(screen.getAllByText("Edit")).toHaveLength(1);
  });

  it("saves strict launcher fields and refreshes confirmed state", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockImplementationOnce(() => response({ capabilities: [] }))
      .mockImplementationOnce((_input, init) => {
        expect(init?.method).toBe("PUT");
        expect(JSON.parse(String(init?.body))).toEqual({
          id: "search", name: "Search", launcher: { command: "search-mcp", args: [], env: {} }, policy: {}, enabled: true,
        });
        return response({ record: {} });
      })
      .mockImplementationOnce(() => response({ capabilities: [{ ...capability, id: "user:search", name: "Search" }] }));
    render(<AmbientMcpSettings />);
    fireEvent.click(await screen.findByRole("button", { name: "Add user MCP" }));
    fireEvent.change(screen.getByLabelText("ID"), { target: { value: "search" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Search" } });
    fireEvent.change(screen.getByLabelText("Command"), { target: { value: "search-mcp" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText("user:search");
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("keeps the editor open and reports rejected mutations", async () => {
    vi.spyOn(globalThis, "fetch")
      .mockImplementationOnce(() => response({ capabilities: [] }))
      .mockImplementationOnce(() => response({ detail: "Command rejected" }, false));
    render(<AmbientMcpSettings />);
    fireEvent.click(await screen.findByRole("button", { name: "Add user MCP" }));
    fireEvent.change(screen.getByLabelText("ID"), { target: { value: "bad" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Bad" } });
    fireEvent.change(screen.getByLabelText("Command"), { target: { value: "bad" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Command rejected");
    await waitFor(() => expect(screen.getByLabelText("ID")).toBeTruthy());
  });
});
