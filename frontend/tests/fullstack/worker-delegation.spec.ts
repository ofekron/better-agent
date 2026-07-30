import { test, expect } from "./harness/fixtures";
import { createSessionWithPrompt } from "./harness/session";

// Validates real fork/delegation: forking from an existing turn spawns a
// real child session, runs a real provider turn in it, and the parent's
// ForkSplitView renders both panes.
test("forks from an existing turn into a real child session", async ({ authedPage: page }) => {
  await createSessionWithPrompt(page, "Reply with exactly the single word: PARENT.");
  await expect(page.getByTestId("assistant-message")).toContainText("PARENT", {
    timeout: 120_000,
  });

  await page.getByTestId("input-textarea").fill("Reply with exactly the single word: CHILD.");
  await page.locator(".input-overflow-trigger").click();
  await page.getByTestId("fork-btn").click();

  const forkGrid = page.getByTestId("fork-grid");
  await expect(forkGrid).toBeVisible({ timeout: 20_000 });
  // ForkSplitView always includes the root/original session as its own
  // fork-pane too (see the next test's comment), so a bare fork-pane
  // locator matches 2 elements here — scope to the new fork's reply text.
  const forkPane = forkGrid.getByTestId("fork-pane").filter({ hasText: "CHILD" });
  await expect(forkPane).toBeVisible();
  await expect(forkPane.getByTestId("assistant-message")).toContainText("CHILD", {
    timeout: 120_000,
  });
});

// Validates that two forks spawned from the SAME parent turn both land as
// their own side-by-side panes in the split grid, and that the pre-fork
// history (the parent turn) is rendered once — shared above the split —
// rather than duplicated per pane. ForkSplitView.tsx always includes the
// root/original session as a pane too (see its `flatPanes` flattening), so
// `fork-pane` alone would match 3 elements here (root + 2 forks); the
// assertions below scope to the two new fork panes by their distinct reply
// text instead of asserting a raw count across all panes.
test("forks twice from the same parent turn into side-by-side panes sharing one history", async ({
  authedPage: page,
}) => {
  await createSessionWithPrompt(page, "Reply with exactly the single word: PARENT.");
  await expect(page.getByTestId("assistant-message")).toContainText("PARENT", {
    timeout: 120_000,
  });

  const forkGrid = page.getByTestId("fork-grid");

  // First fork off the parent turn.
  await page.getByTestId("input-textarea").fill("Reply with exactly the single word: BRANCH_A.");
  await page.locator(".input-overflow-trigger").click();
  await page.getByTestId("fork-btn").click();
  await expect(forkGrid).toBeVisible({ timeout: 20_000 });
  await expect(
    forkGrid.getByTestId("fork-pane").filter({ hasText: "BRANCH_A" }).getByTestId("assistant-message"),
  ).toContainText("BRANCH_A", { timeout: 120_000 });

  // Switch focus back to the original pane (always the first tab — see
  // flatPanes' depth-first root-then-forks order) so the second fork
  // branches from the same parent turn rather than from fork A.
  await page.getByTestId("fork-tabs-strip").getByRole("tab").first().click();

  // Second fork off the SAME parent turn.
  await page.getByTestId("input-textarea").fill("Reply with exactly the single word: BRANCH_B.");
  await page.locator(".input-overflow-trigger").click();
  await page.getByTestId("fork-btn").click();
  await expect(
    forkGrid.getByTestId("fork-pane").filter({ hasText: "BRANCH_B" }).getByTestId("assistant-message"),
  ).toContainText("BRANCH_B", { timeout: 120_000 });

  // Creating a fork switches the view into a single-pane "focused view"
  // (the other panes stay in the DOM at zero width); expand back to the
  // full grid so both new forks are genuinely rendered side by side.
  await page.getByTestId("fork-back-to-split").click();

  const newForkPanes = forkGrid.getByTestId("fork-pane").filter({ hasText: /BRANCH_A|BRANCH_B/ });
  await expect(newForkPanes).toHaveCount(2);
  await expect(newForkPanes.filter({ hasText: "BRANCH_A" })).toBeVisible();
  await expect(newForkPanes.filter({ hasText: "BRANCH_B" })).toBeVisible();

  // The pre-fork history (the PARENT turn) is rendered once, above the
  // split, and shared identically by both panes rather than duplicated.
  const sharedHistory = page.getByTestId("fork-shared");
  await expect(sharedHistory).toHaveCount(1);
  await expect(sharedHistory).toContainText("PARENT");
});

// Validates depth-2 forking: forking again from an already-forked pane's
// own composer (a fork of a fork). ForkSplitView has no per-pane
// InputArea — Chat.tsx mounts exactly one shared InputArea whose Fork
// action (App.tsx's handleForkAndSend) always targets `currentSession`,
// which resolves via `focusedForkId` (see App.tsx around line 857). So
// `fork-btn` behaves identically whether the focused pane is the root or
// a fork — no special interaction is needed to fork "from within" a fork
// pane, beyond making sure that pane is the focused one.
//
// Tokens are chosen so none is a substring of another (unlike e.g.
// "CHILD"/"GRANDCHILD") so `hasText` filters can't cross-match.
test("forks a second time from an already-forked child into a depth-2 grandchild", async ({
  authedPage: page,
}) => {
  await createSessionWithPrompt(page, "Reply with exactly the single word: PARENT.");
  await expect(page.getByTestId("assistant-message")).toContainText("PARENT", {
    timeout: 120_000,
  });

  const forkGrid = page.getByTestId("fork-grid");

  // First fork: root -> CHILDFORK.
  await page.getByTestId("input-textarea").fill("Reply with exactly the single word: CHILDFORK.");
  await page.locator(".input-overflow-trigger").click();
  await page.getByTestId("fork-btn").click();
  await expect(forkGrid).toBeVisible({ timeout: 20_000 });
  const childPane = forkGrid.getByTestId("fork-pane").filter({ hasText: "CHILDFORK" });
  await expect(childPane.getByTestId("assistant-message")).toContainText("CHILDFORK", {
    timeout: 120_000,
  });

  // Forking auto-focuses the new child (handleForkAndSend calls
  // setFocusedForkId(child.id) immediately), but switch to its tab
  // explicitly so the second fork is unambiguously issued from WITHIN
  // the child pane rather than relying on that implicit focus. Tab
  // order mirrors flatPanes' depth-first root-then-forks order, so
  // index 1 is CHILDFORK.
  await page.getByTestId("fork-tabs-strip").getByRole("tab").nth(1).click();

  // Second fork, issued from the same shared composer while CHILDFORK
  // is focused: CHILDFORK -> GRANDFORK.
  await page.getByTestId("input-textarea").fill("Reply with exactly the single word: GRANDFORK.");
  await page.locator(".input-overflow-trigger").click();
  await page.getByTestId("fork-btn").click();

  const grandchildPane = forkGrid.getByTestId("fork-pane").filter({ hasText: "GRANDFORK" });
  await expect(grandchildPane.getByTestId("assistant-message")).toContainText("GRANDFORK", {
    timeout: 120_000,
  });

  // Expand out of the post-fork focused single-pane view (see the
  // two-forks test's comment) so every pane is genuinely rendered.
  await page.getByTestId("fork-back-to-split").click();

  // The grandchild's own reply is distinct from its ancestors' turns.
  await expect(grandchildPane).not.toContainText("PARENT");

  // The root's own turn (PARENT) is the tree's earliest fork point, so
  // it renders once, shared above the split — never duplicated per pane.
  const sharedHistory = page.getByTestId("fork-shared");
  await expect(sharedHistory).toHaveCount(1);
  await expect(sharedHistory).toContainText("PARENT");
  await expect(sharedHistory).not.toContainText("CHILDFORK");

  // The grandchild copied CHILDFORK's full history (PARENT + CHILDFORK)
  // at ITS fork time. Per-pane splitting uses the EARLIEST fork point
  // in the whole tree (root -> CHILDFORK's), so CHILDFORK's own turn
  // falls after that boundary and renders inside the grandchild's own
  // pane as inherited ancestor history, alongside its distinct new reply.
  await expect(grandchildPane).toContainText("CHILDFORK");
});
