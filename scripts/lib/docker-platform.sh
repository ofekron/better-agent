# Shared by scripts/run-backend-tests.sh and scripts/run-app.sh.
#
# Auto-detect the Docker daemon's native arch and only force linux/amd64
# when it's arm64 — pyxdelta (a backend/requirements.txt dependency) has no
# linux/arm64 wheel on PyPI, and its sdist is missing xdelta/xdelta3/xdelta3.c
# (upstream packaging bug: only headers are shipped), so a native arm64 build
# (e.g. Apple Silicon Docker Desktop/OrbStack) fails with "fatal error:
# xdelta3.c: No such file or directory". On an x86_64 daemon (matches CI:
# GitHub Actions runners are x86_64) the native build already has the wheel
# available, so building natively is both correct and avoids unnecessary
# QEMU emulation.
#
# Sets the global array PLATFORM_ARGS — empty on x86_64, (--platform
# linux/amd64) on arm64. Callers expand it as
# "${PLATFORM_ARGS[@]+"${PLATFORM_ARGS[@]}"}" (not plain "${PLATFORM_ARGS[@]}")
# because macOS's default /bin/bash is 3.2, where an empty array under
# `set -u` throws "unbound variable" on plain expansion.
docker_platform_detect() {
  local docker_arch
  docker_arch="$(docker version --format '{{.Server.Arch}}' 2>/dev/null || true)"
  case "$docker_arch" in
    arm64|aarch64)
      PLATFORM_ARGS=(--platform linux/amd64)
      ;;
    *)
      PLATFORM_ARGS=()
      ;;
  esac
}
