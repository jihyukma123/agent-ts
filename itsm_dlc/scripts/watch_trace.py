#!/usr/bin/env python3
"""별도 터미널 창에서 Agent LLM / Tool Call Trace 를 실시간으로 모니터링하는 뷰어.

사용법:
  .venv/bin/python scripts/watch_trace.py
  .venv/bin/python scripts/watch_trace.py --clear   # 기존 로그를 비우고 새로 시작
  (또는 간단히 `tail -f data/agent_trace.log` 를 사용해도 동일하게 보입니다.)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRACE_FILE = Path(os.environ.get("AGENT_DATA_DIR", ROOT / "data")) / "agent_trace.log"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ITSM Agent 실시간 Trace 모니터")
    parser.add_argument(
        "--file",
        type=Path,
        default=Path(os.environ.get("AGENT_TRACE_FILE", DEFAULT_TRACE_FILE)),
        help="모니터링할 trace 로그 파일 경로 (기본: data/agent_trace.log)",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="시작할 때 기존 trace 로그 파일 내용을 비웁니다.",
    )
    parser.add_argument(
        "--from-start",
        action="store_true",
        help="파일의 처음부터 전체 내용을 출력한 뒤 실시간 대기합니다.",
    )
    args = parser.parse_args(argv)

    trace_path: Path = args.file.resolve()
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    if args.clear or not trace_path.exists():
        trace_path.write_text("", encoding="utf-8")

    print(f"\033[1;36m👀 [ITSM Agent Trace Monitor]\033[0m 파일 감지 중: {trace_path}")
    print("\033[2m   (종료하려면 Ctrl+C 를 누르세요. 채팅 요청이 발생하면 아래에 실시간으로 표시됩니다.)\033[0m\n")
    sys.stdout.flush()

    try:
        with trace_path.open("r", encoding="utf-8", errors="replace") as f:
            if not args.from_start and not args.clear:
                f.seek(0, os.SEEK_END)
            while True:
                line = f.readline()
                if line:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                else:
                    # 파일이 truncate/재생성되었는지 확인
                    try:
                        if trace_path.exists() and trace_path.stat().st_size < f.tell():
                            f.seek(0)
                    except OSError:
                        pass
                    time.sleep(0.08)
    except KeyboardInterrupt:
        print("\n\033[2mTrace 모니터를 종료합니다.\033[0m")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
