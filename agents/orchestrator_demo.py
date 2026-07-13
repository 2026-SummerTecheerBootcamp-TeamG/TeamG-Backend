"""
A방식 오케스트레이터 검증 데모.

[실행 전 준비]
    docker compose up -d   (trace 기록용 Redis)
    .env에 ANTHROPIC_API_KEY, SERPAPI_KEY, LITEAPI_KEY 필요

[실행 — 리포 루트에서]
    python -m agents.orchestrator_demo

[주의] 실제 외부 API를 호출한다:
    Claude API(4~6회) + SerpApi(항공 검색, 무료 쿼터 월 250회) + LiteAPI(숙소).
    반복 실행은 쿼터를 소모하니 필요할 때만.

[관전 포인트]
    trace 타임라인에서 Claude가 항공 서버와 숙소 서버의 툴을
    "스스로 판단한 순서"로 넘나들며 호출하는 것을 볼 수 있다.
    (우리는 순서를 코딩하지 않았다 — 그게 A방식)
"""

import asyncio
import sys
import uuid

from agents.orchestrator import run_agent_loop

sys.stdout.reconfigure(encoding="utf-8")   # Windows 콘솔 이모지 방어

# 시나리오: 항공+숙소가 모두 필요한 요청 (두 서버를 다 쓰게 유도)
USER_MESSAGE = (
    "성인 2명이 2026년 8월 1일부터 8월 3일까지 서울(ICN)에서 오사카(KIX)로 "
    "여행을 가려고 해. 왕복 항공권 후보와 쇼핑하기 좋은 호텔 3개를 추천해줘."
)


async def main():
    run_id = uuid.uuid4().hex[:12]
    print(f"run_id = {run_id}")
    print(f"[요청] {USER_MESSAGE}\n")
    print("--- trace 타임라인 (실시간) ---")

    final_answer = await run_agent_loop(run_id, USER_MESSAGE)

    print("\n--- Claude 최종 답변 ---")
    print(final_answer)


if __name__ == "__main__":
    asyncio.run(main())