# Tool Calling PoC Harness

AI-DLC로 업무 Agent에 도구를 붙일 때 **호출자가 LLM이라서 필요한 V1~V6**이 매번 설계·구현·검증되도록 만드는 최소 harness다. 특정 실행 결과를 보고 고친 것이 아니라, 다음 실행에서 무엇을 놓치든 같은 지점에서 걸리도록 개발 과정에 끼워 넣는다.

- 대상: OpenAI Agents SDK(Python) 기반 Agent, AI-DLC 2.11.1
- 상태: PoC. 구성 요소는 itsm_dlc 사본에서 개별 동작을 확인했다. 전체 `/aidlc` 실행으로는 아직 검증하지 않았다.

## 구성

| 파일 | AI-DLC 확장 지점 | 역할 |
|---|---|---|
| `space/knowledge/aidlc-shared/tool-calling-for-llm-callers.md` | Team Knowledge | V1~V6 설계 방법, SDK 사용법, eval 작성법, `tool-contract` 블록 형식 |
| `space/memory/team-rules.tool-calling.md` | Rules (`team.md`) | V1~V6을 지켜야 할 규칙으로 고정 |
| `space/memory/templates/requirements.md` | Template override | 요구사항에 V1~V6 섹션과 contract 블록을 강제 |
| `space/memory/templates/build-and-test-summary.md` | Template override | eval 결과와 실제 모델 평가 섹션을 강제 |
| `harness/sensors/aidlc-tool-contract.md` + `tools/aidlc-sensor-tool-contract.ts` | Sensor (blocking, code-generation gate) | contract와 실제 Agent 비교 |
| `harness/sensors/aidlc-tool-eval.md` + `tools/aidlc-sensor-tool-eval.ts` | Sensor (blocking, build-and-test gate) | eval 사례 커버리지 확인, 결정론 사례 재실행 |
| `scopes/create-tool-draft.md` | Scope (composed scope record) | 도구 하나를 빠르게 추가하는 짧은 경로 |
| `install.sh` | — | 위 파일을 프로젝트에 배치하고 stage graph를 다시 컴파일 |

## 동작 흐름

```text
Requirements Analysis   template → V1~V6 섹션 + json tool-contract 블록
        |                rules·knowledge → 무엇을 써야 하는지
        v
Code Generation         gate: tool-contract sensor (blocking)
        |                 contract ↔ 실제 Agent 도구·instructions·Runner 호출
        v
Build and Test          gate: tool-eval sensor (blocking)
                          V1~V6 사례 커버리지, 결정론 사례 pytest 재실행, 보고 누락
                          template → 실제 모델 평가 섹션
```

## V1~V6별 보장 방식

| | 설계에 쓰게 함 (template·rules) | 코드에 이어졌는지 (tool-contract) | 동작하는지 (tool-eval) |
|---|---|---|---|
| V1 도구 정의 | Tool Definitions | contract 도구 = Agent 도구, 설명 50자 이상, contract 인자 = 모델에 보이는 인자, 모든 인자·중첩 필드에 설명 | 결정론 사례 |
| V2 사용 안내 | Tool Usage Policy (목적 외 질문 거부 정책 포함) | instructions가 모든 도구 이름을 언급 | 결정론 + **실제 모델** 사례 (목적 외 질문 거절 포함) |
| V3 권한 연계 | Authorization Boundary | 금지 인자(user_id, token 등)가 스키마 어디에도 없음 | 결정론 사례 |
| V4 반복·상한 | Execution Limits | 모든 `Runner.run*`에 `max_turns` 명시, contract 값과 일치 | 결정론 사례 |
| V5 값 유효성 | Argument Validation | 모든 인자에 `validation` 규칙 | 결정론 사례 |
| V6 결과 속 지시문 | Tool Result Trust Boundary | 모든 도구가 `untrusted_result_fields` 선언, 조회 도구는 1개 이상 | 결정론 + **실제 모델** 사례 |

Sensor는 "결정이 코드까지 이어졌는가"와 "검증 사례가 있고 통과하는가"까지 본다. 설명이 충분히 좋은지, 실제 모델이 잘 행동하는지는 실제 모델 평가 결과를 보고 gate에서 사람이 판단한다.

## create-tool-draft scope

도구 하나를 추가·변경하는 반복용 경로다. 33개 stage 중 7개만 실행한다.

| 실행 | 이유 |
|---|---|
| Initialization 3개 | 모든 scope 필수 |
| Reverse Engineering | 기존 Agent 이해. 재실행 시 최신 code KB를 재사용할 수 있다 |
| Requirements Analysis | tool contract 작성 |
| Code Generation | 구현 + 결정론 eval 테스트, tool-contract gate |
| Build and Test | 테스트·실제 모델 평가, tool-eval gate |

Ideation, user stories·mockups, domain·unit 설계, 배포 단계는 생략한다. 가장 가까운 기본 scope인 `express`에서 배포 단계를 뺀 구성이다.

- `sensors: on`은 필수다. 꺼지면 두 blocking sensor가 돌지 않는다.
- 속도를 위해 `review_cap: none`, `plan_approval: off`, `summary_confirmation: off`, `learnings: off`로 둔다. 도구 설명 품질에 reviewer가 필요하면 `review_cap: advisory`로 올린다.
- `aidlc engine graph validate-scope create-tool-draft`는 code-generation의 `unit-of-work` 입력에 대해 advisory를 하나 낸다. `express`도 같은 advisory를 낸다.

## 설치와 실행

```bash
harness/tool-calling-poc/install.sh <project-dir> [space] [harness-dir]
```

- space 기본값은 `aidlc/active-space`, harness 기본값은 `.claude`/`.codex` 등 자동 탐지
- 다시 실행해도 안전하다. 바뀌는 기존 파일은 `*.before-tool-calling-poc`로 한 번 보관한다.
- 실행 후 `/aidlc --scope create-tool-draft <만들 도구 설명>`으로 시작한다.

### 연속 실행 팁 (Multi-Tool Iteration Loop)

도구를 순차적으로 여러 개 추가할 때(예: `ticket_read` → `ticket_write` → `ticket_update`):
1. **Code KB 재사용**: 첫 번째 도구 루프가 끝나면 최신 코드 베이스가 space 내 code KB에 축적되므로, 두 번째 도구부터는 `reverse-engineering` 단계가 기존 지식을 재사용하여 훨씬 빠르게 통과합니다.
2. **도구 전용 space 운용 권장**: `requirements.md` 템플릿 오버라이드가 space 단위로 적용되므로, 일반 애플리케이션 개발 space와 분리하여 `tool-dev` 같은 전용 space에서 작업하는 것을 권장합니다 (`/aidlc space create tool-dev`).
3. **Sensor 피드백을 통한 자가 치유(Self-Correction)**: `tool-contract` 센서가 차단(`pass: false`)될 경우, finding 메시지에 구체적인 코드 수정 가이드(예: 누락된 `Field(description=...)` 추가, `Runner.run`의 `max_turns` 지정 위치 등)가 포함되므로 개발 Agent가 메시지를 보고 즉시 코드를 자가 수정합니다.
4. **V2·V6 참조 패턴**: 시스템 프롬프트(V2) 작성 권장 템플릿과 외부 텍스트 래핑 헬퍼(V6)는 `space/knowledge/aidlc-shared/tool-calling-for-llm-callers.md`에 예시가 제공됩니다.

## 검증한 것

itsm_dlc 사본(원본은 수정하지 않음)에서 확인했다.

- 설치 후 `aidlc engine graph scope create-tool-draft`가 위 7개 stage로 해석되고, `aidlc engine sensor list`에 두 sensor가 등록된다. `aidlc --doctor`에 새 오류가 없다.
- `required-sections`가 `requirements.md` template을 적용해 빠진 V1~V6 섹션을 보고한다.
- `tool-contract`를 기존 ITSM Agent에 실행하면 V1(설명 길이·인자 설명 누락), V2(instructions에 도구 이름 없음), V4(`max_turns` 없음)를 보고한다. contract를 일부러 틀리게 고치면 V3(금지 인자), V5(검증 규칙 누락), V6(untrusted 필드 누락), 없는 도구도 각각 잡는다.
- `tool-eval`은 V2·V4·V5 결정론 사례 누락, V2 실제 모델 사례 누락, 보고 누락을 잡는다. 없는 pytest node id를 넣으면 실패를 보고한다.

## 한계

- **전체 실행 미검증**: sensor는 `aidlc engine sensor fire`로 직접 실행해 확인했다. 실제 `/aidlc` 흐름에서 gate를 막는 동작은 아직 보지 않았다.
- **space 단위 적용**: template과 rules는 space 전체에 적용된다. 같은 space의 다른 작업(feature 등)에도 V1~V6 섹션이 요구된다. 도구 개발 전용 space를 쓰는 편이 깔끔하다.
- **엔진 파일 수정**: stage 파일의 sensor 목록과 harness 트리의 sensor는 `aidlc config` 갱신 때 덮어써진다. 그때마다 `install.sh`를 다시 돌린다. 검증이 끝나면 plugin으로 옮기는 것이 다음 단계다(rules는 plugin에 넣을 수 없어 별도로 둔다).
- **휴리스틱 하한**: 설명 50자, instructions의 도구 이름 언급은 최소 조건일 뿐 충분하다는 뜻이 아니다.
- **실제 모델 평가는 sensor가 실행하지 않는다**: 비용과 비결정성 때문이다. 결과 보고 섹션만 강제한다.
- **전제**: Python + OpenAI Agents SDK, Agent를 모델 호출 없이 만들 수 있는 factory, `Runner.run*`의 정적 호출, contract는 활성 intent의 `requirements.md`에 있음.
- **active-memory**: 설치 직후에는 `aidlc/active-memory/`에 새 rules가 아직 없다. 다음 세션 시작이나 space 전환 때 복사된다.
