# Contributing

Better Agent is source-available for non-commercial use. It is not
OSI-approved open-source software because commercial rights are reserved.

By submitting a contribution, you confirm that:

1. you have the right to submit it;
2. your contribution can be distributed under the Better Agent license in
   `LICENSE`;
3. your contribution does not include secrets, private marketplace signing
   material, paid extension packages, or proprietary third-party code; and
4. your contribution does not grant trademark, commercial-use, hosted-service,
   or marketplace rights.

This project uses Developer Certificate of Origin sign-off for outside
contributions unless a future counsel-reviewed CLA replaces it. Every commit in
a merge request must include:

```text
Signed-off-by: Name <email@example.com>
```

By signing off, you certify that you wrote the contribution or otherwise have
the right to submit it under the Better Agent license, and that you understand
the project does not grant commercial-use, hosted-service, marketplace, or
trademark rights through contribution acceptance.

## How to submit a contribution

Contributors do not have push access to the main repository. Every change
lands through a pull request from your own fork.

1. **Fork** the repository to your own account — the "Fork" button on GitHub,
   or:

   ```bash
   gh repo fork ofekron/better-agent --clone=false
   ```

2. **Add your fork as a remote** and start a branch for the change:

   ```bash
   git remote add fork https://github.com/<your-username>/better-agent.git
   git switch -c my-change
   ```

3. **Make your change.** Keep provider parity and follow the security and
   state-ownership rules in `CLAUDE.md`.

4. **Sign off every commit** (DCO — required, see above). Pass `-s` on each
   commit:

   ```bash
   git commit -s -m "fix: short description"
   ```

   Or install a local hook so the trailer is added automatically. It lives in
   `.git/` and is never committed:

   ```sh
   # .git/hooks/prepare-commit-msg   (then: chmod +x .git/hooks/prepare-commit-msg)
   #!/bin/sh
   case "$2" in merge|squash) exit 0 ;; esac
   git interpret-trailers --if-exists doNothing \
     --trailer "Signed-off-by: Your Name <you@example.com>" \
     --in-place "$1"
   ```

5. **Push to your fork and open a pull request** against `ofekron/better-agent`:

   ```bash
   git push fork my-change
   gh pr create --repo ofekron/better-agent --base main \
     --head <your-username>:my-change --fill
   ```

6. **A maintainer reviews and merges.** Respond to review feedback by pushing
   more (signed-off) commits to the same branch — the pull request updates
   automatically.

Every commit in the pull request must carry the `Signed-off-by` trailer, or it
cannot be merged.

The license is project policy, not legal advice. Have counsel review it before
relying on it for commercial licensing, enforcement, or outside contributions.
