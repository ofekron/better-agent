import { describe, it, expect, vi, afterEach } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { fireEvent, render, screen } from "@testing-library/react";
import {
  baMarkersToMarkdown,
  compactLinkLabel,
  eventLinkMarker,
  linkifyFilePaths,
  markdownLinkifyComponents,
  parseMarkdownFileHref,
  sessionLinkMarker,
} from "../src/utils/linkifyFilePaths";

describe("parseMarkdownFileHref — bcfile + decode edge cases", () => {
  it("returns null for a bcfile href with an empty path", () => {
    expect(parseMarkdownFileHref("bcfile:")).toBeNull();
  });

  it("keeps the raw path when the bcfile body is malformed URI encoding", () => {
    expect(parseMarkdownFileHref("bcfile:%E0%A4%E0")).toEqual({ path: "%E0%A4%E0" });
  });

  it("keeps the raw path when a file-like href fails to URI-decode", () => {
    // FILE_LIKE_RE matches the .py tail; decodeURIComponent throws on %E0%80y.
    expect(parseMarkdownFileHref("x%E0%80y.py")).toEqual({ path: "x%E0%80y.py" });
  });
});

describe("compactLinkLabel — uncovered branches", () => {
  it("returns the label (or raw href) when the href is blank", () => {
    expect(compactLinkLabel("", "my label")).toBe("my label");
    // Both trim empty → falls back to the raw href.
    expect(compactLinkLabel("   ", "")).toBe("   ");
    expect(compactLinkLabel("", "")).toBe("");
  });

  it("compacts to basename+focus when a file href has no label", () => {
    expect(compactLinkLabel("a/b.py")).toBe("b.py");
    expect(compactLinkLabel("a/b.py:7-9")).toBe("b.py:7-9");
  });

  it("compacts when the label normalizes equal to the href despite differing parse", () => {
    // Trailing slash makes parsedLabel fail FILE_LIKE_RE, but normalized equality holds.
    expect(compactLinkLabel("a/b.py", "a/b.py/")).toBe("b.py");
  });

  it("falls back to the trimmed href when the URL is unparseable and no label differs", () => {
    expect(compactLinkLabel("not a url")).toBe("not a url");
  });
});

describe("ba copy-id markers — event + malformed bodies", () => {
  it("round-trips an event marker to a readable markdown link", () => {
    const marker = eventLinkMarker("s1", "m1", "My Event");
    expect(marker).toBe("[[ba-event:s1|m1|My%20Event]]");
    expect(baMarkersToMarkdown(marker)).toBe("[My Event · m1](/s/s1?m=m1)");
  });

  it("returns the whole marker unchanged when the body arity is wrong", () => {
    expect(baMarkersToMarkdown("[[ba-session:only-part]]")).toBe("[[ba-session:only-part]]");
    expect(baMarkersToMarkdown("[[ba-event:a|b]]")).toBe("[[ba-event:a|b]]");
  });

  it("falls back to the raw part when a marker body fails to URI-decode", () => {
    const out = baMarkersToMarkdown("[[ba-session:%E0%A4%E0|name]]");
    // Decoded sessionId is the raw invalid encoding; link still rendered.
    expect(out).toContain("(/s/");
    expect(out).toContain("name ·");
  });

  it("sanitizes a name that itself carries marker syntax", () => {
    // An embedded marker is stripped from the name before re-encoding (defense-in-depth).
    const marker = sessionLinkMarker("s1", "[[ba-event:x|y|z]]");
    expect(marker).toBe("[[ba-session:s1|]]");
    expect(baMarkersToMarkdown(marker)).toBe("[Session · s1](/s/s1)");
  });
});

describe("linkifyFilePaths — passthrough and recursion", () => {
  it("returns null/undefined/boolean children untouched", () => {
    expect(linkifyFilePaths(null)).toBeNull();
    expect(linkifyFilePaths(undefined)).toBeUndefined();
    expect(linkifyFilePaths(true)).toBe(true);
    expect(renderToStaticMarkup(linkifyFilePaths(true) as unknown as null)).toBe("");
  });

  it("returns a number child untouched", () => {
    expect(linkifyFilePaths(42)).toBe(42);
  });

  it("recurses into an array of strings", () => {
    expect(renderToStaticMarkup(linkifyFilePaths(["a", "b"]) as unknown as never)).toBe("ab");
  });

  it("recurses into an element's children", () => {
    const html = renderToStaticMarkup(linkifyFilePaths(createElement("div", null, "hi")) as never);
    expect(html).toBe("<div>hi</div>");
  });

  it("returns an <a> element unchanged (no nested linkify)", () => {
    const html = renderToStaticMarkup(
      linkifyFilePaths(createElement("a", { href: "/x" }, "txt")) as never,
    );
    expect(html).toBe('<a href="/x">txt</a>');
  });

  it("returns an element with no children unchanged", () => {
    const html = renderToStaticMarkup(linkifyFilePaths(createElement("br")) as never);
    expect(html).toBe("<br/>");
  });

  it("leaves a non-file markdown link intact (pushed raw)", () => {
    const html = renderToStaticMarkup(
      linkifyFilePaths("see [google](https://google.com)") as never,
    );
    expect(html).toContain("[google](https://google.com)");
  });

  it("renders an unparseable ba marker as raw text", () => {
    const html = renderToStaticMarkup(
      linkifyFilePaths("[[ba-session:only-part]]") as never,
    );
    expect(html).toContain("[[ba-session:only-part]]");
  });
});

describe("FileLinkButton — keyboard activation", () => {
  function setup() {
    const opened: Array<{ path: string; line?: number }> = [];
    render(linkifyFilePaths("[a.py](a.py:3)", (path, focus) => {
      opened.push({ path, line: focus?.startLine });
    }) as never);
    return { opened, target: screen.getByRole("link", { name: "a.py:3" }) };
  }

  it("activates on Enter", () => {
    const { opened, target } = setup();
    fireEvent.keyDown(target, { key: "Enter" });
    expect(opened).toEqual([{ path: "a.py", line: 3 }]);
  });

  it("activates on Space", () => {
    const { opened, target } = setup();
    fireEvent.keyDown(target, { key: " " });
    expect(opened).toEqual([{ path: "a.py", line: 3 }]);
  });

  it("does not activate on other keys", () => {
    const { opened, target } = setup();
    fireEvent.keyDown(target, { key: "ArrowDown" });
    expect(opened).toEqual([]);
  });
});

describe("SessionLink — static mode and event links", () => {
  it("renders a static chip when sessionLinks is 'static'", () => {
    const html = renderToStaticMarkup(
      linkifyFilePaths(sessionLinkMarker("s1", "Linked"), undefined, { sessionLinks: "static" }) as never,
    );
    expect(html).toContain("session-smart-link-static");
    expect(html).not.toContain("<a ");
  });

  it("navigates the session route when an event marker link is clicked", () => {
    window.history.pushState(null, "", "/");
    render(linkifyFilePaths(eventLinkMarker("s1", "m1", "My Event")) as never);
    const link = screen.getByRole("link", { name: "My Event · m1" });
    fireEvent.click(link);
    expect(window.location.pathname).toBe("/s/s1");
  });
});

describe("MediaPreviewInline — interactive media branch", () => {
  it("renders a media preview and opens via onFileClick", () => {
    const opened: string[] = [];
    render(linkifyFilePaths("[clip](clip.mp4)", (p) => opened.push(p)) as never);
    const name = screen.getByRole("link", { name: "clip.mp4" });
    fireEvent.click(name);
    expect(opened).toEqual(["clip.mp4"]);
  });
});

describe("markdownLinkifyComponents Anchor — uncovered branches", () => {
  afterEach(() => vi.restoreAllMocks());

  it("renders a /s/ session href as a smart session link", () => {
    const { a: Anchor } = markdownLinkifyComponents();
    const { container } = render(createElement(Anchor, { href: "/s/sess1/" }));
    const link = container.querySelector("a");
    expect(link?.getAttribute("href")).toBe("/s/sess1");
    expect(link?.textContent).toContain("Session · sess");
  });

  it("renders a session href with malformed encoding without throwing", () => {
    const { a: Anchor } = markdownLinkifyComponents();
    const { container } = render(createElement(Anchor, { href: "/s/%E0%A4%E0/" }));
    expect(container.querySelector("a")).not.toBeNull();
  });

  it("renders a media href as an inline media preview that opens via callback", () => {
    const opened: string[] = [];
    const { a: Anchor } = markdownLinkifyComponents((p) => opened.push(p));
    render(createElement(Anchor, { href: "doc.pdf" }));
    fireEvent.click(screen.getByRole("link", { name: "doc.pdf" }));
    expect(opened).toEqual(["doc.pdf"]);
  });

  it("opens an external link through window.open on click", () => {
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);
    const { a: Anchor } = markdownLinkifyComponents();
    const { container } = render(createElement(Anchor, { href: "https://example.com/p" }, "go"));
    fireEvent.click(container.querySelector("a")!);
    expect(openSpy).toHaveBeenCalledWith("https://example.com/p", "_blank", "noopener,noreferrer");
  });

  it("concatenates number and array children into the label via plainText", () => {
    const { a: Anchor } = markdownLinkifyComponents();
    const num = render(createElement(Anchor, { href: "https://example.com/p" }, 42));
    expect(num.container.querySelector("a")?.textContent).toBe("42");
    const arr = render(createElement(Anchor, { href: "https://example.com/p" }, ["a", "b"]));
    expect(arr.container.querySelector("a")?.textContent).toBe("ab");
  });
});

describe("markdownLinkifyComponents ScrollableTable", () => {
  it("wraps a table in a scroll wrapper", () => {
    const { table } = markdownLinkifyComponents();
    const { container } = render(
      createElement(table as never, null, createElement("tbody", null, createElement("tr", null, createElement("td", null, "cell")))),
    );
    expect(container.querySelector(".table-scroll-wrapper")).not.toBeNull();
    expect(container.querySelector("table")).not.toBeNull();
    expect(container.textContent).toContain("cell");
  });
});
