import { describe, it, expect, vi, afterEach } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { fireEvent, render, screen } from "@testing-library/react";
import {
  baMarkersToMarkdown,
  compactLinkLabel,
  eventLinkMarker,
  isAbsolutePath,
  linkifyFilePaths,
  markdownLinkifyComponents,
  parseMarkdownFileHref,
  sessionLinkMarker,
  sessionMarkersToMarkdown,
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

describe("isAbsolutePath — absolute path detection", () => {
  it("recognizes POSIX, backslash, and Windows-drive absolutes, rejects relative", () => {
    expect(isAbsolutePath("/usr/bin")).toBe(true);
    // A leading single backslash (source "\\" is one backslash).
    expect(isAbsolutePath("\\srv\\share")).toBe(true);
    expect(isAbsolutePath("C:\\Users")).toBe(true);
    expect(isAbsolutePath("D:/code")).toBe(true);
    expect(isAbsolutePath("rel/path")).toBe(false);
    expect(isAbsolutePath("file.txt")).toBe(false);
  });
});

describe("sessionMarkersToMarkdown — delegation", () => {
  it("aliases baMarkersToMarkdown", () => {
    expect(sessionMarkersToMarkdown(sessionLinkMarker("s1", "Hi"))).toBe("[Hi · s1](/s/s1)");
  });
});

describe("parseBcfileHref — focus query parsing (via parseMarkdownFileHref)", () => {
  it("extracts a single-line focus", () => {
    expect(parseMarkdownFileHref("bcfile:/a/b.py?L=3")).toEqual({ path: "/a/b.py", focus: { startLine: 3, endLine: 3 } });
  });
  it("extracts a line range", () => {
    expect(parseMarkdownFileHref("bcfile:/a/b.py?L=5-9")).toEqual({ path: "/a/b.py", focus: { startLine: 5, endLine: 9 } });
  });
  it("ignores a malformed L value but keeps the path", () => {
    expect(parseMarkdownFileHref("bcfile:/a/b.py?L=abc")).toEqual({ path: "/a/b.py" });
  });
  it("ignores an unrelated query param", () => {
    expect(parseMarkdownFileHref("bcfile:/a/b.py?Z=1")).toEqual({ path: "/a/b.py" });
  });
});

describe("parseMarkdownFileHref — guard and query-split branches", () => {
  it("rejects anchor and empty hrefs", () => {
    expect(parseMarkdownFileHref("#section")).toBeNull();
    expect(parseMarkdownFileHref("")).toBeNull();
  });
  it("rejects a url-scheme href that is not a file", () => {
    expect(parseMarkdownFileHref("https://example.com")).toBeNull();
  });
  it("splits a query off a plain file href", () => {
    expect(parseMarkdownFileHref("a/b.py?x")).toEqual({ path: "a/b.py" });
  });
});

describe("plainText — array child that carries no text", () => {
  it("returns null when an array child is a non-text element, falling back to the generated label", () => {
    const { a: Anchor } = markdownLinkifyComponents();
    const { container } = render(createElement(Anchor, { href: "/s/s1/" }, [42, createElement("i", null, "z")]));
    // plainText bails on the <i> element → label falls back to sessionLinkLabel.
    expect(container.querySelector("a")?.textContent).toContain("Session");
  });
});

describe("compactLinkLabel — basename, kept-label, and url-compact branches", () => {
  it("compacts when label basename matches href basename but the dir differs", () => {
    expect(compactLinkLabel("dir1/x.py", "dir2/x.py")).toBe("x.py");
  });
  it("keeps a differing file label", () => {
    expect(compactLinkLabel("a/b.py", "a/c.py")).toBe("a/c.py");
  });
  it("keeps a non-matching label on a non-file href", () => {
    expect(compactLinkLabel("https://example.com", "Click")).toBe("Click");
  });
  it("compacts a bare url without a label to host/last-segment", () => {
    expect(compactLinkLabel("https://x.com/a/b")).toBe("x.com/b");
    expect(compactLinkLabel("https://x.com")).toBe("x.com");
  });
});

describe("eventLinkMarker — empty name falls back to 'Event'", () => {
  it("renders the default label when the name is blank", () => {
    expect(baMarkersToMarkdown(eventLinkMarker("s1", "m1", ""))).toBe("[Event · m1](/s/s1?m=m1)");
  });
});

describe("FileLinkButton — static render (no onFileClick) via Anchor", () => {
  it("renders a non-media file as a static, non-interactive span", () => {
    const { a: Anchor } = markdownLinkifyComponents();
    const { container } = render(createElement(Anchor, { href: "notes.txt" }));
    expect(container.querySelector(".file-path-link-static")).not.toBeNull();
    expect(container.textContent).toContain("notes.txt");
  });
});

describe("SessionLink — click guards and session-only navigation", () => {
  it("skips navigation on a modified click (early return before navigateRoute)", () => {
    const pushSpy = vi.spyOn(window.history, "pushState");
    render(linkifyFilePaths(eventLinkMarker("s1", "m1", "Ev")) as never);
    fireEvent.click(screen.getByRole("link", { name: "Ev · m1" }), { metaKey: true });
    expect(pushSpy).not.toHaveBeenCalled();
  });
  it("navigates a session link (no messageId) without requesting message focus", () => {
    window.history.pushState(null, "", "/");
    render(linkifyFilePaths(sessionLinkMarker("s1", "Hi")) as never);
    fireEvent.click(screen.getByRole("link", { name: "Hi · s1" }));
    expect(window.location.pathname).toBe("/s/s1");
  });
});

describe("preserveTextBreaks + linkifyRawFileString — multiline and trailing text", () => {
  it("renders <br> for embedded newlines and trailing text around a file link", () => {
    const html = renderToStaticMarkup(linkifyFilePaths("see\n[a.py](a.py:3) end") as never);
    expect(html).toContain("<br/>");
    expect(html).toContain("see");
    expect(html).toContain("a.py");
    expect(html).toContain("end");
  });
});

describe("linkifyRawString — text before and after a ba marker", () => {
  it("emits inter and trailing text around a session marker", () => {
    const html = renderToStaticMarkup(linkifyFilePaths("x[[ba-session:s1|Hi]]y") as never);
    expect(html.startsWith("x")).toBe(true);
    expect(html).toContain("<a ");
    expect(html).toContain("</a>y");
    expect(html).toContain("Hi · s1");
  });
});

describe("linkifyFilePaths — non-element fallback node", () => {
  it("returns a plain object child untouched", () => {
    const node = { not: "an element" };
    expect(linkifyFilePaths(node as never)).toBe(node);
  });
});

describe("markdownLinkifyComponents Anchor — href-type edge branches", () => {
  afterEach(() => vi.restoreAllMocks());

  it("renders an event session href (/s/.../?m=...) as an event smart link", () => {
    const { a: Anchor } = markdownLinkifyComponents();
    const { container } = render(createElement(Anchor, { href: "/s/s1/?m=m9" }, "Go"));
    const link = container.querySelector("a");
    expect(link?.getAttribute("href")).toBe("/s/s1?m=m9");
    expect(link?.textContent).toBe("Go");
  });

  it("uses the generated event label when children carry no plain text", () => {
    // plainText(<i/>) is null → the ?? fallback evaluates the messageId ternary (eventLinkLabel).
    const { a: Anchor } = markdownLinkifyComponents();
    const { container } = render(createElement(Anchor, { href: "/s/s1/?m=m9" }, createElement("i", null, "z")));
    expect(container.querySelector("a")?.getAttribute("href")).toBe("/s/s1?m=m9");
    expect(container.textContent).toContain("Event");
  });

  it("renders an in-page #anchor without routing it through external-link compaction", () => {
    const { a: Anchor } = markdownLinkifyComponents();
    const { container } = render(createElement(Anchor, { href: "#section" }, "§"));
    const link = container.querySelector("a");
    expect(link?.getAttribute("href")).toBe("#section");
    expect(link?.textContent).toContain("§");
  });

  it("renders a non-string href as an empty, non-navigating anchor", () => {
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);
    const { a: Anchor } = markdownLinkifyComponents();
    const { container } = render(createElement(Anchor, { href: undefined }, "x"));
    const link = container.querySelector("a");
    expect(link?.getAttribute("href")).toBeNull();
    expect(link?.getAttribute("title")).toBeNull();
    expect(link?.textContent).toBe("x");
    fireEvent.click(link!);
    expect(openSpy).not.toHaveBeenCalled();
  });
});
