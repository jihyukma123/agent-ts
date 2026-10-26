#!/usr/bin/env python3
"""Install each project's pinned dependencies without copying credentials."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ('itsm', 'itsm_dlc', 'harness-1')


def main():
    uv = shutil.which('uv')
    if not uv and sys.version_info < (3, 13):
        raise SystemExit('Install uv or run this script with Python 3.13+. See README.md.')
    for name in PROJECTS:
        project = ROOT / name
        python = project / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        if not python.exists():
            command = [uv, 'venv', '--python', '3.13', str(project / '.venv')] if uv else [sys.executable, '-m', 'venv', str(project / '.venv')]
            subprocess.run(command, cwd=ROOT, check=True)
        command = [uv, 'pip', 'install', '--python', str(python)] if uv else [str(python), '-m', 'pip', 'install']
        subprocess.run(command + ['-r', str(project / 'requirements.txt')], cwd=ROOT, check=True)
        print(f'Installed: {name}', flush=True)
    env = ROOT / '.env'
    if not env.exists():
        # The template contains no credential. Existing user values are never overwritten.
        with env.open('x') as file:
            file.write((ROOT / '.env.example').read_text())
    print('Ready. Fill OPENAI_API_KEY in .env, then run: python3 run.py --trace')


if __name__ == '__main__':
    main()
