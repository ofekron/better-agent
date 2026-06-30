import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";

vi.mock("../src/hooks/useViewport", () => ({
  useViewport: () => ({ mode: "desktop" }),
}));

vi.mock("../src/components/FileViewer", () => ({
  FileViewer: ({ filePath }: { filePath: string }) => (
    <div data-testid="mock-file-viewer">source:{filePath}</div>
  ),
}));

const { FilePanels, htmlPreviewSrcDoc } = await import("../src/components/FilePanels");

afterEach(() => {
  vi.restoreAllMocks();
});

describe("FilePanels browser rendering", () => {
  it("lets an HTML file panel switch from source to browser preview", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ({
        content: "<html><head><link href='./styles.css'></head><body><h1>Hi</h1><img src='img/logo.png'></body></html>",
      }),
    } as Response);

    render(
      <FilePanels
        panels={[{ id: "p1", path: "/repo/marketing/singular/index.html" }]}
        onClosePanel={() => {}}
        registerEditor={() => {}}
      />,
    );

    expect(screen.getByTestId("mock-file-viewer").textContent).toContain("source:/repo/marketing/singular/index.html");

    fireEvent.click(screen.getByRole("button", { name: "Browser" }));

    const preview = await screen.findByTestId("file-browser-preview");
    expect(preview).toBeTruthy();
    await waitFor(() => {
      const iframe = preview.querySelector("iframe");
      expect(iframe).toBeTruthy();
      expect(iframe?.getAttribute("sandbox")).toBe("allow-same-origin allow-popups allow-downloads");
      expect(iframe?.getAttribute("srcdoc") || iframe?.getAttribute("srcDoc") || "").toContain(
        "/api/file/raw?path=%2Frepo%2Fmarketing%2Fsingular%2Fstyles.css",
      );
    });

    fireEvent.click(screen.getByRole("button", { name: "Source" }));

    expect(screen.getByTestId("mock-file-viewer")).toBeTruthy();
  });

  it("does not show browser toggle for non-HTML file panels", () => {
    render(
      <FilePanels
        panels={[{ id: "p1", path: "/repo/src/App.tsx" }]}
        onClosePanel={() => {}}
        registerEditor={() => {}}
      />,
    );

    expect(screen.queryByRole("button", { name: "Browser" })).toBeNull();
    expect(screen.getByTestId("mock-file-viewer")).toBeTruthy();
  });

  it("rewrites local html assets through the authenticated file endpoint", () => {
    const srcDoc = htmlPreviewSrcDoc(
      "<html><head><link href='./styles.css'></head><body><img src='shots/a.png'><a href='#slide-2'>jump</a><a href='https://example.com'>x</a></body></html>",
      "/repo/marketing/singular/index.html",
      "primary",
    );

    expect(srcDoc).toContain("/api/file/raw?path=%2Frepo%2Fmarketing%2Fsingular%2Fstyles.css");
    expect(srcDoc).toContain("/api/file/raw?path=%2Frepo%2Fmarketing%2Fsingular%2Fshots%2Fa.png");
    expect(srcDoc).toContain('href="#slide-2"');
    expect(srcDoc).not.toContain('href="#slide-2" target="_blank"');
    expect(srcDoc).toContain('href="https://example.com"');
    expect(srcDoc).toContain('target="_blank"');
  });
});
