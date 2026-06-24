from __future__ import annotations

from pathlib import Path
from string import Template
import sys


_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def render_prompt(name: str, params: dict[str, object]) -> str:
    path = _PROMPTS_DIR / name
    if not path.is_file() and name in {"get_requirements_processor.md", "requirement_analysis_worker.md"}:
        import extension_package_loader
        import extension_store

        extension_path = extension_package_loader.prompt_path(
            extension_store.BUILTIN_REQUIREMENTS_EXTENSION_ID,
            name,
        )
        if extension_path is not None:
            path = extension_path
        else:
            for root in sys.path:
                candidate = Path(root) / "provisioning" / "prompts" / name
                if candidate.is_file():
                    path = candidate
                    break
    template = Template(path.read_text(encoding="utf-8"))
    values = {key: str(value) for key, value in params.items()}
    return template.substitute(values)
