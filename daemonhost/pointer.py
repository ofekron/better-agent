from switch_control_daemon.line_switch_runtime import pointer as _impl
from switch_control_daemon.line_switch_runtime.transaction import _platform_lock

_canonical_checkout = _impl._canonical_checkout
_is_runnable_checkout = _impl._is_runnable_checkout
confirm_healthy = _impl.confirm_healthy
is_switching = _impl.is_switching
main = _impl.main
mark_result = _impl.mark_result
read = _impl.read
reconcile_startup = _impl.reconcile_startup
resolve = _impl.resolve
revert = _impl.revert
revert_if_switching = _impl.revert_if_switching
set_active = _impl.set_active

if __name__ == "__main__":
    raise SystemExit(main())
