import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";

vi.mock("../src/hooks/useViewport", () => ({
  useViewport: () => ({ mode: "desktop" }),
}));

vi.mock("../src/components/FileViewer", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/components/FileViewer")>();
  return {
    ...actual,
    FileViewer: ({ filePath }: { filePath: string }) => (
      <div data-testid="mock-file-viewer">source:{filePath}</div>
    ),
  };
});

const { FilePanels } = await import("../src/components/FilePanels");

const SIGNED_URL = "/api/file/preview/1234.abcd/primary/repo/marketing/singular/index.html";

function mockPreviewUrlFetch(url = SIGNED_URL) {
  return vi.spyOn(globalThis, "fetch").mockResolvedValue({
    ok: true,
    json: async () => ({ url }),
  } as Response);
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("FilePanels browser rendering", () => {
  it("focuses a reopened existing panel when backend order moves it last", async () => {
    const { rerender } = render(
      <FilePanels
        panels={[
          { id: "p1", path: "/repo/src/App.tsx" },
          { id: "p2", path: "/repo/src/other.ts" },
        ]}
        onClosePanel={() => {}}
        registerEditor={() => {}}
      />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("mock-file-viewer").textContent).toContain("/repo/src/other.ts");
    });
    fireEvent.click(screen.getByText("App.tsx"));
    expect(screen.getByTestId("mock-file-viewer").textContent).toContain("/repo/src/App.tsx");

    rerender(
      <FilePanels
        panels={[
          { id: "p1", path: "/repo/src/App.tsx" },
          { id: "p2", path: "/repo/src/other.ts" },
        ]}
        onClosePanel={() => {}}
        registerEditor={() => {}}
      />,
    );
    expect(screen.getByTestId("mock-file-viewer").textContent).toContain("/repo/src/App.tsx");
    fireEvent.click(screen.getByText("other.ts"));
    expect(screen.getByTestId("mock-file-viewer").textContent).toContain("/repo/src/other.ts");

    rerender(
      <FilePanels
        panels={[
          { id: "p2", path: "/repo/src/other.ts" },
          { id: "p1", path: "/repo/src/App.tsx" },
        ]}
        onClosePanel={() => {}}
        registerEditor={() => {}}
      />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("mock-file-viewer").textContent).toContain("/repo/src/App.tsx");
    });
  });

  it("keeps the active panel when an inactive last panel closes", async () => {
    const { rerender } = render(
      <FilePanels
        panels={[
          { id: "p1", path: "/repo/src/App.tsx" },
          { id: "p2", path: "/repo/src/other.ts" },
          { id: "p3", path: "/repo/src/closed.ts" },
        ]}
        onClosePanel={() => {}}
        registerEditor={() => {}}
      />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("mock-file-viewer").textContent).toContain("/repo/src/closed.ts");
    });
    fireEvent.click(screen.getByText("App.tsx"));
    expect(screen.getByTestId("mock-file-viewer").textContent).toContain("/repo/src/App.tsx");

    rerender(
      <FilePanels
        panels={[
          { id: "p1", path: "/repo/src/App.tsx" },
          { id: "p2", path: "/repo/src/other.ts" },
        ]}
        onClosePanel={() => {}}
        registerEditor={() => {}}
      />,
    );

    expect(screen.getByTestId("mock-file-viewer").textContent).toContain("/repo/src/App.tsx");
  });


  it("lets an HTML file panel switch from source to a signed browser preview", async () => {
    const fetchSpy = mockPreviewUrlFetch();

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
    expect(String(fetchSpy.mock.calls[0]?.[0])).toContain(
      "/api/file/preview-url?path=%2Frepo%2Fmarketing%2Fsingular%2Findex.html&node_id=primary",
    );
    await waitFor(() => {
      const iframe = preview.querySelector("iframe");
      expect(iframe).toBeTruthy();
      // Scripts run inside an opaque origin: allow-scripts WITHOUT
      // allow-same-origin, so the page can execute but cannot reach
      // Better Agent's origin.
      expect(iframe?.getAttribute("sandbox")).toBe(
        "allow-scripts allow-popups allow-downloads allow-forms allow-modals",
      );
      expect(iframe?.getAttribute("src") || "").toContain(SIGNED_URL);
    });

    fireEvent.click(screen.getByRole("button", { name: "Source" }));

    expect(screen.getByTestId("mock-file-viewer")).toBeTruthy();
  });

  it("shows a failure state when the preview URL cannot be minted", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: false,
      status: 403,
      json: async () => ({}),
    } as Response);

    render(
      <FilePanels
        panels={[{ id: "p1", path: "/repo/deck/slides.html" }]}
        onClosePanel={() => {}}
        registerEditor={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Browser" }));

    await screen.findByText("Could not render this HTML file.");
    expect(screen.getByTestId("file-browser-preview").querySelector("iframe")).toBeNull();
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
    expect(screen.queryByRole("button", { name: "Open in tab" })).toBeNull();
    expect(screen.getByTestId("mock-file-viewer")).toBeTruthy();
  });

  it("opens the signed preview URL in a new browser tab", async () => {
    mockPreviewUrlFetch();
    const fakeWin = { location: { href: "" }, opener: {}, close: vi.fn() };
    const openSpy = vi
      .spyOn(window, "open")
      .mockReturnValue(fakeWin as unknown as Window);

    render(
      <FilePanels
        panels={[{ id: "p1", path: "/repo/marketing/singular/index.html" }]}
        onClosePanel={() => {}}
        registerEditor={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Open in tab" }));

    expect(openSpy).toHaveBeenCalledWith("about:blank", "_blank");
    await waitFor(() => {
      expect(fakeWin.location.href).toContain(SIGNED_URL);
      expect(fakeWin.opener).toBeNull();
    });
  });
});
