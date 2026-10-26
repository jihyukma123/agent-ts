# ITSM Assistant

사내 IT 서비스 관리(ITSM) 시스템을 쓰는 동료들을 돕는 대화형 도우미입니다.
OpenAI Agents SDK(`Agent` + `Runner`)로 실제 모델과 대화하며, 기존 ITSM 시스템에 **연동 클라이언트(`client_id=itsm-agent`)로 붙는 별도 서비스**입니다.

- 현재 상태: 사용자 전환·세션, ITSM 위임 토큰 발급·갱신, 채팅 API·화면, 대화 이력, 티켓 조회·등록·수정 도구가 동작합니다.
- 기존 Agent에 OpenAI Agents SDK function tool `ticket_read`, `ticket_write`, `ticket_update` 세 개만 연결되어 있습니다. 새 Agent나 별도 서비스를 두지 않습니다.
- ITSM 시스템의 소스 코드는 이 프로젝트에 없습니다. ITSM 에 대해 알 수 있는 것은 ITSM 팀이 전달한 **`docs/itsm/` 의 연동 문서**와 실행 중인 ITSM API 뿐입니다.

> ⚠️ ITSM 개발 환경의 티켓은 모두 실험용 모의 데이터입니다.

---

## 1. 구조

```
브라우저 ──(쿠키: itsm_agent_session)──▶ ITSM Assistant (이 프로젝트, :8100)
                                          ├─ app/session.py      사용자 전환 · 서버 측 세션 · ITSM 위임 토큰 보관/갱신
                                          ├─ app/itsm_auth.py    ITSM v2 인증·디렉터리·위임 HTTP 경계
                                          ├─ app/itsm_tickets.py 티켓 목록·상세·등록·수정 gateway
                                          ├─ app/agent/
                                          │    ├─ routes.py      /api/agent/* (세션 필수)
                                          │    ├─ service.py     Runner.run(max_turns=8), SQLiteSession 대화 이력
                                          │    ├─ assistant.py   기존 Agent 정의·안전 지침·세 도구 등록
                                          │    ├─ tools.py       ticket_read/write/update 입력 계약·결과 래핑
                                          │    └─ context.py     AgentUserContext + 서버 전용 티켓 capability
                                          ├─ app/db.py           data/agent.db (sessions)
                                          └─ app/config.py       설정, .env.local 에서 OPENAI_API_KEY · ITSM 접속 정보 로드
                                                 │ HTTP  Authorization: Bearer <위임 토큰>
                                                 ▼
                                        기존 ITSM 시스템의 ITSM API (기본 http://127.0.0.1:8001)
```

### 요청 흐름

1. 화면에서 테스트 사용자를 고르면 `POST /api/session {user_id}` → ITSM 디렉터리에서 사용자를 확인하고,
   ITSM `POST /api/v2/auth/token`(grant_type=`token_exchange`, subject=username)으로 그 사용자의 **위임 토큰**을 받아
   `GET /api/v2/me` 의 역할·권한과 함께 `sessions` 테이블에 저장합니다. 쿠키에는 세션 ID만 들어갑니다.
2. `app/session.py` 의 `itsm_token_for(request, user)` 가 현재 사용자의 유효한 위임 토큰을 돌려줍니다(만료 60초 전이면 다시 교환해 세션에 저장).
3. `POST /api/agent/chat` → 세션 확인 → 현재 Request와 CurrentUser에 결합된 `ItsmTicketGateway` 생성 →
   `AgentUserContext` 안에 repr로 노출되지 않는 서버 전용 capability로 전달 →
   `Runner.run(agent, message, context=..., session=SQLiteSession(conversation_id), max_turns=8)`.
4. 도구가 실행되면 gateway가 유효한 위임 토큰을 Authorization 헤더에만 사용해 `/api/v2/tickets*`를 호출합니다.
   토큰 만료 401은 기존 갱신 경로로 한 번만 갱신·재시도하며, 권한과 티켓 가시성은 ITSM이 판단합니다.
5. 사용자를 전환하면(같은 사용자를 다시 선택해도) 새 세션과 새 대화가 시작되고 이전 대화 이력은 지워집니다.

### 주요 선택과 근거

| 선택 | 근거 |
|---|---|
| ITSM 과는 HTTP API 로만 연동, 자기 `client_id` 사용 | 기존 시스템에 나중에 붙는 연동 서비스. ITSM 내부 구현에 의존하지 않음 |
| 위임 토큰을 서버 측 세션에 보관 | ITSM 이 대화 중인 사용자의 권한·가시성으로 판단하게 함. 토큰은 브라우저·모델 입력에 노출하지 않음 |
| 모델을 `OpenAIResponsesModel(model="gpt-6-luna", openai_client=AsyncOpenAI(api_key=...))`로 명시 생성 | 키를 `.env.local` 에서만 읽고 `os.environ` 에 쓰지 않음. 대체 모델 로직 없음 |
| 대화 이력에 SDK `SQLiteSession` 사용 | 자체 이력 관리·Agent loop 를 만들지 않고 SDK 기능을 그대로 사용 |
| SDK 트레이싱 비활성(`RunConfig(tracing_disabled=True)`) | 대화 데이터를 외부 트레이싱으로 보내지 않음. 필요하면 `app/agent/service.py` 에서 켤 수 있음 |
| 티켓 텍스트를 `untrusted_data` 로 래핑 | ITSM 본문 안의 지시문을 Agent 지시로 취급하지 않음 |
| 등록 멱등키와 수정 `If-Match` 헤더를 서버에서 관리 | 중복 등록을 줄이고, 가장 최근 version을 사용한 확인된 수정만 허용 |
| `create_app()` 팩토리 + `uvicorn --factory` | 테스트에서 DB 경로·모델·ITSM 클라이언트를 주입하기 쉬움 |

---

## 2. 설치·실행

요구 사항: Python 3.13 (`.python-version`), [uv](https://docs.astral.sh/uv/) 권장. **ITSM API 가 실행 중이어야** 사용자 목록·세션이 동작합니다.

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements.txt

cp .env.local.example .env.local      # OPENAI_API_KEY 입력 (이미 있으면 덮어쓰지 마세요)

.venv/bin/uvicorn app.main:create_app --factory --port 8100
# → http://127.0.0.1:8100
```

| 환경 변수 / `.env.local` | 기본값 | 설명 |
|---|---|---|
| `OPENAI_API_KEY` (`.env.local` 에만) | – | 없으면 채팅이 503 `agent_not_configured`. 서버 시작 시 한 번 읽음 |
| `ITSM_BASE_URL` | `http://127.0.0.1:8001` | ITSM API 주소 |
| `ITSM_CLIENT_ID` / `ITSM_CLIENT_SECRET` | 개발 환경 설정값 | ITSM 연동 클라이언트 자격증명. 운영에서는 별도 발급 값 사용 |
| `ITSM_TIMEOUT_SECONDS` | `10` | ITSM API 호출 시간 제한 |
| `AGENT_DATA_DIR` | `./data` | `agent.db`, `agent_history.db` 위치 |

- 키·secret 은 `os.environ`, 로그, API 응답, Agent 실행 context, 대화 이력에 넣지 않습니다. OpenAI 오류 메시지에 키 일부가 섞이면 `sk-***` 로 가립니다.
- 모델 `gpt-6-luna`, reasoning effort `high` 는 고정입니다(`app/config.py`). 사용할 수 없으면 **다른 모델로 대체하지 않고** 원인을 응답합니다.
- 세션·대화 초기화: `.venv/bin/python scripts/reset_db.py` (ITSM 데이터는 건드리지 않음)

---

## 3. ITSM 연동 문서 (`docs/itsm/`)

| 파일 | 내용 |
|---|---|
| `integration-guide.md` | 연동 방식(클라이언트·토큰), 테스트 사용자·역할·권한, 가시성 규칙, v2 API 규칙, 오류 형식과 코드, 상태 전이, 우선순위·SLA, 문제·내보내기, 레거시 v1, 이 서비스에 발급된 개발 환경 자격증명 |
| `openapi.json` | ITSM API 전체 요청·응답 스키마 (OpenAPI 3) |

ITSM 운영팀이 전달한 문서이며 이 프로젝트에서 수정하지 않습니다.

---

## 4. API

| 메서드·경로 | 세션 | 설명 |
|---|:-:|---|
| `GET /api/health` | | `{status, itsm: {base_url, reachable, version}, agent: {configured, model, reasoning_effort}}` |
| `GET /api/users` | | 전환할 수 있는 테스트 사용자(ITSM 디렉터리의 활성 사용자) |
| `POST /api/session` | | `{user_id}` → 사용자 전환. 없는 사용자 404 `user_not_found`, 비활성 409 `user_inactive` |
| `GET /api/session`, `DELETE /api/session` | ✔ / | 현재 사용자(`permissions` 포함) / 세션 종료(항상 204) |
| `POST /api/agent/chat` | ✔ | `{message}`(1–4000자) → `{reply, context: {user_id, username, role}}` |
| `GET /api/agent/messages`, `DELETE /api/agent/messages` | ✔ | 현재 대화 이력 / 초기화 |

| HTTP | code | 의미 |
|---|---|---|
| 401 | `no_session` | 세션 없음 (키 설정 여부보다 먼저 검사) |
| 503 | `itsm_unavailable` | ITSM API 연결 실패 |
| 502 | `itsm_auth_failed` | ITSM 이 이 서비스의 자격증명을 거부 |
| 503 | `agent_not_configured` | `.env.local` 에 `OPENAI_API_KEY` 없음 |
| 502 | `model_unavailable` / `model_access_denied` / `model_request_rejected` / `agent_auth_failed` / `model_api_error` | OpenAI API 오류 (404 / 403 / 400 / 401 / 기타) |
| 503 | `model_rate_limited` | OpenAI 사용 한도 초과 |
| 504 | `model_connection_failed` | OpenAI 연결 실패·시간 초과 |
| 500 | `agent_run_failed` | SDK 실행 오류 |

502 계열 모델 오류 메시지에는 "다른 모델로 대체하지 않았습니다"가 포함됩니다.

---

## 5. 티켓 도구

| 도구 | 지원 범위 | 필수 절차 |
|---|---|---|
| `ticket_read` | ID 상세 조회 또는 검색·상태·우선순위·서비스·컴포넌트·모델·담당·요청자·분류·시각·커서 목록 조회 | 상세는 version/ETag, 목록은 다음 커서를 반환 |
| `ticket_write` | 제목, 설명, 서비스, 선택 컴포넌트·모델, 영향도, 긴급도, 영향 범위, 발생 시각, 선택 분류로 등록 | 필수 정보 수집 → 초안 요약 → 사용자의 명시적 확인 후 호출 |
| `ticket_update` | 제목, 설명, 컴포넌트, 모델, 영향도, 긴급도, 영향 범위 수정 | `ticket_read` → 변경 전/후 제시 → 명시적 확인 → 조회한 version으로 PATCH |

상태 전이, 배정, 댓글·내부 메모, 병합, 문제 연결, `is_major`, 서비스·분류 수정은 세 도구의 범위 밖입니다. 수정 중 412 `version_conflict`가 발생하면 자동으로 덮어쓰지 않고 다시 조회·확인해야 합니다. 403/404/412/422/428/429와 연결·시간 초과는 `error_code`, `field`, `reason`, `retryable`, `hint`를 갖는 구조화 결과로 정규화됩니다.

---

## 6. 테스트

```bash
.venv/bin/python -m pytest -q
```

ITSM 이나 OpenAI 를 호출하지 않습니다. ITSM 인증·디렉터리는 `tests/conftest.py`의 `FakeItsm`으로,
티켓 HTTP 경계는 `httpx.MockTransport`로 대체합니다. 모델은 호출 인자를 기록하는 `FakeModel`로 바꾸고 SDK의 `Agent`/`Runner`/`SQLiteSession`은 그대로 씁니다.

| 파일 | 확인 내용 |
|---|---|
| `tests/test_session.py` | ITSM 디렉터리 사용자 목록, 사용자 전환·세션, 위임 토큰 서버 보관·갱신, 세션 필수, 위조 쿠키, ITSM 연결 실패·자격증명 거부, 화면 |
| `tests/test_itsm_tickets.py` | v2 목록·상세·등록·수정, 커서, ETag/If-Match, 멱등키, 401 갱신 1회, 412/428/422/429/timeout 정규화, 비밀 비노출 |
| `tests/test_ticket_tools.py` | V1~V6 계약, JSON Schema, 인자 검증, 세션 capability 격리, 생략/null, untrusted 래핑, 연속 실패 상한 |
| `tests/test_agent.py` | 키 없음 안내, 고정 모델·reasoning, 세 도구·instructions, 백엔드 context, `max_turns=8`, 대화 이력, 모델 오류 비대체, 키 비노출 |

실제 모델 연결 확인(비용 발생): ITSM API 와 이 서버를 띄운 뒤 `.venv/bin/python scripts/agent_smoke.py`. V2(쓰기 전 확인·범위 밖 거절)와 V6(티켓 본문 지시 무시) live 평가는 실제 OpenAI API 키, 사용 가능한 고정 모델, 실험용 ITSM이 준비된 경우에만 수동으로 실행합니다. 자동화 suite에서는 실제 OpenAI API를 호출하지 않습니다.

---

## 7. 구현 위치

| 위치 | 할 일 |
|---|---|
| `app/agent/assistant.py` | 기존 Agent에 `TICKET_TOOLS`와 조회→확인→쓰기·untrusted·범위 지침 등록 |
| `app/agent/tools.py` | 세 `@function_tool`, 모델 노출 JSON Schema, 구조화 오류, untrusted 결과 조립 |
| `app/itsm_tickets.py` | 위임 토큰을 사용한 ITSM v2 티켓 HTTP 계약과 오류 정규화 |
| `app/session.py` 의 `itsm_token_for` | 현재 사용자 위임 토큰의 선제/401 강제 갱신 |
| `app/agent/service.py` | `Runner.run` 호출 위치 (실행 설정, 오류 처리) |
| `tests/test_itsm_tickets.py`, `tests/test_ticket_tools.py`, `tests/test_agent.py` | HTTP 경계, 도구 계약, Agent/Runner 통합 회귀 |
