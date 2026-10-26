"""실제 모델 연결 확인용 스모크 스크립트 (OpenAI API를 실제로 호출하며 비용이 발생합니다).

사용법:
  1) ITSM 시스템(ITSM API)이 실행 중이어야 합니다 (기본 http://127.0.0.1:8001)
  2) .env.local 에 OPENAI_API_KEY 설정 후 에이전트 서버 실행 (uvicorn app.main:create_app --factory --port 8100)
  3) .venv/bin/python scripts/agent_smoke.py [--base-url http://127.0.0.1:8100] [--user admin01]

시나리오 질문과 티켓 등록 요청을 보내고 응답을 출력합니다.
도구와 별도 지침 없이 페르소나 한 줄만 준 상태에서 Agent가 어떻게 답하는지 사람이 확인합니다.
"""

import argparse

import httpx

QUESTIONS = [
    "안녕하세요. 지금 저는 누구로 접속해 있고 역할이 뭔가요?",
    "지금 Codex 로그인 관련해서 등록된 장애가 있어?",
    "gpt-6-luna API 관련 이슈가 뭐가 있어?",
    "응답이 아예 안 되는 거야, 느린 거야?",
    "Codex 로그인 장애가 해결됐다는 기록도 있어?",
    "아직 해결되지 않은 이슈만 보여줘.",
    "ChatGPT 화면이 안 열린다는 티켓을 새로 등록해줘.",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--user", default="admin01", help="테스트 사용자 username")
    args = parser.parse_args()

    with httpx.Client(base_url=args.base_url, timeout=180) as client:
        health = client.get("/api/health").json()
        print(f"agent: {health['agent']}")
        users = {u["username"]: u for u in client.get("/api/users").json()}
        me = client.post("/api/session", json={"user_id": users[args.user]["id"]}).json()["user"]
        print(f"session: {me['display_name']} ({me['username']}, {me['role']})\n")

        for question in QUESTIONS:
            res = client.post("/api/agent/chat", json={"message": question})
            print(f"Q: {question}")
            if res.status_code == 200:
                body = res.json()
                print(f"A: {body['reply']}\n   (context: {body['context']})\n")
            else:
                print(f"ERROR HTTP {res.status_code}: {res.json().get('detail')}\n")
                break


if __name__ == "__main__":
    main()
