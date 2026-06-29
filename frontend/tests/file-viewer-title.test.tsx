import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";

vi.unmock("../src/components/FileViewer");

const { FileViewer } = await import("../src/components/FileViewer");

afterEach(() => {
  vi.restoreAllMocks();
});

describe("FileViewer title", () => {
  it("shows the full file path in the title line", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ({ content: "", language: "typescript" }),
    } as Response);

    render(
      <FileViewer
        filePath="/tmp/project/src/nested/app.ts"
        onClose={() => {}}
      />,
    );

    expect(screen.getByText("/tmp/project/src/nested/app.ts")).toBeTruthy();
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());
  });

  it("keeps markdown in edit mode until View is pressed", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ({ content: "# Title", language: "markdown" }),
    } as Response);

    render(
      <FileViewer
        filePath="/tmp/project/notes.md"
        onClose={() => {}}
      />,
    );

    const formatted = await screen.findByTestId("file-viewer-md-formatted");
    fireEvent.doubleClick(formatted);

    expect(screen.getByTestId("file-viewer-md-monaco")).toBeTruthy();
    expect(screen.getByTestId("file-viewer-md-view")).toBeTruthy();

    fireEvent.click(screen.getByTestId("file-viewer-md-view"));

    expect(await screen.findByTestId("file-viewer-md-formatted")).toBeTruthy();
    expect(screen.queryByTestId("file-viewer-md-monaco")).toBeNull();
  });

  it("shows when the loaded file changed on disk", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
      const text = String(url);
      if (text.includes("/api/file/metadata")) {
        return {
          ok: true,
          json: async () => ({ path: "/tmp/project/app.ts", mtime_ns: 2, size: 4 }),
        } as Response;
      }
      return {
        ok: true,
        json: async () => ({
          content: "one",
          language: "typescript",
          path: "/tmp/project/app.ts",
          mtime_ns: 1,
          size: 3,
        }),
      } as Response;
    });

    render(
      <FileViewer
        filePath="/tmp/project/app.ts"
        onClose={() => {}}
      />,
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("Changed")).toBeTruthy();
  });

  it("updates a changed file panel to the latest disk content", async () => {
    let fileReadCount = 0;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
      const text = String(url);
      if (text.includes("/api/file/metadata")) {
        return {
          ok: true,
          json: async () => ({ path: "/tmp/project/notes.md", mtime_ns: 2, size: 4 }),
        } as Response;
      }
      fileReadCount += 1;
      return {
        ok: true,
        json: async () => ({
          content: fileReadCount === 1 ? "one" : "two",
          language: "markdown",
          path: "/tmp/project/notes.md",
          mtime_ns: fileReadCount === 1 ? 1 : 2,
          size: fileReadCount === 1 ? 3 : 4,
        }),
      } as Response;
    });

    render(
      <FileViewer
        filePath="/tmp/project/notes.md"
        onClose={() => {}}
      />,
    );

    expect(await screen.findByText("one")).toBeTruthy();
    await screen.findByText("Changed");

    fireEvent.click(screen.getByRole("button", { name: "Update" }));

    expect(await screen.findByText("two")).toBeTruthy();
    await waitFor(() => expect(screen.queryByText("Changed")).toBeNull());
    expect(fileReadCount).toBe(2);
    expect(fetchMock).toHaveBeenCalled();
  });
});
