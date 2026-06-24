import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { markdownLinkifyComponents } from "../src/utils/linkifyFilePaths";

const { a: Anchor } = markdownLinkifyComponents();

describe("markdownLinkifyComponents `a` override", () => {
  // rehype-autolink-headings injects <a href="#slug"> with an icon child
  // into every heading; @uiw/react-markdown-preview routes that anchor
  // through this override. It must keep the icon, not show the slug text.
  it("preserves the icon child of an in-page # anchor and does not render the slug as text", () => {
    const { container } = render(
      <Anchor href="#skill-skill-tool">
        <span data-testid="anchor-icon">🔗</span>
      </Anchor>,
    );
    expect(container.textContent).not.toContain("skill-skill-tool");
    expect(container.querySelector('[data-testid="anchor-icon"]')).not.toBeNull();
  });

  it("still compacts a label-less external http link to host/last-segment", () => {
    const { container } = render(<Anchor href="https://example.com/some/long/path" />);
    const link = container.querySelector("a");
    expect(link?.getAttribute("href")).toBe("https://example.com/some/long/path");
    expect(link?.textContent).toBe("example.com/path");
  });
});
