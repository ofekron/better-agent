import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useState } from "react";
import { useResizable } from "../src/hooks/useResizable";

function ResizableProbe() {
  const [tab, setTab] = useState<"files" | "notes">("files");
  const resizable = useResizable({
    storageKey: `right-panel-width:${tab}`,
    defaultSize: 450,
    min: 280,
    max: 800,
    axis: "x",
    direction: "reverse",
  });

  return (
    <div>
      <div data-testid="size">{resizable.size}</div>
      <button type="button" onClick={() => setTab("files")}>Files</button>
      <button type="button" onClick={() => setTab("notes")}>Notes</button>
      <div data-testid="resizer" onMouseDown={resizable.onMouseDown} />
    </div>
  );
}

describe("useResizable", () => {
  it("loads and persists sizes independently when the storage key changes", () => {
    localStorage.setItem("right-panel-width:files", "320");
    localStorage.setItem("right-panel-width:notes", "560");

    render(<ResizableProbe />);

    expect(screen.getByTestId("size").textContent).toBe("320");

    fireEvent.click(screen.getByText("Notes"));
    expect(screen.getByTestId("size").textContent).toBe("560");

    fireEvent.mouseDown(screen.getByTestId("resizer"), { clientX: 400, clientY: 0 });
    fireEvent.mouseMove(document, { clientX: 320, clientY: 0 });
    fireEvent.mouseUp(document);

    expect(screen.getByTestId("size").textContent).toBe("640");
    expect(localStorage.getItem("right-panel-width:notes")).toBe("640");
    expect(localStorage.getItem("right-panel-width:files")).toBe("320");

    fireEvent.click(screen.getByText("Files"));
    expect(screen.getByTestId("size").textContent).toBe("320");
  });
});
