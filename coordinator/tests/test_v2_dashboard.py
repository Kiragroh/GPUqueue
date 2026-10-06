from pathlib import Path
import re
ROOT=Path(__file__).resolve().parents[1]

def test_operator_controls_and_scope_are_explicit():
    html=(ROOT/'dashboard.html').read_text(encoding='utf8')
    assert 's.operator_unlocked' in html
    assert 'id="cancel-confirm"' in html and 'X-GPU-CSRF' in html
    assert "'/priority'" in html and "'/cancel'" in html
    assert 'j.lane' in html and 'j.vram_mb' in html and 'j.effective_priority' in html

def test_launcher_uses_scoped_operator_mode():
    assert 'open --control' in (ROOT/'Open-GPUQueue.ps1').read_text(encoding='utf8')

def test_refresh_never_overlaps_slow_request_and_always_releases_guard():
    html=(ROOT/'dashboard.html').read_text(encoding='utf8')
    assert re.search(r'if\s*\(refreshInFlight\)\s*\{\s*refreshAgain\s*=\s*true;\s*return;', html)
    assert re.search(r'refreshInFlight\s*=\s*true', html)
    assert re.search(r'finally\s*\{\s*refreshInFlight\s*=\s*false', html)


def test_cancel_uses_accessible_inline_confirmation_not_native_modal():
    html=(ROOT/'dashboard.html').read_text(encoding='utf8')
    assert 'confirm(' not in html
    assert 'Abbruch bestätigen' in html and 'Doch nicht' in html
    assert 'pendingCancel' in html and "promptCancel(j)" in html
