from pathlib import Path
import os
import sys
import types


ROOT = Path(__file__).resolve().parents[2]


def test_credential_authority_signing_is_macos_only() -> None:
    script = (ROOT / "desktop" / "build_credential_authority.sh").read_text()

    guard = 'if [ "$(uname -s)" = "Darwin" ]; then'
    signing = 'bash "$DIR/local_codesign.sh" sign "$TARGET"'
    assert guard in script
    assert script.index(guard) < script.index(signing)


def test_credential_authority_bundles_headless_keyring() -> None:
    spec = (ROOT / "desktop" / "CredentialAuthority.spec").read_text()
    build_script = (ROOT / "desktop" / "build_credential_authority.sh").read_text()
    entrypoint = (ROOT / "desktop" / "credential_supervisor_main.py").read_text()

    assert '"headless_keyring"' in spec
    assert '"$REPO/backend/headless_keyring.py"' in build_script
    assert "from headless_keyring import Keyring" in entrypoint
    assert "Keyring()" in entrypoint


def test_credential_authority_self_test_isolates_keyring_home(monkeypatch) -> None:
    sys.path.insert(0, str(ROOT / "desktop"))
    import credential_supervisor_main

    observed = {}

    class FakeKeyring:
        def __init__(self) -> None:
            observed.update({
                key: os.environ[key]
                for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME")
            })
            assert len(set(observed.values())) == 1
            assert Path(observed["HOME"]).is_dir()

    original = {key: os.environ.get(key) for key in (
        "BETTER_AGENT_HEADLESS_KEYRING_KEY",
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    )}
    monkeypatch.setitem(
        sys.modules,
        "headless_keyring",
        types.SimpleNamespace(Keyring=FakeKeyring),
    )

    assert credential_supervisor_main._self_test() == 0
    assert observed
    assert {key: os.environ.get(key) for key in original} == original
