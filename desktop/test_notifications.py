from __future__ import annotations

from notifications import build_notification_command, normalize_notification


def test_notification_text_is_bounded_and_control_free() -> None:
    title, body = normalize_notification("A\nB", "x" * 2000 + "\x00")
    assert title == "A B"
    assert len(body) == 500
    assert "\x00" not in body


def test_macos_command_keeps_untrusted_text_out_of_script() -> None:
    title = '$(touch /tmp/nope) "quoted"'
    body = "body; rm -rf /"
    command = build_notification_command("Darwin", title, body)
    assert command[0:2] == ["osascript", "-e"]
    assert command[-2:] == [title, body]
    assert title not in command[2]
    assert body not in command[2]


def test_windows_command_keeps_untrusted_text_out_of_script() -> None:
    title = "'; Start-Process calc; '"
    body = "$env:SECRET"
    command = build_notification_command("Windows", title, body)
    assert command[0] == "powershell.exe"
    assert title not in command[4]
    assert body not in command[4]
    assert command[-2:] == [title, body]

