# ITSM 시스템 (ITSM API + ITSM 웹)

사내 IT 서비스 관리(ITSM) 시스템입니다. Agent 도구 개발 실험(A: 기본 AI-DLC / B: Harness 추가 AI-DLC)에서
**이미 운영 중인 기존 시스템** 역할을 합니다.

- **ITSM API** (`itsm_api/`, :8001): 티켓·카탈로그·디렉터리·문제(Problem)·내보내기를 소유하는 서비스
- **ITSM 웹** (`itsm_web/` + `static/`, :8000): 사용자 화면. 테스트 사용자 전환, 화면용 티켓 API, 별도 Agent 서비스의 same-origin 채팅 프록시
- **하네스 적용 전** (`../itsm_dlc`, :8100) / **적용 후** (`../harness-1`, :8101): AI Assistant 패널에서 전환하는 두 에이전트.

> ⚠️ 모든 티켓은 실험용 **모의 데이터**입니다. 실제 OpenAI 서비스·모델의 현재 장애 상황을 나타내지 않으며,
> 실시간 상태 서비스와 연동되어 있지 않습니다.

---

## 전후 비교 데모: 한 번에 실행

폴더를 옮기거나 합칠 필요 없이 `demo/run.py`가 기존 세 프로젝트를 실행합니다.
각 프로젝트의 `.venv`와 의존성은 먼저 설치되어 있어야 합니다. 각 에이전트의 기존 `.env.local`을 그대로 읽으며, 키를 생성·복사·변경하지 않습니다.

```text
c/
├── itsm/         # 웹 + ITSM API + demo/run.py
├── itsm_dlc/     # 하네스 적용 전
└── harness-1/    # 하네스 적용 후
```

```bash
cd itsm
python3 demo/run.py --check  # 경로·Python·포트 확인 (모델 연결 검사는 아님)
python3 demo/run.py         # ITSM API + 웹 + 두 에이전트 모두 실행

# itsm_dlc의 기존 실시간 trace 뷰어까지 함께 실행
python3 demo/run.py --trace
```

두 실행 명령 중 하나만 사용합니다. 화면 주소는 실행 마지막에 `데모: http://127.0.0.1:포트`로 표시됩니다. 기본은 **http://127.0.0.1:8000**이며, 사용 중이면 빈 포트로 자동 변경됩니다.
사용자를 선택한 다음 **하네스 적용 전 / 적용 후**를 눌러 같은 질문을 번갈아 입력하세요.
각 에이전트의 대화는 따로 유지되며, ↻ 버튼은 선택한 에이전트의 대화만 초기화합니다.
응답 생성 중에는 에이전트·사용자 전환이 잠깐 비활성화됩니다.

두 에이전트는 같은 ITSM API와 티켓 데이터를 사용합니다. 한쪽에서 티켓을 등록·변경하면 다른 쪽에서도 보입니다.
동일 조건 비교는 조회 질문부터 시작하세요. 등록·변경 비교에서는 앞선 실행이 데이터를 바꾼다는 점을 고려해야 합니다.

- 우선 사용할 포트: 웹 `8000`, API `8001`, 적용 전 `8100`, 적용 후 `8101`. 사용 중인 포트는 자동으로 피하고, 서비스 간 연결 주소도 함께 맞춥니다. 선택한 포트는 서버가 넘겨받을 때까지 예약해 둡니다.
- DB·로그: `demo/.runtime/` 아래에 저장합니다. 기존 프로젝트의 `data/`와 분리되며 재실행 시 유지됩니다.
- 서버 로그: `demo/.runtime/logs/{api,web,baseline,harness}.log`.
- `--trace`는 **적용 전 itsm_dlc**의 기존 trace만 터미널에 표시합니다. 파일은 `demo/.runtime/baseline/agent_trace.log`입니다.
- `Ctrl+C`로 이 명령이 시작한 서버와 trace 뷰어를 모두 종료합니다. 기존 서버는 그대로 두고 다른 포트에서 실행합니다.
- 키가 없으면 서버와 화면은 실행되지만 해당 에이전트의 채팅은 비활성화됩니다.

경로나 우선 사용할 포트는 옵션으로 지정할 수 있습니다. 지정한 포트도 사용 중이면 자동 변경됩니다. `--runtime-dir`을 새 경로로 지정하면 새 데모 데이터로 시작합니다.

```bash
python3 demo/run.py --trace \
  --baseline-dir ../itsm_dlc --harness-dir ../harness-1 \
  --web-port 18000 --api-port 18001 --baseline-port 18100 --harness-port 18101 \
  --runtime-dir /tmp/itsm-demo-fresh

# 이미 실행한 데모의 trace를 별도 터미널에서 보기
../itsm_dlc/.venv/bin/python ../itsm_dlc/scripts/watch_trace.py \
  --file demo/.runtime/baseline/agent_trace.log
```

---

## 1. 시스템 구조

```
브라우저 ──(쿠키: itsm_session)──▶ ITSM 웹 (itsm_web/, :8000)
                                   ├─ session.py      사용자 전환 · 서버 측 세션 · ITSM 위임 토큰 보관/갱신
                                   ├─ tickets.py      화면용 티켓 API (ITSM API v2 중계)
                                   ├─ agent.py        화면용 Agent API (별도 서비스 중계 · 사용자 결속)
                                   ├─ agent_client.py Agent 세션 쿠키·HTTP 클라이언트
                                   ├─ itsm_client.py  ITSM API HTTP 클라이언트
                                   └─ data/web.db     세션
                                          │ HTTP  Authorization: Bearer <위임 토큰>   (client_id=portal)
                                          ▼
                                 ITSM API (itsm_api/, :8001) ◀── HTTP ── ITSM Assistant (../itsm_dlc, :8100, client_id=itsm-agent)
                                   ├─ auth.py             클라이언트 인증 · 토큰 발급/검증 · 권한 · 요청 한도
                                   ├─ routes/             auth · directory · catalog · tickets · problems · exports · legacy_v1
                                   ├─ ticket_store.py     조회: 가시성 규칙 · 검색 · 커서 페이지네이션 · 통계
                                   ├─ ticket_service.py   변경: 등록 · 수정 · 배정 · 상태 전이 · worklog · 병합 · 문제 연결
                                   ├─ workflow.py, sla.py 상태 전이 규칙 · 우선순위 매트릭스 · 업무시간 SLA
                                   ├─ errors.py           problem+json(v2) / text/plain(v1) 오류
                                   └─ data/itsm.db        ITSM 데이터
```

### 요청 흐름 (ITSM 웹)

1. 화면에서 테스트 사용자를 고르면 `POST /api/session {user_id}` → ITSM 웹이 ITSM 디렉터리에서 사용자를 확인하고,
   `POST /api/v2/auth/token`(grant_type=`token_exchange`, subject=username)으로 그 사용자의 **위임 토큰**을 받은 뒤
   `GET /api/v2/me` 로 역할·권한을 받아 `sessions` 테이블에 저장합니다. 쿠키에는 세션 ID만 들어갑니다.
2. 화면의 티켓 요청은 `/api/tickets*` → 세션의 위임 토큰으로 ITSM API `/api/v2/tickets*` 를 호출하고 응답을 그대로 전달합니다.
   위임 토큰 만료가 60초 안으로 다가오면 다시 교환해 세션에 저장합니다.
3. 권한·가시성·입력 검증의 최종 판단은 항상 ITSM API 가 합니다.

### 주요 선택과 근거

| 선택 | 근거 |
|---|---|
| ITSM API 를 별도 서비스로 두고 화면·연동 시스템은 HTTP 로만 접근 | 실제 사내 ITSM 연동과 같은 구조. 접속 주소는 `ITSM_BASE_URL` 하나로 바꿀 수 있음 |
| 에이전트는 별도 프로젝트(`../itsm_dlc`), 자기 `client_id` 로 연동 | 기존 시스템에 나중에 붙는 연동 시스템이라는 전제. ITSM 소스 대신 공개 명세만 봄 |
| 클라이언트 자격증명 + 사용자 위임 토큰(token exchange) | 연동 시스템이 사용자를 대신해 호출해도 ITSM 이 그 사용자의 권한·가시성으로 판단. 토큰은 15분 만료 |
| 행 단위 가시성 + 보이지 않는 티켓은 404 | “누가 어떤 티켓을 볼 수 있나”를 서버가 강제하고 존재 여부를 드러내지 않음 |
| 상태 전이 전용 API + 전이별 권한·필수 입력 | 상태를 임의 필드 수정으로 바꾸지 못하게 하고, 해결·보류 등에 필요한 정보를 강제 |
| 우선순위는 영향도 × 긴급도로 서버가 계산 | 클라이언트가 우선순위를 직접 정하지 못하게 함. SLA 도 우선순위에서 결정 |
| 낙관적 잠금(`If-Match`), 등록 멱등키(`Idempotency-Key`) | 동시 변경 덮어쓰기와 재시도로 인한 중복 등록 방지 |
| 모든 변경을 `ticket_service` 한 곳에서, 감사 이벤트·알림 outbox 를 같은 트랜잭션에 기록 | 변경 이력 누락 방지. 알림 발송은 outbox 를 읽는 별도 작업(미구현)의 몫 |
| v2 오류는 problem+json, 확장 필드로 다음 행동에 필요한 정보 제공 | 예: 409 전이 오류에 `allowed_transitions`, 422 카탈로그 오류에 `allowed_values` |
| 레거시 v1 유지 (`Deprecation`/`Sunset` 헤더) | 기존 사내 연동 호환. 신규 연동은 v2 |
| `create_app()` 팩토리 + `uvicorn --factory` | import 시 DB가 생기지 않고, 테스트에서 DB 경로·시계·ITSM 클라이언트를 주입하기 쉬움 |

---

## 2. 설치·실행

요구 사항: Python 3.13 (`.python-version`), [uv](https://docs.astral.sh/uv/) 권장.

```bash
# 설치 (버전이 고정된 requirements.txt 사용)
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements.txt
#   uv 없이: python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 실행: ITSM API(:8001) + ITSM 웹(:8000) 함께
scripts/dev.sh
# → http://127.0.0.1:8000

# 따로 실행하려면
.venv/bin/uvicorn itsm_api.main:create_app --factory --port 8001
.venv/bin/uvicorn itsm_web.main:create_app --factory --port 8000
```

완성된 `../itsm_dlc` Agent를 같은 화면의 **AI Assistant** 패널에서 사용하려면 ITSM을 실행한 상태에서 별도 터미널로 Agent 서비스를 시작합니다.

```bash
cd ../itsm_dlc
.venv/bin/uvicorn app.main:create_app --factory --port 8100
```

브라우저는 Agent를 직접 호출하지 않습니다. `itsm_web`의 `/api/assistant/*` 프록시가 현재 웹 세션의 사용자 ID로 Agent 세션을 확인·전환하고, Agent의 opaque HttpOnly 쿠키만 중계합니다. Agent가 꺼져 있거나 설정되지 않은 경우에도 기존 티켓 화면은 계속 동작합니다.

첫 실행 시 `data/itsm.db`, `data/web.db` 가 없으면 만들고 고정 초기 데이터를 넣습니다. 이미 있으면 기존 데이터를 그대로 사용합니다.
**이전 버전의 `data/itsm.db` 가 남아 있으면 스키마가 달라 동작하지 않으니 먼저 `scripts/reset_db.py` 를 실행하세요.**

| 환경 변수 | 대상 | 기본값 | 설명 |
|---|---|---|---|
| `ITSM_API_DATA_DIR` | ITSM API | `./data` | `itsm.db` 위치 |
| `ITSM_API_TOKEN_TTL_SECONDS` | ITSM API | `900` | 토큰 유효 시간 |
| `ITSM_API_RATE_LIMIT_PER_MINUTE` | ITSM API | `300` | 주체(클라이언트+사용자)별 분당 요청 한도 |
| `ITSM_API_EXPORT_STEP_SECONDS` | ITSM API | `1.0` | 내보내기 작업 단계별 처리 시간 |
| `ITSM_API_ENABLE_DOCS` | ITSM API | 꺼짐 | `1` 이면 `/docs`, `/redoc`, `/openapi.json` 공개 (운영 환경처럼 기본은 비공개) |
| `ITSM_WEB_DATA_DIR` | ITSM 웹 | `./data` | `web.db` 위치 |
| `ITSM_BASE_URL` | ITSM 웹 | `http://127.0.0.1:8001` | ITSM API 주소 (환경 변수 → `.env.local` → 기본값 순) |
| `ITSM_CLIENT_ID` / `ITSM_CLIENT_SECRET` | ITSM 웹 | `portal` / `portal-dev-secret` | ITSM API 클라이언트 자격증명 (개발용 시드 값) |
| `ITSM_TIMEOUT_SECONDS` | ITSM 웹 | `10` | ITSM API 호출 시간 제한 |
| `ITSM_AGENT_BASE_URL` | ITSM 웹 | `http://127.0.0.1:8100` | 하네스 적용 전 Agent 주소 |
| `ITSM_HARNESS_AGENT_BASE_URL` | ITSM 웹 | `http://127.0.0.1:8101` | 하네스 적용 후 Agent 주소 |
| `ITSM_AGENT_TIMEOUT_SECONDS` | ITSM 웹 | `120` | Agent 모델 응답을 기다리는 웹 프록시 제한 시간 |

`.env.local` 은 선택입니다(`.env.local.example` 참고). 앱은 읽기만 하고 수정하지 않으며 Git 추적에서 제외됩니다.

---

## 3. 사용자·권한·API 명세

ITSM API 를 사용하는 쪽(연동 시스템 개발자)을 위한 문서는 **[`docs/itsm-api-guide.md`](docs/itsm-api-guide.md)** 에 있습니다.

- 연동 클라이언트와 토큰(서비스 토큰·위임 토큰)
- 테스트 사용자 19명, 역할 7개와 권한표, 행 단위 가시성 규칙
- v2 API: 공통 규칙, problem+json 오류와 코드 목록, 티켓·전이·우선순위·SLA·문제·내보내기, 레거시 v1

요청·응답 스키마 전체는 `ITSM_API_ENABLE_DOCS=1` 로 ITSM API 를 실행했을 때 `/docs`, `/openapi.json` 에서 볼 수 있습니다.

테스트 사용자 요약: `admin01`(관리자), `member01`–`member03`(요청자), `desk01`·`desk02`(서비스데스크 L1),
`idops01`·`api01`·`api02`·`gpt01`·`codex01`·`media01`(운영 엔지니어 L2), `lead01`–`lead03`(팀장), `sec01`(보안 담당), `viewer01`·`viewer02`(조회자, 일반 티켓 조회만), `desk03`(비활성).

---

## 4. ITSM 웹 API

| 메서드·경로 | 세션 | 설명 |
|---|:-:|---|
| `GET /api/health` | | `{status, itsm: {base_url, reachable, version}}` |
| `GET /api/users` | | 전환할 수 있는 테스트 사용자(ITSM 디렉터리의 활성 사용자) |
| `POST /api/session` | | `{user_id}` → 사용자 전환. 없는 사용자 404 `user_not_found`, 비활성 409 `user_inactive` |
| `GET /api/session`, `DELETE /api/session` | ✔ / | 현재 사용자(`permissions` 포함) / 세션 종료(항상 204) |
| `GET /api/tickets` | ✔ | ITSM `GET /api/v2/tickets` 중계 (`q`, `status`, `cursor`, `limit` 기본 30) |
| `GET /api/tickets/{id}`, `GET /api/tickets/{id}/worklogs` | ✔ | ITSM 상세·worklog 중계 |
| `POST /api/tickets` | ✔ | ITSM 등록 중계 (`Idempotency-Key` 헤더 전달). 화면은 폼을 열 때마다 새 키를 만듭니다 |
| `GET /api/catalog/services`, `GET /api/catalog/models` | ✔ | 등록 폼용 카탈로그 |
| `GET /api/assistant/health` | | 별도 Agent 서비스의 연결·모델 설정 상태 |
| `POST /api/assistant/session` | ✔ | 현재 웹 사용자를 기준으로 Agent 세션 확인·전환 |
| `POST /api/assistant/chat` | ✔ | Agent 채팅 요청 중계. 클라이언트가 보낸 사용자 ID는 사용하지 않음 |
| `GET /api/assistant/messages`, `DELETE /api/assistant/messages` | ✔ | 현재 Agent 대화 이력 조회·초기화 |

- `/api/assistant/*`는 `?variant=baseline`(기본값) 또는 `?variant=harness`로 대상을 선택합니다. 서버에 등록된 두 주소만 사용하며, 에이전트별 쿠키·대화를 분리합니다.
- 세션 없음: 401 `no_session`. ITSM 오류는 같은 HTTP 상태로 `{"detail": {"code", "message", "errors"?}}` 형태로 전달합니다.
- ITSM API 에 연결할 수 없으면 503 `itsm_unavailable`, ITSM 웹의 자격증명이 거부되면 502 `itsm_auth_failed`.

---

## 5. 초기 데이터

`itsm_api/seed_data.py` 에 고정되어 있으며 시각도 고정값이라 초기화할 때마다 동일하게 복원됩니다.

- 팀 7개, 역할 7개, 사용자 19명(비활성 1명), 연동 클라이언트 3개(`portal`·`itsm-agent` 위임 가능, `status-board` 위임 불가)
- 서비스 7개(폐기 1개: Assistants API → OpenAI API), 컴포넌트 21개(Codex·ChatGPT 의 `로그인` 은 계정·인증팀 담당), 모델 8개(종료 1개: gpt-5.5)
- 티켓 36건(INC-001–036): 모든 상태, P1–P4, 주요 장애 3건(INC-001, 002, 024), 보안 제한 3건(INC-026–028),
  보류 2건(INC-007 외부 업체, INC-022 요청자 응답 대기), 병합된 중복 2건(INC-030 → 001, INC-031 → 002)
- worklog 32건(공개·내부 메모), 문제 3건(PRB-001 인증 세션, PRB-002 gpt-6-luna 용량, PRB-003 CDN 캐시)

| 경우 | 요청 (desk01 기준) | 결과 |
|---|---|---|
| 서비스·모델 구분 | INC-001·004(Codex 로그인, `model=null`) / INC-002·003(OpenAI API, `gpt-6-luna`) | 같은 “로그인”이라도 서비스가 다름 |
| 안 되는 것 vs 느린 것 | INC-002(일부 요청 503) / INC-003(응답은 오지만 느림) | |
| 0건 | `q=없는검색어` | 없음 |
| 여러 건 | `q=gpt-6-luna` | INC-002, 003, 031 |
| 미해결 | `status=received&status=assigned&status=investigating&status=pending&status=monitoring` | 20건 (보안 제한 2건 제외) |
| 요청자 범위 | member01 로 전체 목록 | 본인 요청 10건 + 주요 장애 2건 = 12건 |

---

## 6. 데이터 초기화와 검증

```bash
.venv/bin/python scripts/reset_db.py
```

- ITSM 데이터(티켓·사용자·카탈로그 등)를 고정 초기 상태로 되돌리고 발급된 ITSM 토큰을 모두 무효화합니다. 다음 등록 번호는 다시 `INC-037` 부터입니다.
- ITSM 웹 세션도 지웁니다. 서버가 실행 중이어도 동작하며, 브라우저에서는 사용자를 다시 선택하세요.
  (에이전트 서비스의 세션·대화는 `itsm_agent/scripts/reset_db.py` 로 따로 지웁니다.)
- `data/` 폴더를 통째로 지우고 서버를 다시 시작해도 같은 초기 상태가 만들어집니다.

### 자동화 테스트

```bash
.venv/bin/python -m pytest -q          # ITSM API + ITSM 웹 (tests/)
```

테스트는 임시 디렉터리의 별도 DB를 씁니다. ITSM 웹 테스트는 같은 프로세스의 ITSM API 앱에 `httpx.ASGITransport` 로 HTTP 연결합니다(네트워크 없음).
ITSM API 테스트는 고정 시계(`FixedClock`, 2026-10-26 월 10:00 KST)를 씁니다. `itsm_agent/` 는 자체 테스트를 가집니다.

| 파일 | 확인 내용 |
|---|---|
| `tests/test_itsm_auth.py` | 토큰 발급·위임·만료, 서비스 토큰 범위, problem+json 형식, 요청 한도 |
| `tests/test_itsm_tickets_read.py` | 역할별 가시성, 404 동일성, 내부 메모 숨김, 검색·필터, 커서 페이지네이션, 통계, 상세·SLA, 카탈로그·디렉터리 |
| `tests/test_itsm_tickets_write.py` | 등록(우선순위·자동 배정·감사·outbox), 서버 소유 필드 거부, 카탈로그 검증, 멱등키, If-Match, 수정 권한, 배정 |
| `tests/test_itsm_workflow.py` | 역할별 전이 목록, 전체 생명주기, SLA 일시정지, 409/403/422/428/412, 요청자 취소·재오픈 |
| `tests/test_itsm_misc.py` | worklog, 병합, 문제, 내보내기(202·폴링·다운로드), 레거시 v1 |
| `tests/test_sla.py` | 우선순위 매트릭스, 업무시간·주말·휴무일 계산, 일시정지 |
| `tests/test_web.py` | 사용자 전환·세션·위임 토큰 저장/갱신, 세션 필수, 티켓 중계, ITSM 오류 전달, ITSM 연결 실패, 재시작 유지, 초기화 |

---

## 7. 에이전트 프로젝트

`itsm_agent/` 는 이 저장소 안에 있지만 **독립 프로젝트**입니다(자체 `requirements.txt`, `.venv`, 테스트, README).
ITSM 코드를 import 하지 않으므로 폴더만 복사해 별도 저장소로 써도 동작합니다.

에이전트 쪽이 가진 ITSM 정보는 `itsm_agent/docs/itsm/` 의 연동 문서 두 개(`integration-guide.md`, `openapi.json`)입니다.

```bash
.venv/bin/python scripts/export_spec.py    # docs/itsm-api-guide.md + OpenAPI → itsm_agent/docs/itsm/ (itsm-agent 자격증명만 포함)
```

ITSM API 나 `docs/itsm-api-guide.md` 를 바꾸면 다시 실행하세요. 에이전트를 실제로 돌릴 때는 이 저장소의 ITSM 시스템을 먼저 실행합니다.
