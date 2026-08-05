import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { PrLinkEvent } from "../src/components/MessageBubble";

// PrLinkEvent renders a pull-request link anchor. Branch coverage targets:
// the !prUrl early return (null), the prNumber ternary (label "Pull request #N"
// vs "Pull request"), and the prRepository conditional span (present vs absent).
// Anchor attributes href/target/rel/title are pinned for the render case.

describe("PrLinkEvent empty guard", () => {
  it("renders nothing when prUrl is absent", () => {
    const { container } = render(<PrLinkEvent />);
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when prUrl is empty string", () => {
    const { container } = render(<PrLinkEvent prUrl="" />);
    expect(container.firstChild).toBeNull();
  });
});

describe("PrLinkEvent label", () => {
  it("uses numbered label when prNumber is present", () => {
    const { container } = render(<PrLinkEvent prUrl="https://x/y/pull/42" prNumber={42} />);
    const label = container.querySelector(".event-pr-link-label");
    expect(label?.textContent).toBe("Pull request #42");
  });

  it("falls back to generic label when prNumber is absent", () => {
    const { container } = render(<PrLinkEvent prUrl="https://x/y/pull/7" />);
    const label = container.querySelector(".event-pr-link-label");
    expect(label?.textContent).toBe("Pull request");
  });
});

describe("PrLinkEvent repository span", () => {
  it("renders the repository span when prRepository is present", () => {
    const { container } = render(
      <PrLinkEvent prUrl="https://x/y/pull/1" prRepository="octo/repo" />,
    );
    const repo = container.querySelector(".event-pr-link-repo");
    expect(repo?.textContent).toBe("octo/repo");
  });

  it("omits the repository span when prRepository is absent", () => {
    const { container } = render(<PrLinkEvent prUrl="https://x/y/pull/1" />);
    expect(container.querySelector(".event-pr-link-repo")).toBeNull();
  });
});

describe("PrLinkEvent anchor attributes", () => {
  it("sets href, title, target, rel from prUrl", () => {
    const url = "https://example.com/owner/repo/pull/9";
    const { container } = render(<PrLinkEvent prUrl={url} prNumber={9} prRepository="owner/repo" />);
    const anchor = container.querySelector(".event-pr-link") as HTMLAnchorElement;
    expect(anchor).not.toBeNull();
    expect(anchor.getAttribute("href")).toBe(url);
    expect(anchor.getAttribute("title")).toBe(url);
    expect(anchor.getAttribute("target")).toBe("_blank");
    expect(anchor.getAttribute("rel")).toBe("noopener noreferrer");
    // svg icon present
    expect(anchor.querySelector("svg")).not.toBeNull();
  });
});
