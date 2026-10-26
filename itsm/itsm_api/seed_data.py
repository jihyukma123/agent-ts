"""고정 초기 데이터.

모든 티켓은 실험용 가상 데이터입니다. 실제 OpenAI 서비스의 현재 장애 상황을 나타내지 않습니다.
시각은 모두 고정값(UTC)이라 초기화할 때마다 같은 상태가 복원됩니다.
"""

from __future__ import annotations

from datetime import timedelta

from itsm_api.timeutil import parse_iso, to_utc_iso

PERMISSIONS = [
    ("ticket:read", "티켓 조회 (역할별 가시성 범위 안에서)"),
    ("ticket:read:all", "모든 일반 티켓 조회"),
    ("ticket:read:restricted", "보안 제한 티켓 조회"),
    ("ticket:create", "티켓 등록"),
    ("ticket:update", "티켓 내용 수정"),
    ("ticket:assign", "담당 그룹·담당자 지정"),
    ("ticket:transition", "진행 상태 변경 (배정 후 조사·보류·모니터링)"),
    ("ticket:resolve", "해결 처리"),
    ("ticket:close", "종료·취소"),
    ("ticket:reopen", "재오픈"),
    ("ticket:merge", "중복 티켓 병합"),
    ("ticket:declare_major", "주요 장애 지정·해제"),
    ("worklog:write", "공개 댓글 작성"),
    ("worklog:internal", "내부 메모 조회·작성"),
    ("audit:read", "변경 이력 조회"),
    ("problem:read", "문제(Problem) 조회"),
    ("problem:write", "문제 등록·티켓 연결"),
    ("export:create", "티켓 데이터 내보내기"),
    ("catalog:read", "서비스 카탈로그 조회"),
    ("directory:read", "사용자·팀 디렉터리 조회"),
]

_REQUESTER = ["ticket:read", "ticket:create", "worklog:write", "catalog:read", "directory:read"]
_L1 = _REQUESTER + [
    "ticket:read:all",
    "ticket:update",
    "ticket:assign",
    "ticket:transition",
    "ticket:close",
    "worklog:internal",
    "problem:read",
]
_L2 = _L1 + ["ticket:resolve", "ticket:reopen", "problem:write", "audit:read"]
_LEAD = _L2 + ["ticket:merge", "ticket:declare_major", "export:create"]
_SECURITY = _REQUESTER + ["ticket:read:all", "ticket:read:restricted", "worklog:internal", "audit:read", "problem:read"]

ROLES = [
    # (id, name, label, description, permissions)
    (1, "admin", "관리자", "모든 기능", [code for code, _ in PERMISSIONS]),
    (2, "requester", "요청자", "본인 티켓과 공지된 주요 장애 조회, 티켓 등록, 공개 댓글", _REQUESTER),
    (3, "agent_l1", "서비스데스크(L1)", "접수·분류·배정, 진행 상태 변경, 종료·취소", _L1),
    (4, "agent_l2", "운영 엔지니어(L2)", "L1 권한 + 해결·재오픈, 문제 연결, 변경 이력", _L2),
    (5, "team_lead", "팀장", "L2 권한 + 병합, 주요 장애 지정, 내보내기", _LEAD),
    (6, "security_officer", "보안 담당", "보안 제한 티켓 포함 전체 조회, 내부 메모", _SECURITY),
    (7, "viewer", "조회자", "일반 티켓 전체 조회만 가능 (협력사·감사 등)", ["ticket:read", "ticket:read:all"]),
]

TEAMS = [
    (1, "service-desk", "서비스데스크"),
    (2, "identity", "계정·인증팀"),
    (3, "api-platform", "API 플랫폼팀"),
    (4, "chatgpt", "ChatGPT 서비스팀"),
    (5, "codex", "Codex 서비스팀"),
    (6, "media", "미디어 생성팀"),
    (7, "security", "보안관제팀"),
]

_USERS = [
    # (id, username, display_name, role_id, team_id, active)
    (1, "admin01", "김하늘", 1, 1, 1),
    (2, "member01", "박바다", 2, None, 1),
    (3, "member02", "최가람", 2, None, 1),
    (4, "member03", "서지민", 2, None, 1),
    (5, "desk01", "이서준", 3, 1, 1),
    (6, "desk02", "정하린", 3, 1, 1),
    (7, "idops01", "한도윤", 4, 2, 1),
    (8, "api01", "오지후", 4, 3, 1),
    (9, "api02", "윤채원", 4, 3, 1),
    (10, "gpt01", "강민재", 4, 4, 1),
    (11, "codex01", "배수아", 4, 5, 1),
    (12, "media01", "송예린", 4, 6, 1),
    (13, "lead01", "조현우", 5, 3, 1),
    (14, "lead02", "임소윤", 5, 2, 1),
    (15, "lead03", "남궁별", 5, 1, 1),
    (16, "sec01", "장태오", 6, 7, 1),
    (17, "desk03", "문지아", 3, 1, 0),  # 퇴사(비활성)
    (18, "viewer01", "윤하람", 7, None, 1),
    (19, "viewer02", "고은별", 7, None, 1),
]
USERS = [(i, u, n, f"{u}@corp.example", r, t, a) for i, u, n, r, t, a in _USERS]

API_CLIENTS = [
    # (client_id, name, secret, can_delegate) — 개발용 기본 비밀값입니다.
    ("portal", "ITSM 웹", "portal-dev-secret", 1),
    ("itsm-agent", "ITSM Assistant (사내 AI 도우미)", "itsm-agent-dev-secret", 1),
    ("status-board", "서비스 상태 게시판", "status-board-dev-secret", 0),
]

SERVICES = [
    {"id": "codex", "name": "Codex", "description": "코드 작성·실행 에이전트 서비스", "owner_team_id": 5},
    {"id": "openai-api", "name": "OpenAI API", "description": "모델 호출 API (Responses, Batch, Files)", "owner_team_id": 3},
    {"id": "chatgpt", "name": "ChatGPT", "description": "ChatGPT 웹·모바일 서비스", "owner_team_id": 4},
    {"id": "sora", "name": "Sora", "description": "영상 생성 서비스", "owner_team_id": 6},
    {"id": "image-gen", "name": "이미지 생성", "description": "이미지 생성·편집 서비스", "owner_team_id": 6},
    {"id": "sso", "name": "사내 SSO", "description": "임직원 통합 로그인·MFA", "owner_team_id": 2},
    {
        "id": "assistants-api",
        "name": "Assistants API",
        "description": "Responses API 로 대체되어 2026-09-30 신규 접수가 종료된 API",
        "owner_team_id": 3,
        "status": "deprecated",
        "replacement_service_id": "openai-api",
    },
]
for _s in SERVICES:
    _s.setdefault("status", "active")
    _s.setdefault("replacement_service_id", None)

COMPONENTS = [
    # (id, service_id, name, owner_team_id 또는 None=서비스 담당 팀)
    (1, "codex", "로그인", 2),
    (2, "codex", "코드 실행 환경", None),
    (3, "codex", "IDE 확장", None),
    (4, "codex", "CLI", None),
    (5, "openai-api", "Responses API", None),
    (6, "openai-api", "Batch API", None),
    (7, "openai-api", "파일 업로드", None),
    (8, "openai-api", "사용량 대시보드", None),
    (9, "chatgpt", "웹 화면 (모델 선택)", None),
    (10, "chatgpt", "웹 화면 (대화)", None),
    (11, "chatgpt", "모바일 앱", None),
    (12, "chatgpt", "파일 첨부", None),
    (13, "chatgpt", "로그인", 2),
    (14, "sora", "영상 생성", None),
    (15, "sora", "대기열", None),
    (16, "image-gen", "생성", None),
    (17, "image-gen", "편집", None),
    (18, "sso", "로그인", None),
    (19, "sso", "MFA", None),
    (20, "assistants-api", "스레드", None),
    (21, "assistants-api", "실행(Run)", None),
]

MODELS = [
    ("gpt-6-luna", "gpt-6-luna", "active"),
    ("gpt-6", "gpt-6", "active"),
    ("gpt-6-mini", "gpt-6-mini", "active"),
    ("o5", "o5", "active"),
    ("o5-mini", "o5-mini", "active"),
    ("sora-3", "sora-3", "active"),
    ("image-3", "image-3", "active"),
    ("gpt-5.5", "gpt-5.5", "retired"),
]

PROBLEMS = [
    {
        "id": "PRB-001",
        "seq": 1,
        "title": "인증 세션 처리 반복 장애",
        "description": "Codex·ChatGPT 로그인 직후 세션이 유지되지 않는 장애가 반복됨. 인증 게이트웨이 세션 처리 공통 원인 분석.",
        "status": "open",
        "root_cause": None,
        "workaround": "기존 로그인 세션이 있는 브라우저 탭을 유지하면 계속 사용할 수 있음.",
        "owner_team_id": 2,
        "created_at": "2026-10-01T01:00:00Z",
        "updated_at": "2026-10-20T03:00:00Z",
    },
    {
        "id": "PRB-002",
        "seq": 2,
        "title": "gpt-6-luna 추론 클러스터 용량 부족",
        "description": "트래픽 증가 시 gpt-6-luna 추론 노드가 메모리 부족으로 재시작되어 503·응답 지연이 발생함.",
        "status": "known_error",
        "root_cause": "추론 노드 메모리 한도 대비 최대 동시 요청 설정이 과도함.",
        "workaround": "클라이언트에서 지수 백오프로 재시도. 용량 증설은 변경 작업으로 진행 중.",
        "owner_team_id": 3,
        "created_at": "2026-10-20T04:00:00Z",
        "updated_at": "2026-10-21T09:00:00Z",
    },
    {
        "id": "PRB-003",
        "seq": 3,
        "title": "ChatGPT 웹 정적 자산 CDN 캐시 불일치",
        "description": "배포 직후 CDN 에 이전 버전 정적 자산이 남아 화면이 깨지거나 느려지는 문제.",
        "status": "resolved",
        "root_cause": "배포 파이프라인에서 CDN 캐시 무효화 단계가 비동기로 실행됨.",
        "workaround": None,
        "owner_team_id": 4,
        "created_at": "2026-09-16T02:00:00Z",
        "updated_at": "2026-10-10T08:00:00Z",
    },
]

_SERVICE_OWNER = {s["id"]: s["owner_team_id"] for s in SERVICES}
_COMPONENT = {cid: (service_id, owner) for cid, service_id, _name, owner in COMPONENTS}


def _shift(iso: str, minutes: int) -> str:
    return to_utc_iso(parse_iso(iso) + timedelta(minutes=minutes))


def _ticket(
    seq: int,
    title: str,
    description: str,
    service_id: str,
    impact_level: str,
    urgency: str,
    impact_note: str,
    requester_id: int,
    created_at: str,
    status: str,
    **extra,
) -> dict:
    component_id = extra.get("component_id")
    if component_id is not None:
        assert _COMPONENT[component_id][0] == service_id, (seq, component_id)
    group = extra.pop("assignment_group_id", None)
    if group is None:
        group = (_COMPONENT[component_id][1] if component_id else None) or _SERVICE_OWNER[service_id]
    times = [created_at] + [extra[k] for k in ("responded_at", "resolved_at", "closed_at", "pending_since") if extra.get(k)]
    ticket = {
        "id": f"INC-{seq:03d}",
        "seq": seq,
        "title": title,
        "description": description,
        "service_id": service_id,
        "component_id": None,
        "model": None,
        "status": status,
        "impact_level": impact_level,
        "urgency": urgency,
        "impact_note": impact_note,
        "classification": "standard",
        "is_major": 0,
        "requester_id": requester_id,
        "assignment_group_id": group,
        "assignee_id": None,
        "problem_id": None,
        "merged_into": None,
        "pending_reason": None,
        "resolution_code": None,
        "resolution_note": None,
        "occurred_at": _shift(created_at, -15),
        "created_at": created_at,
        "updated_at": max(times, key=parse_iso),
        "responded_at": None,
        "resolved_at": None,
        "closed_at": None,
        "pending_since": None,
    }
    ticket.update(extra)
    return ticket


TICKETS = [
    _ticket(
        1, "Codex 로그인 실패",
        "일부 사용자가 Codex에 로그인한 직후 다시 로그인 화면으로 돌아오는 현상이 보고됨. "
        "이미 로그인되어 있던 기존 세션에서는 Codex를 정상적으로 사용할 수 있음. "
        "로그인 이후 세션 처리 단계의 문제로 추정하며 원인 조사 중. "
        "특정 모델 API의 장애가 아니라 Codex 로그인 기능의 문제로 분류함.",
        "codex", "significant", "high", "새로 로그인하는 일부 Codex 사용자. 기존 로그인 세션은 영향 없음.",
        2, "2026-10-20T00:55:00Z", "investigating",
        component_id=1, assignee_id=7, is_major=1, problem_id="PRB-001",
        occurred_at="2026-10-20T00:40:00Z", responded_at="2026-10-20T01:05:00Z", updated_at="2026-10-20T02:10:00Z",
    ),
    _ticket(
        2, "gpt-6-luna API 호출 시 간헐적 503",
        "OpenAI API에서 gpt-6-luna 모델을 호출할 때 일부 요청이 HTTP 503 응답으로 실패함. "
        "재시도하면 성공하는 경우가 많으며, 모든 요청이 실패하는 상황은 아님. 실패 비율과 영향 지역을 조사 중.",
        "openai-api", "extensive", "high", "gpt-6-luna 모델을 호출하는 API 요청 중 일부. 전체 요청 실패는 아님.",
        1, "2026-10-20T03:20:00Z", "investigating",
        component_id=5, model="gpt-6-luna", assignee_id=8, is_major=1, problem_id="PRB-002",
        occurred_at="2026-10-20T03:05:00Z", responded_at="2026-10-20T03:28:00Z", updated_at="2026-10-21T09:00:00Z",
    ),
    _ticket(
        3, "gpt-6-luna 응답 지연",
        "gpt-6-luna 모델 호출 시 응답은 정상적으로 반환되지만 평소보다 응답 시간이 길어짐. "
        "오류 응답은 동반되지 않음. 처리 용량 조정 조치를 적용했으며, 응답 시간이 평소 수준으로 돌아오는지 경과를 확인하는 중.",
        "openai-api", "significant", "medium", "gpt-6-luna 모델을 호출하는 API 요청의 응답 시간 증가. 요청 실패는 아님.",
        1, "2026-10-20T05:45:00Z", "monitoring",
        component_id=5, model="gpt-6-luna", assignee_id=9, problem_id="PRB-002",
        occurred_at="2026-10-20T05:30:00Z", responded_at="2026-10-20T06:10:00Z", updated_at="2026-10-20T07:15:00Z",
    ),
    _ticket(
        4, "과거 Codex 로그인 장애 복구",
        "2026-09-28에 Codex 로그인 시도가 실패하던 장애. 조치 후 정상화됨. 현재 진행 중인 INC-001과는 별개의 과거 사건임.",
        "codex", "significant", "critical", "당시 로그인을 시도한 Codex 사용자 다수.",
        2, "2026-09-28T01:25:00Z", "closed",
        component_id=1, assignee_id=7, problem_id="PRB-001",
        occurred_at="2026-09-28T01:10:00Z", responded_at="2026-09-28T01:31:00Z",
        resolved_at="2026-09-28T04:40:00Z", closed_at="2026-10-01T00:00:00Z",
        resolution_code="fixed",
        resolution_note="인증 토큰 갱신 설정 오류를 수정해 배포함. 배포 후 로그인 성공률이 평소 수준으로 회복된 것을 확인하고 종료함.",
    ),
    _ticket(
        5, "ChatGPT 모델 선택 화면 로딩 실패",
        "ChatGPT 웹 화면에서 모델 선택 목록이 표시되지 않아 사용자가 모델을 선택하기 어려움. "
        "웹 화면 표시 문제로 접수되었으며, 특정 모델 자체의 장애로 확인된 것은 아님. 아직 담당자 배정 전.",
        "chatgpt", "moderate", "medium", "ChatGPT 웹 화면에서 모델을 변경하려는 사용자.",
        2, "2026-10-20T08:20:00Z", "received",
        component_id=9, occurred_at="2026-10-20T08:10:00Z",
    ),
    _ticket(
        6, "Sora 영상 생성 대기열 지연",
        "영상 생성 요청이 대기열에서 평소보다 오래 머무름. 생성이 시작된 뒤의 처리 속도는 정상.",
        "sora", "significant", "medium", "영상 생성을 요청한 사용자 다수. 생성 실패는 아님.",
        3, "2026-10-22T02:10:00Z", "investigating",
        component_id=15, assignee_id=12, responded_at="2026-10-22T03:00:00Z",
    ),
    _ticket(
        7, "ChatGPT iOS 앱 푸시 알림 미수신",
        "iOS 앱에서 긴 작업 완료 푸시 알림이 오지 않음. Android 는 정상. Apple 푸시 서비스 측 지연 공지가 있어 회신 대기.",
        "chatgpt", "moderate", "low", "iOS 앱 사용자 중 알림을 켠 사용자.",
        4, "2026-10-21T02:00:00Z", "pending",
        component_id=11, assignee_id=10, responded_at="2026-10-21T04:30:00Z",
        pending_reason="awaiting_vendor", pending_since="2026-10-21T06:00:00Z",
    ),
    _ticket(
        8, "Batch API 결과 파일 다운로드 404",
        "Batch 작업 상태는 completed 인데 결과 파일을 내려받으면 404 가 반환됨.",
        "openai-api", "moderate", "high", "Batch API 를 사용하는 일부 조직.",
        3, "2026-10-21T00:30:00Z", "resolved",
        component_id=6, assignee_id=9, responded_at="2026-10-21T01:00:00Z", resolved_at="2026-10-23T05:00:00Z",
        resolution_code="fixed", resolution_note="결과 파일 복제 지연 시 상태를 completed 로 먼저 바꾸던 순서를 수정함.",
    ),
    _ticket(
        9, "사용량 대시보드 일일 집계 지연",
        "사용량 대시보드의 전일 집계가 오전 10시가 지나도 갱신되지 않음.",
        "openai-api", "minor", "medium", "사용량 대시보드를 확인하는 관리자.",
        4, "2026-10-23T01:30:00Z", "assigned",
        component_id=8, assignee_id=8, responded_at="2026-10-23T02:00:00Z",
    ),
    _ticket(
        10, "Codex CLI 업데이트 후 인증 토큰 만료 오류",
        "Codex CLI 최신 버전으로 업데이트한 뒤 명령 실행 시 'token expired' 오류가 반복됨. 재로그인 후에도 일부 재발.",
        "codex", "moderate", "high", "최신 CLI 로 업데이트한 개발자.",
        2, "2026-10-22T05:00:00Z", "investigating",
        component_id=4, assignee_id=11, responded_at="2026-10-22T05:40:00Z",
    ),
    _ticket(
        11, "Codex 코드 실행 환경 컨테이너 기동 지연",
        "작업 시작 시 실행 환경 컨테이너 준비에 수 분이 걸림. 이미지 캐시 노드 증설 후 경과 관찰 중.",
        "codex", "significant", "high", "Codex 작업을 새로 시작하는 사용자.",
        3, "2026-10-23T00:10:00Z", "monitoring",
        component_id=2, assignee_id=11, responded_at="2026-10-23T00:20:00Z", updated_at="2026-10-23T06:00:00Z",
    ),
    _ticket(
        12, "Codex IDE 확장 응답 표시 깨짐",
        "VS Code 확장에서 긴 응답의 코드 블록이 줄바꿈 없이 한 줄로 표시됨.",
        "codex", "minor", "low", "VS Code 확장 사용자 일부.",
        4, "2026-10-24T02:00:00Z", "received",
        component_id=3,
    ),
    _ticket(
        13, "사내 SSO MFA 푸시 승인 지연",
        "MFA 푸시 승인 알림이 1~2분 늦게 도착해 로그인 시간이 초과됨.",
        "sso", "significant", "high", "사내 SSO 로 로그인하는 임직원.",
        1, "2026-10-16T00:20:00Z", "resolved",
        component_id=19, assignee_id=7, responded_at="2026-10-16T00:30:00Z", resolved_at="2026-10-18T03:00:00Z",
        resolution_code="workaround", resolution_note="OTP 코드 입력 방식을 임시로 허용함. 푸시 업체 개선 일정 확인 중.",
    ),
    _ticket(
        14, "사내 SSO 로그인 반복 (특정 브라우저)",
        "구버전 Safari 에서 SSO 로그인 후 다시 로그인 화면으로 돌아감.",
        "sso", "moderate", "medium", "구버전 Safari 사용 임직원.",
        2, "2026-09-30T01:00:00Z", "closed",
        component_id=18, assignee_id=7, responded_at="2026-09-30T01:30:00Z",
        resolved_at="2026-10-02T02:00:00Z", closed_at="2026-10-05T00:00:00Z",
        resolution_code="fixed", resolution_note="쿠키 SameSite 속성 호환 처리를 추가함.",
    ),
    _ticket(
        15, "ChatGPT 20MB 이상 파일 첨부 실패",
        "20MB 이상 PDF 를 첨부하면 업로드 진행률이 99% 에서 멈춤.",
        "chatgpt", "moderate", "medium", "대용량 파일을 첨부하는 사용자.",
        3, "2026-10-22T07:00:00Z", "investigating",
        component_id=12, assignee_id=10, responded_at="2026-10-22T08:00:00Z",
    ),
    _ticket(
        16, "이미지 생성 결과 일부 깨짐",
        "image-3 로 생성한 이미지의 하단 일부가 회색으로 채워져 반환됨.",
        "image-gen", "moderate", "medium", "image-3 모델 사용자 일부.",
        4, "2026-10-23T04:00:00Z", "assigned",
        component_id=16, model="image-3", assignee_id=12, responded_at="2026-10-23T05:00:00Z",
    ),
    _ticket(
        17, "이미지 편집 시 마스크 영역 무시",
        "편집 요청에 마스크를 지정해도 이미지 전체가 바뀜.",
        "image-gen", "minor", "medium", "이미지 편집 기능 사용자.",
        2, "2026-10-24T05:30:00Z", "received",
        component_id=17,
    ),
    _ticket(
        18, "Assistants API 스레드 조회 간헐적 타임아웃",
        "스레드 메시지 목록 조회가 간헐적으로 30초 타임아웃됨.",
        "assistants-api", "minor", "medium", "Assistants API 를 아직 사용하는 연동.",
        3, "2026-09-20T03:00:00Z", "closed",
        component_id=20, assignee_id=8, responded_at="2026-09-20T04:00:00Z",
        resolved_at="2026-09-25T02:00:00Z", closed_at="2026-09-28T00:00:00Z",
        resolution_code="workaround", resolution_note="Responses API 전환 가이드를 안내하고 조회 페이지 크기를 줄이도록 권고함.",
    ),
    _ticket(
        19, "Assistants API 실행(Run)이 queued 에서 멈춤",
        "Run 생성 후 상태가 queued 에서 바뀌지 않음.",
        "assistants-api", "minor", "low", "Assistants API 연동 1건.",
        4, "2026-09-26T06:00:00Z", "cancelled",
        component_id=21, closed_at="2026-09-27T01:00:00Z",
    ),
    _ticket(
        20, "파일 업로드 API 응답 지연",
        "Files 업로드 요청이 평소 대비 5배 이상 느림. 클라우드 스토리지 업체 장애 공지 확인.",
        "openai-api", "significant", "high", "파일을 업로드하는 API 사용자.",
        1, "2026-10-14T06:00:00Z", "resolved",
        component_id=7, assignee_id=9, responded_at="2026-10-14T06:10:00Z", resolved_at="2026-10-14T11:00:00Z",
        resolution_code="vendor_fixed", resolution_note="스토리지 업체 복구 후 업로드 지연이 해소됨을 확인함.",
    ),
    _ticket(
        21, "gpt-6-mini 함수 호출 인자 JSON 형식 오류 증가",
        "gpt-6-mini 로 함수 호출 시 인자가 올바른 JSON 이 아닌 응답 비율이 증가함.",
        "openai-api", "moderate", "high", "함수 호출을 사용하는 gpt-6-mini 연동.",
        3, "2026-10-23T08:00:00Z", "investigating",
        component_id=5, model="gpt-6-mini", assignee_id=8, responded_at="2026-10-23T08:30:00Z",
    ),
    _ticket(
        22, "o5 모델 응답이 중간에 끊김",
        "o5 로 긴 응답을 생성하면 중간에 응답이 끊기는 사례가 보고됨. 재현 가능한 요청 ID 확인 대기.",
        "openai-api", "moderate", "medium", "o5 모델로 긴 응답을 생성하는 사용자 일부.",
        4, "2026-10-22T09:00:00Z", "pending",
        component_id=5, model="o5", assignee_id=9, responded_at="2026-10-23T01:00:00Z",
        pending_reason="awaiting_requester", pending_since="2026-10-23T05:00:00Z",
    ),
    _ticket(
        23, "ChatGPT 웹 대화 목록 불러오기 느림",
        "대화 기록이 많은 계정에서 대화 목록 첫 화면이 10초 이상 걸림.",
        "chatgpt", "significant", "medium", "대화 기록이 많은 사용자.",
        2, "2026-10-08T02:00:00Z", "resolved",
        component_id=10, assignee_id=10, problem_id="PRB-003", responded_at="2026-10-08T03:00:00Z",
        resolved_at="2026-10-10T08:00:00Z",
        resolution_code="fixed", resolution_note="배포 시 CDN 캐시 무효화를 동기 단계로 바꾸고 목록 API 페이지 크기를 조정함.",
    ),
    _ticket(
        24, "ChatGPT 웹 배포 후 흰 화면",
        "신규 배포 직후 ChatGPT 웹 접속 시 흰 화면만 표시됨.",
        "chatgpt", "extensive", "critical", "ChatGPT 웹 사용자 전체.",
        1, "2026-09-16T01:40:00Z", "closed",
        component_id=10, assignee_id=10, problem_id="PRB-003", is_major=1, responded_at="2026-09-16T01:45:00Z",
        resolved_at="2026-09-16T02:50:00Z", closed_at="2026-09-18T00:00:00Z",
        resolution_code="fixed", resolution_note="이전 버전으로 롤백 후 CDN 캐시를 무효화함.",
    ),
    _ticket(
        25, "ChatGPT 로그인 시 세션 만료 메시지 반복",
        "로그인 직후 '세션이 만료되었습니다' 메시지가 반복 표시됨. INC-001 과 같은 원인인지 확인 중.",
        "chatgpt", "significant", "high", "ChatGPT 에 새로 로그인하는 일부 사용자.",
        3, "2026-10-21T01:00:00Z", "investigating",
        component_id=13, assignee_id=7, problem_id="PRB-001", responded_at="2026-10-21T01:20:00Z",
    ),
    _ticket(
        26, "비정상 로그인 시도 탐지로 계정 잠금 다수 발생",
        "특정 IP 대역에서 대량 로그인 시도가 탐지되어 자동 잠금 정책으로 정상 계정도 잠김.",
        "sso", "moderate", "high", "잠금된 임직원 계정 약 40개.",
        4, "2026-10-22T23:00:00Z", "investigating",
        component_id=18, classification="restricted", assignment_group_id=7, assignee_id=16,
        responded_at="2026-10-22T23:10:00Z",
    ),
    _ticket(
        27, "API 키 유출 의심 신고",
        "공개 저장소에 조직 API 키로 보이는 문자열이 올라간 것을 발견함.",
        "openai-api", "moderate", "critical", "해당 조직의 API 사용량·과금.",
        2, "2026-10-24T00:30:00Z", "assigned",
        classification="restricted", assignment_group_id=7, assignee_id=16, responded_at="2026-10-24T00:40:00Z",
    ),
    _ticket(
        28, "외부 공유 링크로 대화 내용 노출 의심",
        "공유 링크를 해제했는데도 외부에서 대화 내용이 보인다는 제보.",
        "chatgpt", "significant", "high", "공유 링크 기능을 사용한 사용자.",
        3, "2026-10-24T06:00:00Z", "received",
        component_id=10, classification="restricted", assignment_group_id=7,
    ),
    _ticket(
        29, "Sora 영상 다운로드 링크 만료 오류",
        "생성 직후 받은 다운로드 링크가 바로 만료 오류를 반환함.",
        "sora", "minor", "medium", "sora-3 로 영상을 생성한 사용자 일부.",
        4, "2026-10-17T02:00:00Z", "resolved",
        component_id=14, model="sora-3", assignee_id=12, responded_at="2026-10-17T03:00:00Z",
        resolved_at="2026-10-19T06:00:00Z",
        resolution_code="fixed", resolution_note="서명 URL 생성 서버의 시계 오차를 바로잡음.",
    ),
    _ticket(
        30, "Codex 로그인 직후 로그인 화면으로 돌아감",
        "로그인하면 다시 로그인 화면이 뜸.",
        "codex", "moderate", "high", "본인 포함 팀원 일부.",
        3, "2026-10-20T01:30:00Z", "closed",
        component_id=1, merged_into="INC-001", responded_at="2026-10-20T02:00:00Z",
        closed_at="2026-10-20T02:00:00Z", resolution_code="duplicate", resolution_note="INC-001 과 같은 장애로 병합함.",
    ),
    _ticket(
        31, "gpt-6-luna 503 오류",
        "API 호출하면 503 이 자주 나요.",
        "openai-api", "moderate", "high", "우리 팀 서비스.",
        4, "2026-10-20T04:10:00Z", "closed",
        component_id=5, model="gpt-6-luna", merged_into="INC-002", responded_at="2026-10-20T04:30:00Z",
        closed_at="2026-10-20T04:30:00Z", resolution_code="duplicate", resolution_note="INC-002 와 같은 장애로 병합함.",
    ),
    _ticket(
        32, "Responses API 스트리밍 이벤트 순서 뒤바뀜",
        "gpt-6 스트리밍 응답에서 output_text.delta 이벤트가 간헐적으로 순서가 바뀌어 도착함.",
        "openai-api", "moderate", "medium", "스트리밍을 사용하는 연동 일부.",
        2, "2026-10-24T07:00:00Z", "received",
        component_id=5, model="gpt-6",
    ),
    _ticket(
        33, "ChatGPT 모바일 앱 음성 입력 인식 실패",
        "Android 앱에서 음성 입력 버튼을 누르면 '인식할 수 없습니다'만 표시됨.",
        "chatgpt", "minor", "medium", "Android 앱 음성 입력 사용자 일부.",
        3, "2026-10-23T09:00:00Z", "assigned",
        component_id=11, assignee_id=10, responded_at="2026-10-24T00:30:00Z",
    ),
    _ticket(
        34, "Codex 작업 결과 PR 생성 실패",
        "작업 완료 후 'PR 만들기'를 누르면 권한 오류가 표시됨. 저장소 연동 권한은 정상.",
        "codex", "moderate", "high", "GitHub 연동으로 PR 을 만드는 사용자.",
        4, "2026-10-24T01:00:00Z", "investigating",
        component_id=2, assignee_id=11, responded_at="2026-10-24T01:20:00Z",
    ),
    _ticket(
        35, "이미지 생성 요청 시 429 과다 발생",
        "사용 한도 내인데도 image-3 생성 요청에 429 가 자주 반환됨. 한도 계산 버그 수정 배포 후 관찰 중.",
        "image-gen", "significant", "medium", "image-3 를 사용하는 API 고객.",
        2, "2026-10-21T03:00:00Z", "monitoring",
        component_id=16, model="image-3", assignee_id=12, responded_at="2026-10-21T03:30:00Z",
        updated_at="2026-10-23T02:00:00Z",
    ),
    _ticket(
        36, "gpt-5.5 모델 호출 시 model_not_found",
        "기존 연동에서 gpt-5.5 호출 시 model_not_found 오류가 반환됨.",
        "openai-api", "minor", "low", "gpt-5.5 를 고정 사용하는 연동 1건.",
        3, "2026-10-02T02:00:00Z", "resolved",
        component_id=5, model="gpt-5.5", assignee_id=8, responded_at="2026-10-02T03:00:00Z",
        resolved_at="2026-10-02T05:00:00Z",
        resolution_code="no_fault_found", resolution_note="2026-10-01 서비스 종료된 모델임을 안내하고 gpt-6 전환을 권고함.",
    ),
]  # fmt: skip


def _w(ticket_seq: int, author_id: int, visibility: str, body: str, at: str, kind: str = "comment") -> dict:
    return {
        "ticket_id": f"INC-{ticket_seq:03d}",
        "author_id": author_id,
        "visibility": visibility,
        "kind": kind,
        "body": body,
        "created_at": at,
    }


WORKLOGS = [
    _w(1, 7, "public", "상태 변경: 접수 → 조사 중", "2026-10-20T01:05:00Z", "status_change"),
    _w(1, 7, "public", "로그인 직후 세션 쿠키가 저장되지 않는 사례를 확인했습니다. 원인 조사 중입니다.", "2026-10-20T01:10:00Z"),
    _w(1, 7, "internal", "10/19 인증 게이트웨이 배포에서 쿠키 SameSite 속성이 바뀐 것으로 보임. 롤백 여부 팀장과 협의 필요.", "2026-10-20T01:40:00Z"),
    _w(1, 14, "internal", "롤백은 보안 검토 후 진행. 우선 영향 사용자 수 집계 부탁합니다.", "2026-10-20T02:00:00Z"),
    _w(1, 2, "public", "저희 팀 5명 모두 같은 증상입니다. 기존 탭은 계속 쓸 수 있습니다.", "2026-10-20T02:05:00Z"),
    _w(1, 15, "public", "INC-030 을 이 티켓으로 병합했습니다.", "2026-10-20T02:00:00Z", "system"),
    _w(2, 8, "public", "일부 리전에서 503 비율이 2~3% 수준으로 확인됩니다.", "2026-10-20T03:40:00Z"),
    _w(2, 8, "internal", "추론 클러스터 노드 3대가 OOM 으로 재시작 반복. PRB-002 로 연결함.", "2026-10-20T04:05:00Z"),
    _w(2, 13, "public", "재시도하면 대부분 성공합니다. 지수 백오프 재시도를 권장합니다. 용량 증설은 변경 작업으로 진행 중입니다.", "2026-10-21T09:00:00Z"),
    _w(3, 9, "public", "상태 변경: 조사 중 → 모니터링 중 (처리 용량 조정 적용)", "2026-10-20T07:15:00Z", "status_change"),
    _w(3, 9, "internal", "p95 지연 4.2s → 1.9s. 24시간 관찰 후 해결 처리 예정.", "2026-10-20T07:20:00Z"),
    _w(4, 7, "public", "인증 토큰 갱신 설정 오류를 수정해 배포했습니다.", "2026-09-28T04:30:00Z"),
    _w(4, 7, "public", "상태 변경: 조사 중 → 해결", "2026-09-28T04:40:00Z", "status_change"),
    _w(6, 12, "public", "대기열 워커 수를 늘리는 중입니다.", "2026-10-22T03:00:00Z"),
    _w(6, 12, "internal", "워커 오토스케일 상한이 낮게 설정되어 있음. 변경 요청 CHG-2210 진행.", "2026-10-22T04:30:00Z"),
    _w(7, 10, "public", "Apple 푸시 서비스 지연 공지를 확인해 회신을 기다리고 있습니다.", "2026-10-21T06:00:00Z"),
    _w(7, 10, "public", "상태 변경: 조사 중 → 보류 (외부 업체 대기)", "2026-10-21T06:00:00Z", "status_change"),
    _w(8, 9, "public", "결과 파일 복제 순서를 수정해 배포했습니다. 재시도하면 내려받을 수 있습니다.", "2026-10-23T05:00:00Z"),
    _w(10, 11, "public", "CLI 1.42.0 에서 토큰 캐시 경로가 바뀐 것을 확인했습니다.", "2026-10-22T06:00:00Z"),
    _w(10, 11, "internal", "구버전 캐시 파일을 읽다가 만료로 판단하는 버그. 핫픽스 1.42.1 준비 중.", "2026-10-22T07:00:00Z"),
    _w(11, 11, "public", "이미지 캐시 노드를 증설했습니다. 기동 시간이 정상 범위로 돌아오는지 지켜보고 있습니다.", "2026-10-23T06:00:00Z"),
    _w(13, 7, "public", "OTP 코드 입력 방식을 임시로 허용했습니다.", "2026-10-18T03:00:00Z"),
    _w(15, 10, "public", "업로드 마지막 단계(바이러스 검사)에서 시간 초과가 나는 것으로 보입니다.", "2026-10-22T08:00:00Z"),
    _w(21, 8, "internal", "10/22 디코딩 설정 변경 이후 증가. 설정 비교 중.", "2026-10-23T09:00:00Z"),
    _w(22, 9, "public", "재현 가능한 요청 ID(request_id)를 공유해 주세요. 확인 후 이어서 조사하겠습니다.", "2026-10-23T05:00:00Z"),
    _w(22, 9, "public", "상태 변경: 조사 중 → 보류 (요청자 응답 대기)", "2026-10-23T05:00:00Z", "status_change"),
    _w(25, 7, "public", "INC-001 과 같은 원인인지 확인 중입니다.", "2026-10-21T01:20:00Z"),
    _w(26, 16, "internal", "의심 IP 대역 차단 완료. 잠금 해제는 본인 확인 후 순차 진행.", "2026-10-23T00:30:00Z"),
    _w(26, 16, "public", "계정 잠금 해제를 순차적으로 진행하고 있습니다.", "2026-10-23T01:00:00Z"),
    _w(27, 16, "internal", "해당 키 즉시 폐기 요청. 사용 내역 이상 여부 확인 중.", "2026-10-24T00:45:00Z"),
    _w(34, 11, "public", "GitHub 앱 권한 범위 변경 이후 발생하는 것으로 보입니다.", "2026-10-24T01:30:00Z"),
    _w(35, 12, "public", "한도 계산 버그 수정을 배포했습니다. 이틀간 관찰 후 해결 처리하겠습니다.", "2026-10-23T02:00:00Z"),
]  # fmt: skip


def _check_seed() -> None:
    ids = [t["id"] for t in TICKETS]
    assert len(ids) == len(set(ids))
    for t in TICKETS:
        assert parse_iso(t["occurred_at"]) <= parse_iso(t["created_at"])
        if t["status"] == "pending":
            assert t["pending_since"] and t["pending_reason"]
        if t["status"] in ("resolved", "closed") and not t["merged_into"]:
            assert t["resolution_code"] and t["resolution_note"]


_check_seed()
