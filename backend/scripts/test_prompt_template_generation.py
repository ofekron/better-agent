import json
import subprocess
import sys
import tempfile
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]


def _run(root: Path, script: str) -> list[str]:
    completed = subprocess.run(
        [sys.executable, "-c", script, str(BACKEND), str(root)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_core_prompt_snapshot_is_process_generation_scoped() -> None:
    root = Path(tempfile.mkdtemp(prefix="prompt-generation-"))
    prompt = root / "prompts" / "demo.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("old $name", encoding="utf-8")

    same_process = _run(
        root,
        """
import json, pathlib, sys
sys._MEIPASS = sys.argv[2]
sys.path.insert(0, sys.argv[1])
import prompt_templates
path = pathlib.Path(sys.argv[2]) / 'prompts' / 'demo.md'
first = prompt_templates.render_prompt('demo.md', {'name': 'one'})
path.write_text('new $name $generation', encoding='utf-8')
second = prompt_templates.render_prompt('demo.md', {'name': 'two'})
print(json.dumps([first, second]))
""",
    )
    assert same_process == ["old one", "old two"]

    fresh_process = _run(
        root,
        """
import json, sys
sys._MEIPASS = sys.argv[2]
sys.path.insert(0, sys.argv[1])
import prompt_templates
print(json.dumps([prompt_templates.render_prompt(
    'demo.md', {'name': 'three', 'generation': 'fresh'},
)]))
""",
    )
    assert fresh_process == ["new three fresh"]


def test_desktop_package_includes_complete_prompt_tree() -> None:
    spec = (BACKEND.parent / "desktop" / "BetterAgent.spec").read_text(encoding="utf-8")
    assert '(os.path.join(_BACKEND, "prompts"), "prompts")' in spec


if __name__ == "__main__":
    test_core_prompt_snapshot_is_process_generation_scoped()
    test_desktop_package_includes_complete_prompt_tree()
    print("PASS test_prompt_template_generation")
