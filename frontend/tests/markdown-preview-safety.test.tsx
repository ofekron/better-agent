import { render } from "@testing-library/react";
import type { ComponentProps, ComponentType } from "react";
import type { ExtraProps } from "react-markdown";
import { beforeAll, describe, expect, it, vi } from "vitest";
import type { MarkdownPreviewProps } from "@uiw/react-markdown-preview";
import { markdownLinkifyComponents } from "../src/utils/linkifyFilePaths";

type SafeMarkdownProps = Omit<
  MarkdownPreviewProps,
  "pluginsFilter" | "rehypePlugins" | "skipHtml"
>;

let MarkdownPreview: ComponentType<SafeMarkdownProps>;

beforeAll(async () => {
  vi.resetModules();
  vi.doUnmock("@uiw/react-markdown-preview/nohighlight");
  vi.doUnmock("react-markdown");
  MarkdownPreview = (await import("../src/components/SafeMarkdownPreview")).SafeMarkdownPreview;
});

describe("markdown preview safety", () => {
  it("renders raw HTML as inert text", () => {
    const source = '<h1 ref="boom">raw html</h1>';
    const { container } = render(<MarkdownPreview source={source} />);

    expect(container.querySelector("h1")).toBeNull();
    expect(container.textContent).toContain(source);
  });

  it("does not project rehype attribute directives into element properties", () => {
    let headingProperties: Record<string, unknown> | undefined;
    const Heading = ({ node, children, ...props }: ComponentProps<"h1"> & ExtraProps) => {
      headingProperties = node?.properties;
      return <h1 {...props}>{children}</h1>;
    };

    render(
      <MarkdownPreview
        source={"# directive\n<!--rehype:ref=boom-->"}
        components={{ h1: Heading }}
      />,
    );

    expect(headingProperties).not.toHaveProperty("ref");
  });

  it("preserves rich markdown features and custom links", () => {
    const source = [
      "# Heading",
      "",
      "| A | B |",
      "| --- | --- |",
      "| 1 | 2 |",
      "",
      "```js",
      "const answer = 42;",
      "```",
      "",
      "[file](bcfile:%2Ftmp%2Fdemo.ts)",
      "",
      "[session](/s/sid-1)",
    ].join("\n");
    const { container } = render(
      <MarkdownPreview
        source={source}
        components={markdownLinkifyComponents()}
      />,
    );

    expect(container.querySelector("table")).not.toBeNull();
    expect(container.querySelector(".hljs .hljs-keyword")?.textContent).toBe("const");
    expect(container.querySelector("h1#heading a.anchor .octicon-link")).not.toBeNull();
    expect(container.querySelector('pre .copied[data-code*="const answer"]')).not.toBeNull();
    expect(container.querySelector(".file-path-link-static")?.textContent).toContain("file");
    expect(container.querySelector('.session-smart-link[href="/s/sid-1"]')?.textContent).toBe("session");
  });
});
