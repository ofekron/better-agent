from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
RUNNER = (BACKEND / "requirements_query_runner.py").read_text(encoding="utf-8")
API = (BACKEND / "requirements_api.py").read_text(encoding="utf-8")
MAIN = (BACKEND / "main.py").read_text(encoding="utf-8")


def test_requirements_routes_keep_blocking_work_off_the_event_loop():
    # The runner owns two dedicated thread-pool executors and dispatches every
    # blocking call through run_in_executor — never inline on the event loop.
    assert "REQUIREMENTS_PROCESSOR_EXECUTOR = ThreadPoolExecutor(" in RUNNER
    assert "REQUIREMENTS_SEARCH_EXECUTOR = ThreadPoolExecutor(" in RUNNER
    assert "run_in_executor(executor, _call)" in RUNNER
    assert "executor=REQUIREMENTS_SEARCH_EXECUTOR," in RUNNER

    # The requirement endpoints delegate to the runner with those executors and
    # its metric labels, rather than blocking inline.
    assert "from requirements_query_runner import" in API
    assert '"requirements.processed.processor"' in API
    assert '"requirements.processed.finalize"' in API
    assert '"requirements.search"' in API
    assert "executor=REQUIREMENTS_PROCESSOR_EXECUTOR," in API
    assert "executor=REQUIREMENTS_SEARCH_EXECUTOR," in API
    assert "processor_failure_result(exc)" in API

    # main.py only imports the extracted runner; the old inline blocking calls
    # (private executor + direct requirement_context to_thread) are gone.
    assert "from requirements_query_runner import (" in MAIN
    assert "run_requirements_processor_query," in MAIN
    assert "_REQUIREMENTS_QUERY_EXECUTOR" not in MAIN
    assert "_run_requirements_query" not in MAIN
    assert "asyncio.to_thread(\n        requirement_context.get_processed_requirements," not in MAIN
    assert "asyncio.to_thread(\n        requirement_context.search_requirements," not in MAIN
