import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import React from "react";
import { FileEditorOverlay } from "../src/components/FileEditorOverlay";

describe("FileEditorOverlay layout", () => {
  it("puts the file editor before chat", () => {
    const { container } = render(
      <FileEditorOverlay
        state={{
          sessionId: "fe-layout",
          filePaths: ["/tmp/a.md"],
          originalContents: {},
        }}
        persistent
        onDone={async () => {}}
        onCancel={async () => {}}
        chatSlot={<div data-testid="chat-slot" />}
        fileViewerSlot={<div data-testid="file-slot" />}
      />,
    );

    const body = container.querySelector(".prompt-eng-body");
    const children = Array.from(body?.children ?? []);

    expect(children[0]?.querySelector('[data-testid="file-slot"]')).not.toBeNull();
    expect(children[2]?.querySelector('[data-testid="chat-slot"]')).not.toBeNull();
  });
});
