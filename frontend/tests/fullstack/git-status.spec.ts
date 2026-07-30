import { execFileSync } from "node:child_process";
import { test, expect } from "./harness/fixtures";
import { addUntrackedFile, createRealGitRepo, type RealGitRepo } from "./harness/git-repo";
import { addProjectByPath } from "./harness/projects";

// Validates the real git integration end-to-end: a REAL git repository on
// disk (real `git` subprocess, real commits, real dirty files) is registered
// as a project through the real "+" → DirPickerModal → POST /api/projects
// flow, then the real ProjectGitStatus + GitTreeView components fetch
// `/api/git-status` and `/api/git-tree`, which shell out to real `git` on the
// real backend and return real branch/commit/dirty state — no mocked git, no
// mocked backend.
test.describe("git status and history", () => {
  let repo: RealGitRepo;

  test.afterEach(() => {
    repo?.cleanup();
  });

  test("reflects real branch, dirty count, and commit history from disk", async ({
    authedPage: page,
  }) => {
    repo = createRealGitRepo("initial commit");
    // A real uncommitted file so `git status --porcelain` reports it before
    // the UI ever loads this project — the backend's git-status cache is
    // per-(node, cwd) TTL'd, so the fixture must exist before the first read.
    addUntrackedFile(repo, "scratch.txt", "real uncommitted content\n");

    // addProjectByPath both registers (POST /api/projects) and selects the
    // project (App.tsx's handleAddProject sets selectedProjectPath), so the
    // git status panel renders immediately after it resolves.
    await addProjectByPath(page, repo.dir);

    const gitStatus = page.locator(".project-git-status");
    await expect(gitStatus).toBeVisible({ timeout: 15_000 });
    await expect(page.locator(".project-git-branch")).toContainText("main");
    await expect(page.locator(".project-git-dirty")).toContainText("1 changed");

    // Open the real commit-history graph (GitTreeView) and assert it shows
    // the real commit written to disk above, through the real `git log`.
    await page.locator(".project-git-info").click();
    const treeView = page.getByTestId("git-tree-view");
    await expect(treeView).toBeVisible();
    await expect(treeView.locator(".git-tree-summary")).toContainText("Commits · 1");
    await expect(treeView.locator(".git-tree-dirty")).toContainText("Changes · 1");
    await expect(treeView.locator(".git-tree-row")).toHaveCount(1);
    await expect(treeView.locator(".git-tree-row h2")).toHaveText("initial commit");
    await expect(treeView.locator(".git-tree-row code")).toHaveText(repo.initialCommitShortHash);
  });

  test("reflects a second real commit after a real commit is made on disk", async ({
    authedPage: page,
  }) => {
    repo = createRealGitRepo("first commit");

    await addProjectByPath(page, repo.dir);

    await expect(page.locator(".project-git-status")).toBeVisible({ timeout: 15_000 });
    // Clean tree: one real commit, zero real dirty files.
    await expect(page.locator(".project-git-dirty")).toContainText("clean");

    await page.locator(".project-git-info").click();
    const treeView = page.getByTestId("git-tree-view");
    await expect(treeView).toBeVisible();
    await expect(treeView.locator(".git-tree-row")).toHaveCount(1);

    // Make a real second commit directly on disk (not through the UI), then
    // refresh the tree view and confirm the UI picks up the real new state
    // from a real `git log` re-read.
    addUntrackedFile(repo, "second.txt", "second file\n");
    execFileSync("git", ["add", "second.txt"], { cwd: repo.dir });
    execFileSync("git", ["commit", "-m", "second commit"], { cwd: repo.dir });

    await page.locator(".git-tree-icon-btn").first().click();
    await expect(treeView.locator(".git-tree-row")).toHaveCount(2, { timeout: 15_000 });
    await expect(treeView.locator(".git-tree-row h2").first()).toHaveText("second commit");
  });

  // `GET /api/git-diff` (backend/git_api.py -> file_browser.get_file_diff)
  // has no frontend UI consumer anywhere in frontend/src, so it can't be
  // exercised by clicking through the app. Instead this drives the real
  // REST surface directly with `authedPage.request` (an APIRequestContext
  // sharing the authenticated cookie jar, same pattern as
  // marketplace.spec.ts) against the real backend, which shells out to a
  // real `git diff` subprocess.
  test("git-diff endpoint returns a real unified diff for a modified tracked file", async ({
    authedPage: page,
    backend,
  }) => {
    repo = createRealGitRepo("initial commit");
    // Overwrite the tracked README.md (committed by createRealGitRepo, whose
    // sole line reads "# fullstack git test repo") with real changed content
    // so `git diff` reports both a real removed line and a real added line.
    addUntrackedFile(repo, "README.md", "# fullstack git test repo - modified\nreal new line\n");

    const res = await page.request.get(`${backend.baseURL}/api/git-diff`, {
      params: { path: "README.md", cwd: repo.dir },
    });
    expect(res.ok()).toBe(true);

    const body = await res.json();
    expect(body.diff).toContain("diff --git a/README.md b/README.md");
    expect(body.diff).toContain("-# fullstack git test repo\n");
    expect(body.diff).toContain("+# fullstack git test repo - modified");
    expect(body.diff).toContain("+real new line");
  });

  test("dirty count and git-tree summary combine a modified, untracked, and staged file into one total", async ({
    authedPage: page,
  }) => {
    repo = createRealGitRepo("initial commit");

    // Modified tracked file (unstaged): overwrite the README.md committed by
    // createRealGitRepo, producing a real unstaged "M" porcelain line.
    addUntrackedFile(repo, "README.md", "# fullstack git test repo - modified\n");
    // New untracked file: a real "??" porcelain line.
    addUntrackedFile(repo, "untracked.txt", "brand new file\n");
    // Staged-but-uncommitted file: a real `git add` produces a real "A "
    // porcelain line, distinct from the untracked and modified ones above.
    addUntrackedFile(repo, "staged.txt", "staged content\n");
    execFileSync("git", ["add", "staged.txt"], { cwd: repo.dir });

    // backend/file_browser.py's get_git_status parses `git status --porcelain
    // -b` one output line per path, bucketing each line into exactly one of
    // modified/added/deleted/untracked (first match of "M" > "A" > "D" > "?"
    // in the 2-char XY status wins). get_git_tree's dirty_count then sums the
    // bucket lengths, so it counts DISTINCT DIRTY PATHS, not staged and
    // unstaged hunks separately, and does not double-count a path that is
    // both staged and unstaged. With 3 distinct dirty paths (one modified,
    // one untracked, one staged) the total is 3, not 6.
    await addProjectByPath(page, repo.dir);

    const gitStatus = page.locator(".project-git-status");
    await expect(gitStatus).toBeVisible({ timeout: 15_000 });
    await expect(page.locator(".project-git-dirty")).toContainText("3 changed");

    await page.locator(".project-git-info").click();
    const treeView = page.getByTestId("git-tree-view");
    await expect(treeView).toBeVisible();
    await expect(treeView.locator(".git-tree-dirty")).toContainText("Changes · 3");
  });
});
