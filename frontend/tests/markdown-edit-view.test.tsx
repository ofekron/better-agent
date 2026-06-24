import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";

const { FileEditor } = await import("../src/components/FileEditor");

afterEach(() => {
  vi.restoreAllMocks();
});

describe("markdown edit view", () => {
  it("keeps FileEditor markdown in edit mode until View is pressed", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ({ content: "# Title" }),
    } as Response);

    const { unmount } = render(
      <FileEditor
        tempFilePath="/tmp/prompt.md"
        originalContent="# Title"
        onSubmitComment={async () => {}}
      />,
    );

    const formatted = await screen.findByTestId("eng-file-md-formatted");
    fireEvent.doubleClick(formatted);

    expect(screen.getByTestId("eng-file-md-monaco")).toBeTruthy();
    expect(screen.getByTestId("eng-file-md-view")).toBeTruthy();

    fireEvent.click(screen.getByTestId("eng-file-md-view"));

    expect(await screen.findByTestId("eng-file-md-formatted")).toBeTruthy();
    expect(screen.queryByTestId("eng-file-md-monaco")).toBeNull();
    unmount();
  });
});
