from pathlib import Path

RUN_SH = Path(__file__).resolve().parents[2] / "run.sh"
_SOURCE = RUN_SH.read_text(encoding="utf-8")
# The supervisor loop body — starts at the per-iteration refresh-id reset, which
# excludes the function definitions above it (start_backend/wait_for_backend etc.).
LOOP = _SOURCE[_SOURCE.index('PENDING_REFRESH_ID=""') :]


def test_refresh_kicks_frontend_build_before_backend_and_rebuilds_after_health():
    initial_build = LOOP.index('start_frontend_build ""')
    refresh_build_first = LOOP.index('start_frontend_build "$PENDING_REFRESH_ID"')
    backend_start = LOOP.index("start_backend")
    backend_ready = LOOP.index("wait_for_backend")
    refresh_build_after_ready = LOOP.rindex('start_frontend_build "$PENDING_REFRESH_ID"')

    # Frontend build is kicked off in the background BEFORE backend spawn so the
    # backend can start against a placeholder and self-restart once it lands.
    assert initial_build < backend_start
    assert refresh_build_first < backend_start
    # Backend spawn precedes its own health wait.
    assert backend_start < backend_ready
    # A pending refresh rebuilds the frontend only after the backend is healthy.
    assert backend_ready < refresh_build_after_ready
