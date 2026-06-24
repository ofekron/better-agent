import { useEffect, useRef } from "react";
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { applyTagHighlights } from "../src/utils/tagHighlights";

function DecoratedMessage({ content }: { content: string }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    return applyTagHighlights(ref.current, [{
      id: "tag-1",
      messageId: "message-1",
      selectedText: "selected",
      comment: "",
      timestamp: "",
    }]);
  }, [content]);

  return (
    <div ref={ref}>
      <div key={content}>{content}</div>
    </div>
  );
}

describe("decorated message reconciliation", () => {
  it("replaces a decorated subtree without reconciling moved text nodes", () => {
    const view = render(<DecoratedMessage content="selected first" />);
    expect(view.container.querySelector(".inline-tag-highlight")).not.toBeNull();

    expect(() => view.rerender(
      <DecoratedMessage content="selected second" />,
    )).not.toThrow();
    expect(view.container.textContent).toBe("selected second");
  });
});
