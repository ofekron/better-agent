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

describe("Ask proposal rejection", () => {
  it("passes persisted cancel and alternative actions to the Ask picker", () => {
    expect(appSource).toContain("const handleAskDismiss = useCallback");
    expect(appSource).toContain('chosen_session_id: "__dismissed__"');
    expect(appSource).toContain("handleAskDismiss(currentSession!.id, g.responseMessage!.id)");
    expect(appSource).toContain("const handleAskAlternative = useCallback");
    expect(appSource).toContain("currentSession!.id,");
  });

  it("renders Cancel and Do something else actions", () => {
    expect(askViewSource).toContain("const onCancel =");
    expect(askViewSource).toContain('"Do something else"');
    expect(askViewSource).toContain("ask-never-mind");
    expect(askViewSource).toContain("ask-alternative");
    expect(askViewSource).toContain('h("textarea"');
  });

  it("keeps a visible dismissed status after Never Mind is persisted", () => {
    expect(askViewSource).toContain('context.chosenSessionId === "__dismissed__"');
    expect(askViewSource).toContain('"Never minded"');
    expect(askViewSource).toContain("ask-picker-resolution");
  });
});
