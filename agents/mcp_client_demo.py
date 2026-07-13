"""
ToolHub 검증 데모 — MCP 서버 2대 연결 + 각 서버 툴 1개씩 실제 호출.

[실행 전 준비]
    docker compose up -d   (trace가 Redis에 기록되므로 Redis 필요)
    ※ 워커는 필요 없음 — 이 데모는 Celery를 안 거치고 허브만 검증한다.

[실행 — 리포 루트에서]
    python -m agents.mcp_client_demo

[일부러 API 키가 필요 없는 툴만 호출한다]
    flight_build_route / accommodation_allocate_rooms는 외부 API를 안 부르는
    순수 계산 툴이라, 라우팅 검증에 딱 좋다. (실제 검색 툴은 3b에서 Claude가 부름)
"""

import asyncio
import sys
import uuid

# 이 데모는 Django/Celery를 안 거치므로 .env를 직접 로드해야
# 서버 자식 프로세스에 넘겨줄 환경변수(API 키)가 준비된다
from dotenv import load_dotenv
load_dotenv()

from agents.mcp_client import ToolHub

sys.stdout.reconfigure(encoding="utf-8")   # Windows 콘솔 이모지 방어


async def main():
    run_id = uuid.uuid4().hex[:12]
    print(f"run_id = {run_id} (trace 녹화본: trace:{run_id}:events)\n")

    hub = ToolHub(run_id)

    print("1) MCP 서버 2대 기동 + 연결 중... (자식 프로세스 2개가 뜬다)")
    await hub.connect()

    print(f"\n2) 통합 툴 목록 ({len(hub.claude_tools)}개):")
    for tool in hub.claude_tools:
        print(f"   - {tool['name']}")

    print("\n3) 항공 서버 툴 호출 (flight_build_route):")
    result = await hub.call("flight_build_route", {
        "origin": {"city": "서울", "iata": "ICN"},
        "destinations": [{"iata": "FUK"}],
    })
    print(f"   {result}")

    print("\n4) 숙소 서버 툴 호출 (accommodation_allocate_rooms):")
    result = await hub.call("accommodation_allocate_rooms", {
        "adults": 3,
        "children_ages": [5],
    })
    print(f"   {result}")

    print("\n5) 없는 툴 호출 방어 확인:")
    result = await hub.call("tool_that_does_not_exist", {})
    print(f"   {result}")

    await hub.close()
    print("\n서버 2대 통합 연결 + 이름 라우팅 검증 완료!")


if __name__ == "__main__":
    # asyncio.run = 동기 세계에서 async 세계로 들어가는 표준 입구
    asyncio.run(main())