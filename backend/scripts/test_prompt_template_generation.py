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


def test_snapshot_ignores_non_markdown_junk_files() -> None:
    root = Path(tempfile.mkdtemp(prefix="prompt-junk-"))
    prompts = root / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "demo.md").write_text("hello $name", encoding="utf-8")
    (prompts / ".DS_Store").write_bytes(b"\x00\x01\xff\xfe")
    (prompts / "demo.md.orig").write_bytes(b"\xff\xfe junk")

    rendered = _run(
        root,
        """
import json, sys
sys._MEIPASS = sys.argv[2]
sys.path.insert(0, sys.argv[1])
import prompt_templates
print(json.dumps([prompt_templates.render_prompt('demo.md', {'name': 'x'})]))
""",
    )
    assert rendered == ["hello x"]


def test_provisioning_prompts_served_by_single_renderer() -> None:
    names = _run(
        Path(tempfile.mkdtemp(prefix="prompt-prov-")),
        """
import json, sys
sys.path.insert(0, sys.argv[1])
import prompt_templates
keys = sorted(k for k in prompt_templates._CORE_PROMPTS if k.startswith('provisioning/'))
prompt_templates.render_prompt('provisioning/worker_prep.md', {'description': 'd'})
print(json.dumps(keys))
""",
    )
    assert "provisioning/worker_prep.md" in names
    assert "provisioning/search_worker.md" in names
    assert "provisioning/extension_context_auditor.md" in names
    assert "provisioning/project_structure_maintainer.md" in names
    assert not (BACKEND / "provisioning" / "prompts.py").exists()


def test_desktop_package_includes_complete_prompt_tree() -> None:
    spec = (BACKEND.parent / "desktop" / "BetterAgent.spec").read_text(encoding="utf-8")
    assert '(os.path.join(_BACKEND, "prompts"), "prompts")' in spec
    assert '(os.path.join(_BACKEND, "provisioning", "prompts"), os.path.join("prompts", "provisioning"))' in spec


if __name__ == "__main__":
    test_core_prompt_snapshot_is_process_generation_scoped()
    test_snapshot_ignores_non_markdown_junk_files()
    test_provisioning_prompts_served_by_single_renderer()
    test_desktop_package_includes_complete_prompt_tree()
    print("PASS test_prompt_template_generation")
