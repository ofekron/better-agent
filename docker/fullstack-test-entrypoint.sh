#!/bin/sh
# Entrypoint for docker/Dockerfile.fullstack-test's working-tree path.
#
# scripts/run-fullstack-tests.sh bind-mounts the host repo read-only at
# /repo-src. We can't run the test suite directly against that mount:
# frontend/tests/fullstack/harness/venv.ts's resolveVenvPython() calls
# `backend/dependency_plan.py activate`, which writes backend/.active-venv
# and backend/.venvs/<hash>/ into the repo it's run from. Doing that
# against the bind-mounted host tree would overwrite the HOST's own
# .active-venv marker with a path to a Linux-only venv that doesn't exist
# on the host filesystem — breaking the next native `run.sh` invocation
# outside this container. So: snapshot the working tree (tracked + not
# gitignored, i.e. what a fresh `git add -A` would see — matches "test the
# working tree" semantics, naturally excludes node_modules/dist/.venvs/
# .git without a manual exclude list) into a container-private /repo, then
# run there. This costs a fast tree copy per run, not a rebuild.
set -eu

if [ -d /repo-src ]; then
  mkdir -p /repo
  cd /repo-src
  git ls-files -z --cached --others --exclude-standard | tar --null -T - -cf - | (cd /repo && tar -xf -)
  cd /repo
fi

cd /repo/frontend
npm run build
exec npx playwright test "$@"
