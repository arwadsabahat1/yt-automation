"""Fail closed on prohibited source paths and common account/secret signatures.

Only finding type and path are printed; matched private values are never printed.
"""
import argparse
import re
import subprocess
from pathlib import Path, PurePosixPath

ROOT_FILES = {'main.py', 'README.md', 'SECURITY.md', 'requirements.txt', 'pytest.ini', '.env.example', 'config.example.yaml',
              '.gitignore', '.gitattributes'}
PACKAGES = {'core', 'research', 'writing', 'production', 'publishing', 'analytics', 'income', 'tests'}
SCRIPTS = {'scripts/schedule.ps1', 'scripts/schedule-analytics.ps1', 'scripts/check_source_privacy.py'}
EXAMPLES = {'examples/campaign.json', 'examples/studio_metrics.csv'}
PATTERNS = {
    'Google API key': re.compile(r'\bAIza[A-Za-z0-9_-]{35}\b'),
    'Google OAuth client': re.compile(r'\b\d{8,}-[a-z0-9_-]+\.apps\.googleusercontent\.com\b'),
    'Google OAuth token': re.compile(r'\b(?:ya29\.[A-Za-z0-9._-]{15,}|GOCSPX-[A-Za-z0-9_-]{15,})'),
    'GitHub access token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b'),
    'Provider API key': re.compile(r'\b(?:gsk_[A-Za-z0-9]{25,}|sk-(?:proj-)?[A-Za-z0-9_-]{25,})\b'),
    'Private key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'Actual channel ID': re.compile(r'\bUC[A-Za-z0-9_-]{22}\b'),
    'Existing video URL': re.compile(r'https?://(?:www\.)?(?:youtu\.be/|youtube\.com/(?:shorts/|watch\?v=))[A-Za-z0-9_-]{11}(?![A-Za-z0-9_-])'),
    'User profile path': re.compile(r'[A-Z]:[\\/]Users[\\/][^\\/\s"\']+', re.I),
}
EMAIL = re.compile(r'\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})\b', re.I)


def allowed_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '__pycache__' in path.parts:
        return False
    if name in ROOT_FILES or name in SCRIPTS or name in EXAMPLES or name == '.githooks/pre-commit':
        return True
    if name in ('motion/package.json', 'motion/package-lock.json'):
        return True
    if len(path.parts) == 3 and path.parts[:2] == ('assets', 'fonts'):
        return path.suffix == '.ttf' or (path.name.startswith('OFL-') and path.suffix == '.txt')
    if path.parts[0] in PACKAGES and path.suffix == '.py':
        return True
    return name == 'analytics/dashboard.html' or name == 'production/clip_lab.html' or (
        path.parts[:2] == ('publishing', 'templates') and path.suffix == '.html')


def scan_content(name, data, known_private=()):
    findings = []
    if len(data) > 5 * 1024 * 1024:
        findings.append((name, 'Unexpected large file'))
    if name.endswith('.ttf'):
        return findings
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return findings + [(name, 'Unexpected binary content')]
    for label, pattern in PATTERNS.items():
        if pattern.search(text):
            findings.append((name, label))
    if any(domain.lower() not in {'example.com', 'example.org', 'example.net', 'users.noreply.github.com'}
           for domain in EMAIL.findall(text)):
        findings.append((name, 'Personal or non-example email'))
    if any(value and len(value) >= 8 and value.encode() in data for value in known_private):
        findings.append((name, 'Matches local private material'))
    if name == '.env.example' and any(line.strip() and not line.lstrip().startswith('#') and
        ('=' not in line or line.partition('=')[2].strip()) for line in text.splitlines()):
        findings.append((name, 'Environment example contains a value'))
    return findings


def scan(root: Path, tracked=False, staged=False, known_private=()):
    if tracked or staged:
        raw = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root)
        names = [name for name in raw.decode('utf-8').split('\0') if name]
    else:
        names = [p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()
                 and not set(p.relative_to(root).parts) & {'.git', '__pycache__', '.pytest_cache', '.venv', 'node_modules'}]
    findings = []
    for name in names:
        path = root / name
        if not allowed_path(name):
            findings.append((name, 'Not on the source allowlist'))
            continue
        if path.is_symlink():
            findings.append((name, 'Symlink is not allowed'))
            continue
        if staged:
            data = subprocess.check_output(['git', 'show', ':' + name], cwd=root)
        else:
            data = path.read_bytes()
        findings.extend(scan_content(name, data, known_private))
    return findings, len(names)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--tracked', action='store_true')
    parser.add_argument('--staged', action='store_true')
    args = parser.parse_args()
    findings, count = scan(args.root.resolve(), args.tracked, args.staged)
    for name, label in findings:
        print(f'BLOCKED {name}: {label}')
    print(f'{count} source files checked; {len(findings)} privacy findings.')
    raise SystemExit(bool(findings))


if __name__ == '__main__':
    main()
