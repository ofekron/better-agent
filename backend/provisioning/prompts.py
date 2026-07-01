from __future__ import annotations

from pathlib import Path
from string import Template


_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def render_prompt(name: str, params: dict[str, object]) -> str:
    path = _PROMPTS_DIR / name
    template = Template(path.read_text(encoding="utf-8"))
    values = {key: str(value) for key, value in params.items()}
    return template.substitute(values)
