from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_credential_authority_signing_is_macos_only() -> None:
    script = (ROOT / "desktop" / "build_credential_authority.sh").read_text()

    guard = 'if [ "$(uname -s)" = "Darwin" ]; then'
    signing = 'bash "$DIR/local_codesign.sh" sign "$TARGET"'
    assert guard in script
    assert script.index(guard) < script.index(signing)
