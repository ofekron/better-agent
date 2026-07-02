"""HMAC-signed, expiring, directory-scoped URLs for the HTML browser
preview.

The preview iframe runs in an opaque origin (iframe + CSP sandbox), so
its asset subrequests cannot carry the SameSite session cookie. These
URLs are the credential instead: an authenticated endpoint mints a URL
whose signature binds (node_id, the HTML file's directory, expiry).
Any file under that directory is readable until expiry; nothing else.
"""

import hashlib
import hmac
import posixpath
import time

TTL_SECONDS = 6 * 3600


def _key() -> bytes:
    import auth
    return hashlib.sha256(f"file-preview:{auth.get_session_secret()}".encode()).digest()


def _sig(node_id: str, root_dir: str, exp: int) -> str:
    msg = f"{node_id}|{root_dir}|{exp}".encode()
    return hmac.new(_key(), msg, hashlib.sha256).hexdigest()


def normalize(path: str) -> str:
    """Collapse the path and reject anything that is not a clean
    absolute path (traversal, relative paths)."""
    norm = posixpath.normpath(path)
    if not norm.startswith("/") or ".." in norm.split("/"):
        raise ValueError("invalid preview path")
    return norm


def mint(path: str, node_id: str) -> str:
    norm = normalize(path)
    root_dir = posixpath.dirname(norm) or "/"
    exp = int(time.time()) + TTL_SECONDS
    return f"/api/file/preview/{exp}.{_sig(node_id, root_dir, exp)}/{node_id}{norm}"


def verify(token: str, node_id: str, path: str) -> str:
    """Return the normalized path if the token authorizes it. The token
    signs one directory; the path is allowed when that directory is any
    of its ancestors, so relative assets in subfolders resolve while
    everything outside the signed root fails closed."""
    exp_raw, _, sig = token.partition(".")
    if not exp_raw.isdigit() or not sig:
        raise ValueError("malformed preview token")
    exp = int(exp_raw)
    if time.time() > exp:
        raise ValueError("expired preview token")
    norm = normalize(path)
    ancestor = posixpath.dirname(norm) or "/"
    while True:
        if hmac.compare_digest(sig, _sig(node_id, ancestor, exp)):
            return norm
        if ancestor == "/":
            raise ValueError("preview token does not cover path")
        ancestor = posixpath.dirname(ancestor)
