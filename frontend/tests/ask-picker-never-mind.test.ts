import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const appSource = fs.readFileSync(
  path.resolve(__dirname, "../src/App.tsx"),
  "utf8",
);
const askViewSource = fs.readFileSync(
  path.resolve(__dirname, "../../extensions/ask/ui/ask-view.entry.js"),
  "utf8",
);

describe("Ask proposal dismissal", () => {
  it("passes a persisted dismiss action to the Ask picker", () => {
    expect(appSource).toContain("const handleAskDismiss = useCallback");
    expect(appSource).toContain('chosen_session_id: "__dismissed__"');
    expect(appSource).toContain("onDismiss: () => handleAskDismiss(g.responseMessage!.id)");
  });

  it("renders Never Mind as a secondary proposal action", () => {
    expect(askViewSource).toContain("const onDismiss =");
    expect(askViewSource).toContain('"Never Mind"');
    expect(askViewSource).toContain("ask-never-mind");
  });

  it("keeps a visible dismissed status after Never Mind is persisted", () => {
    expect(askViewSource).toContain('context.chosenSessionId === "__dismissed__"');
    expect(askViewSource).toContain('"Never minded"');
    expect(askViewSource).toContain("ask-picker-resolution");
  });
});
