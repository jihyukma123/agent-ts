#!/usr/bin/env python3
"""Run all offline project tests; never calls a live model."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
if __name__ == '__main__':
    for name in ('itsm', 'itsm_dlc', 'harness-1'):
        print(f'\nTesting {name}', flush=True)
        subprocess.run([str(ROOT / name / '.venv/bin/python'), '-m', 'pytest', '-q'], cwd=ROOT / name, check=True)
