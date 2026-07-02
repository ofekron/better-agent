import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useSaveShortcut } from "../src/hooks/useSaveShortcut";

function ShortcutProbe({
  enabled = true,
  onSave,
}: {
  enabled?: boolean;
  onSave: () => void;
}) {
  useSaveShortcut({ enabled, onSave });
  return null;
}

describe("useSaveShortcut", () => {
  it("runs save for Ctrl+S and Cmd+S", () => {
    const onSave = vi.fn();
    render(<ShortcutProbe onSave={onSave} />);

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "s", ctrlKey: true }));
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "s", metaKey: true }));

    expect(onSave).toHaveBeenCalledTimes(2);
  });

  it("ignores modified save variants and disabled handlers", () => {
    const onSave = vi.fn();
    render(<ShortcutProbe enabled={false} onSave={onSave} />);

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "s", ctrlKey: true }));
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "s", metaKey: true, shiftKey: true }));

    expect(onSave).not.toHaveBeenCalled();
  });
});
