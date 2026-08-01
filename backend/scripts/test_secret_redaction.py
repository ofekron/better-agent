from __future__ import annotations

import logging

import pytest

import secret_redaction as sr


@pytest.fixture
def access_logger_reset():
    logger = logging.getLogger("uvicorn.access")
    original_filters = list(logger.filters)
    yield logger
    logger.filters = original_filters


# --- redact_secrets ----------------------------------------------------------

def test_redact_query_token():
    # secret value is gone; the named-secret pass also swallows the trailing
    # &keep=1 (no space to bound it) — over-redaction is safe for a filter.
    assert sr.redact_secrets("https://x/api?token=sekret&keep=1") == "https://x/api?token=[REDACTED]"
    assert "sekret" not in sr.redact_secrets("https://x/api?token=sekret&keep=1")


def test_redact_query_access_and_refresh_and_ticket_case_insensitive():
    out = sr.redact_secrets("?access_token=aaa&refresh_token=bbb&TICKET=ccc")
    assert "aaa" not in out and "bbb" not in out and "ccc" not in out
    # named-secret pass greedily swallows the unbounded tail (no space to bound it)
    assert out == "?access_token=[REDACTED]"


def test_redact_path_preview_secret():
    assert sr.redact_secrets("/api/file/preview/SECRETUUID") == "/api/file/preview/[REDACTED]"
    assert sr.redact_secrets("/api/task-output/preview/SECRETUUID") == "/api/task-output/preview/[REDACTED]"


def test_redact_bearer_token():
    assert sr.redact_secrets("Authorization: Bearer abc.def._~+/=-") == "Authorization: Bearer [REDACTED]"


def test_redact_named_secret_assignment():
    assert sr.redact_secrets("token: sekret access_token=other; refresh_token=x") == "token: [REDACTED] access_token=[REDACTED]; refresh_token=[REDACTED]"


def test_redact_passthrough_when_no_secret():
    assert sr.redact_secrets("nothing to see here") == "nothing to see here"


# --- SecretRedactionFilter ---------------------------------------------------

def test_filter_redacts_msg_without_args():
    filt = sr.SecretRedactionFilter()
    record = logging.LogRecord(
        name="x", level=logging.INFO, pathname="", lineno=0,
        msg="token=sekret", args=None, exc_info=None,
    )
    assert filt.filter(record) is True
    assert record.msg == "token=[REDACTED]"
    assert record.args is None


def test_filter_redacts_string_args_and_preserves_non_string_args():
    filt = sr.SecretRedactionFilter()
    record = logging.LogRecord(
        name="x", level=logging.INFO, pathname="", lineno=0,
        msg="hello", args=("token=sekret", 42, None), exc_info=None,
    )
    assert filt.filter(record) is True
    # string arg redacted, non-string args untouched
    assert record.args[0] == "token=[REDACTED]"
    assert record.args[1] == 42
    assert record.args[2] is None


# --- install_access_log_redaction --------------------------------------------

def test_install_adds_single_filter_then_idempotent(access_logger_reset):
    logger = access_logger_reset
    assert not any(isinstance(f, sr.SecretRedactionFilter) for f in logger.filters)

    sr.install_access_log_redaction()
    assert sum(isinstance(f, sr.SecretRedactionFilter) for f in logger.filters) == 1

    # second call must be a no-op (early-return), not a second filter
    sr.install_access_log_redaction()
    assert sum(isinstance(f, sr.SecretRedactionFilter) for f in logger.filters) == 1
