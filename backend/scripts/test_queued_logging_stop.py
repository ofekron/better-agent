"""Dedicated coverage for queued_logging.stop_queued_logging — the
drain-and-join shutdown path the blocking-regression test skips (it manually
stops only its own listener to avoid stopping unrelated listeners)."""
from __future__ import annotations

import logging

import queued_logging


def _restore_listeners(snapshot: list[logging.handlers.QueueListener]) -> None:
    queued_logging._LOG_QUEUE_LISTENERS[:] = snapshot


def _install_capturing_logger(messages: list[str]) -> logging.Logger:
    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    logger = logging.getLogger(f"test.queued_logging_stop.{id(messages)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    queued_logging.install_queued_logging(logger, CaptureHandler())
    return logger


def test_stop_drains_pending_records_and_joins_listener_thread() -> None:
    snapshot = list(queued_logging._LOG_QUEUE_LISTENERS)
    try:
        messages: list[str] = []
        logger = _install_capturing_logger(messages)
        listener = queued_logging._LOG_QUEUE_LISTENERS[-1]
        assert listener._thread.is_alive()  # type: ignore[attr-defined]

        # Record enqueued but not yet drained when stop is called.
        logger.warning("drain-before-stop")
        queued_logging.stop_queued_logging()

        assert "drain-before-stop" in messages, "stop did not flush the queue to the handler"
        assert listener._thread is None  # type: ignore[attr-defined]  # stop() joins then clears
    finally:
        _restore_listeners(snapshot)


def test_stop_joins_every_listener_when_multiple_installed() -> None:
    snapshot = list(queued_logging._LOG_QUEUE_LISTENERS)
    try:
        first: list[str] = []
        second: list[str] = []
        _install_capturing_logger(first)
        _install_capturing_logger(second)
        listeners = list(queued_logging._LOG_QUEUE_LISTENERS)
        assert len(listeners) >= 2

        # Address each logger through its own QueueHandler. A fresh logger
        # only has the one handler install_queued_logging attached.
        logging.getLogger(f"test.queued_logging_stop.{id(first)}").error("one")
        logging.getLogger(f"test.queued_logging_stop.{id(second)}").error("two")

        queued_logging.stop_queued_logging()

        assert "one" in first, "first listener's queue was not drained"
        assert "two" in second, "second listener's queue was not drained"
        assert all(li._thread is None for li in listeners)  # type: ignore[attr-defined]
    finally:
        _restore_listeners(snapshot)


def test_stop_is_noop_when_no_listeners_installed() -> None:
    snapshot = list(queued_logging._LOG_QUEUE_LISTENERS)
    queued_logging._LOG_QUEUE_LISTENERS.clear()
    try:
        queued_logging.stop_queued_logging()  # must not raise on empty list
    finally:
        _restore_listeners(snapshot)
