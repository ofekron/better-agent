import { execFileSync } from "node:child_process";
import { test, expect } from "./harness/fixtures";
import {
  addUntrackedFile,
  createPlainDir,
  createRealGitRepo,
  type PlainDir,
  type RealGitRepo,
} from "./harness/git-repo";
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
  let plainDir: PlainDir;

  test.afterEach(() => {
    repo?.cleanup();
    plainDir?.cleanup();
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

  // A project directory with no `.git` at all — ProjectGitStatus.tsx returns
  // `null` (no `.project-git-status` panel) when `status.is_git` is false
  // (line 72: `if (!status || !status.is_git) return null;`), so the real
  // backend's `is_git: false` response must both keep the UI free of a
  // stale/crashed git panel AND be verifiable directly via the real
  // `GET /api/git-status` REST call, no mocked git, no mocked backend.
  test("a non-git project directory reports is_git: false and shows no git-status panel", async ({
    authedPage: page,
    backend,
  }) => {
    plainDir = createPlainDir();

    // ProjectGitStatus's own effect fires the real `GET /api/git-status`
    // on mount — wait for that exact real response (not an arbitrary
    // delay) before asserting the panel stays absent, so the assertion is
    // anchored to the real precondition (the fetch having resolved) rather
    // than a guessed timeout.
    const [statusResp] = await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes("/api/git-status") && r.url().includes(encodeURIComponent(plainDir.dir)),
      ),
      addProjectByPath(page, plainDir.dir),
    ]);
    expect(statusResp.ok()).toBe(true);
    const polledBody = await statusResp.json();
    expect(polledBody.is_git).toBe(false);

    await expect(page.locator(".project-tab").last()).toBeVisible({ timeout: 15_000 });
    await expect(page.locator(".project-git-status")).toHaveCount(0);

    // Also drive the real REST surface directly, matching the pattern used
    // by the git-diff test above.
    const res = await page.request.get(`${backend.baseURL}/api/git-status`, {
      params: { cwd: plainDir.dir },
    });
    expect(res.ok()).toBe(true);
    const body = await res.json();
    expect(body.is_git).toBe(false);
  });

  // ProjectGitStatus.tsx has no manual refresh control — its only re-fetch
  // path after mount is the component's own `setInterval(refresh, 15_000)`
  // poll of `/api/git-status` (no button/click can force it). So after the
  // real `git checkout -b` below, this test waits on that same real poll via
  // `expect(...).toContainText`'s built-in retry-until-timeout, rather than
  // an arbitrary sleep, to assert the UI picks up the real new branch name
  // from a real `git branch --show-current` re-read on the real backend.
  test("reflects the real current branch name after a real git checkout -b on disk", async ({
    authedPage: page,
  }) => {
    repo = createRealGitRepo("initial commit");

    await addProjectByPath(page, repo.dir);

    await expect(page.locator(".project-git-status")).toBeVisible({ timeout: 15_000 });
    await expect(page.locator(".project-git-branch")).toContainText("main");

    execFileSync("git", ["checkout", "-b", "feature-xyz"], { cwd: repo.dir });

    await expect(page.locator(".project-git-branch")).toContainText("feature-xyz", {
      timeout: 20_000,
    });
  });

  // `POST /api/git-commit` (backend/git_api.py -> file_browser.git_commit)
  // has no frontend UI consumer anywhere in frontend/src, so — same pattern
  // as the git-diff test above — this drives the real REST surface directly
  // with `authedPage.request` against the real backend, which shells out to
  // real `git add -u` + `git commit` subprocesses.
  test("git-commit endpoint commits real staged changes to a real new commit on disk", async ({
    authedPage: page,
    backend,
  }) => {
    repo = createRealGitRepo("initial commit");
    // git_commit() only runs `git add -u` (tracked files), which does not
    // pick up brand-new untracked files, so the new file must be explicitly
    // `git add`ed to be staged before the endpoint commits it.
    addUntrackedFile(repo, "staged.txt", "staged content\n");
    execFileSync("git", ["add", "staged.txt"], { cwd: repo.dir });

    const res = await page.request.post(`${backend.baseURL}/api/git-commit`, {
      data: { cwd: repo.dir, message: "fullstack test commit" },
    });
    expect(res.ok()).toBe(true);

    const body = await res.json();
    expect(body.ok).toBe(true);

    const subject = execFileSync("git", ["log", "-1", "--format=%s"], {
      cwd: repo.dir,
      encoding: "utf-8",
    }).trim();
    expect(subject).toBe("fullstack test commit");
  });

  // `GET /api/git-tree` (backend/git_api.py) caps `limit` at 200 and passes
  // it straight through to `git log -n <limit>` (backend/file_browser.py),
  // which is newest-first by default (no `--reverse`). GitTreeView.tsx always
  // requests `limit=200` and renders `commits` in the exact array order the
  // API returns (gitGraph.ts's buildGitGraphRows does a 1:1 .map with no
  // sort). A handful of real commits (well under the 200 cap) is enough to
  // prove that pass-through/ordering logic end-to-end without the minutes a
  // 200+-commit fixture would cost.
  test("git-tree with the real limit=200 cap renders all real commits in exact reverse-chronological order, no dupes/off-by-one", async ({
    authedPage: page,
  }) => {
    repo = createRealGitRepo("commit 1");

    const commitCount = 8;
    const shortHashes: string[] = [
      execFileSync("git", ["rev-parse", "--short=7", "HEAD"], {
        cwd: repo.dir,
        encoding: "utf-8",
      }).trim(),
    ];
    for (let i = 2; i <= commitCount; i++) {
      addUntrackedFile(repo, "progress.txt", `progress ${i}\n`);
      execFileSync("git", ["add", "progress.txt"], { cwd: repo.dir });
      execFileSync("git", ["commit", "-m", `commit ${i}`], { cwd: repo.dir });
      shortHashes.push(
        execFileSync("git", ["rev-parse", "--short=7", "HEAD"], {
          cwd: repo.dir,
          encoding: "utf-8",
        }).trim(),
      );
    }
    // `shortHashes` is oldest-first (index 0 = "commit 1" ... last = "commit 8");
    // the UI must render newest-first, i.e. this array reversed.
    const expectedSubjectsNewestFirst = Array.from(
      { length: commitCount },
      (_, i) => `commit ${commitCount - i}`,
    );
    const expectedHashesNewestFirst = [...shortHashes].reverse();

    await addProjectByPath(page, repo.dir);

    await expect(page.locator(".project-git-status")).toBeVisible({ timeout: 15_000 });
    await page.locator(".project-git-info").click();
    const treeView = page.getByTestId("git-tree-view");
    await expect(treeView).toBeVisible();
    await expect(treeView.locator(".git-tree-summary")).toContainText(`Commits · ${commitCount}`);

    // Exact count: not off-by-one (7 or 9), not duplicated.
    await expect(treeView.locator(".git-tree-row")).toHaveCount(commitCount);

    const renderedSubjects = await treeView.locator(".git-tree-row h2").allTextContents();
    const renderedHashes = await treeView.locator(".git-tree-row code").allTextContents();

    expect(renderedSubjects).toEqual(expectedSubjectsNewestFirst);
    expect(renderedHashes).toEqual(expectedHashesNewestFirst);
    // No duplicate rows hiding behind a correct count.
    expect(new Set(renderedHashes).size).toBe(commitCount);
  });
});
