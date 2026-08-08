#!/bin/sh
# Test-container entrypoint (docker/Dockerfile.test).
#
# Runs pytest as normal, then loosens read permissions on artifacts it
# wrote into the bind-mounted host repo (junit XML, the --coverage output
# dir). The container always runs as root (no USER directive). On a
# native Linux Docker host — self-hosted CI runners, plain docker-ce — a
# bind mount is the SAME filesystem/inode ownership on both sides, so a
# file this root process creates under /repo is root-owned on the host
# too. Docker Desktop's macOS VM remaps bind-mount ownership to the
# invoking host user regardless of the in-container UID, so this is
# already a no-op there.
#
# Without this, a non-root CI step reading the artifact afterward (e.g.
# GitHub Actions' `actions/upload-artifact`, run as the unprivileged
# `runner` user) gets EACCES — this is exactly what silently dropped
# JUnit timing data for every self-hosted-Linux shard while the
# Docker-Desktop-on-Mac shard uploaded fine.
#
# Beyond readability, root ownership itself is a problem on native Linux
# Docker hosts: any file this root process creates or touches anywhere
# under bind-mounted /repo (junit XML, backend/.pytest_cache, __pycache__,
# etc.) is root-owned on the host too, and the NEXT CI job's
# actions/checkout (run as the unprivileged `runner` user) can't remove
# root-owned leftovers from the previous job's checkout — it fails with
# "fatal: not a git repository". scripts/run-backend-tests.sh forwards
# BETTER_AGENT_TEST_CHOWN=<uid>:<gid> only on native Linux Docker hosts
# (see docker_test_chown_env_args in scripts/lib/docker-test-lifecycle.sh);
# chown the whole bind-mounted tree back to that identity so the checkout
# after this job's is left clean. Unset (macOS/Docker Desktop, whose VM
# already remaps bind-mount ownership) leaves this a no-op.
set -eu
status=0
python -m pytest "$@" || status=$?
find /repo/backend -maxdepth 1 -name '*.xml' -exec chmod a+r {} \; 2>/dev/null || true
if [ -d /coverage ]; then
  chmod -R a+rX /coverage 2>/dev/null || true
fi
if [ -n "${BETTER_AGENT_TEST_CHOWN:-}" ]; then
  chown -R "$BETTER_AGENT_TEST_CHOWN" /repo 2>/dev/null || true
fi
exit "$status"
