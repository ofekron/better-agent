import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "../src/i18n";
import { InputArea } from "../src/components/InputArea";

afterEach(cleanup);

describe("composer image preview", () => {
  it("opens attached images in the shared modal and supports keyboard navigation", async () => {
    render(
      <InputArea
        onSend={vi.fn()}
        isStreaming={false}
        disabled={false}
        draft=""
        onDraftChange={vi.fn()}
        queuedPrompt={null}
        onPromoteQueued={vi.fn()}
        draftImages={[
          { dataUrl: "data:image/png;base64,QUJD", base64: "QUJD", mediaType: "image/png" },
          { dataUrl: "data:image/png;base64,REVG", base64: "REVG", mediaType: "image/png" },
        ]}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Attached image 1" }));

    const dialog = screen.getByRole("dialog", { name: "Attached image 1" });
    expect(dialog.querySelector(".image-lightbox-img")?.getAttribute("src"))
      .toBe("data:image/png;base64,QUJD");

    fireEvent.keyDown(window, { key: "ArrowRight" });
    expect(screen.getByRole("dialog", { name: "Attached image 2" })).toBeTruthy();

    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
});
