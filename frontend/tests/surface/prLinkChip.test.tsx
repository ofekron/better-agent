// `fact` node, `kind: "pr_link"` — native PrLinkChip (src/surface/leaf/Chips.tsx),
// the native-typed equivalent of legacy MessageBubble.tsx's PrLinkEvent.
// Ported from tests/messagebubble-prlinkevent.test.tsx (deleted): PrLinkChip
// is a near-1:1 port (same CSS classes, same label/anchor logic) just never
// exercised by a native-path test.

import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { PrLinkChip } from "../../src/surface/leaf/Chips";

describe("PrLinkChip empty guard", () => {
  it("renders nothing when prUrl is absent", () => {
    const { container } = render(<PrLinkChip data={{}} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when prUrl is empty string", () => {
    const { container } = render(<PrLinkChip data={{ prUrl: "" }} />);
    expect(container.firstChild).toBeNull();
  });
});

describe("PrLinkChip label", () => {
  it("uses numbered label when prNumber is present", () => {
    const { container } = render(<PrLinkChip data={{ prUrl: "https://x/y/pull/42", prNumber: 42 }} />);
    const label = container.querySelector(".event-pr-link-label");
    expect(label?.textContent).toBe("Pull request #42");
  });

  it("falls back to generic label when prNumber is absent", () => {
    const { container } = render(<PrLinkChip data={{ prUrl: "https://x/y/pull/7" }} />);
    const label = container.querySelector(".event-pr-link-label");
    expect(label?.textContent).toBe("Pull request");
  });
});

describe("PrLinkChip repository span", () => {
  it("renders the repository span when prRepository is present", () => {
    const { container } = render(
      <PrLinkChip data={{ prUrl: "https://x/y/pull/1", prRepository: "octo/repo" }} />,
    );
    const repo = container.querySelector(".event-pr-link-repo");
    expect(repo?.textContent).toBe("octo/repo");
  });

  it("omits the repository span when prRepository is absent", () => {
    const { container } = render(<PrLinkChip data={{ prUrl: "https://x/y/pull/1" }} />);
    expect(container.querySelector(".event-pr-link-repo")).toBeNull();
  });
});

describe("PrLinkChip anchor attributes", () => {
  it("sets href, title, target, rel from prUrl", () => {
    const url = "https://example.com/owner/repo/pull/9";
    const { container } = render(
      <PrLinkChip data={{ prUrl: url, prNumber: 9, prRepository: "owner/repo" }} />,
    );
    const anchor = container.querySelector(".event-pr-link") as HTMLAnchorElement;
    expect(anchor).not.toBeNull();
    expect(anchor.getAttribute("href")).toBe(url);
    expect(anchor.getAttribute("title")).toBe(url);
    expect(anchor.getAttribute("target")).toBe("_blank");
    expect(anchor.getAttribute("rel")).toBe("noopener noreferrer");
    expect(anchor.querySelector("svg")).not.toBeNull();
  });
});
