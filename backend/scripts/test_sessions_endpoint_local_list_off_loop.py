import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
LISTING = (BACKEND / "session_listing_api.py").read_text(encoding="utf-8")
MAIN = (BACKEND / "main.py").read_text(encoding="utf-8")


def test_local_sessions_list_pipeline_runs_off_the_event_loop():
    # The local list pipeline moved out of main.py into session_listing_api.py.
    assert "def _local_session_summaries_for_sidebar() -> list[dict]:" in LISTING
    assert "def _build_local_sessions_page_for_list(" in LISTING
    assert "def _decorate_local_sidebar_sessions(" in LISTING
    assert "def _local_session_summaries_for_sidebar" not in MAIN
    assert "def _build_local_sessions_page_for_list" not in MAIN
    # The page build runs on a worker thread via the hot-path executor, not inline.
    assert "from hot_path_executor import" in LISTING
    assert "session_list_path" in LISTING
    assert re.search(
        r'await session_list_path\.run\(\s+"sessions\.list\.local_page_thread",\s+_build_local_sessions_page_for_list,',
        LISTING,
    )
    # The decorated page is what's shipped on the wire.
    assert '"sessions": page' in LISTING
