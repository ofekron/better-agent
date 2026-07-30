"""SessionMiddleware whose signing key tracks `auth.get_session_secret()`
live, instead of Starlette's default of baking `secret_key` into a signer
once at construction time.

Starlette's `SessionMiddleware.__call__` only ever reads `self.signer`
(never reassigns it after `__init__`), so overriding `signer` as a
property that re-derives the key on every access is a safe, minimal way to
make session-cookie signing honor secret rotation (see
`auth_secrets.write_login_credentials`) without a process restart. Without
this, rotating the keychain secret on password change would invalidate
bearer tokens (which already call `auth.get_session_secret()` per use) but
leave every existing browser session cookie — signed by the process-startup
secret baked into the stock middleware — valid until its own expiry.
"""

import itsdangerous
from starlette.middleware.sessions import SessionMiddleware

import auth


class DynamicSecretSessionMiddleware(SessionMiddleware):
    def __init__(self, app, **kwargs) -> None:
        super().__init__(app, secret_key=auth.get_session_secret(), **kwargs)

    @property
    def signer(self) -> itsdangerous.TimestampSigner:
        return itsdangerous.TimestampSigner(str(auth.get_session_secret()))

    @signer.setter
    def signer(self, _value: itsdangerous.TimestampSigner) -> None:
        pass  # SessionMiddleware.__init__ assigns once; the property above wins on every read.
