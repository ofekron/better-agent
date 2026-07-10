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


def _prompt_roots() -> list[tuple[str, Path]]:
    frozen_root = getattr(sys, "_MEIPASS", "")
    if frozen_root:
        # The desktop bundle collects every prompt tree under one root,
        # with provisioning prompts at prompts/provisioning/.
        return [("", Path(frozen_root) / "prompts")]
    backend = Path(__file__).resolve().parent
    return [
        ("", backend / "prompts"),
        ("provisioning/", backend / "provisioning" / "prompts"),
    ]


def _snapshot_core_prompts() -> MappingProxyType[str, str]:
    prompts: dict[str, str] = {}
    for prefix, root in _prompt_roots():
        for path in root.rglob("*.md"):
            if path.is_file():
                key = prefix + path.relative_to(root).as_posix()
                if key in prompts:
                    raise ValueError(f"prompt key collision: {key}")
                prompts[key] = path.read_text(encoding="utf-8")
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
