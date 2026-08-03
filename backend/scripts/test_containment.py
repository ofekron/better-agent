"""Unit tests for containment.py — process-tree containment backends.

Covers the macOS-reachable logic: the /proc cgroup parsers (via fixture
files), run_id path-safety validation, tmpdir-backed cgroup file ops, WSL
detection, the platform factory, and the POSIX + Windows non-ctypes paths.
Real subprocess-based kill verification for the v1 force-kill loop.

Windows-ctypes methods (CreateJobObjectW / OpenProcess / etc.) and the real
Linux /proc default-arg reads are platform-only honest exclusions, parallel
to the codex_execution_launch msvcrt case.

Run with:

    cd backend && .venv/bin/python -m pytest scripts/test_containment.py -o addopts=""
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

backend = Path(__file__).resolve().parents[1]
if str(backend) not in sys.path:
    sys.path.insert(0, str(backend))

import _test_home  # noqa: E402

_test_home.isolate()

import pytest  # noqa: E402

import containment as c  # noqa: E402


# ---------------------------------------------------------------------------
# _unescape_mountinfo_path (pure)
# ---------------------------------------------------------------------------

def test_unescape_space() -> None:
    assert c._unescape_mountinfo_path("foo\\040bar") == "foo bar"


def test_unescape_all_tokens() -> None:
    assert c._unescape_mountinfo_path("a\\040b\\011c\\012d\\134e") == "a b\tc\nd\\e"


def test_unescape_passthrough() -> None:
    assert c._unescape_mountinfo_path("/sys/fs/cgroup") == "/sys/fs/cgroup"


def test_unescape_backslash_replacement_is_not_recursive() -> None:
    # A literal backslash produced by unescaping "\\134" is not reinterpreted.
    assert c._unescape_mountinfo_path("\\134040") == "\\040"


# ---------------------------------------------------------------------------
# _current_cgroup_v2_directory (parses fixture files)
# ---------------------------------------------------------------------------

def _build_v2(tmp: Path, *, members="12345", membership="/cg/sess",
              mount_root="/cg", mount_point=None, with_controllers=True,
              with_type=True, cgroup_lines=None, extra_lines=None) -> tuple[str, str, str]:
    mount_point = mount_point or str(tmp / "mnt")
    cg_file = tmp / "self_cgroup"
    cg_file.write_text(cgroup_lines if cgroup_lines is not None else f"0::{membership}\n")
    mi = tmp / "mountinfo"
    lines = [f"34 33 0:30 {mount_root} {mount_point} rw,nosuid - cgroup2 rw,nsdelegate\n"]
    if extra_lines:
        lines.extend(extra_lines)
    mi.write_text("".join(lines))
    rel = "." if membership == mount_root else os.path.relpath(membership, mount_root)
    resolved = os.path.normpath(os.path.join(mount_point, rel))
    Path(resolved).mkdir(parents=True, exist_ok=True)
    (Path(resolved) / "cgroup.procs").write_text(members)
    if with_controllers:
        (Path(resolved) / "cgroup.controllers").write_text("pids cpu")
    if with_type:
        (Path(resolved) / "cgroup.type").write_text("directory")
    return str(cg_file), str(mi), resolved


def test_v2_directory_happy_descendant(tmp_path: Path) -> None:
    cg, mi, resolved = _build_v2(tmp_path)
    assert c._current_cgroup_v2_directory(mountinfo_path=mi, cgroup_path=cg, pid=12345) == resolved


def test_v2_directory_membership_equals_mount_root(tmp_path: Path) -> None:
    cg, mi, resolved = _build_v2(tmp_path, membership="/cg", mount_root="/cg")
    assert c._current_cgroup_v2_directory(mountinfo_path=mi, cgroup_path=cg, pid=12345) == resolved


def test_v2_directory_membership_not_under_mount_root_unavailable(tmp_path: Path) -> None:
    cg, mi, _ = _build_v2(tmp_path, membership="/other/sess", mount_root="/cg")
    with pytest.raises(c.ContainmentUnavailable):
        c._current_cgroup_v2_directory(mountinfo_path=mi, cgroup_path=cg, pid=12345)


def test_v2_directory_relative_not_absolute_unavailable(tmp_path: Path) -> None:
    cg, mi, _ = _build_v2(tmp_path, cgroup_lines="0::relative\n")
    with pytest.raises(c.ContainmentUnavailable):
        c._current_cgroup_v2_directory(mountinfo_path=mi, cgroup_path=cg, pid=12345)


def test_v2_directory_no_v2_line_unavailable(tmp_path: Path) -> None:
    # only a v1-style line -> membership stays None -> ValueError
    cg, mi, _ = _build_v2(tmp_path, cgroup_lines="11:pids:/cg/sess\n")
    with pytest.raises(c.ContainmentUnavailable):
        c._current_cgroup_v2_directory(mountinfo_path=mi, cgroup_path=cg, pid=12345)


def test_v2_directory_pid_not_member_continues_then_unavailable(tmp_path: Path) -> None:
    cg, mi, _ = _build_v2(tmp_path, members="999")
    with pytest.raises(c.ContainmentUnavailable):
        c._current_cgroup_v2_directory(mountinfo_path=mi, cgroup_path=cg, pid=12345)


def test_v2_directory_missing_controllers_unavailable(tmp_path: Path) -> None:
    cg, mi, _ = _build_v2(tmp_path, with_controllers=False)
    with pytest.raises(c.ContainmentUnavailable):
        c._current_cgroup_v2_directory(mountinfo_path=mi, cgroup_path=cg, pid=12345)


def test_v2_directory_missing_type_unavailable(tmp_path: Path) -> None:
    cg, mi, _ = _build_v2(tmp_path, with_type=False)
    with pytest.raises(c.ContainmentUnavailable):
        c._current_cgroup_v2_directory(mountinfo_path=mi, cgroup_path=cg, pid=12345)


def test_v2_directory_unreadable_cgroup_file_unavailable(tmp_path: Path) -> None:
    with pytest.raises(c.ContainmentUnavailable):
        c._current_cgroup_v2_directory(mountinfo_path=str(tmp_path / "mi"),
                                       cgroup_path=str(tmp_path / "missing"),
                                       pid=12345)


def test_v2_directory_non_cgroup2_line_skipped(tmp_path: Path) -> None:
    # a tmpfs line should be skipped; only the cgroup2 line matches
    extra = ["35 34 0:31 / /tmp rw - tmpfs rw\n"]
    cg, mi, resolved = _build_v2(tmp_path, extra_lines=extra)
    assert c._current_cgroup_v2_directory(mountinfo_path=mi, cgroup_path=cg, pid=12345) == resolved


def test_v2_directory_separator_missing_skipped(tmp_path: Path) -> None:
    # line without " - " separator -> skipped -> no match
    cg = tmp_path / "self_cgroup"
    cg.write_text("0::/cg/sess\n")
    mi = tmp_path / "mountinfo"
    mi.write_text("34 33 0:30 /cg /mnt rw,nosuid\n")  # no " - "
    with pytest.raises(c.ContainmentUnavailable):
        c._current_cgroup_v2_directory(mountinfo_path=str(mi), cgroup_path=str(cg), pid=12345)


def test_v2_directory_fields_too_few_skipped(tmp_path: Path) -> None:
    cg = tmp_path / "self_cgroup"
    cg.write_text("0::/cg/sess\n")
    mi = tmp_path / "mountinfo"
    mi.write_text("34 33 - cgroup2 rw\n")  # before has 2 fields
    with pytest.raises(c.ContainmentUnavailable):
        c._current_cgroup_v2_directory(mountinfo_path=str(mi), cgroup_path=str(cg), pid=12345)


# ---------------------------------------------------------------------------
# _current_cgroup_v1_pids_directory
# ---------------------------------------------------------------------------

def _build_v1(tmp: Path, *, members="12345", membership="/cg/sess",
              mount_root="/cg", mount_point=None, with_current=True) -> tuple[str, str, str]:
    mount_point = mount_point or str(tmp / "mnt")
    cg_file = tmp / "self_cgroup"
    cg_file.write_text(f"11:cpuid,pids:{membership}\n")
    mi = tmp / "mountinfo"
    mi.write_text(f"34 33 0:30 {mount_root} {mount_point} rw,nosuid - cgroup rw pids\n")
    rel = "." if membership == mount_root else os.path.relpath(membership, mount_root)
    resolved = os.path.normpath(os.path.join(mount_point, rel))
    Path(resolved).mkdir(parents=True, exist_ok=True)
    (Path(resolved) / "tasks").write_text(members)
    if with_current:
        (Path(resolved) / "pids.current").write_text("0")
    return str(cg_file), str(mi), resolved


def test_v1_directory_happy(tmp_path: Path) -> None:
    cg, mi, resolved = _build_v1(tmp_path)
    assert c._current_cgroup_v1_pids_directory(mountinfo_path=mi, cgroup_path=cg, pid=12345) == resolved


def test_v1_directory_no_pids_controller_unavailable(tmp_path: Path) -> None:
    cg = tmp_path / "self_cgroup"
    cg.write_text("11:cpu:/cg/sess\n")  # no pids controller -> membership None
    mi = tmp_path / "mountinfo"
    mp = tmp_path / "mnt"
    mi.write_text(f"34 33 0:30 /cg {mp} rw,nosuid - cgroup rw pids\n")
    with pytest.raises(c.ContainmentUnavailable):
        c._current_cgroup_v1_pids_directory(mountinfo_path=str(mi), cgroup_path=str(cg), pid=12345)


def test_v1_directory_mountinfo_not_pids_skipped(tmp_path: Path) -> None:
    # mountinfo line is cgroup but without pids in options -> skipped
    cg = tmp_path / "self_cgroup"
    cg.write_text("11:pids:/cg/sess\n")
    mi = tmp_path / "mountinfo"
    mp = tmp_path / "mnt"
    mi.write_text(f"34 33 0:30 /cg {mp} rw,nosuid - cgroup rw cpu\n")
    with pytest.raises(c.ContainmentUnavailable):
        c._current_cgroup_v1_pids_directory(mountinfo_path=str(mi), cgroup_path=str(cg), pid=12345)


def test_v1_directory_pid_not_member_unavailable(tmp_path: Path) -> None:
    cg, mi, _ = _build_v1(tmp_path, members="999")
    with pytest.raises(c.ContainmentUnavailable):
        c._current_cgroup_v1_pids_directory(mountinfo_path=mi, cgroup_path=cg, pid=12345)


def test_v1_directory_missing_current_unavailable(tmp_path: Path) -> None:
    cg, mi, _ = _build_v1(tmp_path, with_current=False)
    with pytest.raises(c.ContainmentUnavailable):
        c._current_cgroup_v1_pids_directory(mountinfo_path=mi, cgroup_path=cg, pid=12345)


def test_v1_directory_membership_not_under_root_unavailable(tmp_path: Path) -> None:
    cg, mi, _ = _build_v1(tmp_path, membership="/other/sess", mount_root="/cg")
    with pytest.raises(c.ContainmentUnavailable):
        c._current_cgroup_v1_pids_directory(mountinfo_path=mi, cgroup_path=cg, pid=12345)


# ---------------------------------------------------------------------------
# _LinuxCgroupContainment path-safety + filesystem ops (tmpdir-backed)
# ---------------------------------------------------------------------------

def test_linux_dir_safe_run_id(tmp_path: Path) -> None:
    inst = c._LinuxCgroupContainment(cgroup_directory=str(tmp_path))
    assert inst._dir("run-1") == os.path.join(tmp_path, "better-agent", "run-1")


def test_linux_dir_rejects_traversal(tmp_path: Path) -> None:
    inst = c._LinuxCgroupContainment(cgroup_directory=str(tmp_path))
    for bad in ("../escape", "a/b", "", ".", ".."):
        with pytest.raises(c.ContainmentUnavailable):
            inst._dir(bad)


def test_can_create_run_group_base_missing_parent_writable(tmp_path: Path) -> None:
    inst = c._LinuxCgroupContainment(cgroup_directory=str(tmp_path))
    assert inst._can_create_run_group() is True


def test_can_create_run_group_base_is_dir_owned(tmp_path: Path) -> None:
    (tmp_path / "better-agent").mkdir()
    inst = c._LinuxCgroupContainment(cgroup_directory=str(tmp_path))
    assert inst._can_create_run_group() is True


def test_can_create_run_group_base_is_file_false(tmp_path: Path) -> None:
    (tmp_path / "better-agent").write_text("x")
    inst = c._LinuxCgroupContainment(cgroup_directory=str(tmp_path))
    assert inst._can_create_run_group() is False


def test_can_create_run_group_lstat_oserror_false(tmp_path: Path) -> None:
    # An over-long base path makes os.lstat raise OSError (ENAMETOOLONG),
    # hitting the generic except -> False defensive branch.
    inst = c._LinuxCgroupContainment(cgroup_directory="x" * 4000)
    assert inst._can_create_run_group() is False


def _seed_run_dir(tmp_path: Path, run_id: str, *, procs="", tasks=False,
                  kill_file=False) -> str:
    base = tmp_path / "better-agent"
    run_dir = base / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if tasks:
        (run_dir / "tasks").write_text(procs)
    else:
        (run_dir / "cgroup.procs").write_text(procs)
    if kill_file:
        (run_dir / "cgroup.kill").write_text("")
    return str(run_dir)


def test_enumerate_parses_procs(tmp_path: Path) -> None:
    _seed_run_dir(tmp_path, "run-1", procs="100\n200\n")
    inst = c._LinuxCgroupContainment(cgroup_directory=str(tmp_path))
    assert inst.enumerate("run-1") == [100, 200]


def test_enumerate_empty_procs(tmp_path: Path) -> None:
    _seed_run_dir(tmp_path, "run-1", procs="")
    inst = c._LinuxCgroupContainment(cgroup_directory=str(tmp_path))
    assert inst.enumerate("run-1") == []


def test_enumerate_missing_returns_empty(tmp_path: Path) -> None:
    inst = c._LinuxCgroupContainment(cgroup_directory=str(tmp_path))
    assert inst.enumerate("run-1") == []


def test_enumerate_garbage_returns_empty(tmp_path: Path) -> None:
    _seed_run_dir(tmp_path, "run-1", procs="not-a-num\n")
    inst = c._LinuxCgroupContainment(cgroup_directory=str(tmp_path))
    assert inst.enumerate("run-1") == []


def test_teardown_removes_empty_run_dir(tmp_path: Path) -> None:
    run_dir = _seed_run_dir(tmp_path, "run-1")
    # remove the cgroup.procs so rmdir succeeds (empty dir)
    (Path(run_dir) / "cgroup.procs").unlink()
    inst = c._LinuxCgroupContainment(cgroup_directory=str(tmp_path))
    inst.teardown("run-1")
    assert not Path(run_dir).exists()


def test_teardown_non_empty_swallows(tmp_path: Path) -> None:
    _seed_run_dir(tmp_path, "run-1", procs="1\n")  # dir not empty
    inst = c._LinuxCgroupContainment(cgroup_directory=str(tmp_path))
    inst.teardown("run-1")  # rmdir fails -> swallowed, no raise
    assert Path(tmp_path / "better-agent" / "run-1").exists()


def test_teardown_missing_is_noop(tmp_path: Path) -> None:
    inst = c._LinuxCgroupContainment(cgroup_directory=str(tmp_path))
    inst.teardown("run-1")  # no crash


def test_teardown_closes_stale_procs_fd(tmp_path: Path) -> None:
    inst = c._LinuxCgroupContainment(cgroup_directory=str(tmp_path))
    r, w = os.pipe()
    inst._procs_fd["run-1"] = w
    inst.teardown("run-1")
    assert "run-1" not in inst._procs_fd
    os.close(r)


def test_force_kill_all_v2_writes_kill_and_counts(tmp_path: Path) -> None:
    run_dir = _seed_run_dir(tmp_path, "run-1", procs="100\n200\n", kill_file=True)
    inst = c._LinuxCgroupContainment(cgroup_directory=str(tmp_path))
    count = inst.force_kill_all("run-1")
    assert count == 2
    assert (Path(run_dir) / "cgroup.kill").read_text() == "1"


def test_force_kill_all_v2_gone_run_returns_zero(tmp_path: Path) -> None:
    # no dir at all -> enumerate [], cgroup.kill write fails -> 0
    inst = c._LinuxCgroupContainment(cgroup_directory=str(tmp_path))
    assert inst.force_kill_all("run-1") == 0


def test_force_kill_all_v2_unsafe_run_id_fails_closed(tmp_path: Path) -> None:
    # _dir rejects the traversal inside enumerate; force_kill_all does not
    # swallow ContainmentUnavailable, so invalid input fails closed.
    inst = c._LinuxCgroupContainment(cgroup_directory=str(tmp_path))
    with pytest.raises(c.ContainmentUnavailable):
        inst.force_kill_all("../x")


def test_reattach_validates_run_id(tmp_path: Path) -> None:
    inst = c._LinuxCgroupContainment(cgroup_directory=str(tmp_path))
    inst.reattach("run-1", None)  # ok
    with pytest.raises(c.ContainmentUnavailable):
        inst.reattach("../x", None)


def test_create_fail_closed_without_procs(tmp_path: Path) -> None:
    # cgroup.procs does not auto-exist on a plain tmpdir -> fail closed
    inst = c._LinuxCgroupContainment(cgroup_directory=str(tmp_path))
    with pytest.raises(c.ContainmentUnavailable):
        inst.create("run-1")


def test_create_then_spawn_kwargs_and_after_spawn(tmp_path: Path) -> None:
    # pre-seed the run cgroup dir with a writable cgroup.procs so create() can
    # open it; this mirrors what a real cgroup kernel exposes.
    run_dir = tmp_path / "better-agent" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "cgroup.procs").write_text("")
    inst = c._LinuxCgroupContainment(cgroup_directory=str(tmp_path))
    inst.create("run-1")
    assert "run-1" in inst._procs_fd

    kwargs = inst.spawn_kwargs("run-1")
    assert "preexec_fn" in kwargs and "pass_fds" in kwargs
    fd = inst._procs_fd["run-1"]
    assert kwargs["pass_fds"] == (fd,)
    # The preexec closure writes "0" (async-signal-safe enrollment).
    kwargs["preexec_fn"]()
    assert (run_dir / "cgroup.procs").read_text() == "0"

    inst.after_spawn("run-1", 999)
    assert "run-1" not in inst._procs_fd


def test_after_spawn_unknown_run_id_is_noop(tmp_path: Path) -> None:
    inst = c._LinuxCgroupContainment(cgroup_directory=str(tmp_path))
    inst.after_spawn("run-1", 999)  # no crash, nothing popped


# ---------------------------------------------------------------------------
# _LinuxCgroupV1PidsContainment
# ---------------------------------------------------------------------------

def test_v1_enumerate_parses_tasks(tmp_path: Path) -> None:
    _seed_run_dir(tmp_path, "run-1", procs="5\n6\n", tasks=True)
    inst = c._LinuxCgroupV1PidsContainment(cgroup_directory=str(tmp_path))
    assert inst.enumerate("run-1") == [5, 6]


def test_v1_enumerate_missing_empty(tmp_path: Path) -> None:
    inst = c._LinuxCgroupV1PidsContainment(cgroup_directory=str(tmp_path))
    assert inst.enumerate("run-1") == []


def test_v1_force_kill_all_empty_tasks_zero(tmp_path: Path) -> None:
    _seed_run_dir(tmp_path, "run-1", procs="", tasks=True)
    inst = c._LinuxCgroupV1PidsContainment(cgroup_directory=str(tmp_path))
    assert inst.force_kill_all("run-1") == 0


def test_v1_force_kill_all_missing_tasks_zero(tmp_path: Path) -> None:
    inst = c._LinuxCgroupV1PidsContainment(cgroup_directory=str(tmp_path))
    assert inst.force_kill_all("run-1") == 0


def test_v1_force_kill_all_real_subprocess(tmp_path: Path) -> None:
    # Real end-to-end: a live child whose pid is listed in the tasks file is
    # SIGKILL'd by the bounded force-kill loop.
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        _seed_run_dir(tmp_path, "run-1", procs=f"{child.pid}\n", tasks=True)
        inst = c._LinuxCgroupV1PidsContainment(cgroup_directory=str(tmp_path))
        count = inst.force_kill_all("run-1")
        assert count == 1
        assert child.poll() is not None  # dead
    finally:
        child.wait()


# ---------------------------------------------------------------------------
# _WindowsJobContainment (non-ctypes paths; ctypes paths are Windows-only)
# ---------------------------------------------------------------------------

def test_windows_name_strips_path(tmp_path: Path) -> None:
    assert c._WindowsJobContainment._name("../evil") == f"Local\\better-agent-evil"
    assert c._WindowsJobContainment._name("run-1") == f"Local\\better-agent-run-1"


def test_windows_enumerate_no_handle_empty() -> None:
    inst = c._WindowsJobContainment()
    assert inst.enumerate("run-1") == []


def test_windows_force_kill_all_no_handle_zero() -> None:
    inst = c._WindowsJobContainment()
    assert inst.force_kill_all("run-1") == 0


def test_windows_teardown_no_handle_noop() -> None:
    inst = c._WindowsJobContainment()
    inst.teardown("run-1")  # no crash


def test_windows_spawn_kwargs_empty() -> None:
    inst = c._WindowsJobContainment()
    assert inst.spawn_kwargs("run-1") == {}


def test_windows_has_background_work_empty_false() -> None:
    inst = c._WindowsJobContainment()
    assert inst.has_background_work("run-1", 1) is False


def test_windows_has_background_work_member_true(monkeypatch) -> None:
    inst = c._WindowsJobContainment()
    monkeypatch.setattr(inst, "enumerate", lambda rid: [1, 2])
    assert inst.has_background_work("run-1", 1) is True


# ---------------------------------------------------------------------------
# _PosixBestEffortContainment (proc_control-free branches)
# ---------------------------------------------------------------------------

def test_posix_create_noop_and_spawn_kwargs(tmp_path: Path) -> None:
    inst = c._PosixBestEffortContainment()
    inst.create("run-1")
    assert inst.spawn_kwargs("run-1") == {}


def test_posix_after_spawn_stores_pid() -> None:
    inst = c._PosixBestEffortContainment()
    inst.after_spawn("run-1", 4242)
    assert inst._runner_pid["run-1"] == 4242


def test_posix_reattach_none_noop_and_pid_stores() -> None:
    inst = c._PosixBestEffortContainment()
    inst.reattach("run-1", None)
    assert "run-1" not in inst._runner_pid
    inst.reattach("run-1", 7)
    assert inst._runner_pid["run-1"] == 7


def test_posix_enumerate_no_pid_empty() -> None:
    inst = c._PosixBestEffortContainment()
    assert inst.enumerate("run-1") == []


def test_posix_force_kill_all_no_pid_zero() -> None:
    inst = c._PosixBestEffortContainment()
    assert inst.force_kill_all("run-1") == 0


def test_posix_teardown_clears_pid() -> None:
    inst = c._PosixBestEffortContainment()
    inst.after_spawn("run-1", 9)
    inst.teardown("run-1")
    assert "run-1" not in inst._runner_pid


def test_posix_guaranteed_false() -> None:
    assert c._PosixBestEffortContainment.guaranteed is False


def test_wsl_containment_is_posix_subclass() -> None:
    assert issubclass(c._LinuxWslBestEffortContainment, c._PosixBestEffortContainment)
    assert c._LinuxWslBestEffortContainment.guaranteed is False


# ---------------------------------------------------------------------------
# has_background_work (base POSIX group heuristic)
# ---------------------------------------------------------------------------

def test_has_background_work_dead_runner_false() -> None:
    inst = c._PosixBestEffortContainment()
    # a pid that definitely does not exist
    assert inst.has_background_work("run-1", 999999) is False


def test_has_background_work_same_pgid_false(monkeypatch) -> None:
    inst = c._PosixBestEffortContainment()
    pgids = {100: 5, 200: 5}
    monkeypatch.setattr("os.getpgid", lambda pid: pgids.get(pid) or (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(inst, "enumerate", lambda rid: [100, 200])
    assert inst.has_background_work("run-1", 100) is False


def test_has_background_work_diff_pgid_true(monkeypatch) -> None:
    inst = c._PosixBestEffortContainment()
    pgids = {100: 5, 200: 9}
    monkeypatch.setattr("os.getpgid", lambda pid: pgids.get(pid) or (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(inst, "enumerate", lambda rid: [100, 200])
    assert inst.has_background_work("run-1", 100) is True


def test_has_background_work_member_pgid_raises_continues(monkeypatch) -> None:
    inst = c._PosixBestEffortContainment()

    def fake_getpgid(pid: int) -> int:
        if pid == 100:
            return 5
        raise ProcessLookupError

    monkeypatch.setattr("os.getpgid", fake_getpgid)
    monkeypatch.setattr(inst, "enumerate", lambda rid: [100, 200])
    # runner 100 ok, member 200 raises -> continue -> no bg work found
    assert inst.has_background_work("run-1", 100) is False


# ---------------------------------------------------------------------------
# _is_wsl
# ---------------------------------------------------------------------------

def test_is_wsl_env_interop_true(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WSL_INTEROP", "1")
    assert c._is_wsl(osrelease_path=str(tmp_path / "missing")) is True


def test_is_wsl_env_distro_true(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    assert c._is_wsl(osrelease_path=str(tmp_path / "missing")) is True


def test_is_wsl_osrelease_microsoft_true(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    f = tmp_path / "osrelease"
    f.write_text("5.15.153.1-microsoft-standard-WSL2")
    assert c._is_wsl(osrelease_path=str(f)) is True


def test_is_wsl_osrelease_other_false(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    f = tmp_path / "osrelease"
    f.write_text("6.8.0-rc4")
    assert c._is_wsl(osrelease_path=str(f)) is False


def test_is_wsl_missing_osrelease_false(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    assert c._is_wsl(osrelease_path=str(tmp_path / "missing")) is False


# ---------------------------------------------------------------------------
# additional parse-error + posix proc_control branch coverage
# ---------------------------------------------------------------------------

def test_v2_directory_garbage_members_unavailable(tmp_path: Path) -> None:
    # cgroup.procs with non-numeric content -> int() raises -> inner continue
    cg, mi, _ = _build_v2(tmp_path, members="abc\n")
    with pytest.raises(c.ContainmentUnavailable):
        c._current_cgroup_v2_directory(mountinfo_path=mi, cgroup_path=cg, pid=12345)


def test_v1_directory_garbage_tasks_unavailable(tmp_path: Path) -> None:
    cg, mi, _ = _build_v1(tmp_path, members="abc\n")
    with pytest.raises(c.ContainmentUnavailable):
        c._current_cgroup_v1_pids_directory(mountinfo_path=mi, cgroup_path=cg, pid=12345)


def test_v1_create_fail_closed_without_tasks(tmp_path: Path) -> None:
    inst = c._LinuxCgroupV1PidsContainment(cgroup_directory=str(tmp_path))
    with pytest.raises(c.ContainmentUnavailable):
        inst.create("run-1")


def test_v1_create_happy_with_seeded_tasks(tmp_path: Path) -> None:
    run_dir = tmp_path / "better-agent" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "tasks").write_text("")
    inst = c._LinuxCgroupV1PidsContainment(cgroup_directory=str(tmp_path))
    inst.create("run-1")
    assert "run-1" in inst._procs_fd
    inst.after_spawn("run-1", 1)
    assert "run-1" not in inst._procs_fd


def test_v1_force_kill_all_nonexistent_pid_zero(tmp_path: Path) -> None:
    # tasks lists a pid that does not exist -> os.kill raises -> caught; static
    # file never empties so the bounded loop runs out and returns 0.
    _seed_run_dir(tmp_path, "run-1", procs="999999\n", tasks=True)
    inst = c._LinuxCgroupV1PidsContainment(cgroup_directory=str(tmp_path))
    assert inst.force_kill_all("run-1") == 0


def test_after_spawn_swallows_close_oserror(tmp_path: Path) -> None:
    inst = c._LinuxCgroupContainment(cgroup_directory=str(tmp_path))
    r, w = os.pipe()
    os.close(w)  # already closed -> os.close will raise OSError
    inst._procs_fd["run-1"] = w
    inst.after_spawn("run-1", 1)  # no crash
    assert "run-1" not in inst._procs_fd
    os.close(r)


def test_teardown_swallows_close_oserror(tmp_path: Path) -> None:
    inst = c._LinuxCgroupContainment(cgroup_directory=str(tmp_path))
    r, w = os.pipe()
    os.close(w)
    inst._procs_fd["run-1"] = w
    inst.teardown("run-1")  # no crash
    os.close(r)


def test_posix_enumerate_live_pid() -> None:
    inst = c._PosixBestEffortContainment()
    inst.after_spawn("run-1", os.getpid())
    members = inst.enumerate("run-1")
    assert isinstance(members, list)
    assert os.getpid() in members


def test_posix_force_kill_all_disposable_child() -> None:
    from proc_control import process_control
    inst = c._PosixBestEffortContainment()
    # start_new_session: the child gets its own process group so the backend's
    # os.killpg(force_kill) targets ONLY the child, never the test runner.
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        inst.after_spawn("run-1", child.pid)
        count = inst.force_kill_all("run-1")
        assert count >= 1
        # Event-driven: block until the SIGKILL'd child is finalized. If
        # proc_control reaped it, wait() raises ChildProcessError (still dead).
        try:
            child.wait(timeout=5)
        except subprocess.ChildProcessError:
            pass
        assert process_control().pid_alive(child.pid) is False
    finally:
        try:
            child.wait(timeout=3)
        except (subprocess.TimeoutExpired, subprocess.ChildProcessError):
            child.kill()
            try:
                child.wait(timeout=2)
            except (subprocess.TimeoutExpired, subprocess.ChildProcessError):
                pass


def test_posix_force_kill_all_dead_runner_zero() -> None:
    inst = c._PosixBestEffortContainment()
    inst.after_spawn("run-1", 999999)  # not alive
    assert inst.force_kill_all("run-1") == 0


# ---------------------------------------------------------------------------
# containment() factory
# ---------------------------------------------------------------------------

def _reset_singleton() -> None:
    c._INSTANCE = None


def test_factory_macos_returns_posix_best_effort(monkeypatch) -> None:
    _reset_singleton()
    monkeypatch.setattr("os.name", "posix")
    monkeypatch.setattr(sys, "platform", "darwin")
    inst = c.containment()
    assert isinstance(inst, c._PosixBestEffortContainment)
    assert inst.guaranteed is False


def test_factory_windows_returns_job_object(monkeypatch) -> None:
    _reset_singleton()
    monkeypatch.setattr("os.name", "nt")
    inst = c.containment()
    assert isinstance(inst, c._WindowsJobContainment)
    assert inst.guaranteed is True


def test_factory_linux_no_cgroup_no_wsl_fail_closed(monkeypatch, tmp_path: Path) -> None:
    _reset_singleton()
    monkeypatch.setattr("os.name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    # /proc absent on macOS -> both cgroup backends unavailable; not WSL -> raise
    with pytest.raises(c.ContainmentUnavailable):
        c.containment()


def test_factory_linux_wsl_falls_back_to_best_effort(monkeypatch) -> None:
    _reset_singleton()
    monkeypatch.setattr("os.name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("WSL_INTEROP", "1")  # _is_wsl True
    inst = c.containment()
    assert isinstance(inst, c._LinuxWslBestEffortContainment)
    assert inst.guaranteed is False


def test_factory_caches_singleton(monkeypatch) -> None:
    _reset_singleton()
    monkeypatch.setattr("os.name", "posix")
    monkeypatch.setattr(sys, "platform", "darwin")
    first = c.containment()
    second = c.containment()
    assert first is second


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-o", "addopts=", "-q"]))
