import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import React from "react";
import { FileEditor } from "../src/components/FileEditor";

describe("FileEditor disk diff controls", () => {
  const filePath = "/tmp/example.md";
  let fileContent = "";
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fileContent = "after\n";
    fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (method === "GET" && url.includes("/api/file")) {
        return jsonResponse({ content: fileContent, language: "markdown" });
      }
      if (method === "POST" && url.includes("/api/file")) {
        const body = JSON.parse(String(init?.body ?? "{}")) as {
          path?: string;
          content?: string;
        };
        if (body.path === filePath) fileContent = body.content ?? "";
        return jsonResponse({ ok: true });
      }
      return jsonResponse({}, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  it("shows changed, accept, revert, and a baseline diff when disk content differs", async () => {
    renderEditor();

    expect(await screen.findByTestId("eng-accept-diff")).toBeTruthy();
    expect(screen.getByTestId("eng-revert-diff")).toBeTruthy();
    expect(document.querySelector(".file-viewer-stale")?.textContent).toBe("engFile.changed");

    fireEvent.click(screen.getByTestId("eng-view-diff"));

    expect(document.querySelector(".file-viewer-diff-badge")?.textContent).toBe(
      "engFile.baselineToCurrent",
    );
  });

  it("accepting disk changes clears the diff controls without writing", async () => {
    renderEditor();
    fireEvent.click(await screen.findByTestId("eng-accept-diff"));

    await waitFor(() => {
      expect(screen.queryByTestId("eng-accept-diff")).toBeNull();
    });
    expect(screen.queryByTestId("eng-revert-diff")).toBeNull();
    expect(fetchMock).not.toHaveBeenCalledWith(
      expect.stringContaining("/api/file"),
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("reverting writes the original baseline back to disk and clears the controls", async () => {
    renderEditor();
    fireEvent.click(await screen.findByTestId("eng-revert-diff"));

    await waitFor(() => {
      expect(fileContent).toBe("before\n");
    });
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/file"),
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ path: filePath, content: "before\n" }),
      }),
    );
    await waitFor(() => {
      expect(screen.queryByTestId("eng-revert-diff")).toBeNull();
    });
  });

  function renderEditor() {
    return render(
      <FileEditor
        tempFilePath={filePath}
        originalContent={"before\n"}
        onSubmitComment={async () => {}}
        diskWritable={false}
      />,
    );
  }
});

function jsonResponse(data: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => data,
  } as Response;
}
