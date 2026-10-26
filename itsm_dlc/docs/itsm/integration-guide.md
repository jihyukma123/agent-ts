# ITSM API 연동 가이드

사내 IT 서비스 관리(ITSM) 시스템의 API 를 사용하는 연동 시스템 개발자를 위한 문서입니다.
요청·응답 스키마는 `openapi.json` 에, 이 문서에는 인증 방식·권한·가시성·상태 전이·SLA·오류 처리처럼
스키마만으로는 알 수 없는 규칙을 정리합니다.

> 개발 환경의 모든 티켓·사용자는 모의 데이터입니다.

## 1. 연동 방식

| 항목 | 값 |
|---|---|
| 기본 주소 (개발 환경) | `http://127.0.0.1:8001` |
| 현재 버전 | v2 (`/api/v2/*`). v1 은 2027-03-31 종료 예정 |
| 인증 | 연동 클라이언트 자격증명 → 서비스 토큰 또는 사용자 위임 토큰 (`POST /api/v2/auth/token`) |
| 상태 확인 | `GET /api/v2/health` (인증 불필요) |

### 1.1 연동 클라이언트

연동 시스템마다 `client_id` / `client_secret` 이 발급됩니다(연동 신청 시 서비스데스크에서 발급).
사용자를 대신해 호출할 수 있는지(위임 허용)는 클라이언트마다 다르게 설정됩니다.

### 1.2 토큰

```http
POST /api/v2/auth/token
Content-Type: application/json

{"grant_type": "token_exchange", "client_id": "<client_id>", "client_secret": "<client_secret>", "subject": "member01"}
```

```json
{"access_token": "...", "token_type": "Bearer", "expires_in": 900, "expires_at": "2026-10-26T01:15:00Z", "subject": "member01"}
```

- `grant_type=token_exchange`: `subject`(username) 사용자를 **대신하는 위임 토큰**. 그 사용자의 역할 권한과 가시성 범위를 그대로 가집니다.
  티켓 조회·등록·변경은 모두 위임 토큰으로 해야 합니다.
- `grant_type=client_credentials`: 사용자 없는 **서비스 토큰**. `catalog:read`, `directory:read` 만 가집니다(사용자 목록·카탈로그 조회용).
- 토큰 유효 시간은 기본 15분입니다. 만료되면 401 `token_expired` 이므로 다시 발급받습니다. 토큰 원문은 서버에 저장되지 않습니다.
- 비활성(퇴사) 사용자는 위임할 수 없습니다(400 `invalid_grant`).

## 2. 사용자·역할·권한

개발·테스트 환경 디렉터리(`GET /api/v2/users`)에 있는 계정입니다. 연동 시스템은 이 사용자들의 username 으로 위임 토큰을 받습니다.

| ID | username | 이름 | 역할 | 팀 |
|---|---|---|---|---|
| 1 | `admin01` | 김하늘 | admin 관리자 | 서비스데스크 |
| 2 | `member01` | 박바다 | requester 요청자 | – |
| 3 | `member02` | 최가람 | requester 요청자 | – |
| 4 | `member03` | 서지민 | requester 요청자 | – |
| 5 | `desk01` | 이서준 | agent_l1 서비스데스크(L1) | 서비스데스크 |
| 6 | `desk02` | 정하린 | agent_l1 서비스데스크(L1) | 서비스데스크 |
| 7 | `idops01` | 한도윤 | agent_l2 운영 엔지니어(L2) | 계정·인증팀 |
| 8 | `api01` | 오지후 | agent_l2 운영 엔지니어(L2) | API 플랫폼팀 |
| 9 | `api02` | 윤채원 | agent_l2 운영 엔지니어(L2) | API 플랫폼팀 |
| 10 | `gpt01` | 강민재 | agent_l2 운영 엔지니어(L2) | ChatGPT 서비스팀 |
| 11 | `codex01` | 배수아 | agent_l2 운영 엔지니어(L2) | Codex 서비스팀 |
| 12 | `media01` | 송예린 | agent_l2 운영 엔지니어(L2) | 미디어 생성팀 |
| 13 | `lead01` | 조현우 | team_lead 팀장 | API 플랫폼팀 |
| 14 | `lead02` | 임소윤 | team_lead 팀장 | 계정·인증팀 |
| 15 | `lead03` | 남궁별 | team_lead 팀장 | 서비스데스크 |
| 16 | `sec01` | 장태오 | security_officer 보안 담당 | 보안관제팀 |
| 17 | `desk03` | 문지아 | agent_l1 (비활성, 퇴사) | 서비스데스크 |
| 18 | `viewer01` | 윤하람 | viewer 조회자 | – |
| 19 | `viewer02` | 고은별 | viewer 조회자 | – |

| 권한 코드 | 의미 | requester | L1 | L2 | team_lead | security | admin | viewer |
|---|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| `ticket:read` | 티켓 조회 (가시성 범위 안에서) | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| `ticket:read:all` | 모든 일반 티켓 조회 | | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| `ticket:read:restricted` | 보안 제한 티켓 조회 | | | | | ✔ | ✔ | |
| `ticket:create` | 등록 | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | |
| `ticket:update` | 내용 수정 | | ✔ | ✔ | ✔ | | ✔ | |
| `ticket:assign` | 담당 그룹·담당자 지정, 접수→배정됨 | | ✔ | ✔ | ✔ | | ✔ | |
| `ticket:transition` | 조사·보류·모니터링 전이 | | ✔ | ✔ | ✔ | | ✔ | |
| `ticket:resolve` | 해결 | | | ✔ | ✔ | | ✔ | |
| `ticket:close` | 종료·취소 | | ✔ | ✔ | ✔ | | ✔ | |
| `ticket:reopen` | 재오픈 | | | ✔ | ✔ | | ✔ | |
| `ticket:merge` | 중복 병합 | | | | ✔ | | ✔ | |
| `ticket:declare_major` | 주요 장애 지정 | | | | ✔ | | ✔ | |
| `worklog:write` | 공개 댓글 | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | |
| `worklog:internal` | 내부 메모 조회·작성 | | ✔ | ✔ | ✔ | ✔ | ✔ | |
| `audit:read` | 변경 이력 | | | ✔ | ✔ | ✔ | ✔ | |
| `problem:read` / `problem:write` | 문제 조회 / 등록·연결 | | 조회 | ✔ | ✔ | 조회 | ✔ | |
| `export:create` | 내보내기 | | | | ✔ | | ✔ | |
| `catalog:read`, `directory:read` | 카탈로그·디렉터리 | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | |

### 2.1 가시성 규칙 (행 단위)

| 주체 | 볼 수 있는 티켓 |
|---|---|
| `ticket:read:restricted` 보유 (보안 담당, 관리자) | 전부 |
| `ticket:read:all` 보유 (L1·L2·팀장·조회자) | 일반 티켓 + 자기 팀이 담당 그룹인 보안 제한 티켓 + 본인이 요청한 티켓 |
| 그 외 (요청자) | 본인이 요청한 티켓 + 주요 장애(`is_major`)로 지정된 일반 티켓 |

- 볼 수 없는 티켓은 목록·통계에서 빠지고, 상세·하위 리소스는 **404 `ticket_not_found`**(없는 티켓과 같은 응답)입니다.
- 내부 메모(`visibility=internal` worklog)는 `worklog:internal` 권한이 있을 때만 목록·건수에 포함됩니다.
- 요청자는 본인이 요청한 티켓에만 댓글을 남길 수 있습니다(주요 장애로 보이는 다른 사람 티켓에는 403 `comment_not_allowed`).

---

## 3. API 명세 (v2)

전체 요청·응답 스키마는 함께 제공되는 `openapi.json` 을 보세요. 이 문서는 스키마만으로 알 수 없는 규칙과 동작을 정리합니다.

### 3.1 공통

- 인증: `Authorization: Bearer <access_token>`. 없으면 401 `missing_token`, 무효 401 `invalid_token`, 만료 401 `token_expired`
  (`WWW-Authenticate: Bearer error="..."` 헤더 포함).
- 시각: 응답은 UTC ISO 8601(`2026-10-20T00:40:00Z`). 입력 시각(`occurred_at`, `created_from` 등)은 **시간대 필수**.
- 요청 한도: 주체별 분당 300회(기본). 초과 시 429 `rate_limited` + `Retry-After` 헤더(`retry_after_seconds` 필드). 정상 응답에는 `X-RateLimit-Limit`, `X-RateLimit-Remaining`.
- 요청 ID: `X-Request-ID` 를 보내면 그대로, 없으면 새로 만들어 응답 헤더와 오류 본문(`request_id`)에 넣습니다.
- 목록은 **커서 페이지네이션**: 응답 `page: {limit, next_cursor, has_more}`. 다음 페이지는 같은 조건에 `cursor=<next_cursor>`.
  조건·정렬을 바꾸고 이전 커서를 쓰면 400 `invalid_cursor`. `limit` 기본 20, 최대 100.

### 3.2 오류 형식 (problem+json)

```json
{
  "type": "https://itsm.internal/problems/invalid_transition",
  "title": "Conflict",
  "status": 409,
  "code": "invalid_transition",
  "detail": "접수(received) 상태에서 resolved 로 바꿀 수 없습니다.",
  "instance": "/api/v2/tickets/INC-005/transitions",
  "request_id": "5f0c...",
  "current_status": "received",
  "requested_status": "resolved",
  "allowed_transitions": ["assigned", "cancelled"]
}
```

- `code` 는 안정적인 기계 판독용 값, `detail` 은 사람이 읽는 설명입니다.
- 필드 오류는 `errors: [{field, code, message, ...}]`. 카탈로그 관련 오류는 항목에 `allowed_values` 가 함께 옵니다.
- 입력 검증 실패는 422 `validation_failed`(정의되지 않은 필드는 `code: "extra_forbidden"`).
- 서버 오류는 500 `internal_error`(내부 정보 없이 `request_id` 만).

| HTTP | 주요 code |
|---|---|
| 400 | `invalid_cursor`, `invalid_filter`, `invalid_grant`, `invalid_idempotency_key` |
| 401 | `missing_token`, `invalid_token`, `token_expired`, `user_inactive`, `invalid_client` |
| 403 | `permission_denied`(+`required_permissions`), `transition_not_permitted`, `comment_not_allowed`, `user_token_required`, `delegation_not_allowed` |
| 404 | `ticket_not_found`, `problem_not_found`, `service_not_found`, `user_not_found`, `export_not_found`, `route_not_found` |
| 409 | `invalid_transition`(+`allowed_transitions`), `ticket_closed`, `merge_target_closed`, `export_not_ready` |
| 412 | `version_conflict`(+`current_version`) |
| 422 | `validation_failed`, `transition_fields_invalid`(+`required_fields`, `optional_fields`), `unknown_service`, `service_deprecated`, `component_service_mismatch`, `unknown_model`, `model_retired`, `assignee_not_in_group`, `unknown_team`, `unknown_problem`, `idempotency_key_reused`, `merge_service_mismatch`, `merge_into_self`, `empty_update` |
| 428 | `precondition_required`(+`current_version`) |
| 429 | `rate_limited` |

### 3.3 인증·디렉터리·카탈로그

| 메서드·경로 | 권한 | 설명 |
|---|---|---|
| `POST /api/v2/auth/token` | – | `{grant_type: "client_credentials" \| "token_exchange", client_id, client_secret, subject?}` → `{access_token, token_type, expires_in, expires_at, subject}`. 서비스 토큰은 `catalog:read`, `directory:read` 만 가짐 |
| `GET /api/v2/me` | 위임 토큰 | 현재 사용자 + `permissions` |
| `GET /api/v2/users` | `directory:read` | `q`(username·이름·이메일), `team_id`, `role`, `include_inactive`, `limit`, `cursor` |
| `GET /api/v2/users/{id}`, `GET /api/v2/teams` | `directory:read` | |
| `GET /api/v2/catalog/services` | `catalog:read` | 서비스와 컴포넌트(각 컴포넌트의 담당 팀 포함). `include_deprecated=true` 로 폐기 서비스 포함 |
| `GET /api/v2/catalog/services/{id}` | `catalog:read` | |
| `GET /api/v2/catalog/models` | `catalog:read` | `include_retired=true` 로 종료 모델 포함 |

### 3.4 티켓

| 메서드·경로 | 권한 | 설명 |
|---|---|---|
| `GET /api/v2/tickets` | `ticket:read` | 검색·목록 (아래 필터). 정렬 `sort`: `-created_at`(기본), `-updated_at`, `priority` |
| `GET /api/v2/tickets/stats` | `ticket:read` | `group_by`: `status`, `priority`, `service`, `assignment_group` + 목록과 같은 필터. `{group_by, total, buckets: [{key, label, count}]}` |
| `POST /api/v2/tickets` | `ticket:create` (위임 토큰) | 등록. `Idempotency-Key` 헤더 권장. 201 + `Location` |
| `GET /api/v2/tickets/{id}` | `ticket:read` | 상세. `ETag: "<version>"`. ID 대소문자 무시 |
| `PATCH /api/v2/tickets/{id}` | `ticket:update` 또는 요청자 본인(접수 상태, 제목·설명·영향 범위만) | `If-Match` 필수. `is_major` 는 `ticket:declare_major` |
| `PUT /api/v2/tickets/{id}/assignment` | `ticket:assign` | `{assignment_group_id, assignee_id?}`. 담당자는 그 팀의 활성 L1·L2·팀장·관리자. `If-Match` 필수 |
| `GET /api/v2/tickets/{id}/transitions` | `ticket:read` | 현재 주체가 할 수 있는 전이와 전이별 `required_fields`·`optional_fields`, 현재 `version` |
| `POST /api/v2/tickets/{id}/transitions` | 전이별 (아래) | `{to_status, assignee_id?, pending_reason?, resolution_code?, resolution_note?, reason?, note?}`. `If-Match` 필수 |
| `GET /api/v2/tickets/{id}/worklogs` | `ticket:read` | 오래된 순. `visibility`, `limit`, `cursor` |
| `POST /api/v2/tickets/{id}/worklogs` | `worklog:write` (내부 메모는 `worklog:internal`) | `{body, visibility: "public" \| "internal"}` |
| `GET /api/v2/tickets/{id}/history` | `audit:read` | 감사 이벤트 |
| `POST /api/v2/tickets/{id}/merge` | `ticket:merge` | `{target_id, note?}`. 이 티켓을 대상 티켓의 중복으로 종료. 같은 서비스끼리만. `If-Match`(이 티켓) 필수 |
| `PUT /api/v2/tickets/{id}/problem` | `problem:write` | `{problem_id}` (null 이면 연결 해제). `If-Match` 필수 |

**목록·통계 필터**: `q`(ID·제목·설명·영향 범위·서비스명·컴포넌트명·모델, 공백 구분 AND, 부분 일치), `status`(반복 가능),
`priority`(반복 가능), `service_id`, `component_id`, `model`, `assignment_group_id`, `assignee`(사용자 ID·`me`·`none`),
`requester`(사용자 ID·`me`), `classification`, `is_major`, `created_from`·`created_to`·`updated_from`(시간대 필수). 총 건수가 필요하면 `stats` 를 씁니다.

**등록 입력**

| 필드 | 필수 | 설명 |
|---|---|---|
| `title` | ✔ | 1–200자 |
| `description` | ✔ | 1–5000자 |
| `service_id` | ✔ | 카탈로그 서비스 ID(예: `codex`). 폐기 서비스는 422 `service_deprecated`(+`replacement_service_id`) |
| `component_id` | | 그 서비스에 속한 컴포넌트 ID. 아니면 422 `component_service_mismatch`(+`allowed_values`) |
| `model` | | 활성 모델 ID(예: `gpt-6-luna`). 해당하는 경우에만 |
| `impact_level` | ✔ | `extensive`(전사·전체 고객) `significant`(다수 팀·다수 고객) `moderate`(단일 팀·일부 고객) `minor`(개인) |
| `urgency` | ✔ | `critical`(즉시, 업무 중단) `high`(주요 기능 저하) `medium`(우회 가능) `low` |
| `impact_note` | ✔ | 영향 범위 설명, 1–1000자 |
| `occurred_at` | ✔ | 발생 시각, 시간대 필수 |
| `classification` | | `standard`(기본) 또는 `restricted`(보안 사고 — 보안관제팀으로 배정, 보안 담당·관리자·본인만 열람) |

서버가 정하는 값: `id`(`INC-` + 3자리 일련번호), `status=received`, `priority`(영향도×긴급도), 담당 그룹(컴포넌트 담당 팀 → 서비스 담당 팀),
요청자(위임 토큰의 사용자), 시각들. 이 값들을 본문에 넣으면 422 입니다.

**멱등키**: 같은 사용자·같은 `Idempotency-Key`(24시간) 재요청은 새로 등록하지 않고 원래 티켓을 201 + `Idempotent-Replayed: true` 로 돌려줍니다.
같은 키에 다른 본문이면 422 `idempotency_key_reused`.

**낙관적 잠금**: 변경 요청은 `If-Match: "<version>"` 이 필요합니다(없으면 428, 다르면 412 `version_conflict`).
변경이 성공할 때마다 `version` 이 1 늘어납니다(댓글 추가, 병합 대상이 되는 경우 포함).

### 3.5 상태와 전이

| 상태 | 표시명 |
|---|---|
| `received` | 접수 |
| `assigned` | 배정됨 |
| `investigating` | 조사 중 |
| `pending` | 보류 |
| `monitoring` | 모니터링 중 |
| `resolved` | 해결 |
| `closed` | 종료 (최종) |
| `cancelled` | 취소 (최종) |

| 전이 | 권한 | 필수 입력 | 선택 입력 |
|---|---|---|---|
| received → assigned | `ticket:assign` | `assignee_id`(담당자가 없을 때) | `note` |
| received → cancelled | `ticket:close` 또는 요청자 본인 | `reason` | |
| assigned → investigating | `ticket:transition` | | `note` |
| assigned → cancelled | `ticket:close` | `reason` | |
| investigating → pending | `ticket:transition` | `pending_reason` | `note` |
| pending → investigating | `ticket:transition` | | `note` |
| investigating → monitoring | `ticket:transition` | `note` | |
| monitoring → investigating | `ticket:transition` | `note` | |
| investigating·monitoring → resolved | `ticket:resolve` | `resolution_code`, `resolution_note` | |
| resolved → closed | `ticket:close` | | `note` |
| resolved → investigating (재오픈) | `ticket:reopen` 또는 요청자 본인(해결 후 7일 이내) | `reason` | |

- `pending_reason`: `awaiting_requester`(요청자 응답 대기) `awaiting_vendor`(외부 업체 대기) `awaiting_change`(변경 작업 일정 대기)
- `resolution_code`: `fixed`(근본 수정) `workaround`(우회 조치) `vendor_fixed`(외부 업체 수정) `no_fault_found`(재현 안 됨) `duplicate`(중복 — 병합으로만)
- 표에 없는 전이는 409 `invalid_transition`, 권한이 없으면 403 `transition_not_permitted`, 입력이 빠지거나 해당 전이에서 받지 않는 값이 있으면 422 `transition_fields_invalid`.
- 전이마다 공개 worklog(`kind=status_change`)가 남습니다. 병합된 티켓은 더 이상 전이할 수 없습니다.

### 3.6 우선순위와 SLA

| 영향도 \ 긴급도 | critical | high | medium | low |
|---|---|---|---|---|
| extensive | P1 | P1 | P2 | P3 |
| significant | P1 | P2 | P3 | P3 |
| moderate | P2 | P3 | P3 | P4 |
| minor | P3 | P3 | P4 | P4 |

| 우선순위 | 응답 목표 | 해결 목표 | 기준 |
|---|---|---|---|
| P1 긴급 | 15분 | 4시간 | 24x7 |
| P2 높음 | 30분 | 8시간 | 24x7 |
| P3 보통 | 4 업무시간 | 3 업무일(27 업무시간) | 평일 09:00–18:00 KST, 회사 휴무일 제외 |
| P4 낮음 | 1 업무일 | 5 업무일 | 〃 |

- 응답 시각(`sla.responded_at`)은 접수 상태에서 처음 다른 상태로 바뀌거나, 요청자가 아닌 사람이 첫 공개 댓글을 남긴 시각입니다.
- 보류(`pending`) 기간은 해결 SLA 에서 빠집니다. 보류 중에는 `sla.paused=true`, `resolution_due_at=null`.
- 상세의 `sla` 와 목록의 `sla_breached` 는 조회 시각 기준으로 계산합니다.

### 3.7 문제(Problem)·내보내기

| 메서드·경로 | 권한 | 설명 |
|---|---|---|
| `GET /api/v2/problems` | `problem:read` | `q`, `status`(`open`·`known_error`·`resolved`) |
| `GET /api/v2/problems/{id}` | `problem:read` | `incident_ids` 는 현재 주체가 볼 수 있는 티켓만 |
| `POST /api/v2/problems` | `problem:write` | `{title, description, owner_team_id, workaround?}` |
| `POST /api/v2/exports` | `export:create` | `{format: "csv" \| "jsonl", filters: {status?, priority?, service_id?, created_from?, created_to?}}` → **202** + `Location` + `Retry-After`. 백그라운드 처리 |
| `GET /api/v2/exports/{id}` | 작업 요청자 | `status`: `queued`→`running`→`succeeded`/`failed`, `progress`, `row_count`, `download_url`. 진행 중이면 `Retry-After` |
| `GET /api/v2/exports/{id}/download` | 작업 요청자 | 완료 전에는 409 `export_not_ready` |

### 3.8 레거시 v1 (2027-03-31 종료 예정)

기존 사내 연동 호환용입니다. 응답에 `Deprecation: true`, `Sunset`, `Link: </api/v2/tickets>; rel="successor-version"` 헤더가 붙습니다.
가시성·권한 규칙은 v2 와 같습니다.

| 메서드·경로 | 설명 |
|---|---|
| `GET /api/v1/tickets?keyword=&state=&page=&size=` | `state`: `OPEN`·`IN_PROGRESS`·`RESOLVED`·`CLOSED`(v2 상태를 4단계로 축약). 오프셋 페이지(`size` ≤ 50). `{total, page, size, list: [{ticket_no, subject, state, svc, severity, created}]}` |
| `GET /api/v1/tickets/{ticket_no}` | + `desc`, `module`, `reporter`, `owner`, `modified`, `solution` |
| `POST /api/v1/tickets` | `{subject, desc, svc(서비스 이름), severity(1–4)}` → 200 `{result: "OK", ticket_no}` |

시각은 KST `YYYY-MM-DD HH:MM:SS`, `severity` 1–4 는 P1–P4 에 대응합니다. 오류 본문은 `text/plain` 한 줄(`ERROR NOT_FOUND: ticket INC-999 not found`), 검증 오류는 400 입니다.

---

## 부록. 발급된 연동 자격증명 (개발 환경)

| 항목 | 값 |
|---|---|
| 연동 시스템 | ITSM Assistant (사내 AI 도우미) |
| `client_id` | `itsm-agent` |
| `client_secret` | `itsm-agent-dev-secret` |
| 사용자 위임 | 허용 |
| ITSM API 주소 | `http://127.0.0.1:8001` |

운영 환경 자격증명은 별도로 발급합니다.
