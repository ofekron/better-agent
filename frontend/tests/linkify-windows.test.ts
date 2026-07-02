import { describe, it, expect } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import {
  compactLinkLabel,
  isAbsolutePath,
  linkifyFilePaths,
  parseMarkdownFileHref,
  sessionLinkMarker,
  sessionMarkersToMarkdown,
} from "../src/utils/linkifyFilePaths";

// Regression lock for the Windows file-ref bug: handleOpenFilePanel
// (App.tsx) only joined cwd when `!path.startsWith("/")`, so a Windows
// absolute path like `C:\proj\app.py` was wrongly joined onto cwd.
// isAbsolutePath is the extracted decision used there; it must mirror
// the backend file_ref_resolver `_is_absolute`.
describe("isAbsolutePath (Windows file-ref resolution)", () => {
  it("treats POSIX, Windows-drive and UNC paths as absolute", () => {
    expect(isAbsolutePath("/Users/x/app.py")).toBe(true);
    expect(isAbsolutePath("C:\\proj\\app.py")).toBe(true);
    expect(isAbsolutePath("C:/proj/app.py")).toBe(true);
    expect(isAbsolutePath("\\\\server\\share\\x.py")).toBe(true);
    expect(isAbsolutePath("\\x.py")).toBe(true);
  });

  it("treats relative and drive-relative paths as NOT absolute", () => {
    expect(isAbsolutePath("src/app.py")).toBe(false);
    expect(isAbsolutePath("app.py")).toBe(false);
    expect(isAbsolutePath("C:app.py")).toBe(false); // drive-relative, no separator
  });

  it("resolution does not cwd-join a Windows absolute path", () => {
    const cwd = "C:\\proj";
    const path = "C:\\proj\\src\\app.py";
    const resolved =
      isAbsolutePath(path) || !cwd ? path : `${cwd.replace(/\/$/, "")}/${path}`;
    expect(resolved).toBe(path); // not `C:\proj/C:\proj\src\app.py`
  });
});

describe("parseMarkdownFileHref", () => {
  it("treats absolute markdown hrefs as file links", () => {
    expect(parseMarkdownFileHref("/workspace/testape/tests/np_e2e/flows/facebook_login.py")).toEqual({
      path: "/workspace/testape/tests/np_e2e/flows/facebook_login.py",
    });
  });

  it("treats relative markdown hrefs as file links", () => {
    expect(parseMarkdownFileHref("tests/np_e2e/flows/facebook_login.py")).toEqual({
      path: "tests/np_e2e/flows/facebook_login.py",
    });
  });

  it("extracts line focus from markdown hrefs", () => {
    expect(parseMarkdownFileHref("/workspace/testape/tests/np_e2e/flows/facebook_login.py:12")).toEqual({
      path: "/workspace/testape/tests/np_e2e/flows/facebook_login.py",
      focus: { startLine: 12, endLine: 12 },
    });
    expect(parseMarkdownFileHref("tests/np_e2e/flows/facebook_login.py:12-14")).toEqual({
      path: "tests/np_e2e/flows/facebook_login.py",
      focus: { startLine: 12, endLine: 14 },
    });
  });

  it("leaves external and in-page links alone", () => {
    expect(parseMarkdownFileHref("https://example.com/file.py")).toBeNull();
    expect(parseMarkdownFileHref("mailto:user@example.com")).toBeNull();
    expect(parseMarkdownFileHref("#section")).toBeNull();
    expect(parseMarkdownFileHref("/sessions/current")).toBeNull();
  });
});

describe("compactLinkLabel", () => {
  it("reduces bare external URLs to host plus final path segment", () => {
    expect(compactLinkLabel("https://example.com/docs/reference/api", "https://example.com/docs/reference/api")).toBe(
      "example.com/api",
    );
    expect(compactLinkLabel("https://example.com/docs/reference/api/")).toBe("example.com/api");
    expect(compactLinkLabel("https://example.com/")).toBe("example.com");
  });

  it("keeps intentional markdown labels", () => {
    expect(compactLinkLabel("https://example.com/docs/reference/api", "API reference")).toBe("API reference");
  });

  it("reduces noisy file labels to basename and line focus", () => {
    expect(
      compactLinkLabel(
        "/workspace/testape/tests/np_e2e/flows/facebook_login.py:12-14",
        "/workspace/testape/tests/np_e2e/flows/facebook_login.py:12-14",
      ),
    ).toBe("facebook_login.py:12-14");
    expect(
      compactLinkLabel(
        "bcfile:%2FUsers%2Fofekron%2Fbetter-claude%2Ffrontend%2Fsrc%2FApp.tsx?L=42",
        "/workspace/better-agent/frontend/src/App.tsx",
      ),
    ).toBe("App.tsx:42");
    expect(compactLinkLabel("runner.py:963", "backend/runner.py")).toBe("runner.py:963");
  });
});

describe("linkifyFilePaths", () => {
  it("collapses raw markdown file links into one compact file link", () => {
    const html = renderToStaticMarkup(
      linkifyFilePaths("see [backend/runner.py](runner.py:963)", () => undefined),
    );

    expect(html).toContain("runner.py:963");
    expect(html).not.toContain("[backend/runner.py]");
    expect(html).not.toContain("(runner.py:963)");
  });

  it("renders Better Agent session markers as smart session links", () => {
    const marker = sessionLinkMarker("session-abcdef", "Linked Session");
    const html = renderToStaticMarkup(linkifyFilePaths(`open ${marker}`));

    expect(marker).toBe("[[ba-session:session-abcdef|Linked%20Session]]");
    expect(html).toContain("Linked Session · sess");
    expect(html).not.toContain("[[ba-session:");
  });

  it("converts session markers for markdown renderers", () => {
    expect(sessionMarkersToMarkdown(sessionLinkMarker("session-abcdef", "Linked Session")))
      .toBe("[Linked Session · sess](/s/session-abcdef)");
  });

  it("opens the session route when a smart session link is clicked", () => {
    window.history.pushState(null, "", "/");
    render(createElement("div", null, linkifyFilePaths(sessionLinkMarker("session-abcdef", "Linked Session"))));

    fireEvent.click(screen.getByRole("link", { name: "Linked Session · sess" }));

    expect(window.location.pathname).toBe("/s/session-abcdef");
  });
});
