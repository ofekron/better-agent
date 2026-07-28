from pathlib import Path


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
