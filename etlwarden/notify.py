"""Tell the person at the PC that a job failed: a notification in the corner of the screen.

The engine has no window, so it asks Windows PowerShell to show the message. That needs no
extra package and no installation. Windows only shows messages from programs it knows from
the Start menu, so the message borrows PowerShell's entry, and its top line says so.
"""

from __future__ import annotations

import base64
import re
import subprocess

from . import runners

TIMEOUT_SECONDS = 15

# Characters XML can't hold. A job's output can contain them, and Windows would refuse the message.
_NOT_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")

# The texts come from a job's output, so they arrive in the environment and never become part of the script.
_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$title = [Security.SecurityElement]::Escape($env:ETLWARDEN_TOAST_TITLE)
$text = [Security.SecurityElement]::Escape($env:ETLWARDEN_TOAST_TEXT)
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$title</text><text>$text</text></binding></visual></toast>")
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id)
if ($env:ETLWARDEN_TOAST_SHOW -eq '1') {
    $notifier.Show($toast)
} else {
    [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($xml.GetXml()))
}
"""


def toast(title: str, text: str) -> None:
    """Show a notification. Raises if Windows PowerShell can't."""
    _powershell(title, text, show=True)


def toast_xml(title: str, text: str) -> str:
    """What `toast` would hand to Windows, without showing anything. The tests use this."""
    return base64.b64decode(_powershell(title, text, show=False)).decode("utf-8")


def _powershell(title: str, text: str, *, show: bool) -> str:
    program = runners.system_program(r"WindowsPowerShell\v1.0\powershell.exe")
    done = subprocess.run(
        [program, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _SCRIPT],
        env=dict(
            runners.job_environment(),
            ETLWARDEN_TOAST_TITLE=_clean(title),
            ETLWARDEN_TOAST_TEXT=_clean(text),
            ETLWARDEN_TOAST_SHOW="1" if show else "0",
        ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=TIMEOUT_SECONDS,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if done.returncode != 0:
        raise RuntimeError(f"Windows PowerShell ended with exit code {done.returncode}: {done.stderr.strip()[-300:]}")
    return done.stdout


def _clean(text: str) -> str:
    return runners.shorten(_NOT_XML.sub("", text), 200)
