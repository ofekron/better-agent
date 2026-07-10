from __future__ import annotations

from pathlib import Path
from string import Template
import sys


def _prompts_dir() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", "")
    if frozen_root:
        return Path(frozen_root) / "prompts"
    return Path(__file__).resolve().parent / "prompts"


def render_prompt(name: str, params: dict[str, object] | None = None) -> str:
    path = _prompts_dir() / name
    if not path.is_file() and name.startswith("requirement_analysis/"):
        import extension_package_loader
        import extension_store

        extension_path = extension_package_loader.prompt_path(
            extension_store.extension_id_for_role('requirements'),
            name,
        )
        if extension_path is not None:
            path = extension_path
        else:
            for root in sys.path:
                candidate = Path(root) / "prompts" / name
                if candidate.is_file():
                    path = candidate
                    break
    template = Template(path.read_text(encoding="utf-8"))
    if params is None:
        return template.template
    values = {key: str(value) for key, value in params.items()}
    return template.substitute(values)
