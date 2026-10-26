# Tool Calling Harness 파일별 설명

| 파일 | 어떤 harness | 모델이 어떻게 동작하게 하려는가 |
|---|---|---|
| `space/knowledge/aidlc-shared/tool-calling-for-llm-callers.md` | Knowledge (참고 자료) | 도구 설명을 잘 쓰고, 시스템 프롬프트에 도구 사용 조건을 넣고, 권한은 서버에서 지키고, 반복 호출에 상한을 두고, 입력값은 내용까지 검증하고, 외부 글은 데이터로 표시하는 **방법을 알고** 작업하게 한다 |
| `space/memory/team-rules.tool-calling.md` | Rules (강제 규칙) | 위 6가지(V1~V6)를 선택이 아니라 **반드시 지켜야 하는 기준**으로 여기고 작업하게 한다 |
| `space/memory/templates/requirements.md` | Template (요구사항 문서 양식) | 설계할 때 V1~V6 항목과 tool contract(설계 요약 JSON)를 **빠짐없이 작성**하게 한다 |
| `space/memory/templates/build-and-test-summary.md` | Template (테스트 결과 양식) | 테스트 결과를 V1~V6별로, 실제 모델 평가 결과까지 **보고**하게 한다 |
| `harness/sensors/aidlc-tool-contract.md` + `harness/tools/aidlc-sensor-tool-contract.ts` | Sensor (코드 생성 단계, 통과 못 하면 막음) | 코드를 다 쓴 뒤 **설계(tool contract)대로 구현했는지** 자동 검사를 통과해야 넘어가게 한다. 도구 목록·설명, 프롬프트의 도구 언급, 금지 입력값, `max_turns`를 확인한다 |
| `harness/sensors/aidlc-tool-eval.md` + `harness/tools/aidlc-sensor-tool-eval.ts` | Sensor (빌드·테스트 단계, 통과 못 하면 막음) | V1~V6 각각에 **테스트를 만들고 통과시키고 보고**해야 넘어가게 한다 |
| `scopes/create-tool-draft.md` | Scope (실행 경로) | 도구 하나를 추가할 때 **요구사항 → 코드 → 테스트만** 빠르게 거치게 한다 (33단계 중 7단계) |
| `install.sh` | 설치 스크립트 | 위 파일을 프로젝트의 정해진 폴더에 넣는다. 모델 동작과는 무관 |
