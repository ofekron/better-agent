from __future__ import annotations

import contextlib
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import _test_home


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP_HOME = _test_home.isolate("bc-test-provider-cache-concurrency-")

import _test_installation  # noqa: E402


_test_installation.activate(Path(_TMP_HOME))

import provider  # noqa: E402


class _Authority:
    def __init__(self, records: dict[str, dict]) -> None:
        self._records = records
        self._lock = threading.RLock()

    def get_with_key(self, provider_id: str) -> dict | None:
        with self._lock:
            record = self._records.get(provider_id)
            if record is None or record.get("suspended") is True:
                return None
            return dict(record)

    def suspended(self, provider_id: str) -> bool:
        with self._lock:
            record = self._records.get(provider_id)
            return bool(record and record.get("suspended") is True)

    @contextlib.contextmanager
    def read_transaction(self):
        with self._lock:
            yield

    def apply_if_matches(self, provider_id, predicate, apply) -> bool:
        with self._lock:
            record = self._records.get(provider_id)
            if record is None or not predicate(dict(record)):
                return False
            apply()
            return True

    def replace(self, provider_id: str, record: dict | None) -> None:
        with self._lock:
            if record is None:
                self._records.pop(provider_id, None)
                return
            self._records[provider_id] = record


class _ProbeProvider:
    constructed = 0
    activated = 0
    deactivated = 0
    construct_hook = None
    activation_error: BaseException | None = None

    def __init__(self, record: dict) -> None:
        type(self).constructed += 1
        hook = type(self).construct_hook
        if hook is not None:
            hook()
        self.id = record["id"]
        self.record = dict(record)
        self.defunct = False
        self.suspended = False
        self._cache_active = False

    def active_runs(self) -> list[dict]:
        return []

    def _activate_cache(self) -> None:
        if self._cache_active:
            return
        error = type(self).activation_error
        if error is not None:
            type(self).activation_error = None
            raise error
        type(self).activated += 1
        self._cache_active = True

    def _deactivate_cache(self) -> None:
        if not self._cache_active:
            return
        type(self).deactivated += 1
        self._cache_active = False


def _record(provider_id: str, kind: str = "claude", **updates) -> dict:
    return {
        "id": provider_id,
        "generation": f"generation-{provider_id}",
        "revision": 0,
        "execution_revision": 0,
        "kind": kind,
        "mode": "subscription",
        "runner": "native",
        **updates,
    }


@pytest.fixture(autouse=True)
def _clear_cache():
    provider._PROVIDER_CACHE.clear()
    key_locks = getattr(provider, "_PROVIDER_KEY_LOCKS", None)
    if key_locks is not None:
        key_locks.clear()
    yield
    provider._PROVIDER_CACHE.clear()
    if key_locks is not None:
        assert key_locks == {}


def _install_authority(monkeypatch, authority: _Authority, classes: dict[str, type]) -> None:
    monkeypatch.setattr(provider.config_store, "get_provider_with_key", authority.get_with_key)
    monkeypatch.setattr(provider.config_store, "provider_suspended", authority.suspended)
    monkeypatch.setattr(
        provider.config_store,
        "provider_state_read_transaction",
        authority.read_transaction,
    )
    monkeypatch.setattr(
        provider.config_store,
        "apply_if_provider_matches",
        authority.apply_if_matches,
    )
    monkeypatch.setattr(provider, "_resolve_class", classes.__getitem__)


def test_slow_provider_key_does_not_block_another_provider(monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()

    class Slow(_ProbeProvider):
        construct_hook = staticmethod(lambda: (entered.set(), release.wait()))

    class Fast(_ProbeProvider):
        pass

    authority = _Authority({"slow": _record("slow"), "fast": _record("fast", "codex")})
    _install_authority(monkeypatch, authority, {"claude": Slow, "codex": Fast})

    with ThreadPoolExecutor(max_workers=2) as executor:
        slow = executor.submit(provider.get_provider, "slow", "native")
        assert entered.wait(1)
        fast = executor.submit(provider.get_provider, "fast", "native")
        try:
            assert isinstance(fast.result(timeout=1), Fast)
        finally:
            release.set()
        assert isinstance(slow.result(timeout=1), Slow)


def test_slow_runner_key_does_not_block_another_runner(monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()

    class Slow(_ProbeProvider):
        construct_hook = staticmethod(lambda: (entered.set(), release.wait()))

    class Fast(_ProbeProvider):
        pass

    record = _record("shared", "fugu", mode="api_key")
    authority = _Authority({"shared": record})
    _install_authority(monkeypatch, authority, {"fugu": Slow, "openai": Fast})

    with ThreadPoolExecutor(max_workers=2) as executor:
        slow = executor.submit(provider.get_provider, "shared", "native")
        assert entered.wait(1)
        fast = executor.submit(
            provider.get_provider,
            "shared",
            "better_agent_runner",
        )
        try:
            assert isinstance(fast.result(timeout=1), Fast)
        finally:
            release.set()
        assert isinstance(slow.result(timeout=1), Slow)


def test_same_key_constructs_and_activates_once(monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    second_started = threading.Event()

    class Probe(_ProbeProvider):
        construct_hook = staticmethod(lambda: (entered.set(), release.wait()))

    authority = _Authority({"same": _record("same")})
    _install_authority(monkeypatch, authority, {"claude": Probe})

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(provider.get_provider, "same", "native")
        assert entered.wait(1)

        def second_lookup():
            second_started.set()
            return provider.get_provider("same", "native")

        second = executor.submit(second_lookup)
        assert second_started.wait(1)
        release.set()
        first_instance = first.result(timeout=1)
        second_instance = second.result(timeout=1)

    assert first_instance is second_instance
    assert Probe.constructed == 1
    assert Probe.activated == 1


@pytest.mark.parametrize("next_record", [None, {"suspended": True}])
def test_unavailable_authority_during_construction_is_not_published(
    monkeypatch,
    next_record,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class Probe(_ProbeProvider):
        construct_hook = staticmethod(lambda: (entered.set(), release.wait()))

    initial = _record("changing")
    authority = _Authority({"changing": initial})
    _install_authority(monkeypatch, authority, {"claude": Probe})

    with ThreadPoolExecutor(max_workers=1) as executor:
        lookup = executor.submit(provider.get_provider, "changing", "native")
        assert entered.wait(1)
        authority.replace(
            "changing",
            None if next_record is None else {**initial, **next_record},
        )
        release.set()
        with pytest.raises((KeyError, provider.ProviderSuspendedError)):
            lookup.result(timeout=1)

    assert ("changing", "native") not in provider._PROVIDER_CACHE
    assert Probe.activated == 0


def test_revision_change_during_construction_retries_current_authority(monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()

    class Probe(_ProbeProvider):
        @staticmethod
        def construct_hook():
            if Probe.constructed == 1:
                entered.set()
                release.wait()

    initial = _record("changing")
    authority = _Authority({"changing": initial})
    _install_authority(monkeypatch, authority, {"claude": Probe})

    with ThreadPoolExecutor(max_workers=1) as executor:
        lookup = executor.submit(provider.get_provider, "changing", "native")
        assert entered.wait(1)
        authority.replace("changing", {**initial, "revision": 1})
        release.set()
        instance = lookup.result(timeout=1)

    assert instance.record["revision"] == 1
    assert Probe.constructed == 2
    assert Probe.activated == 1


def test_constructor_and_activation_failure_do_not_poison_retry(monkeypatch) -> None:
    class Probe(_ProbeProvider):
        failures = [RuntimeError("construction failed")]

        @staticmethod
        def construct_hook():
            if Probe.failures:
                raise Probe.failures.pop()

    authority = _Authority({"retry": _record("retry")})
    _install_authority(monkeypatch, authority, {"claude": Probe})

    with pytest.raises(RuntimeError, match="construction failed"):
        provider.get_provider("retry", "native")
    assert provider._PROVIDER_CACHE == {}
    assert getattr(provider, "_PROVIDER_KEY_LOCKS", {}) == {}

    Probe.activation_error = RuntimeError("activation failed")
    with pytest.raises(RuntimeError, match="activation failed"):
        provider.get_provider("retry", "native")
    assert provider._PROVIDER_CACHE == {}
    assert getattr(provider, "_PROVIDER_KEY_LOCKS", {}) == {}

    instance = provider.get_provider("retry", "native")
    assert provider.get_provider("retry", "native") is instance
    assert Probe.activated == 1


def test_key_wait_telemetry_failure_releases_key_lock(monkeypatch) -> None:
    class Probe(_ProbeProvider):
        pass

    authority = _Authority({"retry": _record("retry")})
    _install_authority(monkeypatch, authority, {"claude": Probe})
    original_record = provider.perf.record

    def fail_key_wait(name, value) -> None:
        if name == "provider.resolve.key_wait":
            raise RuntimeError("telemetry failed")
        original_record(name, value)

    monkeypatch.setattr(provider.perf, "record", fail_key_wait)

    with pytest.raises(RuntimeError, match="telemetry failed"):
        provider.get_provider("retry", "native")
    assert provider._PROVIDER_KEY_LOCKS == {}
    monkeypatch.setattr(provider.perf, "record", original_record)
    assert isinstance(provider.get_provider("retry", "native"), Probe)


def test_active_run_inspection_failure_preserves_cached_instance(monkeypatch) -> None:
    class Existing(_ProbeProvider):
        def active_runs(self):
            raise RuntimeError("run state unavailable")

    class Replacement(_ProbeProvider):
        pass

    authority = _Authority({"replace": _record("replace")})
    _install_authority(monkeypatch, authority, {"claude": Replacement})
    existing = Existing(_record("replace"))
    provider._PROVIDER_CACHE[("replace", "native")] = existing

    with pytest.raises(RuntimeError, match="run state unavailable"):
        provider.get_provider("replace", "native")
    assert provider._PROVIDER_CACHE[("replace", "native")] is existing
    assert Replacement.constructed == 0


def test_class_replacement_hands_off_cache_resources_transactionally(monkeypatch) -> None:
    class Existing(_ProbeProvider):
        pass

    class Replacement(_ProbeProvider):
        pass

    authority = _Authority({"replace": _record("replace")})
    _install_authority(monkeypatch, authority, {"claude": Replacement})
    existing = Existing(_record("replace"))
    existing._activate_cache()
    provider._PROVIDER_CACHE[("replace", "native")] = existing

    Replacement.activation_error = RuntimeError("activation failed")
    with pytest.raises(RuntimeError, match="activation failed"):
        provider.get_provider("replace", "native")
    assert provider._PROVIDER_CACHE[("replace", "native")] is existing
    assert existing._cache_active is True
    assert Existing.deactivated == 0

    replacement = provider.get_provider("replace", "native")
    assert isinstance(replacement, Replacement)
    assert provider._PROVIDER_CACHE[("replace", "native")] is replacement
    assert replacement._cache_active is True
    assert existing._cache_active is False
    assert Existing.deactivated == 1


def test_class_replacement_deactivation_failure_restores_old_cache(monkeypatch) -> None:
    class Existing(_ProbeProvider):
        failures = 1

        def _deactivate_cache(self) -> None:
            if type(self).failures:
                type(self).failures -= 1
                raise RuntimeError("deactivation failed")
            super()._deactivate_cache()

    class Replacement(_ProbeProvider):
        pass

    authority = _Authority({"replace": _record("replace")})
    _install_authority(monkeypatch, authority, {"claude": Replacement})
    existing = Existing(_record("replace"))
    existing._activate_cache()
    provider._PROVIDER_CACHE[("replace", "native")] = existing

    with pytest.raises(RuntimeError, match="deactivation failed"):
        provider.get_provider("replace", "native")
    assert provider._PROVIDER_CACHE[("replace", "native")] is existing
    assert existing._cache_active is True
    assert Replacement.activated == 1
    assert Replacement.deactivated == 1


def test_deleted_provider_deactivates_every_cached_runner(monkeypatch) -> None:
    class Probe(_ProbeProvider):
        pass

    authority = _Authority({"multi": _record("multi", "fugu", mode="api_key")})
    _install_authority(monkeypatch, authority, {"fugu": Probe, "openai": Probe})
    native = provider.get_provider("multi", "native")
    better_agent = provider.get_provider("multi", "better_agent_runner")
    authority.replace("multi", None)

    assert provider.get_provider("multi") in {native, better_agent}
    assert native.defunct is True
    assert better_agent.defunct is True
    assert native._cache_active is False
    assert better_agent._cache_active is False
    assert Probe.deactivated == 2


def test_deleted_provider_transitions_later_runners_after_cleanup_failure(
    monkeypatch,
) -> None:
    class Probe(_ProbeProvider):
        fail_deactivation = False

        def _deactivate_cache(self) -> None:
            if self.fail_deactivation:
                raise RuntimeError("cleanup failed")
            super()._deactivate_cache()

    authority = _Authority({"multi": _record("multi", "fugu", mode="api_key")})
    _install_authority(monkeypatch, authority, {"fugu": Probe, "openai": Probe})
    native = provider.get_provider("multi", "native")
    better_agent = provider.get_provider("multi", "better_agent_runner")
    native.fail_deactivation = True
    authority.replace("multi", None)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        provider.get_provider("multi")
    assert native.defunct is True
    assert better_agent.defunct is True
    assert better_agent._cache_active is False
    assert Probe.deactivated == 1


def test_claude_cache_gauges_are_runner_scoped(monkeypatch) -> None:
    import provider_claude

    gauges: dict[str, object] = {}
    monkeypatch.setattr(
        provider_claude.perf,
        "register_queue",
        lambda name, gauge: gauges.__setitem__(name, gauge),
    )
    monkeypatch.setattr(
        provider_claude.perf,
        "unregister_queue",
        lambda name: gauges.pop(name, None),
    )
    base = _record("claude-account")
    native = provider_claude.ClaudeProvider(base)
    better_agent = provider_claude.ClaudeProvider(
        {**base, "runner": "better_agent_runner"}
    )

    assert gauges == {}
    native._activate_cache()
    better_agent._activate_cache()
    assert native._perf_gauge_name != better_agent._perf_gauge_name
    assert set(gauges) == {
        native._perf_gauge_name,
        better_agent._perf_gauge_name,
    }
    native._deactivate_cache()
    assert set(gauges) == {better_agent._perf_gauge_name}
    better_agent._deactivate_cache()
    assert gauges == {}


def test_base_cache_activation_compensates_partial_resource_failure(monkeypatch) -> None:
    import provider_claude

    instance = provider_claude.ClaudeProvider(_record("partial"))
    resources: set[str] = set()

    def partial_activation() -> None:
        resources.add("registered")
        raise RuntimeError("activation failed")

    monkeypatch.setattr(instance, "_activate_cache_resources", partial_activation)
    monkeypatch.setattr(
        instance,
        "_deactivate_cache_resources",
        lambda: resources.discard("registered"),
    )

    with pytest.raises(RuntimeError, match="activation failed"):
        instance._activate_cache()
    assert resources == set()
    assert instance._cache_active is False


def test_replacement_uses_base_deactivation_rollback(monkeypatch) -> None:
    import provider_claude

    class Replacement(_ProbeProvider):
        pass

    authority = _Authority({"replace": _record("replace")})
    _install_authority(monkeypatch, authority, {"claude": Replacement})
    existing = provider_claude.ClaudeProvider(_record("replace"))
    resources = {"old"}

    def restore_old() -> None:
        resources.add("old")

    def fail_old_deactivation() -> None:
        resources.discard("old")
        raise RuntimeError("deactivation failed")

    monkeypatch.setattr(existing, "_activate_cache_resources", restore_old)
    monkeypatch.setattr(
        existing,
        "_deactivate_cache_resources",
        fail_old_deactivation,
    )
    existing._cache_active = True
    provider._PROVIDER_CACHE[("replace", "native")] = existing

    with pytest.raises(RuntimeError, match="deactivation failed"):
        provider.get_provider("replace", "native")
    assert provider._PROVIDER_CACHE[("replace", "native")] is existing
    assert existing._cache_active is True
    assert resources == {"old"}
    assert Replacement.deactivated == 1


def test_defunct_resurrection_reactivates_once(monkeypatch) -> None:
    class Probe(_ProbeProvider):
        pass

    initial = _record("resurrect")
    authority = _Authority({"resurrect": initial})
    _install_authority(monkeypatch, authority, {"claude": Probe})

    instance = provider.get_provider("resurrect", "native")
    assert provider.get_provider("resurrect", "native") is instance
    authority.replace("resurrect", None)
    assert provider.get_provider("resurrect", "native") is instance
    assert instance.defunct is True
    authority.replace("resurrect", {**initial, "revision": 1})
    assert provider.get_provider("resurrect", "native") is instance
    assert instance.defunct is False
    assert Probe.activated == 2
    assert Probe.deactivated == 1
