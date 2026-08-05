from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from node_id import validate_node_id


MACHINE_ID_TEMPLATE_VARIABLE = "machine_id"
MACHINE_ID_TEMPLATE_TOKEN = "{{better_agent.machine_id}}"
SUPPORTED_TEMPLATE_VARIABLES = frozenset({MACHINE_ID_TEMPLATE_VARIABLE})


@dataclass(frozen=True)
class RuntimeSkillSource:
    root: Path
    template_variables: tuple[str, ...] = ()


def normalize_template_variables(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if type(value) not in (list, tuple) or any(type(item) is not str for item in value):
        raise ValueError("runtime skill template_variables must be a list of strings")
    variables = tuple(dict.fromkeys(value))
    unknown = set(variables) - SUPPORTED_TEMPLATE_VARIABLES
    if unknown:
        raise ValueError(f"unsupported runtime skill template variable: {sorted(unknown)[0]}")
    return variables


def specialize_skill_text(
    text: str,
    *,
    template_variables: object,
    machine_id: object = None,
) -> str:
    variables = normalize_template_variables(template_variables)
    if (
        MACHINE_ID_TEMPLATE_VARIABLE not in variables
        or MACHINE_ID_TEMPLATE_TOKEN not in text
    ):
        return text
    return text.replace(MACHINE_ID_TEMPLATE_TOKEN, validate_node_id(machine_id))


def specialize_skill_file(
    path: Path,
    *,
    template_variables: object,
    machine_id: object = None,
) -> None:
    text = path.read_text(encoding="utf-8")
    specialized = specialize_skill_text(
        text,
        template_variables=template_variables,
        machine_id=machine_id,
    )
    if specialized != text:
        path.write_text(specialized, encoding="utf-8")
