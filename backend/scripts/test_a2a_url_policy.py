"""Outbound URL policy for A2A base URLs: https required, plain http
allowed only for localhost/127.0.0.1, credentials/query/fragment
rejected fail-closed."""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest

from a2a.url_policy import A2AUrlPolicyError, validate_base_url


def test_https_remote_host_accepted():
    assert validate_base_url("https://agent.example.com/a2a") == "https://agent.example.com/a2a"


def test_http_localhost_accepted():
    assert validate_base_url("http://localhost:8080") == "http://localhost:8080"
    assert validate_base_url("http://127.0.0.1:8080/x/") == "http://127.0.0.1:8080/x"


@pytest.mark.parametrize(
    "url",
    [
        "http://agent.example.com",
        "http://10.0.0.5:9000",
        "http://192.168.1.5",
    ],
)
def test_http_remote_host_rejected(url):
    with pytest.raises(A2AUrlPolicyError):
        validate_base_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://agent.example.com",
        "https://",
        "https://user:pass@agent.example.com",
        "https://agent.example.com?x=1",
        "https://agent.example.com#frag",
    ],
)
def test_malformed_urls_rejected(url):
    with pytest.raises(A2AUrlPolicyError):
        validate_base_url(url)
