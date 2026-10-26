#!/usr/bin/env bash
# ITSM API(:8001)와 ITSM 웹(:8000)을 함께 실행합니다. Ctrl+C 로 둘 다 종료됩니다.
# 사용법: scripts/dev.sh
set -euo pipefail
cd "$(dirname "$0")/.."

.venv/bin/uvicorn itsm_api.main:create_app --factory --port 8001 --reload --reload-dir itsm_api &
ITSM_PID=$!
trap 'kill $ITSM_PID 2>/dev/null || true' EXIT INT TERM

.venv/bin/uvicorn itsm_web.main:create_app --factory --port 8000 --reload --reload-dir itsm_web --reload-dir static
