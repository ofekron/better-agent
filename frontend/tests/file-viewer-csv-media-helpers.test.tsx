import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";

// FileViewer is globally null-mocked by setup.ts; unmount the mock and stub
// @monaco-editor/react so the module loads under happy-dom. The csv/tsv/pdf/
// video paths under test never mount Monaco, but the top-level import still
// resolves it.
vi.unmock("../src/components/FileViewer");
vi.doMock("@monaco-editor/react", () => ({
  default: ({ value, onChange }: { value?: string; onChange?: (v: string) => void }) => (
    <textarea
      data-testid="mock-editor"
      value={value ?? ""}
      onChange={(e) => onChange?.(e.currentTarget.value)}
    />
  ),
  Editor: ({ value, onChange }: { value?: string; onChange?: (v: string) => void }) => (
    <textarea
      data-testid="mock-editor"
      value={value ?? ""}
      onChange={(e) => onChange?.(e.currentTarget.value)}
    />
  ),
  DiffEditor: ({ original, modified }: { original?: string; modified?: string }) => (
    <div data-testid="mock-diff-editor">
      <span data-testid="mock-diff-original">{original}</span>
      <span data-testid="mock-diff-modified">{modified}</span>
    </div>
  ),
}));

const {
  FileViewer,
  identitiesDiffer,
  fetchFileIdentity,
  fetchTextFile,
} = await import("../src/components/FileViewer");

function jsonResponse(body: unknown): Response {
  return { ok: true, json: async () => body } as Response;
}

// Route a single fetch spy by URL suffix. Returns the spy so a test can assert
// call args. Re-running it replaces the implementation wholesale, so set every
// route a test needs in one call.
function routeFetch(routes: { suffix: string; body: unknown }[]): ReturnType<typeof vi.spyOn> {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = typeof input === "string" ? input : (input as URL).toString();
    for (const r of routes) {
      if (url.includes(r.suffix)) return jsonResponse(r.body);
    }
    return jsonResponse({});
  });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("identitiesDiffer", () => {
  it("returns false when either identity is missing", () => {
    const id = { mtime_ns: 1, size: 2 };
    expect(identitiesDiffer(null, null)).toBe(false);
    expect(identitiesDiffer(null, id)).toBe(false);
    expect(identitiesDiffer(id, null)).toBe(false);
  });

  it("returns false when mtime and size match", () => {
    expect(identitiesDiffer({ mtime_ns: 1, size: 2 }, { mtime_ns: 1, size: 2 })).toBe(false);
  });

  it("returns true when mtime or size differs", () => {
    expect(identitiesDiffer({ mtime_ns: 1, size: 2 }, { mtime_ns: 9, size: 2 })).toBe(true);
    expect(identitiesDiffer({ mtime_ns: 1, size: 2 }, { mtime_ns: 1, size: 9 })).toBe(true);
  });
});

describe("fetchFileIdentity", () => {
  it("builds the metadata URL with encoded path + node_id and parses a full payload", async () => {
    const spy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ mtime_ns: 7, size: 42 }));

    const id = await fetchFileIdentity("a b/c.ts", "node-1");

    expect(id).toEqual({ mtime_ns: 7, size: 42 });
    const url = String(spy.mock.calls[0][0]);
    expect(url).toContain("/api/file/metadata");
    expect(url).toContain("path=a%20b%2Fc.ts");
    expect(url).toContain("node_id=node-1");
  });

  it("returns null for payloads missing mtime_ns or size", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ mtime_ns: 7 }));
    expect(await fetchFileIdentity("x", "primary")).toBeNull();
  });

  it("returns null for non-object payloads", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse(null));
    expect(await fetchFileIdentity("x", "primary")).toBeNull();

    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse("nope"));
    expect(await fetchFileIdentity("x", "primary")).toBeNull();
  });
});

describe("fetchTextFile", () => {
  it("returns content/language/identity from the response", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ content: "hello", language: "typescript", mtime_ns: 3, size: 5 }),
    );

    const loaded = await fetchTextFile("dir/a.ts", "primary", "op-1");
    expect(loaded).toEqual({
      path: "dir/a.ts",
      content: "hello",
      language: "typescript",
      identity: { mtime_ns: 3, size: 5 },
    });
  });

  it("falls back to empty content + plaintext and null identity", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({}));

    const loaded = await fetchTextFile("dir/a.ts", "primary", "op-2");
    expect(loaded.content).toBe("");
    expect(loaded.language).toBe("plaintext");
    expect(loaded.identity).toBeNull();
  });
});

describe("FileViewer csv/tsv rendering (CsvTable)", () => {
  it("renders a parsed csv as a header + body table", async () => {
    routeFetch([
      { suffix: "/api/file/draft", body: { exists: false } },
      { suffix: "/api/file", body: { content: "h1,h2\na,b\nc,d", language: "" } },
    ]);

    const { container } = render(<FileViewer filePath="/p/data.csv" onClose={() => {}} />);

    await waitFor(() => {
      expect(container.querySelector("table")).toBeTruthy();
    });
    const headers = Array.from(container.querySelectorAll("thead th")).map((e) => e.textContent);
    expect(headers).toEqual(["h1", "h2"]);
    const firstRow = Array.from(container.querySelectorAll("tbody tr:first-child td")).map(
      (e) => e.textContent,
    );
    expect(firstRow).toEqual(["a", "b"]);
    expect(container.querySelector(".file-viewer-kind")?.textContent).toBe("csv");
  });

  it("renders a tsv using tab delimiters", async () => {
    routeFetch([
      { suffix: "/api/file/draft", body: { exists: false } },
      { suffix: "/api/file", body: { content: "k1\tk2\nv1\tv2", language: "" } },
    ]);

    const { container } = render(<FileViewer filePath="/p/data.tsv" onClose={() => {}} />);

    await waitFor(() => {
      expect(container.querySelectorAll("tbody tr:first-child td").length).toBe(2);
    });
    const cells = Array.from(container.querySelectorAll("tbody tr:first-child td")).map(
      (e) => e.textContent,
    );
    expect(cells).toEqual(["v1", "v2"]);
  });

  it("shows a truncation notice when the csv exceeds the row cap", async () => {
    const rows = Array.from({ length: 2001 }, (_, i) => `r${i},x`).join("\n");
    routeFetch([
      { suffix: "/api/file/draft", body: { exists: false } },
      { suffix: "/api/file", body: { content: rows, language: "" } },
    ]);

    const { container } = render(<FileViewer filePath="/p/big.csv" onClose={() => {}} />);

    await waitFor(() => {
      expect(container.querySelector(".file-viewer-truncated")).toBeTruthy();
    });
    // Capped at MAX_ROWS (2000): header + 1999 body rows.
    expect(container.querySelectorAll("tbody tr").length).toBe(1999);
  });
});

describe("FileViewer media kinds (pdf / video)", () => {
  it("renders a pdf iframe pointing at the raw endpoint and reads metadata", async () => {
    const spy = routeFetch([{ suffix: "/api/file/metadata", body: { mtime_ns: 1, size: 2 } }]);

    const { container } = render(<FileViewer filePath="/p/doc.pdf" onClose={() => {}} />);

    await waitFor(() => {
      expect(container.querySelector("iframe")).toBeTruthy();
    });
    const src = container.querySelector("iframe")?.getAttribute("src") ?? "";
    expect(src).toContain("/api/file/raw");
    expect(src).toContain("path=%2Fp%2Fdoc.pdf");
    expect(src).toContain("node_id=primary");
    // Media load branch fetches metadata exactly for identity.
    expect(
      spy.mock.calls.some((c) => String(c[0]).includes("/api/file/metadata")),
    ).toBe(true);
    expect(container.querySelector(".file-viewer-kind")?.textContent).toBe("pdf");
  });

  it("renders a video player for a video extension", async () => {
    routeFetch([{ suffix: "/api/file/metadata", body: { mtime_ns: 1, size: 2 } }]);

    const { container } = render(<FileViewer filePath="/p/clip.mp4" onClose={() => {}} />);

    await waitFor(() => {
      expect(container.querySelector("video")).toBeTruthy();
    });
    const src = container.querySelector("video")?.getAttribute("src") ?? "";
    expect(src).toContain("/api/file/raw");
    expect(container.querySelector(".file-viewer-kind")?.textContent).toBe("video");
  });
});

describe("FileViewer json kind", () => {
  it("routes a json-language file to the code editor via the json branch", async () => {
    routeFetch([
      { suffix: "/api/file/draft", body: { exists: false } },
      { suffix: "/api/file", body: { content: "{}", language: "json", mtime_ns: 1, size: 2 } },
    ]);

    const { container, getByTestId } = render(
      <FileViewer filePath="/p/cfg.json" onClose={() => {}} />,
    );

    await waitFor(() => {
      expect(getByTestId("mock-editor")).toBeTruthy();
    });
    expect(getByTestId("mock-editor").textContent).toBe("{}");
    expect(container.querySelector(".file-viewer-kind")?.textContent).toBe("json");
  });
});

describe("FileViewer with no file path", () => {
  it("renders nothing and skips the load effect", () => {
    const { container } = render(<FileViewer filePath={null} onClose={() => {}} />);
    expect(container.querySelector(".file-viewer")).toBeNull();
  });
});

describe("FileViewer diff mode", () => {
  it("renders a side-by-side diff from diffBefore/diffAfter", async () => {
    vi.spyOn(globalThis, "fetch");

    const { getByTestId } = render(
      <FileViewer
        filePath="/p/a.ts"
        diffBefore="old"
        diffAfter="new"
        onClose={() => {}}
      />,
    );

    await waitFor(() => {
      expect(getByTestId("mock-diff-original").textContent).toBe("old");
      expect(getByTestId("mock-diff-modified").textContent).toBe("new");
    });
  });
});
