# Requirements

## Sources

## Intent Analysis

## Functional Requirements

<!-- FR{n} / FR{n}.{m} ID를 유지한다. 도구별 기능 요구사항을 쓴다. -->

## Non-Functional Requirements

<!-- NFR{n} ID를 유지한다. -->

## Tool Definitions (V1)

<!-- 도구마다: 이름, 하는 일, 사용할 때, 사용하지 않을 때, 반환값, 비슷한 도구와의 차이. 인자마다: 의미, 형식, 예시, 허용 값. -->

## Tool Usage Policy (V2)

<!-- Agent instructions에 들어갈 내용:
- 도구 이름별 사용 조건, 조회→확인→변경 순서, 정보가 부족할 때 질문, 도구가 필요 없는 대화.
- [필수 확인] 업무 목적 외 질문(Out-of-Scope) 거부 정책:
  AI가 임의로 판단하지 말고 사람(요구자)에게 직접 물어보고 결정한 내용을 기록할 것:
  1) 이 Agent가 목적과 무관한 질문에 답해야 하는가, 아니면 거부해야 하는가?
  2) 거부 시 출력할 정중한 거절 문구 및 본래 업무 유도 메시지는 무엇인가?
-->

## Authorization Boundary (V3)

<!-- 사용자 식별·권한의 출처(서버 컨텍스트), 모델에게 노출하지 않을 인자, 권한 판단 위치(실행 경로), 역할별 노출(is_enabled) 여부. -->

## Execution Limits and Terminal States (V4)

<!-- max_turns 값과 근거, 같은 이유의 연속 실패 상한, timeout, 상한에 닿았을 때 사용자에게 보이는 종료 상태. -->

## Argument Validation (V5)

<!-- 인자별 값 규칙(존재·허용 목록·범위·형식·필드 간 관계)과 실패 시 구조화된 오류(error_code, field, retryable, hint). -->

## Tool Result Trust Boundary (V6)

<!-- 결과 중 외부 텍스트 필드, untrusted 표시 방식, 결과 내용만으로 쓰기를 실행하지 않는 규칙. -->

## Tool Eval Plan

<!-- V1~V6별 결정론 사례(pytest node id)와 실제 모델 사례(시나리오, 프롬프트, 반복 수). 사례 ID: E-V<n>-<번호>, 실제 모델은 E-V<n>-L<번호>. -->

## Tool Contract Block

<!-- 정보 문자열이 `json tool-contract`인 fenced block 하나. 형식은 Team Knowledge의 tool-calling-for-llm-callers.md를 따른다. Agent에 노출되는 모든 도구를 적는다. -->

## Constraints

## Assumptions

## Out of Scope

## Open Questions
