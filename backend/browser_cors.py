from __future__ import annotations

import functools

from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import PlainTextResponse, Response
from starlette.types import Message, Send

import browser_trust


class BrowserTrustCORSMiddleware(CORSMiddleware):
    def _request_origin_allowed(self, request_headers: Headers) -> bool:
        origin = request_headers.get("origin")
        host = browser_trust.effective_host_header(request_headers)
        if origin is None or not host:
            return False
        if self.is_allowed_origin(origin):
            return True
        return browser_trust.is_cors_origin_allowed(origin, host)

    def preflight_response(self, request_headers: Headers) -> Response:
        requested_origin = request_headers["origin"]
        requested_method = request_headers["access-control-request-method"]
        requested_headers = request_headers.get("access-control-request-headers")
        requested_private_network = request_headers.get("access-control-request-private-network")

        headers = dict(self.preflight_headers)
        failures: list[str] = []

        if self._request_origin_allowed(request_headers):
            if self.preflight_explicit_allow_origin:
                headers["Access-Control-Allow-Origin"] = requested_origin
        else:
            failures.append("origin")

        if requested_method not in self.allow_methods:
            failures.append("method")

        if self.allow_all_headers and requested_headers is not None:
            headers["Access-Control-Allow-Headers"] = requested_headers
        elif requested_headers is not None:
            for header in [h.lower() for h in requested_headers.split(",")]:
                if header.strip() not in self.allow_headers:
                    failures.append("headers")
                    break

        if requested_private_network is not None:
            if self.allow_private_network:
                headers["Access-Control-Allow-Private-Network"] = "true"
            else:
                failures.append("private-network")

        if failures:
            failure_text = "Disallowed CORS " + ", ".join(failures)
            return PlainTextResponse(failure_text, status_code=400, headers=headers)

        return PlainTextResponse("OK", status_code=200, headers=headers)

    async def simple_response(self, scope, receive, send: Send, request_headers: Headers) -> None:
        send = functools.partial(self.send, send=send, request_headers=request_headers)
        await self.app(scope, receive, send)

    async def send(self, message: Message, send: Send, request_headers: Headers) -> None:
        if message["type"] != "http.response.start":
            await send(message)
            return

        message.setdefault("headers", [])
        headers = MutableHeaders(scope=message)
        headers.update(self.simple_headers)
        origin = request_headers["Origin"]
        has_cookie = "cookie" in request_headers

        if self.allow_all_origins and has_cookie:
            self.allow_explicit_origin(headers, origin)
        elif not self.allow_all_origins and self._request_origin_allowed(request_headers):
            self.allow_explicit_origin(headers, origin)

        await send(message)
