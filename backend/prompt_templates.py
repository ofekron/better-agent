from __future__ import annotations

from pathlib import Path
from string import Template
import sys
from types import MappingProxyType


def _prompts_dir() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", "")
    if frozen_root:
        return Path(frozen_root) / "prompts"
    return Path(__file__).resolve().parent / "prompts"


def _snapshot_core_prompts() -> MappingProxyType[str, str]:
    root = _prompts_dir()
    prompts = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in root.rglob("*")
        if path.is_file()
    }
    return MappingProxyType(prompts)


_CORE_PROMPTS = _snapshot_core_prompts()


def render_prompt(name: str, params: dict[str, object] | None = None) -> str:
    template_text = _CORE_PROMPTS.get(name)
    if template_text is None and name.startswith("requirement_analysis/"):
        import extension_package_loader
        import extension_store

        extension_path = extension_package_loader.prompt_path(
            extension_store.extension_id_for_role('requirements'),
            name,
        )
        if extension_path is not None:
            template_text = extension_path.read_text(encoding="utf-8")
        else:
            for root in sys.path:
                candidate = Path(root) / "prompts" / name
                if candidate.is_file():
                    template_text = candidate.read_text(encoding="utf-8")
                    break
    if template_text is None:
        raise FileNotFoundError(_prompts_dir() / name)
    template = Template(template_text)
    if params is None:
        return template.template
    values = {key: str(value) for key, value in params.items()}
    return template.substitute(values)
