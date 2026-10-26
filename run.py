#!/usr/bin/env python3
"""Launch the comparison demo from any working directory."""
from pathlib import Path
import runpy

if __name__ == '__main__':
    runpy.run_path(str(Path(__file__).resolve().parent / 'itsm/demo/run.py'), run_name='__main__')
