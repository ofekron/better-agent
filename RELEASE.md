# Releasing Better Agent

Releases must be reproducible enough for users to verify what they installed.

`scripts/release.py` automates every step that can be automated and prints the
exact commands for the steps that must stay in operator hands (signing, pushing,
publishing). It is a **dry run by default** and never mutates git or calls `gh`
unless you pass an explicit action flag. It never needs `sudo`.

## Version authority

- The release version is owned by the signed annotated git tag `v<semver>`.
- `package.json` -> `version` is the single in-repo mirror. `release.py`
  refuses to build when the two disagree.
- `desktop/_version.py` is **not** the release version. It is the tufup desktop
  update-channel version, auto-bumped to a build timestamp by the desktop
  artifact pipeline. Do not hand-align it to the release semver.

## Cutting a release

Run from a clean `dev` (or release) checkout. Replace `0.2.0` throughout.

```
scripts/release.py 0.2.0 --set-version
git add package.json && git commit -m "Release 0.2.0"
scripts/release.py 0.2.0
```

The third command is the dry run. It validates the version, refuses a dirty
tree, audits the tag/HEAD file list against the exclusions below, builds the
source tarball in a temp dir to prove it is clean, and prints both the release
notes and the remaining commands.

```
scripts/release.py 0.2.0 --tag
git push github v0.2.0
scripts/release.py 0.2.0 --build --emit-formula
scripts/release.py 0.2.0 --publish
```

`--build` writes to `dist/release/v0.2.0/`:

- `better-agent-0.2.0.tar.gz` — source artifact, `git archive` from the tag,
  gzipped with `mtime=0`, so rebuilding the same tag is byte-identical. Only
  tracked content can appear in it.
- `SHA256SUMS` — verifiable with `shasum -a 256 -c SHA256SUMS`.
- `RELEASE_NOTES.md` — source commit, tag, artifact names, SHA-256 digests.
- `better-agent.rb` — the filled Homebrew formula (tap artifact, not a release
  asset).

To attach pre-built desktop/mobile artifacts, build them from the tag (see
`scripts/rebuild-desktop-artifacts.mjs`) and pass each one:

```
scripts/release.py 0.2.0 --build --emit-formula --artifact /path/to/BetterAgent.dmg
```

They are then checksummed into the same `SHA256SUMS` and notes table.

Sign the artifacts or the checksum manifest, and record the signing key
fingerprint in the release notes before publishing.

## Homebrew tap bump

The tap formula lives in this repo at `packaging/homebrew/better-agent.rb` and
is published to the `ofekron/homebrew-better-agent` tap. It has exactly two
placeholders, `__RELEASE_VERSION__` and `__RELEASE_SHA256__`, and
`--emit-formula` fills both from the artifact it actually built — never edit
them by hand, and never guess a digest.

The formula must stay a thin wrapper: it installs launchers and delegates to
`scripts/bootstrap.sh` -> `scripts/install-macos.sh` -> `scripts/install.py`,
the same path `curl | bash` uses, with `BETTER_AGENT_FROM=brew`. If you find
yourself adding venv, node, or python setup to the formula, stop — that logic
belongs in the installer, not in packaging.

After the GitHub release exists (the formula's `url` points at a release
asset):

```
cp dist/release/v0.2.0/better-agent.rb <tap-checkout>/Formula/better-agent.rb
ruby -c <tap-checkout>/Formula/better-agent.rb
brew install --build-from-source <tap-checkout>/Formula/better-agent.rb
```

Then commit and push the tap repo. Verify the user-facing path end to end:

```
brew install ofekron/better-agent/better-agent
better-agent-setup
```

## Do Not Release

`release.py` enforces the mechanical items; the rest are operator judgement.

Enforced automatically:

- a dirty working tree;
- a version that is not strict semver;
- a version that disagrees with `package.json`;
- a source archive containing `better-agent-private/`, `.env` (templates such
  as `.env.example` are allowed), `.better-claude/`, `.better-agent/`,
  virtualenvs, `node_modules/`, `site-packages/`, `__pycache__/`, or generated
  distributables (`.dmg`, `.pkg`, `.apk`, `.exe`, `.msi`, `.zip`, `.tar.gz`,
  ...). Building with `git archive` means untracked and ignored files cannot
  leak in at all; the audit catches anything wrongly *tracked*.

Operator judgement, not enforced:

- an unsigned tag (`--tag` uses `git tag -s`, but a manually created tag is not
  checked);
- a build whose dependencies have not been audited from a clean environment;
- missing artifact/manifest signatures.

## Marketplace artifacts

Marketplace extension artifacts must be verified by digest and signature before
load. The public repo may contain verification keys. Private signing keys and
the release pipeline must stay outside the public repo.
