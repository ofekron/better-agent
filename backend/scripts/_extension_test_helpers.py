from __future__ import annotations

from pathlib import Path

from _test_extension import install_core_role


def install_machine_nodes_extension(home: str | Path) -> None:
    install_core_role(Path(home), "machine-nodes")
