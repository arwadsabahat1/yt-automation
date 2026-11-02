import importlib.util
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('source_guard', Path(__file__).resolve().parents[1] / 'scripts/check_source_privacy.py')
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


@pytest.mark.parametrize('path', ['data/private/token.json', '.env', 'config.yaml', 'PROJECT_PLAN.md',
    'notes/internal.md', 'analytics/REPORT.md', 'scripts/publish_episode.py', 'preview.mp4', '.git/config'])
def test_only_explicit_source_paths_are_allowed(path):
    assert not guard.allowed_path(path)


def test_empty_templates_and_generic_code_are_allowed():
    assert guard.allowed_path('main.py')
    assert guard.allowed_path('config.example.yaml')
    assert guard.allowed_path('publishing/uploader.py')
    assert not guard.scan_content('.env.example', b'YOUTUBE_API_KEY=\n')
    assert guard.scan_content('.env.example', b'YOUTUBE_API_KEY=nonempty\n')


def test_private_values_are_reported_without_printing_the_value():
    private = 'a-local-private-value'
    findings = guard.scan_content('core/example.py', private.encode(), known_private=[private])
    assert findings and private not in repr(findings)


def test_staged_scan_checks_index_not_only_working_copy(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    file = tmp_path / '.env.example'
    file.write_text('YOUTUBE_API_KEY=not-empty\n')
    subprocess.run(['git', 'add', '.env.example'], cwd=tmp_path, check=True)
    file.write_text('YOUTUBE_API_KEY=\n')
    assert guard.scan(tmp_path, staged=True)[0]
    assert not guard.scan(tmp_path, tracked=True)[0]


def test_exported_entry_point_has_help_without_connecting_accounts():
    import sys
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / 'main.py'), '--help'], cwd=root,
                            capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0
    assert 'analytics' in result.stdout and 'upload' in result.stdout
