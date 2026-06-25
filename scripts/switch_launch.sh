# Sourceable helpers for line-switch-aware launching. No top-level side effects,
# so tests can source and exercise them in isolation.

# Resolve which checkout the backend must run from this launch iteration. Honors
# the active-checkout pointer via daemonhost; falls back to the default on any
# error (missing module, unreadable pointer). A pointer left in a failed switch
# resolves to the default too — see daemonhost.pointer.resolve.
# Usage: dir="$(resolve_active_checkout "$PY" "$launcher_dir" "$default")"
resolve_active_checkout() {
  local py="$1" launcher_dir="$2" default="$3"
  PYTHONPATH="$launcher_dir" "$py" -m daemonhost.pointer resolve --default "$default" 2>/dev/null || echo "$default"
}

# A checkout the backend will import must have a built frontend, or uvicorn
# crashes at import time in mount_frontend(). Returns success (0) when the active
# checkout still needs a synchronous build before the backend can start — true
# for a cold clone and for a switch to a never-built line.
active_frontend_needs_build() {
  local active_dir="$1"
  [ ! -f "$active_dir/frontend/dist/index.html" ]
}
