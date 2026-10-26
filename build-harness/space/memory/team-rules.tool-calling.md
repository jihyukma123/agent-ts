<!-- BEGIN tool-calling-poc rules -->
## Tool Calling

- ALWAYS Agent 도구를 추가·변경할 때 `requirements.md`에 V1~V6 섹션과 Agent에 노출되는 모든 도구를 담은 `json tool-contract` 블록을 작성한다. (tool-calling-poc)
- ALWAYS 도구 설명에는 하는 일, 사용할 때, 사용하지 않을 때, 반환값을 쓰고 모든 인자에 의미·형식·허용 값을 설명한다. (V1)
- ALWAYS Agent instructions에 도구 이름별 사용 조건, 조회→확인→변경 순서, 정보가 부족할 때 질문하는 규칙을 쓴다. (V2)
- ALWAYS Agent instructions 설계 시 업무 목적과 무관한 질문(Out-of-scope)에 대한 거부/대응 정책이 필요한지 사람에게 직접 질문하여 확인하고 반영한다. 임의로 판단해 넘어가지 않는다. (V2)
- NEVER 사용자 식별·역할·토큰을 모델이 채우는 도구 인자로 받거나 권한 판단을 프롬프트에 맡긴다. 서버 컨텍스트와 실행 경로에서만 다룬다. (V3)
- ALWAYS 모든 `Runner.run*` 호출에 `max_turns`를 명시하고, 상한 초과와 같은 이유의 연속 실패를 종료 상태로 끝낸다. (V4)
- ALWAYS 모델이 만든 인자의 값(존재·허용 목록·범위·형식)을 실행 전에 검증하고 실패하면 `error_code`·`field`·`retryable`을 담은 구조화된 오류를 돌려준다. (V5)
- ALWAYS 외부 사람이 쓴 텍스트를 untrusted 데이터로 표시해 모델에 전달하고, 그 내용만을 근거로 쓰기 도구를 실행하지 않는다. (V6)
- ALWAYS V1~V6마다 결정론 테스트를 1개 이상 두고, V2와 V6는 실제 모델 반복 평가 결과를 `build-and-test-summary.md`에 남긴다. (tool-calling-poc)
<!-- END tool-calling-poc rules -->
