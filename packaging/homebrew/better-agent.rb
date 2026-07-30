# Better Agent — Homebrew tap formula.
#
# DESIGN CONSTRAINT: this formula must never reimplement install logic.
# Homebrew's job here is only to (1) fetch a verified source tarball and
# (2) install thin launchers. All real provisioning is delegated to the
# repository's own entry points, which are the exact same ones the
# `curl | bash` installer uses:
#
#   scripts/bootstrap.sh  -> syncs ~/.better-agent/checkout, then execs
#   scripts/install-macos.sh -> provisions the toolchain, then execs
#   scripts/install.py    -> the shared installer core
#
# So a `brew` install and a `curl` install run byte-identical code and
# cannot diverge. Consequences of that choice, on purpose:
#
#   * `brew install` does NOT provision anything. Homebrew's build sandbox
#     forbids network access outside the fetched resource and forbids
#     writes outside the prefix, while the installer must clone a repo,
#     create virtualenvs under the user's home, and can prompt. Doing that
#     work in `def install` would require either a sandbox escape or a
#     second, weaker copy of the installer. Instead `caveats` tells the
#     user to run `better-agent-setup` once — a single explicit command.
#   * Toolchain dependencies (python, uv, node) are deliberately NOT
#     declared here. `scripts/install-macos.sh` is the single source of
#     truth for which toolchain versions Better Agent provisions; listing
#     them again would be a second place to keep in sync. Only `git` is
#     declared, because `scripts/bootstrap.sh` itself needs it before the
#     handoff.
#
# PUBLISH-TIME SUBSTITUTION: `__RELEASE_VERSION__` and `__RELEASE_SHA256__`
# are the only two placeholders. `scripts/release.py <version>
# --emit-formula` fills both from the artifact it actually built and writes
# the ready-to-commit formula to dist/release/v<version>/better-agent.rb.
# Do not hand-edit them.
class BetterAgent < Formula
  desc "Every coding agent in one durable workspace"
  homepage "https://github.com/ofekron/better-agent"
  version "__RELEASE_VERSION__"
  url "https://github.com/ofekron/better-agent/releases/download/v__RELEASE_VERSION__/better-agent-__RELEASE_VERSION__.tar.gz"
  sha256 "__RELEASE_SHA256__"
  license :cannot_represent

  depends_on :macos
  depends_on "git"

  def install
    # Only the bootstrap entry point is needed in the Cellar: bootstrap.sh
    # syncs its own git checkout and execs that checkout's installer, so
    # staging the rest of the tree here would be dead weight that also goes
    # stale the moment the checkout updates.
    libexec.install "scripts"

    # Delegate to the repo's own bootstrap, with brew attribution.
    # scripts/install_channel.py owns the BETTER_AGENT_FROM allowlist and is
    # the only place that validates it; "brew" is an allowlisted value.
    # write_env_script is a Pathname method on the wrapper being written, not
    # on the bin directory.
    (bin/"better-agent-setup").write_env_script libexec/"scripts/bootstrap.sh",
                                                BETTER_AGENT_FROM: "brew"

    # Launcher for the installed checkout. run.sh lives in the synced
    # checkout (not in the Cellar), because that checkout is what
    # bootstrap.sh keeps up to date.
    (bin/"better-agent").write <<~'SH'
      #!/bin/bash
      set -euo pipefail
      checkout="${BETTER_AGENT_INSTALL_DIR:-$HOME/.better-agent/checkout}"
      if [ ! -x "$checkout/run.sh" ]; then
        echo "better-agent: not set up yet at $checkout" >&2
        echo "better-agent: run 'better-agent-setup' first." >&2
        exit 1
      fi
      exec "$checkout/run.sh" "$@"
    SH
    chmod 0755, bin/"better-agent"
  end

  def caveats
    <<~EOS
      Homebrew installed the launchers only. Finish setup by running:

        better-agent-setup

      That runs Better Agent's own installer (the same one the curl
      one-liner runs): it syncs a git checkout to
      ~/.better-agent/checkout, provisions the toolchain, and configures
      the app. Override the checkout location with
      BETTER_AGENT_INSTALL_DIR.

      Afterwards, start Better Agent with:

        better-agent
    EOS
  end

  test do
    # The wrappers must exist and delegate; the installer itself is not run
    # under `brew test` because it mutates the user's home.
    assert_match "bootstrap.sh", (bin/"better-agent-setup").read
    assert_match "BETTER_AGENT_FROM", (bin/"better-agent-setup").read
    assert_match "run.sh", (bin/"better-agent").read
    system "/bin/bash", "-n", libexec/"scripts/bootstrap.sh"
    system "/bin/bash", "-n", bin/"better-agent"
  end
end
