from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import containment  # noqa: E402


def write_cgroup_fixture(
    tmp_path,
    *,
    mount_root: str,
    membership: str,
    mount_name: str = "unified",
    pid: int = 123,
):
    mount_point = tmp_path / mount_name
    relative = membership.removeprefix(mount_root).lstrip("/")
    process_directory = mount_point / relative
    process_directory.mkdir(parents=True)
    (process_directory / "cgroup.procs").write_text(f"{pid}\n", encoding="ascii")
    (process_directory / "cgroup.controllers").write_text("cpu memory\n", encoding="ascii")
    (process_directory / "cgroup.type").write_text("domain\n", encoding="ascii")
    mountinfo = tmp_path / f"{mount_name}.mountinfo"
    cgroup = tmp_path / f"{mount_name}.cgroup"
    mountinfo.write_text(
        f"36 25 0:32 {mount_root} {mount_point} rw,nosuid - cgroup2 cgroup2 rw\n",
        encoding="utf-8",
    )
    cgroup.write_text(f"0::{membership}\n2:cpu:/\n", encoding="ascii")
    return mountinfo, cgroup, process_directory


def test_discovers_hybrid_cgroup_v2_mount(tmp_path) -> None:
    mountinfo, cgroup, process_directory = write_cgroup_fixture(
        tmp_path,
        mount_root="/",
        membership="/",
    )

    assert containment._current_cgroup_v2_directory(
        str(mountinfo),
        str(cgroup),
        pid=123,
    ) == str(process_directory)


def test_resolves_delegated_membership_inside_mount(tmp_path) -> None:
    mountinfo, cgroup, process_directory = write_cgroup_fixture(
        tmp_path,
        mount_root="/user.slice",
        membership="/user.slice/user-1000.slice/session.scope",
    )

    assert containment._current_cgroup_v2_directory(
        str(mountinfo),
        str(cgroup),
        pid=123,
    ) == str(process_directory)


def test_discovers_conventional_unified_mount(tmp_path) -> None:
    mountinfo, cgroup, process_directory = write_cgroup_fixture(
        tmp_path,
        mount_root="/",
        membership="/system.slice/better-agent.service",
        mount_name="cgroup",
    )

    assert containment._current_cgroup_v2_directory(
        str(mountinfo),
        str(cgroup),
        pid=123,
    ) == str(process_directory)


def test_decodes_escaped_mount_paths(tmp_path) -> None:
    mountinfo, cgroup, process_directory = write_cgroup_fixture(
        tmp_path,
        mount_root="/tenant space",
        membership="/tenant space/session.scope",
        mount_name="cgroup space",
    )
    escaped_mount = str(tmp_path / "cgroup space").replace(" ", "\\040")
    mountinfo.write_text(
        f"36 25 0:32 /tenant\\040space {escaped_mount} "
        "rw,nosuid - cgroup2 cgroup2 rw\n",
        encoding="utf-8",
    )

    assert containment._current_cgroup_v2_directory(
        str(mountinfo),
        str(cgroup),
        pid=123,
    ) == str(process_directory)


def test_rejects_distractor_mount_without_current_process(tmp_path) -> None:
    mountinfo, cgroup, _ = write_cgroup_fixture(
        tmp_path,
        mount_root="/",
        membership="/",
        pid=999,
    )

    try:
        containment._current_cgroup_v2_directory(
            str(mountinfo),
            str(cgroup),
            pid=123,
        )
    except containment.ContainmentUnavailable:
        pass
    else:
        raise AssertionError("unrelated cgroup2 mount must be rejected")


def test_rejects_v1_only_host(tmp_path) -> None:
    mountinfo = tmp_path / "mountinfo"
    cgroup = tmp_path / "cgroup"
    mountinfo.write_text(
        "36 25 0:31 / /sys/fs/cgroup/cpu rw - cgroup cgroup rw,cpu\n",
        encoding="utf-8",
    )
    cgroup.write_text("2:cpu:/\n", encoding="ascii")

    try:
        containment._current_cgroup_v2_directory(str(mountinfo), str(cgroup))
    except containment.ContainmentUnavailable:
        pass
    else:
        raise AssertionError("v1-only host must fail closed")


def test_linux_containment_uses_discovered_delegated_directory() -> None:
    instance = containment._LinuxCgroupContainment(
        cgroup_directory="/delegated/session.scope",
    )

    assert instance._dir("run-1") == "/delegated/session.scope/better-agent/run-1"


def test_linux_containment_rejects_symlink_run_directory(tmp_path) -> None:
    parent = tmp_path / "delegated"
    base = parent / "better-agent"
    target = tmp_path / "target"
    parent.mkdir()
    base.mkdir()
    target.mkdir()
    (base / "run-1").symlink_to(target, target_is_directory=True)
    instance = containment._LinuxCgroupContainment(cgroup_directory=str(parent))

    try:
        instance.create("run-1")
    except containment.ContainmentUnavailable:
        pass
    else:
        raise AssertionError("symlinked run cgroup must be rejected")


def test_linux_spawn_preserves_cgroup_descriptor() -> None:
    instance = containment._LinuxCgroupContainment(
        cgroup_directory="/delegated/session.scope",
    )
    instance._procs_fd["run-1"] = 42

    kwargs = instance.spawn_kwargs("run-1")

    assert kwargs["pass_fds"] == (42,)
    assert callable(kwargs["preexec_fn"])


def test_discovers_wsl_hybrid_pids_hierarchy(tmp_path) -> None:
    mount_point = tmp_path / "pids"
    mount_point.mkdir()
    (mount_point / "tasks").write_text("123\n", encoding="ascii")
    (mount_point / "pids.current").write_text("1\n", encoding="ascii")
    mountinfo = tmp_path / "mountinfo"
    cgroup = tmp_path / "cgroup"
    mountinfo.write_text(
        f"42 25 0:40 / {mount_point} rw - cgroup cgroup rw,pids\n",
        encoding="utf-8",
    )
    cgroup.write_text("12:pids:/\n0::/\n", encoding="ascii")

    assert containment._current_cgroup_v1_pids_directory(
        str(mountinfo),
        str(cgroup),
        pid=123,
    ) == str(mount_point)


def test_linux_factory_falls_back_to_verified_v1(monkeypatch) -> None:
    monkeypatch.setattr(containment, "_INSTANCE", None)
    monkeypatch.setattr(containment.sys if hasattr(containment, "sys") else __import__("sys"), "platform", "linux")
    monkeypatch.setattr(
        containment,
        "_current_cgroup_v2_directory",
        lambda: (_ for _ in ()).throw(containment.ContainmentUnavailable("no v2")),
    )
    monkeypatch.setattr(
        containment,
        "_current_cgroup_v1_pids_directory",
        lambda: "/pids/session",
    )
    monkeypatch.setattr(containment.os, "access", lambda path, mode: True)

    instance = containment.containment()

    assert isinstance(instance, containment._LinuxCgroupV1PidsContainment)


def test_linux_factory_uses_explicit_degraded_containment_on_wsl(monkeypatch) -> None:
    monkeypatch.setattr(containment, "_INSTANCE", None)
    monkeypatch.setattr(
        containment.sys if hasattr(containment, "sys") else __import__("sys"),
        "platform",
        "linux",
    )
    monkeypatch.setattr(
        containment,
        "_current_cgroup_v2_directory",
        lambda: (_ for _ in ()).throw(containment.ContainmentUnavailable("no v2")),
    )
    monkeypatch.setattr(
        containment,
        "_current_cgroup_v1_pids_directory",
        lambda: (_ for _ in ()).throw(containment.ContainmentUnavailable("no v1")),
    )
    monkeypatch.setattr(containment, "_is_wsl", lambda: True)

    instance = containment.containment()

    assert isinstance(instance, containment._LinuxWslBestEffortContainment)
    assert instance.guaranteed is False


def test_linux_factory_rejects_discovered_but_undelegated_v2_on_wsl(
    monkeypatch,
) -> None:
    monkeypatch.setattr(containment, "_INSTANCE", None)
    monkeypatch.setattr(
        containment.sys if hasattr(containment, "sys") else __import__("sys"),
        "platform",
        "linux",
    )
    monkeypatch.setattr(
        containment,
        "_current_cgroup_v2_directory",
        lambda: "/sys/fs/cgroup/unified/init.scope",
    )
    monkeypatch.setattr(
        containment.os,
        "access",
        lambda path, mode: False,
    )
    monkeypatch.setattr(
        containment,
        "_current_cgroup_v1_pids_directory",
        lambda: (_ for _ in ()).throw(containment.ContainmentUnavailable("no v1")),
    )
    monkeypatch.setattr(containment, "_is_wsl", lambda: True)

    instance = containment.containment()

    assert isinstance(instance, containment._LinuxWslBestEffortContainment)


def test_linux_factory_rejects_discovered_but_undelegated_v1_on_wsl(
    monkeypatch,
) -> None:
    monkeypatch.setattr(containment, "_INSTANCE", None)
    monkeypatch.setattr(
        containment.sys if hasattr(containment, "sys") else __import__("sys"),
        "platform",
        "linux",
    )
    monkeypatch.setattr(
        containment,
        "_current_cgroup_v2_directory",
        lambda: (_ for _ in ()).throw(containment.ContainmentUnavailable("no v2")),
    )
    monkeypatch.setattr(
        containment,
        "_current_cgroup_v1_pids_directory",
        lambda: "/sys/fs/cgroup/pids/init.scope",
    )
    monkeypatch.setattr(containment.os, "access", lambda path, mode: False)
    monkeypatch.setattr(containment, "_is_wsl", lambda: True)

    instance = containment.containment()

    assert isinstance(instance, containment._LinuxWslBestEffortContainment)


def test_linux_factory_fails_closed_without_cgroups_outside_wsl(monkeypatch) -> None:
    monkeypatch.setattr(containment, "_INSTANCE", None)
    monkeypatch.setattr(
        containment.sys if hasattr(containment, "sys") else __import__("sys"),
        "platform",
        "linux",
    )
    monkeypatch.setattr(
        containment,
        "_current_cgroup_v2_directory",
        lambda: (_ for _ in ()).throw(containment.ContainmentUnavailable("no v2")),
    )
    monkeypatch.setattr(
        containment,
        "_current_cgroup_v1_pids_directory",
        lambda: (_ for _ in ()).throw(containment.ContainmentUnavailable("no v1")),
    )
    monkeypatch.setattr(containment, "_is_wsl", lambda: False)

    try:
        containment.containment()
    except containment.ContainmentUnavailable:
        pass
    else:
        raise AssertionError("non-WSL Linux without cgroups must fail closed")
