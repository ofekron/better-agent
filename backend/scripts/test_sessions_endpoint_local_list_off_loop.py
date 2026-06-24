from pathlib import Path


def main() -> int:
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(
        encoding="utf-8"
    )
    assert "def _local_session_summaries_for_sidebar() -> list[dict]:" in source
    assert "def _build_local_sessions_page_for_list(" in source
    assert "page, total = await asyncio.to_thread(_build_local_sessions_page_for_list, **filters)" in source
    assert "page = _decorate_local_sidebar_sessions(out[offset:end])" in source
    assert '"sessions": page' in source
    print("PASS: sessions endpoint keeps local list pipeline off the event loop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
