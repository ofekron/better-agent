"""HTTP sink executor — consuming, TLS-only.

The broker substitutes the secret into the frozen template and performs the
HTTPS request itself. The secret never leaves broker memory except as TLS
ciphertext to the pinned host. The destination is the descriptor's own
``url_template`` host — never anything the caller supplied at execute time.
"""

from __future__ import annotations

import http.client
import socket
import urllib.error
import urllib.parse
import urllib.request

from credential_broker.descriptor import coerce_secret_map, substitute_secrets
from credential_broker.executors.base import ExecResult, SinkExecutor
from ssrf_guard import SSRFBlockedError, resolve_safe_ip

_TIMEOUT_S = 30
_MAX_BODY = 256 * 1024  # cap the response we read back


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that connects to a pre-resolved, vetted IP.

    Resolution and validation happen inside connect(), and the exact
    address returned is the one the socket connects to — no separate
    check-then-connect DNS window an attacker could rebind between.
    TLS still verifies against ``self.host`` (SNI + certificate hostname),
    so a mismatched pinned IP fails the handshake instead of connecting.
    """

    def connect(self):
        ip = resolve_safe_ip(self.host, self.port)
        sock = socket.create_connection(
            (ip, self.port), self.timeout, self.source_address
        )
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PinnedHTTPSConnection, req, context=self._context)


_opener = urllib.request.build_opener(_PinnedHTTPSHandler())


class HttpExecutor(SinkExecutor):
    kind = "http"

    def execute(self, descriptor: dict, secret: str | dict[str, str]) -> ExecResult:
        secrets = coerce_secret_map(secret)
        sink = descriptor["sink"]
        url = substitute_secrets(sink["url_template"], secrets)

        query = {
            k: substitute_secrets(v, secrets)
            for k, v in sink.get("query", {}).items()
        }
        if query:
            sep = "&" if urllib.parse.urlparse(url).query else "?"
            url = url + sep + urllib.parse.urlencode(query)

        # Hard guarantee: never send a secret over plaintext.
        if not url.lower().startswith("https://"):
            return ExecResult(ok=False, error="refused: non-https destination")

        headers = {
            k: substitute_secrets(v, secrets)
            for k, v in sink.get("headers", {}).items()
        }
        body = sink.get("body", "")
        data = substitute_secrets(body, secrets).encode("utf-8") if body else None

        req = urllib.request.Request(
            url, data=data, headers=headers, method=sink["method"]
        )
        try:
            with _opener.open(req, timeout=_TIMEOUT_S) as resp:
                raw = resp.read(_MAX_BODY)
                return ExecResult(
                    ok=True,
                    status=resp.status,
                    body=raw.decode("utf-8", errors="replace"),
                )
        except SSRFBlockedError as e:
            return ExecResult(ok=False, error=str(e))
        except urllib.error.HTTPError as e:
            raw = b""
            try:
                raw = e.read(_MAX_BODY)
            except Exception:
                pass
            return ExecResult(
                ok=False,
                status=e.code,
                body=raw.decode("utf-8", errors="replace"),
                error=f"http {e.code}",
            )
        except urllib.error.URLError as e:
            # Reason strings can echo the URL (which may contain the secret if
            # the provider templated it into the path). The output guard
            # scrubs this, but keep it terse.
            return ExecResult(ok=False, error=f"request failed: {e.reason}")
