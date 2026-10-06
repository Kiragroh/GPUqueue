"""Source-only regression checks. These do not launch a desktop app."""
from pathlib import Path
import re
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SOURCE = '\n'.join((ROOT/'desktop'/file).read_text(encoding='utf-8-sig') for file in ('GPUqueue.cs', 'DesktopServices.cs'))


def release_source():
    return re.sub(r'#if DESKTOP_TESTS\n.*?#endif', '', SOURCE, flags=re.S)


def test_only_process_start_is_user_clicked_fixed_http_link():
    text = release_source()
    assert text.count('Process.Start(') == 1
    assert 'Process.Start(new ProcessStartInfo(Settings.DashboardUrl) { UseShellExecute = true });' in text
    assert 'const string DashboardUrl = "http://127.0.0.1:11436/";' in text
    for launcher in ('client.py', 'settings.Python', 'settings.Coordinator', 'CreateNoWindow', 'ProcessWindowStyle', 'RedirectStandardOutput'):
        assert launcher not in text


def test_release_has_no_writes_control_auth_or_configured_programs():
    text = release_source()
    for prohibited in ('File.Write', 'Directory.Create', 'Registry.', 'control.token', 'open --control', 'config.json', 'PostAsync', 'SendAsync'):
        assert prohibited not in text
    assert 'FileAccess.Read' in text
    assert 'AllowAutoRedirect = false' in text
    assert 'UseDefaultCredentials = false' in text
    assert 'UseCookies = false' in text


def test_manifest_uses_existing_user_rights_only():
    xml = ET.parse(ROOT/'desktop/app.manifest')
    privilege = xml.find('.//{urn:schemas-microsoft-com:asm.v3}requestedExecutionLevel')
    assert privilege.attrib == {'level': 'asInvoker', 'uiAccess': 'false'}


def test_autostart_mutates_only_the_verified_existing_tray_task():
    assert 'const string TaskName = "GPUqueue Tray";' in SOURCE
    assert 'actions.Count != 1' in SOURCE
    assert '(string)action.Arguments != "--tray"' in SOURCE
    assert 'if (change.HasValue) task.Enabled = change.Value;' in SOURCE
    for forbidden in ('RegisterTask', 'CreateTask', 'DeleteTask', 'GPUqueue Supervisor'):
        assert forbidden not in SOURCE


def test_icon_embedded_and_lifetime_preserved():
    assert '/win32icon:' in (ROOT/'Build.ps1').read_text(encoding='utf-8-sig')
    assert (ROOT/'desktop/GPUqueue.ico').is_file()
    assert 'statusIcons.Add(state, next)' in SOURCE
    assert 'old.Dispose()' not in SOURCE
