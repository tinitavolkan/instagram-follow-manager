"""Reject private runtime files in the tracked source distribution.

This is a narrow release check, not a comprehensive secret scanner.
"""
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {'main.py', 'README.md', 'LICENSE', 'requirements.txt', '.gitignore',
              '.gitattributes', 'CONTRIBUTING.md', 'SECURITY.md', 'CHANGELOG.md'}
PATTERNS = {
    'local Windows account path': re.compile(r'(?i)[A-Z]:[/\\]Users[/\\]'),
    'GitHub token': re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})'),
    'Google API key': re.compile(r'AIza[A-Za-z0-9_-]{30,}'),
    'private key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
}


def allowed(name):
    path = Path(name)
    if name in ROOT_FILES:
        return True
    if path.parts[0] in {'instagram_bot', 'tests', 'tools'}:
        return path.suffix == '.py'
    if path.parts[0] == 'docs':
        return path.suffix == '.md'
    if path.parts[0] == '.github':
        return path.suffix in {'.md', '.yml'}
    return name in {'examples/README.md', 'examples/demo-export/followers_1.json',
                    'examples/demo-export/following.json'}


def main():
    result = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True, check=True)
    names = sorted(name for name in result.stdout.decode('utf-8').split('\0') if name)
    if not names:
        raise SystemExit('No tracked release files to inspect.')
    problems = []
    for name in names:
        if not allowed(name):
            problems.append(f'Unexpected release path: {name}')
            continue
        content = (ROOT / name).read_text(encoding='utf-8')
        for label, pattern in PATTERNS.items():
            if pattern.search(content):
                problems.append(f'{label}: {name}')
    if problems:
        raise SystemExit('\n'.join(problems))
    print(f'Release check passed: {len(names)} tracked source/document/example files.')


if __name__ == '__main__':
    main()
