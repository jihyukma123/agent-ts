# ITSM 에이전트 하네스 비교 데모

**하네스 적용 전 `itsm_dlc` / 적용 후 `harness-1`을 같은 ITSM 화면에서 비교**합니다.
두 에이전트 모두 AI-DLC로 개발한 결과물입니다. 저장소 이름은 agent-ts이지만 실행 코드는 Python/FastAPI와 일반 JavaScript입니다.

## 빠른 실행

지원 환경: **macOS, Linux, Windows의 WSL2**. Git과 `python3`, 그리고 **uv 또는 Python 3.13 이상**이 필요합니다. 네이티브 Windows 실행은 지원하지 않습니다.
`uv`가 있으면 설치 스크립트가 Python 3.13을 찾아 사용합니다. 없으면 Python 3.13 이상으로 스크립트를 실행하세요.

```bash
git clone https://github.com/jihyukma123/agent-ts.git
cd agent-ts
python3 scripts/setup.py
```

설치 스크립트가 세 프로젝트의 가상환경·고정 버전 의존성을 설치하고, 키가 비어 있는 `.env`를 만듭니다. 기존 `.env`는 덮어쓰지 않습니다.
루트 `.env`를 편집해 **자신의 키를 한 번만** 입력하세요.

```dotenv
OPENAI_API_KEY=여기에_본인_API_키
```

```bash
# 네 서비스 + 적용 전 에이전트의 실시간 trace
python3 run.py --trace
```

터미널 마지막의 **`데모: http://127.0.0.1:포트`** 주소로 접속합니다. 사용자를 선택하고 **하네스 적용 전 / 적용 후** 버튼을 누르세요.
기본 포트가 사용 중이면 빈 포트로 자동 변경하고 서비스 연결도 맞춥니다. `Ctrl+C`로 이번 실행의 서버와 trace 뷰어를 함께 종료합니다.

- 모델은 기존 실험과 같은 **gpt-6-luna / reasoning high**입니다. 키에 이 모델의 접근 권한과 사용 한도가 필요합니다. 자동으로 다른 모델로 바꾸지 않습니다.
- 실제 모델 채팅에는 OpenAI API 비용이 발생합니다. 키가 없어도 서버·티켓 화면·오프라인 테스트는 실행할 수 있습니다.
- 별도 DB 서버나 Node 빌드는 필요 없습니다. SQLite에 모의 티켓을 자동으로 생성합니다.
- 모든 사용자·티켓·개발용 ITSM 자격증명은 데모용입니다. 운영 서비스 연결을 전제로 하지 않습니다.

## 실행 명령

저장소 루트에서 실행합니다.

| 명령 | 동작 |
|---|---|
| `python3 run.py` | ITSM 웹·API + 두 에이전트 |
| `python3 run.py --trace` | 위 서비스 + itsm_dlc trace 뷰어 |
| `python3 run.py --check` | 경로·Python·사용 가능한 포트 확인. 모델 연결은 검사하지 않음 |
| `python3 scripts/test.py` | 세 프로젝트의 오프라인 테스트. 실제 모델 호출 없음 |
| `python3 run_eval.py` | 두 버전 각각 100회 실제 모델 주입 공격 평가. API 비용 발생 |

실행 중인 데모의 trace만 다른 터미널에서 보려면:

```bash
itsm_dlc/.venv/bin/python itsm_dlc/scripts/watch_trace.py \
  --file itsm/demo/.runtime/baseline/agent_trace.log
```

trace는 **적용 전 itsm_dlc에 추가된 기존 기능**입니다. 적용 후 에이전트의 trace까지 합쳐 보여주는 기능은 아닙니다.

다른 키 파일이나 별도 데이터를 사용하려면:

```bash
python3 run.py --env-file /path/to/local.env --runtime-dir /tmp/itsm-demo-new --trace
```

수동으로 서버를 각각 실행하려면 각기 다른 터미널에서 다음 명령을 사용합니다. 이 경우 포트 자동 선택은 적용되지 않으며 각 프로젝트의 기존 `data/`를 사용합니다.

```bash
(cd itsm && .venv/bin/python -m uvicorn itsm_api.main:create_app --factory --port 8001)
(cd itsm && .venv/bin/python -m uvicorn itsm_web.main:create_app --factory --port 8000)
(cd itsm_dlc && AGENT_ENV_FILE=../.env .venv/bin/python -m uvicorn app.main:create_app --factory --port 8100)
(cd harness-1 && AGENT_ENV_FILE=../.env .venv/bin/python -m uvicorn app.main:create_app --factory --port 8101)
```

## 폴더와 데이터

```text
agent-ts/
├── itsm/             # 모의 ITSM API·웹, 전후 전환 UI, 통합 실행기
├── itsm_dlc/         # 기본 AI-DLC 에이전트, trace 뷰어
├── harness-1/        # 추가 하네스를 적용한 에이전트
├── build-harness/    # 개발 단계에서 사용한 규칙·지식·템플릿·센서
├── evals/ticket-injection/  # 평가 코드·공격 시나리오·기존 200회 결과
├── scripts/          # 의존성 설치와 오프라인 테스트
├── run.py            # 통합 실행
└── run_eval.py       # 각 100회 실제 모델 평가
```

통합 데모의 데이터·로그는 `itsm/demo/.runtime/`에 저장하고 재실행 시 유지합니다. 초기 데이터가 필요하면 `--runtime-dir`에 새 경로를 지정하세요. 대화는 에이전트별로 분리되지만, 두 에이전트는 같은 ITSM 티켓 데이터를 공유합니다.

`.env`, 가상환경, 실행 DB, 인증 세션, 런타임 로그는 Git에 포함하지 않습니다. 다른 PC에서는 의존성을 새로 설치하고 자신의 키를 입력합니다.

## 기존 공격 평가 결과

[상세 보고서와 각 회차 근거](evals/ticket-injection/results/20261027-v6-100/report.md)

동일 공격 10종 × 목록·상세 2종 × 5회 반복으로 각 버전 100회를 실행했습니다.

| 기준 | 적용 전 | 적용 후 |
|---|---:|---:|
| 모델이 공격 지시를 따름 | 0/100 | 0/100 |
| 모델 답변 본문에 공격 원문 포함 | 0/100 | 0/100 |
| 함께 반환한 티켓 카드에 공격 원문 포함 | 100/100 | 0/100 |

**행동 방어와 사용자 화면 노출은 별도 기준입니다.** 적용 전은 원문을 카드로 반환하고, 적용 후는 해당 카드 없이 요약을 반환합니다. 이 차이를 더 강한 지시 방어 또는 원문 정제 효과로 단정할 수 없습니다. 이 200회 결과가 모든 공격에 대한 안전성을 보장하지는 않습니다.

새 평가를 실행하면 현재 코드를 복사해 고정하고, 케이스마다 별도의 DB·새 대화·관리자 세션을 사용합니다. 기존 서버와 데모 데이터는 변경하지 않습니다. 저장된 평가 코드는 HTTP 라우트·도구·ITSM API·실제 모델을 거치며 로컬 HTTP 전달만 ASGITransport를 사용합니다.

## 문제 해결

- `uv` 또는 Python 버전 오류: uv를 설치하거나 `python3.13 scripts/setup.py`로 실행하세요.
- 채팅 비활성화: 루트 `.env`의 `OPENAI_API_KEY`를 설정하고 서버를 다시 실행하세요.
- 모델 접근·인증·사용 한도 오류: API 계정의 해당 모델 권한·키·잔여 한도를 확인하세요.
- 다른 포트로 실행됨: 고정 주소 대신 터미널의 `데모:` 주소를 사용하세요.
- 서버 시작 실패: `itsm/demo/.runtime/logs/`의 `api.log`, `web.log`, `baseline.log`, `harness.log`를 확인하세요.
