"""Pure-logic / security / tmpdir coverage for containment.py.

The cgroup-discovery happy paths and the Linux factory fallbacks live in
test_containment_cgroup_discovery.py; the live POSIX contract (enumerate,
reparented orphan) in test_containment_contract.py. This file closes the
remaining surface that needs no real cgroup kernel API and no Windows
ctypes: mount-path unescaping, run_id path-traversal rejection, the
discovery edge/security branches, has_background_work failure semantics,
Linux-class file ops driven through a tmpdir cgroup root, the POSIX
best-effort reattach/force_kill paths, _is_wsl, and the macOS factory.

No real state home is touched (containment.py never reads BETTER_AGENT_HOME).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import containment  # noqa: E402


# ----------------------------------------------------- _unescape_mountinfo_path


def test_unescape_mountinfo_path_all_escape_codes():
    # space is exercised transitively by the discovery tests; cover the
    # remaining three escape codes directly.
    assert containment._unescape_mountinfo_path("a\\040b") == "a b"
    assert containment._unescape_mountinfo_path("a\\011b") == "a\tb"
    assert containment._unescape_mountinfo_path("a\\012b") == "a\nb"
    assert containment._unescape_mountinfo_path("a\\134b") == "a\\b"
    # unescaped text passes through unchanged.
    assert containment._unescape_mountinfo_path("plain/path") == "plain/path"


# -------------------------------------------------------- _dir path-traversal


def test_linux_dir_rejects_path_traversal_run_ids():
    inst = containment._LinuxCgroupContainment(cgroup_directory="/cg")
    for bad in ("../etc", ".", "..", "", "a/b", "run/../x"):
        try:
            inst._dir(bad)
        except containment.ContainmentUnavailable:
            continue
        raise AssertionError(f"unsafe run_id {bad!r} must be rejected")


def test_linux_dir_accepts_safe_run_id():
    inst = containment._LinuxCgroupContainment(cgroup_directory="/cg/session")
    assert inst._dir("run-42") == "/cg/session/better-agent/run-42"


# ------------------------------------------------- _can_create_run_group branches


def test_can_create_run_group_missing_base_checks_parent_writable(tmp_path):
    # base absent -> defer to parent dir writability (True for tmp_path).
    inst = containment._LinuxCgroupContainment(cgroup_directory=str(tmp_path))
    assert inst._can_create_run_group() is True


def test_can_create_run_group_missing_base_readonly_parent(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir(mode=0o500)
    try:
        inst = containment._LinuxCgroupContainment(cgroup_directory=str(ro))
        # parent of <ro>/better-agent is <ro> which is not writable.
        assert inst._can_create_run_group() is False
    finally:
        ro.chmod(0o700)


def test_can_create_run_group_owned_dir_true(tmp_path):
    (tmp_path / "better-agent").mkdir(mode=0o700)
    inst = containment._LinuxCgroupContainment(cgroup_directory=str(tmp_path))
    assert inst._can_create_run_group() is True


def test_can_create_run_group_base_is_not_a_dir(tmp_path):
    # base exists but is a file -> S_ISDIR False -> returns False.
    (tmp_path / "better-agent").write_text("x", encoding="utf-8")
    inst = containment._LinuxCgroupContainment(cgroup_directory=str(tmp_path))
    assert inst._can_create_run_group() is False


# ------------------------------------------------ v2 discovery edge + security


def _write_v2_fixture(tmp_path, *, mount_root, membership, pid=123,
                      mount_name="unified", procs="123\n",
                      controllers=True, cgroup_type=True):
    mount_point = tmp_path / mount_name
    relative = membership.removeprefix(mount_root).lstrip("/") or "."
    process_directory = mount_point if relative == "." else mount_point / relative
    process_directory.mkdir(parents=True, exist_ok=True)
    (process_directory / "cgroup.procs").write_text(procs, encoding="ascii")
    if controllers:
        (process_directory / "cgroup.controllers").write_text("cpu\n", encoding="ascii")
    if cgroup_type:
        (process_directory / "cgroup.type").write_text("domain\n", encoding="ascii")
    mountinfo = tmp_path / f"{mount_name}.mountinfo"
    cgroup = tmp_path / f"{mount_name}.cgroup"
    mountinfo.write_text(
        f"36 25 0:32 {mount_root} {mount_point} rw - cgroup2 cgroup2 rw\n",
        encoding="utf-8",
    )
    cgroup.write_text(f"0::{membership}\n", encoding="ascii")
    return mountinfo, cgroup


def test_v2_rejects_when_cgroup_controllers_missing(tmp_path):
    mountinfo, cgroup = _write_v2_fixture(
        tmp_path, mount_root="/", membership="/", controllers=False
    )
    try:
        containment._current_cgroup_v2_directory(str(mountinfo), str(cgroup), pid=123)
    except containment.ContainmentUnavailable:
        pass
    else:
        raise AssertionError("missing cgroup.controllers must be rejected")


def test_v2_rejects_when_cgroup_type_missing(tmp_path):
    mountinfo, cgroup = _write_v2_fixture(
        tmp_path, mount_root="/", membership="/", cgroup_type=False
    )
    try:
        containment._current_cgroup_v2_directory(str(mountinfo), str(cgroup), pid=123)
    except containment.ContainmentUnavailable:
        pass
    else:
        raise AssertionError("missing cgroup.type must be rejected")


def test_v2_skips_distractor_mount_then_succeeds(tmp_path):
    # A distractor cgroup2 mount whose root does not carry the membership,
    # followed by the real one, must skip the distractor and resolve the real.
    mount_point_real = tmp_path / "real"
    mount_point_real.mkdir()
    (mount_point_real / "cgroup.procs").write_text("123\n", encoding="ascii")
    (mount_point_real / "cgroup.controllers").write_text("cpu\n", encoding="ascii")
    (mount_point_real / "cgroup.type").write_text("domain\n", encoding="ascii")
    mountinfo = tmp_path / "mountinfo"
    cgroup = tmp_path / "cgroup"
    mountinfo.write_text(
        "36 25 0:32 /other /other mnt - cgroup2 cgroup2 rw\n"
        f"36 26 0:33 / {mount_point_real} rw - cgroup2 cgroup2 rw\n",
        encoding="utf-8",
    )
    cgroup.write_text("0::/\n", encoding="ascii")
    assert containment._current_cgroup_v2_directory(
        str(mountinfo), str(cgroup), pid=123
    ) == str(mount_point_real)


def test_v2_tolerates_non_integer_procs_entry(tmp_path):
    # A malformed (non-int) procs line -> ValueError -> skip that mount; here
    # it is the only mount so discovery fails closed.
    mountinfo, cgroup = _write_v2_fixture(
        tmp_path, mount_root="/", membership="/", procs="not-a-pid\n"
    )
    try:
        containment._current_cgroup_v2_directory(str(mountinfo), str(cgroup), pid=123)
    except containment.ContainmentUnavailable:
        pass
    else:
        raise AssertionError("malformed procs entry must be skipped (fail closed)")


# ------------------------------------------------ v1 discovery edge branches


def _write_v1_fixture(tmp_path, *, mount_root, membership, pid=123,
                      mount_name="pids", tasks="123\n", pids_current=True):
    mount_point = tmp_path / mount_name
    relative = membership.removeprefix(mount_root).lstrip("/") or "."
    process_directory = mount_point if relative == "." else mount_point / relative
    process_directory.mkdir(parents=True, exist_ok=True)
    (process_directory / "tasks").write_text(tasks, encoding="ascii")
    if pids_current:
        (process_directory / "pids.current").write_text("1\n", encoding="ascii")
    mountinfo = tmp_path / f"{mount_name}.mountinfo"
    cgroup = tmp_path / f"{mount_name}.cgroup"
    mountinfo.write_text(
        f"42 25 0:40 {mount_root} {mount_point} rw - cgroup cgroup rw,pids\n",
        encoding="utf-8",
    )
    cgroup.write_text(f"11:pids:{membership}\n0::/\n", encoding="ascii")
    return mountinfo, cgroup


def test_v1_rejects_when_no_pids_membership(tmp_path):
    mountinfo = tmp_path / "mountinfo"
    cgroup = tmp_path / "cgroup"
    mountinfo.write_text(
        f"42 25 0:40 / {tmp_path} rw - cgroup cgroup rw,pids\n", encoding="utf-8"
    )
    # No pids controller line in the cgroup file.
    cgroup.write_text("0::/\n", encoding="ascii")
    try:
        containment._current_cgroup_v1_pids_directory(str(mountinfo), str(cgroup))
    except containment.ContainmentUnavailable:
        pass
    else:
        raise AssertionError("host without a pids membership must fail closed")


def test_v1_rejects_when_pids_current_missing(tmp_path):
    mountinfo, cgroup = _write_v1_fixture(
        tmp_path, mount_root="/", membership="/", pids_current=False
    )
    try:
        containment._current_cgroup_v1_pids_directory(str(mountinfo), str(cgroup), pid=123)
    except containment.ContainmentUnavailable:
        pass
    else:
        raise AssertionError("missing pids.current must be rejected")


def test_v1_tolerates_non_integer_tasks_entry(tmp_path):
    mountinfo, cgroup = _write_v1_fixture(
        tmp_path, mount_root="/", membership="/", tasks="abc\n"
    )
    try:
        containment._current_cgroup_v1_pids_directory(str(mountinfo), str(cgroup), pid=123)
    except containment.ContainmentUnavailable:
        pass
    else:
        raise AssertionError("malformed tasks entry must be skipped (fail closed)")


def test_v1_skips_distractor_then_succeeds(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "tasks").write_text("123\n", encoding="ascii")
    (real / "pids.current").write_text("1\n", encoding="ascii")
    mountinfo = tmp_path / "mountinfo"
    cgroup = tmp_path / "cgroup"
    mountinfo.write_text(
        "42 25 0:40 /other /other mnt - cgroup cgroup rw,pids\n"
        f"42 26 0:41 / {real} rw - cgroup cgroup rw,pids\n",
        encoding="utf-8",
    )
    cgroup.write_text("11:pids:/\n0::/\n", encoding="ascii")
    assert containment._current_cgroup_v1_pids_directory(
        str(mountinfo), str(cgroup), pid=123
    ) == str(real)


# ------------------------------------------- has_background_work failure semantics


class _FakeContainment(containment.Containment):
    def __init__(self, members):
        self._members = list(members)

    def create(self, run_id): pass
    def spawn_kwargs(self, run_id): return {}
    def after_spawn(self, run_id, runner_pid): pass
    def reattach(self, run_id, runner_pid): pass
    def enumerate(self, run_id): return list(self._members)
    def teardown(self, run_id): pass
    def force_kill_all(self, run_id): return 0


def test_has_background_work_false_when_runner_pgid_unavailable(monkeypatch):
    inst = _FakeContainment([555])

    def _boom(_pid):
        raise ProcessLookupError("runner gone")

    monkeypatch.setattr(containment.os, "getpgid", _boom)
    assert inst.has_background_work("r", 1) is False


def test_has_background_work_skips_member_with_unavailable_pgid(monkeypatch):
    # Member list includes the runner pid itself -> the `pid == runner_pid`
    # continue fires before the per-member pgid check.
    inst = _FakeContainment([1, 555, 556])

    table = {1: 100, 555: 100, 556: None}

    def _getpgid(pid):
        if table.get(pid) is None:
            raise ProcessLookupError("member gone")
        return table[pid]

    monkeypatch.setattr(containment.os, "getpgid", _getpgid)
    # All live members share the runner's group -> no background work.
    assert inst.has_background_work("r", 1) is False


def test_has_background_work_detects_detached_member(monkeypatch):
    inst = _FakeContainment([555])
    monkeypatch.setattr(containment.os, "getpgid", lambda pid: 200 if pid == 1 else 999)
    assert inst.has_background_work("r", 1) is True


def test_windows_has_background_work_false_with_no_handle():
    # Windows override: enumerate returns [] when no handle is registered, so
    # any() over an empty membership is False (covers the Windows override on
    # a non-Windows host without touching ctypes).
    inst = containment._WindowsJobContainment()
    assert inst.has_background_work("r", 1) is False


# ----------------------------- Linux class file ops driven through a tmpdir root


def _linux_inst(tmp_path):
    return containment._LinuxCgroupContainment(cgroup_directory=str(tmp_path))


def _make_run_dir(inst, run_id, *, procs=None, kill_file=False):
    base = Path(inst._base)
    base.mkdir(parents=True, exist_ok=True)
    run_dir = base / run_id
    run_dir.mkdir(exist_ok=True)
    if procs is not None:
        (run_dir / "cgroup.procs").write_text(procs, encoding="ascii")
    if kill_file:
        (run_dir / "cgroup.kill").write_text("", encoding="ascii")
    return run_dir


def test_linux_enumerate_reads_procs(tmp_path):
    inst = _linux_inst(tmp_path)
    _make_run_dir(inst, "run-1", procs="10\n20\n\n")
    assert inst.enumerate("run-1") == [10, 20]


def test_linux_enumerate_missing_returns_empty(tmp_path):
    inst = _linux_inst(tmp_path)
    assert inst.enumerate("nope") == []


def test_linux_teardown_removes_empty_run_dir(tmp_path):
    inst = _linux_inst(tmp_path)
    run_dir = _make_run_dir(inst, "run-1")
    inst.teardown("run-1")
    assert not run_dir.exists()


def test_linux_teardown_tolerates_missing(tmp_path):
    inst = _linux_inst(tmp_path)
    inst.teardown("absent")  # no raise


def test_linux_teardown_closes_leftover_procs_fd(tmp_path):
    inst = _linux_inst(tmp_path)
    rd, wr = os.pipe()
    inst._procs_fd["run-1"] = wr
    inst.teardown("run-1")
    assert "run-1" not in inst._procs_fd
    os.close(rd)


def test_linux_after_spawn_closes_procs_fd(tmp_path):
    inst = _linux_inst(tmp_path)
    rd, wr = os.pipe()
    inst._procs_fd["run-1"] = wr
    inst.after_spawn("run-1", 1234)
    assert "run-1" not in inst._procs_fd
    os.close(rd)


def test_linux_after_spawn_tolerates_osclose_failure(tmp_path):
    inst = _linux_inst(tmp_path)
    # An already-closed fd triggers OSError on the second close -> swallowed.
    rd, wr = os.pipe()
    os.close(wr)
    inst._procs_fd["run-1"] = wr
    inst.after_spawn("run-1", 1234)  # no raise
    os.close(rd)


def test_linux_force_kill_all_returns_member_count_when_kill_file_writable(tmp_path):
    inst = _linux_inst(tmp_path)
    _make_run_dir(inst, "run-1", procs="10\n20\n", kill_file=True)
    assert inst.force_kill_all("run-1") == 2


def test_linux_force_kill_all_noop_when_run_dir_absent(tmp_path):
    inst = _linux_inst(tmp_path)
    # No run dir created -> opening _dir/cgroup.kill raises FileNotFoundError
    # (and enumerate returns []) -> the kill write is a no-op returning 0.
    assert inst.force_kill_all("run-1") == 0


def test_linux_reattach_validates_run_id(tmp_path):
    inst = _linux_inst(tmp_path)
    inst.reattach("run-1", None)  # valid id -> no raise
    try:
        inst.reattach("../x", None)
    except containment.ContainmentUnavailable:
        pass
    else:
        raise AssertionError("reattach must reject an unsafe run_id")


def test_linux_create_fails_closed_without_cgroup_procs(tmp_path):
    # On a non-cgroup filesystem the run dir opens but "cgroup.procs" is
    # absent -> open fails -> ContainmentUnavailable (never a silent success).
    inst = _linux_inst(tmp_path)
    _make_run_dir(inst, "run-1")
    try:
        inst.create("run-1")
    except containment.ContainmentUnavailable:
        pass
    else:
        raise AssertionError("create must fail closed without a cgroup.procs file")


def test_linux_create_fails_closed_when_base_is_not_a_dir(tmp_path):
    # base exists as a file (not an owned dir) -> fail closed at the ownership check.
    base = Path(_linux_inst(tmp_path)._base)
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text("x", encoding="utf-8")
    inst = containment._LinuxCgroupContainment(cgroup_directory=str(tmp_path))
    try:
        inst.create("run-1")
    except containment.ContainmentUnavailable:
        pass
    else:
        raise AssertionError("create must fail closed when base is not an owned dir")


def test_linux_spawn_preexec_writes_join_token(tmp_path):
    # The fork-time preexec_fn writes the cgroup join token ("0") to the
    # prepared fd. Drive it against a real writable fd (no fork needed).
    inst = _linux_inst(tmp_path)
    rd, wr = os.pipe()
    inst._procs_fd["run-1"] = wr
    kwargs = inst.spawn_kwargs("run-1")
    assert kwargs["pass_fds"] == (wr,)
    kwargs["preexec_fn"]()  # writes b"0"
    assert os.read(rd, 8) == b"0"
    os.close(rd)
    os.close(wr)


def test_linux_teardown_swallows_procs_fd_close_failure(tmp_path):
    inst = _linux_inst(tmp_path)
    rd, wr = os.pipe()
    os.close(wr)
    inst._procs_fd["run-1"] = wr  # already-closed fd
    inst.teardown("run-1")  # close raises OSError -> swallowed, no propagate
    assert "run-1" not in inst._procs_fd
    os.close(rd)


# ------------------------------- v1 class enumerate + force_kill via tmpdir root


def _v1_inst(tmp_path):
    return containment._LinuxCgroupV1PidsContainment(cgroup_directory=str(tmp_path))


def test_v1_enumerate_reads_tasks(tmp_path):
    inst = _v1_inst(tmp_path)
    base = Path(inst._base)
    run_dir = base / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "tasks").write_text("7\n8\n", encoding="ascii")
    assert inst.enumerate("run-1") == [7, 8]


def test_v1_enumerate_missing_returns_empty(tmp_path):
    inst = _v1_inst(tmp_path)
    assert inst.enumerate("nope") == []


def test_v1_force_kill_all_signals_live_member(tmp_path):
    inst = _v1_inst(tmp_path)
    # Spawn a disposable child the containment backend can SIGKILL.
    import subprocess
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        base = Path(inst._base)
        run_dir = base / "run-1"
        run_dir.mkdir(parents=True)
        (run_dir / "tasks").write_text(f"{child.pid}\n", encoding="ascii")
        signalled = inst.force_kill_all("run-1")
        assert signalled == 1
        assert child.wait(timeout=5) is not None
    finally:
        child.wait()


def test_v1_force_kill_all_tolerates_dead_member(tmp_path):
    inst = _v1_inst(tmp_path)
    base = Path(inst._base)
    run_dir = base / "run-1"
    run_dir.mkdir(parents=True)
    # A pid guaranteed not to exist -> ProcessLookupError swallowed, loop
    # retries until the (persistently empty) enumeration breaks out.
    (run_dir / "tasks").write_text("999999\n", encoding="ascii")
    assert inst.force_kill_all("run-1") == 0


def test_v1_force_kill_all_breaks_on_empty_tasks(tmp_path):
    inst = _v1_inst(tmp_path)
    base = Path(inst._base)
    run_dir = base / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "tasks").write_text("\n", encoding="ascii")  # no members
    assert inst.force_kill_all("run-1") == 0


def test_v1_create_fails_closed_without_tasks_file(tmp_path):
    # v1 run dir opens but "tasks" file is absent -> ContainmentUnavailable.
    inst = _v1_inst(tmp_path)
    base = Path(inst._base)
    (base / "run-1").mkdir(parents=True)
    try:
        inst.create("run-1")
    except containment.ContainmentUnavailable:
        pass
    else:
        raise AssertionError("v1 create must fail closed without a tasks file")


def test_v1_discovers_nested_membership_inside_mount(tmp_path):
    mount_point = tmp_path / "pids"
    # mount_root "/session" with membership "/session/scope" resolves the
    # process dir to <mount_point>/scope (relpath under the mount root).
    nested = mount_point / "scope"
    nested.mkdir(parents=True)
    (nested / "tasks").write_text("123\n", encoding="ascii")
    (nested / "pids.current").write_text("1\n", encoding="ascii")
    mountinfo = tmp_path / "mountinfo"
    cgroup = tmp_path / "cgroup"
    mountinfo.write_text(
        f"42 25 0:40 /session {mount_point} rw - cgroup cgroup rw,pids\n",
        encoding="utf-8",
    )
    cgroup.write_text("11:pids:/session/scope\n0::/\n", encoding="ascii")
    assert containment._current_cgroup_v1_pids_directory(
        str(mountinfo), str(cgroup), pid=123
    ) == str(nested)


def test_v1_skips_mount_where_pid_not_enrolled(tmp_path):
    # A valid pids mount whose tasks file does NOT list our pid is skipped,
    # and with no other mount discovery fails closed.
    mountinfo, cgroup = _write_v1_fixture(
        tmp_path, mount_root="/", membership="/", tasks="999\n"
    )
    try:
        containment._current_cgroup_v1_pids_directory(str(mountinfo), str(cgroup), pid=123)
    except containment.ContainmentUnavailable:
        pass
    else:
        raise AssertionError("mount not enrolling our pid must be skipped")


# ----------------------------- POSIX best-effort reattach + force_kill_all paths


def _posix_inst():
    return containment._PosixBestEffortContainment()


class _FakePC:
    """Configurable proc_control double for the POSIX force_kill path."""

    def __init__(self, *, alive=True, swept=0, kill_raises=False):
        self._alive = alive
        self._swept = swept
        self._kill_raises = kill_raises
        self.killed = None

    def pid_alive(self, pid):
        return self._alive

    def kill_detached_descendant_groups(self, pid):
        return self._swept

    def force_kill(self, pid):
        if self._kill_raises:
            raise PermissionError("denied")
        self.killed = pid


def _patch_proc_control(monkeypatch, fake):
    import proc_control
    monkeypatch.setattr(proc_control, "process_control", lambda: fake)


def test_posix_reattach_noop_when_runner_pid_none():
    inst = _posix_inst()
    inst._runner_pid["r"] = 99
    inst.reattach("r", None)
    # None runner_pid must NOT overwrite a known runner pid.
    assert inst._runner_pid["r"] == 99


def test_posix_reattach_records_runner_pid():
    inst = _posix_inst()
    inst.reattach("r", 77)
    assert inst._runner_pid["r"] == 77


def test_posix_force_kill_all_no_runner_returns_zero():
    inst = _posix_inst()
    assert inst.force_kill_all("r") == 0


def test_posix_force_kill_all_dead_runner_returns_zero(monkeypatch):
    inst = _posix_inst()
    inst._runner_pid["r"] = 1234
    _patch_proc_control(monkeypatch, _FakePC(alive=False))
    assert inst.force_kill_all("r") == 0


def test_posix_force_kill_all_sweeps_and_kills_runner(monkeypatch):
    inst = _posix_inst()
    inst._runner_pid["r"] = 1234
    fake = _FakePC(alive=True, swept=3)
    _patch_proc_control(monkeypatch, fake)
    assert inst.force_kill_all("r") == 4  # 3 swept + 1 runner
    assert fake.killed == 1234


def test_posix_force_kill_all_swallows_force_kill_exception(monkeypatch):
    inst = _posix_inst()
    inst._runner_pid["r"] = 1234
    # force_kill raising is swallowed; still returns swept + 1.
    _patch_proc_control(monkeypatch, _FakePC(alive=True, swept=0, kill_raises=True))
    assert inst.force_kill_all("r") == 1


def test_posix_enumerate_no_runner_returns_empty():
    inst = _posix_inst()
    assert inst.enumerate("r") == []


def test_posix_teardown_drops_runner_pid():
    inst = _posix_inst()
    inst._runner_pid["r"] = 5
    inst.teardown("r")
    assert "r" not in inst._runner_pid


# ------------------------------------------------------------------- _is_wsl


def test_is_wsl_via_env_var(monkeypatch):
    monkeypatch.setenv("WSL_INTEROP", "1")
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    assert containment._is_wsl("/nonexistent") is True


def test_is_wsl_via_osrelease_microsoft(tmp_path):
    f = tmp_path / "osrelease"
    f.write_text("5.15.146.1-microsoft-standard-WSL2\n", encoding="ascii")
    assert containment._is_wsl(str(f)) is True


def test_is_wsl_false_on_plain_linux(tmp_path, monkeypatch):
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    f = tmp_path / "osrelease"
    f.write_text("5.15.0-91-generic\n", encoding="ascii")
    assert containment._is_wsl(str(f)) is False


def test_is_wsl_false_when_osrelease_unreadable(monkeypatch):
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    assert containment._is_wsl("/no/such/path") is False


# ----------------------------------------------------------- containment() macOS


def test_factory_returns_posix_backend_on_macos(monkeypatch):
    monkeypatch.setattr(containment, "_INSTANCE", None)
    # This host is macOS (not 'linux', os.name != 'nt'); the else branch must
    # produce the best-effort POSIX backend.
    inst = containment.containment()
    assert isinstance(inst, containment._PosixBestEffortContainment)
    assert inst.guaranteed is False


def test_factory_returns_windows_backend_when_nt(monkeypatch):
    # os.name == "nt" -> Windows backend. Its __init__ only allocates the
    # handle dict (no ctypes call), so it is safe to construct off-Windows.
    monkeypatch.setattr(containment, "_INSTANCE", None)
    monkeypatch.setattr(containment.os, "name", "nt")
    inst = containment.containment()
    assert isinstance(inst, containment._WindowsJobContainment)


def test_linux_factory_uses_v2_when_delegated(monkeypatch):
    monkeypatch.setattr(containment, "_INSTANCE", None)
    # sys is imported lazily inside containment(); patch the real module.
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        containment, "_current_cgroup_v2_directory", lambda: "/cg/session"
    )
    monkeypatch.setattr(containment.os, "access", lambda path, mode: True)
    inst = containment.containment()
    assert isinstance(inst, containment._LinuxCgroupContainment)
    assert inst.guaranteed is True
    assert inst._base == "/cg/session/better-agent"
