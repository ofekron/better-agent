import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "../src/i18n";
import { InputArea } from "../src/components/InputArea";
import type { Project, Session } from "../src/types";

function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    path: "/Users/test/project-alpha",
    name: "project-alpha",
    node_id: "primary",
    created_at: "2026-01-01T00:00:00",
    last_used: "2026-01-01T00:00:00",
    ...overrides,
  };
}

function makeSession(overrides: Partial<Session> = {}): Session {
  return {
    id: "s1",
    name: "Research thread",
    model: "claude",
    cwd: "/Users/test/research",
    messages: [],
    created_at: "2026-01-01T00:00:00",
    updated_at: "2026-01-01T00:00:00",
    ...overrides,
  };
}

// Stand up the canvas/blob/image plumbing fileToPastedImage relies on so a
// pasted/attached raster resolves into a PastedImage preview.
beforeEach(() => {
  vi.spyOn(HTMLCanvasElement.prototype, "toDataURL").mockReturnValue("data:image/jpeg;base64,QUJD");
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
    drawImage: () => {},
  } as unknown as CanvasRenderingContext2D);
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:fake");
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
  Object.defineProperty(HTMLImageElement.prototype, "src", {
    configurable: true,
    set() {
      this.onload?.(new Event("load"));
    },
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

function clipboardData(text: string, file: File): DataTransfer {
  return {
    getData: (type: string) => (type === "text/plain" ? text : ""),
    items: [{ kind: "file", type: file.type, getAsFile: () => file }],
  } as unknown as DataTransfer;
}

function renderInput() {
  return render(
    <InputArea
      onSend={vi.fn()}
      isStreaming={false}
      disabled={false}
      draft=""
      onDraftChange={vi.fn()}
      onPromoteQueued={vi.fn()}
      queuedPrompt={null}
      projects={[makeProject()]}
      sessions={[makeSession()]}
    />,
  );
}

describe("InputArea attachments + input handlers", () => {
  it("adds a pasted image to the composer and removes it via the preview", async () => {
    renderInput();
    const textarea = screen.getByTestId("input-textarea");
    const image = new File([new Uint8Array([1, 2, 3])], "snap.png", { type: "image/png" });

    fireEvent.paste(textarea, { clipboardData: clipboardData("", image) });

    const removeBtn = await screen.findByTitle("Remove image");
    fireEvent.click(removeBtn);

    await waitFor(() => {
      expect(screen.queryByTitle("Remove image")).toBeNull();
    });
  });

  it("attaches an image and a non-image file via the file input, then removes each", async () => {
    const { container } = renderInput();
    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
    const image = new File([new Uint8Array([1, 2])], "pic.jpg", { type: "image/jpeg" });
    const doc = new File([new Uint8Array([9, 9, 9])], "notes.txt", { type: "text/plain" });

    fireEvent.change(fileInput, { target: { files: [image, doc] } });

    // Non-image file renders into .file-previews with its own remove button.
    const fileRemove = await screen.findByText("notes.txt");
    expect(fileRemove).toBeTruthy();

    // Two "Remove image" affordances now exist (image preview + file preview).
    const removes = await screen.findAllByTitle("Remove image");
    expect(removes.length).toBeGreaterThanOrEqual(2);

    // Remove the file attachment via its button inside .file-previews.
    const filePaneRemoveBtn = container.querySelector(".file-previews button");
    fireEvent.click(filePaneRemoveBtn as HTMLElement);
    await waitFor(() => expect(screen.queryByText("notes.txt")).toBeNull());

    // Remove the image preview.
    fireEvent.click(screen.getByTitle("Remove image"));
    await waitFor(() => expect(screen.queryByTitle("Remove image")).toBeNull());
  });

  it("mirrors textarea scroll onto the highlight layer", () => {
    render(
      <InputArea
        onSend={vi.fn()}
        isStreaming={false}
        disabled={false}
        draft="some draft text"
        onDraftChange={vi.fn()}
        onPromoteQueued={vi.fn()}
        queuedPrompt={null}
        projects={[makeProject()]}
        sessions={[makeSession()]}
      />,
    );
    const textarea = screen.getByTestId("input-textarea") as HTMLTextAreaElement;
    const highlight = screen.getByTestId("input-mention-highlight");

    textarea.scrollTop = 60;
    fireEvent.scroll(textarea);

    expect(highlight.scrollTop).toBe(textarea.scrollTop);
  });

  it("opens the @-mention dropdown when typing a trigger", () => {
    const { container } = renderInput();
    const textarea = screen.getByTestId("input-textarea");
    expect(container.querySelector(".at-mention-dropdown")).toBeNull();

    fireEvent.change(textarea, { target: { value: "see @pro", selectionStart: 8, selectionEnd: 8 } });

    expect(container.querySelector(".at-mention-dropdown")).toBeTruthy();
  });
});
