from __future__ import annotations

import platform
import subprocess
import unicodedata

_MACOS_SCRIPT = """
on run argv
    display notification (item 2 of argv) with title (item 1 of argv)
end run
""".strip()

_WINDOWS_SCRIPT = """
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text></text><text></text></binding></visual></toast>')
$nodes = $xml.GetElementsByTagName('text')
$nodes.Item(0).AppendChild($xml.CreateTextNode($args[0])) > $null
$nodes.Item(1).AppendChild($xml.CreateTextNode($args[1])) > $null
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Better Agent').Show($toast)
""".strip()


def _display_text(value: str, limit: int) -> str:
    cleaned = "".join(
        " " if char in "\r\n\t" else char
        for char in str(value or "")
        if unicodedata.category(char) != "Cc" or char in "\r\n\t"
    )
    return " ".join(cleaned.split())[:limit]


def normalize_notification(title: str, body: str) -> tuple[str, str]:
    return _display_text(title, 120), _display_text(body, 500)


def build_notification_command(system: str, title: str, body: str) -> list[str]:
    if system == "Darwin":
        return ["osascript", "-e", _MACOS_SCRIPT, title, body]
    if system == "Windows":
        return [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            _WINDOWS_SCRIPT,
            title,
            body,
        ]
    return []


class DesktopNotificationApi:
    def notify_user(self, title: str, body: str) -> dict[str, object]:
        safe_title, safe_body = normalize_notification(title, body)
        if not safe_title:
            return {"success": False, "error": "title is required"}
        command = build_notification_command(platform.system(), safe_title, safe_body)
        if not command:
            return {"success": False, "error": "notifications are unsupported on this platform"}
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except OSError as exc:
            return {"success": False, "error": str(exc)}
        return {"success": True}
